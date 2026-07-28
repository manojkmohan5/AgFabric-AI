"""Answer generation behind one seam, mirroring `embed.py`.

Two real implementations:

- `OpenAIChat` is what runs in production (`gpt-4o-mini` by default).
- `FakeChat` lets the check suite and CI run with no API key, no network, and no
  cost. It is a genuine extractive baseline — it scores context lines by word
  overlap with the question and returns the best ones — so a grounding test
  actually proves retrieval fed the answer rather than proving nothing.

Both report token usage so the caller can price a request.
"""

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from .config import settings

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")

SYSTEM_PROMPT = """You are AgFabric AI, an assistant for a grain operations team.

Answer only from the CONTEXT provided. The context contains structured database
records, related entities from the knowledge graph, and excerpts from uploaded
documents.

Rules:
- If the context does not contain the answer, say so plainly. Never guess a
  number, date, or name that is not present.
- Cite the sources you used by their bracketed labels, e.g. [S1], [DB2].
- Be concise. Prefer exact figures from the records over paraphrase.
- Amounts marked "redacted" are hidden by access control. Say they are not
  available to this user rather than estimating them."""


@dataclass(frozen=True)
class Answer:
    text: str
    input_tokens: int
    output_tokens: int


class Chat(Protocol):
    name: str
    model: str

    def answer(self, question: str, context: str) -> Answer: ...

    def generate_sql(self, question: str, system_prompt: str) -> Answer:
        """Return SQL in `.text`, or the literal NO_QUERY if not answerable."""
        ...

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """What this call actually costs. Providers that do not bill return 0."""
        ...


class FakeChat:
    """Deterministic extractive QA. No network, stable across processes."""

    name = "fake"
    model = "extractive-overlap"

    # Keyword sets mapped to canned queries. Deterministic, so the validation
    # gate and the read-only execution path are both exercised in tests without
    # an API key. The real provider composes SQL freely.
    SQL_TEMPLATES: tuple[tuple[frozenset[str], str], ...] = (
        (
            frozenset({"total", "invoice"}),
            "SELECT status, SUM(amount) AS total_amount, COUNT(*) AS invoice_count "
            "FROM invoices GROUP BY status",
        ),
        (
            frozenset({"revenue"}),
            "SELECT SUM(amount) AS total_revenue FROM invoices WHERE status = 'paid'",
        ),
        (
            frozenset({"bushels", "customer"}),
            "SELECT c.name, SUM(d.net_bu) AS total_bu FROM deliveries d "
            "JOIN customers c ON c.id = d.customer_id "
            "GROUP BY c.name ORDER BY total_bu DESC LIMIT 10",
        ),
        (
            frozenset({"many", "deliveries"}),
            "SELECT COUNT(*) AS delivery_count FROM deliveries",
        ),
        (
            frozenset({"capacity", "bins"}),
            "SELECT name, capacity_bu, current_bu FROM storage_bins "
            "ORDER BY current_bu DESC LIMIT 20",
        ),
        (
            frozenset({"overdue"}),
            "SELECT number, amount, due_date FROM invoices "
            "WHERE status = 'overdue' ORDER BY due_date LIMIT 20",
        ),
    )

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        # Nothing is billed, so the audit trail must record zero rather than a
        # figure invented from OpenAI's rate card.
        return 0.0

    def generate_sql(self, question: str, system_prompt: str) -> Answer:
        words = set(_TOKEN.findall(question.lower()))
        best_sql, best_score = "NO_QUERY", 0
        for keywords, sql in self.SQL_TEMPLATES:
            score = len(keywords & words)
            # Every keyword must be present, and more specific templates win.
            if score == len(keywords) and score > best_score:
                best_sql, best_score = sql, score
        return Answer(
            text=best_sql,
            input_tokens=_estimate(question) + _estimate(system_prompt),
            output_tokens=_estimate(best_sql),
        )

    def answer(self, question: str, context: str) -> Answer:
        wanted = set(_TOKEN.findall(question.lower()))
        lines = [ln.strip() for ln in context.splitlines() if ln.strip()]

        scored = []
        for i, line in enumerate(lines):
            tokens = set(_TOKEN.findall(line.lower()))
            if not tokens:
                continue
            # Overlap normalised by line length, so a long line does not win on
            # sheer size. Index breaks ties so the result is fully determined.
            overlap = len(wanted & tokens)
            if overlap:
                weight = overlap / len(tokens) ** 0.5
                # An exact database record outranks a graph triple or a document
                # excerpt at equal overlap — the same precedence `_confidence`
                # applies, and it stops long record lines losing on length alone.
                if line.startswith("[DB"):
                    weight *= 1.5
                scored.append((weight, -i, line))

        if not scored:
            text = (
                "The provided context does not contain information answering that "
                "question."
            )
        else:
            scored.sort(reverse=True)
            best = [line for _, _, line in scored[:3]]
            text = "Based on the provided context:\n" + "\n".join(f"- {b}" for b in best)

        return Answer(
            text=text,
            input_tokens=_estimate(question) + _estimate(context),
            output_tokens=_estimate(text),
        )


class OpenAIChat:
    name = "openai"

    def __init__(self, api_key: str, model: str, max_output_tokens: int) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model
        self._max_output_tokens = max_output_tokens

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return openai_cost_usd(input_tokens, output_tokens, self.model)

    def generate_sql(self, question: str, system_prompt: str) -> Answer:
        response = self._client.chat.completions.create(
            model=self.model,
            # SQL is short; a low ceiling also caps the damage of a runaway reply.
            max_completion_tokens=400,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        )
        usage = response.usage
        return Answer(
            text=(response.choices[0].message.content or "").strip(),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    def answer(self, question: str, context: str) -> Answer:
        response = self._client.chat.completions.create(
            model=self.model,
            max_completion_tokens=self._max_output_tokens,
            # Deterministic-leaning: this is a retrieval-grounded lookup, not
            # creative writing.
            temperature=0.0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}",
                },
            ],
        )
        usage = response.usage
        return Answer(
            text=(response.choices[0].message.content or "").strip(),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


def _estimate(text: str) -> int:
    """Rough token count for the fake provider. Words, not real BPE."""
    return len(text.split())


# USD per 1M tokens, (input, output). Per-model rather than one global pair,
# because a single pair silently misprices the audit trail and the spend cap the
# moment OPENAI_CHAT_MODEL changes — and the cap is what stops a runaway loop.
#
# gpt-5.4-nano confirmed against developers.openai.com/api/docs/pricing on
# 2026-07-27. The gpt-4.1/4o figures are the published rates from when those
# models shipped; OpenAI no longer lists them on the current pricing page, so
# treat them as good-faith and reconcile against the billing dashboard.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4-nano": (0.20, 1.25),
}

# What to charge a model absent from the table. Deliberately expensive: pricing an
# unknown model at zero would make the daily cap unreachable, which is the one
# failure mode that costs real money. Erring high trips the cap early instead.
UNKNOWN_MODEL_PRICE = (5.00, 15.00)


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for a model name.

    Dated snapshots like `gpt-4.1-nano-2025-04-14` price as their base model.
    """
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    # Longest prefix wins, so `gpt-4.1-mini-...` cannot match `gpt-4.1-nano`.
    for base in sorted(MODEL_PRICES, key=len, reverse=True):
        if model.startswith(base):
            return MODEL_PRICES[base]
    logger.warning(
        "no price known for model %r; charging the conservative %s/%s per Mtok so "
        "the spend cap still bites",
        model,
        *UNKNOWN_MODEL_PRICE,
    )
    return UNKNOWN_MODEL_PRICE


def openai_cost_usd(
    input_tokens: int, output_tokens: int, model: str | None = None
) -> float:
    """Price a call at the rate for the model that served it."""
    if model is None:
        # Configured overrides win, so an operator can pin a negotiated rate.
        rate_in = settings.price_input_per_mtok
        rate_out = settings.price_output_per_mtok
    else:
        rate_in, rate_out = price_for(model)
    return round(
        input_tokens / 1_000_000 * rate_in + output_tokens / 1_000_000 * rate_out,
        6,
    )


@lru_cache(maxsize=1)
def get_chat() -> Chat:
    provider = settings.llm_provider.lower()
    if provider == "auto":
        provider = "openai" if settings.openai_api_key else "fake"
    if provider == "fake":
        return FakeChat()
    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIChat(
            settings.openai_api_key,
            settings.openai_chat_model,
            settings.llm_max_output_tokens,
        )
    raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}; use auto|openai|fake")

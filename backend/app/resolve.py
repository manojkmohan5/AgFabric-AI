"""Find which database entities a natural-language question is about.

Deterministic pattern matching, deliberately not LLM-generated SQL. Asking a
model to write queries against this schema would add an injection surface, let it
invent columns, and make every answer unreproducible. Identifiers in this domain
are highly structured (C-2026-1000, T-80011, INV-5099, ELK-04), so matching them
is both safer and more reliable.

Known ceiling: this resolves *entities*, not *aggregates*. "What is contract
C-2026-1000?" works; "what was total revenue last quarter?" does not, because
nothing here generates a GROUP BY. That needs a guarded SQL-generation step —
worth adding only once the entity path is not enough.

Pure functions over strings and (id, name) pairs, so they test without a database.
"""

import re
from collections.abc import Sequence

# Ordered longest-prefix-first so INV-5099 is never mistaken for a bin code.
IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "contract": re.compile(r"\bC-\d{4}-\d+\b", re.IGNORECASE),
    "invoice": re.compile(r"\bINV-\d+\b", re.IGNORECASE),
    "delivery": re.compile(r"\bT-\d+\b", re.IGNORECASE),
    # Bin codes are three letters and exactly two digits (ELK-04, SNG-02). The
    # trailing \b stops it swallowing the front of INV-5099.
    "bin": re.compile(r"\b[A-Z]{3}-\d{2}\b", re.IGNORECASE),
}

# Words too common to identify a customer by.
_STOPWORDS = frozenset(
    {
        "farm",
        "farms",
        "family",
        "brothers",
        "acres",
        "grain",
        "the",
        "and",
        "llc",
        "company",
        "partners",
        "what",
        "which",
        "show",
        "list",
        "about",
        "have",
        "with",
        "from",
        "does",
        "contract",
        "contracts",
        "delivery",
        "deliveries",
        "invoice",
        "invoices",
    }
)
MIN_NAME_TOKEN = 4


def find_identifiers(text: str) -> dict[str, list[str]]:
    """Extract structured identifiers, grouped by entity kind.

    Matches are upper-cased and de-duplicated while preserving first-seen order.
    """
    found: dict[str, list[str]] = {}
    claimed: list[tuple[int, int]] = []

    for kind, pattern in IDENTIFIER_PATTERNS.items():
        hits: list[str] = []
        for match in pattern.finditer(text):
            span = match.span()
            # A later, looser pattern must not re-match text a stricter one took.
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            value = match.group().upper()
            if value not in hits:
                hits.append(value)
        if hits:
            found[kind] = hits
    return found


def match_customers(
    text: str, customers: Sequence[tuple[int, str]], limit: int = 3
) -> list[tuple[int, str]]:
    """Customers whose name shares a distinctive word with `text`.

    Deliberately simple: no fuzzy distance, no ranking model. A shared
    distinctive token is a strong enough signal for these names, and it is
    explainable — the audit trail can say which word matched.
    """
    words = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= MIN_NAME_TOKEN}
    words -= _STOPWORDS
    if not words:
        return []

    scored: list[tuple[int, int, str]] = []
    for customer_id, name in customers:
        tokens = {
            w
            for w in re.findall(r"[a-z]+", name.lower())
            if len(w) >= MIN_NAME_TOKEN and w not in _STOPWORDS
        }
        overlap = len(words & tokens)
        if overlap:
            scored.append((overlap, customer_id, name))

    # Most overlap first; id breaks ties so the result is deterministic.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(cid, name) for _, cid, name in scored[:limit]]

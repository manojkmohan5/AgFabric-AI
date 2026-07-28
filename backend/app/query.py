"""Hybrid question answering: SQL + vector + knowledge graph, then an LLM.

The response is the Explainable AI envelope from the plan — answer, confidence,
the structured rows used, the document chunks retrieved, the graph relationships
walked, plus model, latency, tokens and cost.

Retrieval order matters. Structured records come first because an exact database
row beats a semantically similar paragraph, and the prompt presents them first.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, llm, metrics, resolve, sqlgen, vectors
from . import graph as kg
from .auth import current_user
from .config import settings
from .db import get_db
from .embed import get_embedder
from .models import (
    FINANCE_ROLES,
    AuditLog,
    Commodity,
    Contract,
    Customer,
    Delivery,
    DocumentChunk,
    Facility,
    Invoice,
    StorageBin,
    User,
)
from .ratelimit import SlidingWindow

logger = logging.getLogger(__name__)
router = APIRouter(tags=["query"])

# /query bills money on every call and previously had no throttle at all — one
# loop in a client could burn a budget. Keyed by user id, not IP: this endpoint is
# authenticated, so the account is the thing to limit.
query_limiter = SlidingWindow(
    limit=settings.query_rate_limit,
    window_seconds=settings.query_rate_window_seconds,
)


def _spend_guard(db: Session) -> None:
    """Refuse the request if the rolling spend cap is already reached.

    This is the difference between a cost *control* and a cost *alert*. The
    Prometheus rule from Phase 6 tells you afterwards; this stops the next call.

    Summed from the audit log, so the ceiling survives a restart rather than
    resetting to zero with the process. The fake providers record 0.0, so this can
    only ever trip on real spend.
    """
    if not settings.enable_spend_cap:
        return
    window_start = datetime.now(UTC) - timedelta(hours=24)
    spent = float(
        db.scalar(
            select(func.coalesce(func.sum(AuditLog.cost_usd), 0.0)).where(
                AuditLog.created_at >= window_start
            )
        )
        or 0.0
    )
    if spent >= settings.daily_spend_cap_usd:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"24h LLM spend cap reached (${spent:.2f} of "
            f"${settings.daily_spend_cap_usd:.2f}). Raise DAILY_SPEND_CAP_USD or "
            f"wait for the window to roll.",
        )


DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]

REDACTED = "redacted"


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=settings.max_query_chars)
    limit: int = Field(default=6, ge=1, le=20)
    graph_depth: int = Field(default=1, ge=0, le=kg.MAX_DEPTH)


@router.post("/query")
def hybrid_query(body: QueryRequest, request: Request, db: DbDep, user: UserDep) -> dict:
    if not body.question.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "question cannot be only whitespace"
        )
    # Both guards run before any paid call is made.
    if not query_limiter.allow(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many questions in a short window. Try again shortly.",
            {"Retry-After": str(query_limiter.retry_after(str(user.id)))},
        )
    _spend_guard(db)
    started = time.perf_counter()
    show_money = user.role in FINANCE_ROLES

    # 1. Structured: resolve identifiers and customer names to actual rows.
    identifiers = resolve.find_identifiers(body.question)
    customer_rows = db.execute(select(Customer.id, Customer.name)).all()
    matched_customers = resolve.match_customers(
        body.question, [(c.id, c.name) for c in customer_rows]
    )
    records, node_ids = _fetch_records(db, identifiers, matched_customers, show_money)

    # 2. Vector: semantically similar document chunks.
    chunks, retrieval_error = _vector_hits(db, body.question, body.limit)

    # 3. Graph: what each resolved entity connects to.
    relationships = _graph_context(db, node_ids, body.graph_depth)

    # 4. Text-to-SQL: the model composes a query for anything the fixed lookups
    # cannot express — aggregates, comparisons, rankings.
    chat = llm.get_chat()
    generated, sql_tokens = _text_to_sql(chat, body.question)

    # 5. Answer, grounded in exactly what was retrieved above.
    context = _build_context(records, relationships, chunks, generated)
    try:
        answer = chat.answer(body.question, context)
    except Exception as exc:
        # An upstream LLM failure is not a client error and must not look like
        # a confident empty answer.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"answer generation failed: {exc}"
        ) from exc

    took_ms = round((time.perf_counter() - started) * 1000, 1)
    # Both LLM calls are billable, so the envelope reports their sum.
    input_tokens = answer.input_tokens + sql_tokens[0]
    output_tokens = answer.output_tokens + sql_tokens[1]
    cost = chat.cost_usd(input_tokens, output_tokens)

    payload = {
        "question": body.question,
        "answer": answer.text,
        "confidence": _confidence(records, chunks, generated),
        "explanation": {
            "sql_evidence": records,
            "generated_sql": generated,
            "graph_relationships": relationships,
            "retrieved_chunks": chunks,
            "resolved": {
                "identifiers": identifiers,
                "customers": [{"id": i, "name": n} for i, n in matched_customers],
            },
            "financials_visible": show_money,
            "retrieval_error": retrieval_error,
        },
        "model": {
            "provider": chat.name,
            "chat_model": chat.model,
            "embedding_model": get_embedder().model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "llm_calls": 2 if settings.enable_text_to_sql else 1,
        },
        "took_ms": took_ms,
    }

    metrics.observe_llm(chat.name, input_tokens, output_tokens, cost)
    if generated.get("rejected"):
        # Label by category, not the full message: the message can contain a table
        # name and would explode label cardinality.
        reason = generated["rejected"].split(":")[0].split(";")[0].strip()[:40]
        metrics.sql_gate_rejections.labels(reason).inc()

    audit.record_query(
        db,
        user,
        endpoint="/query",
        request_id=getattr(request.state, "request_id", "unknown"),
        payload=payload,
    )
    return payload


def _text_to_sql(chat: llm.Chat, question: str) -> tuple[dict[str, Any], tuple[int, int]]:
    """Generate, gate, and run a query. Returns (report, (in_tokens, out_tokens)).

    Every outcome is reported rather than raised: a rejected or failed query
    still leaves the structured, graph and document context intact, and the
    reason lands in the audit envelope.
    """
    report: dict[str, Any] = {
        "attempted": settings.enable_text_to_sql,
        "raw": None,
        "sql": None,
        "rejected": None,
        "error": None,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }
    if not settings.enable_text_to_sql:
        return report, (0, 0)

    try:
        generated = chat.generate_sql(question, sqlgen.SQL_SYSTEM_PROMPT)
    except Exception as exc:
        report["error"] = f"generation failed: {exc}"
        return report, (0, 0)

    tokens = (generated.input_tokens, generated.output_tokens)
    report["raw"] = generated.text

    try:
        safe_sql = sqlgen.validate(generated.text)
    except sqlgen.UnsafeSQL as exc:
        report["rejected"] = str(exc)
        # A model declining an unanswerable question is normal operation. A query
        # the gate had to block is not — keep the levels apart so Phase 6 can
        # alert on real rejections without drowning in NO_QUERY.
        if sqlgen.clean(generated.text).upper() == "NO_QUERY":
            logger.info("no query generated for: %r", question)
        else:
            logger.warning("gate BLOCKED generated SQL: %s | %r", exc, generated.text)
        return report, tokens

    report["sql"] = safe_sql
    result = sqlgen.run(safe_sql)
    report.update(
        {
            "error": result.error,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }
    )
    return report, tokens


def _fetch_records(
    db: Session,
    identifiers: dict[str, list[str]],
    customers: list[tuple[int, str]],
    show_money: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load the rows named in the question. Returns (records, graph node ids)."""
    records: list[dict[str, Any]] = []
    node_ids: list[str] = []

    for number in identifiers.get("contract", []):
        row = db.scalar(select(Contract).where(Contract.number == number))
        if row:
            commodity = db.get(Commodity, row.commodity_id)
            customer = db.get(Customer, row.customer_id)
            records.append(
                {
                    "kind": "contract",
                    "number": row.number,
                    "customer": customer.name if customer else None,
                    "commodity": commodity.name if commodity else None,
                    "quantity_bu": float(row.quantity_bu),
                    # Price is commercial data; same rule as /dashboard.
                    "price_per_bu": float(row.price_per_bu) if show_money else REDACTED,
                    "start_date": row.start_date.isoformat(),
                    "end_date": row.end_date.isoformat(),
                    "status": row.status,
                }
            )
            node_ids.append(kg.node_id("contract", row.id))

    for number in identifiers.get("invoice", []):
        row = db.scalar(select(Invoice).where(Invoice.number == number))
        if row:
            customer = db.get(Customer, row.customer_id)
            records.append(
                {
                    "kind": "invoice",
                    "number": row.number,
                    "customer": customer.name if customer else None,
                    "amount": float(row.amount) if show_money else REDACTED,
                    "issued_date": row.issued_date.isoformat(),
                    "due_date": row.due_date.isoformat(),
                    "status": row.status,
                }
            )
            node_ids.append(kg.node_id("invoice", row.id))

    for ticket in identifiers.get("delivery", []):
        row = db.scalar(select(Delivery).where(Delivery.ticket_number == ticket))
        if row:
            commodity = db.get(Commodity, row.commodity_id)
            facility = db.get(Facility, row.facility_id)
            records.append(
                {
                    "kind": "delivery",
                    "ticket": row.ticket_number,
                    "truck_id": row.truck_id,
                    "commodity": commodity.name if commodity else None,
                    "facility": facility.name if facility else None,
                    "net_bu": float(row.net_bu),
                    "moisture_pct": float(row.moisture_pct),
                    "delivered_at": row.delivered_at.isoformat(),
                    "verified": row.verified,
                }
            )
            node_ids.append(kg.node_id("delivery", row.id))

    for name in identifiers.get("bin", []):
        row = db.scalar(select(StorageBin).where(StorageBin.name == name))
        if row:
            facility = db.get(Facility, row.facility_id)
            commodity = db.get(Commodity, row.commodity_id) if row.commodity_id else None
            records.append(
                {
                    "kind": "bin",
                    "name": row.name,
                    "facility": facility.name if facility else None,
                    "commodity": commodity.name if commodity else None,
                    "capacity_bu": float(row.capacity_bu),
                    "current_bu": float(row.current_bu),
                    "moisture_pct": (
                        float(row.moisture_pct) if row.moisture_pct is not None else None
                    ),
                }
            )
            node_ids.append(kg.node_id("bin", row.id))

    for customer_id, name in customers:
        contracts = db.scalars(
            select(Contract).where(Contract.customer_id == customer_id)
        ).all()
        records.append(
            {
                "kind": "customer",
                "name": name,
                "open_contracts": sum(1 for c in contracts if c.status == "open"),
                "contract_numbers": [c.number for c in contracts],
            }
        )
        node_ids.append(kg.node_id("customer", customer_id))

    return records, node_ids


def _vector_hits(
    db: Session, question: str, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Semantic chunk retrieval. A vector-store failure degrades rather than 500s."""
    embedder = get_embedder()
    try:
        vectors.ensure_collection(embedder.dimensions)
        vector = embedder.embed([question])[0]
        hits = vectors.search(vector, limit)
    except Exception as exc:
        # Structured and graph context are still useful without document search;
        # the reason is reported rather than swallowed.
        return [], str(exc)

    if not hits:
        return [], None
    texts = dict(
        db.execute(
            select(DocumentChunk.id, DocumentChunk.text).where(
                DocumentChunk.id.in_([h["chunk_id"] for h in hits])
            )
        ).all()
    )
    return [
        {
            "score": round(hit["score"], 4),
            "text": texts.get(hit["chunk_id"], hit.get("preview", "")),
            "source": {
                "document_id": hit.get("document_id"),
                "filename": hit.get("filename"),
                "sha256": hit.get("sha256"),
                "chunk_ordinal": hit.get("ordinal"),
                "chunk_id": hit["chunk_id"],
            },
        }
        for hit in hits
    ], None


def _graph_context(db: Session, node_ids: list[str], depth: int) -> list[dict[str, Any]]:
    """Edges around the resolved entities, as readable triples."""
    if not node_ids or depth == 0:
        return []
    graph = kg.build(
        customers=db.scalars(select(Customer)).all(),
        commodities=db.scalars(select(Commodity)).all(),
        facilities=db.scalars(select(Facility)).all(),
        bins=db.scalars(select(StorageBin)).all(),
        contracts=db.scalars(select(Contract)).all(),
        deliveries=db.scalars(select(Delivery)).all(),
        invoices=db.scalars(select(Invoice)).all(),
    )
    seen: set[tuple[str, str, str]] = set()
    triples: list[dict[str, Any]] = []
    for node_id in node_ids:
        if node_id not in graph.nodes:
            continue
        for edge in kg.expand(graph, node_id, depth)["edges"]:
            key = (edge["source"], edge["label"], edge["target"])
            if key in seen:
                continue
            seen.add(key)
            triples.append(
                {
                    "source": graph.nodes[edge["source"]]["label"],
                    "source_id": edge["source"],
                    "relationship": edge["label"],
                    "target": graph.nodes[edge["target"]]["label"],
                    "target_id": edge["target"],
                }
            )
    return triples


def _build_context(
    records: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    generated: dict[str, Any] | None = None,
) -> str:
    """Assemble the prompt context with stable labels the answer can cite."""
    parts: list[str] = []

    if generated and generated.get("rows"):
        parts.append("QUERY RESULT")
        parts.append(f"(from: {generated['sql']})")
        for i, row in enumerate(generated["rows"][:50], 1):
            fields = ", ".join(f"{k}={v}" for k, v in row.items())
            parts.append(f"[Q{i}] {fields}")
        if generated.get("truncated"):
            parts.append(f"(truncated at {len(generated['rows'])} rows)")
        parts.append("")

    if records:
        parts.append("DATABASE RECORDS")
        for i, record in enumerate(records, 1):
            fields = ", ".join(f"{k}={v}" for k, v in record.items() if k != "kind")
            parts.append(f"[DB{i}] {record['kind']}: {fields}")

    if relationships:
        parts.append("\nRELATED ENTITIES (knowledge graph)")
        for i, t in enumerate(relationships, 1):
            parts.append(f"[G{i}] {t['source']} -{t['relationship']}-> {t['target']}")

    if chunks:
        parts.append("\nDOCUMENT EXCERPTS")
        for i, chunk in enumerate(chunks, 1):
            source = chunk["source"]
            parts.append(
                f"[S{i}] from {source['filename']} "
                f"(chunk {source['chunk_ordinal']}, score {chunk['score']}):\n"
                f"{chunk['text']}"
            )

    context = "\n".join(parts) if parts else "(no matching records or documents)"
    # Hard ceiling so a big corpus cannot balloon a paid request.
    if len(context) > settings.max_context_chars:
        context = context[: settings.max_context_chars] + "\n... (context truncated)"
    return context


def _confidence(
    records: list[dict], chunks: list[dict], generated: dict[str, Any] | None = None
) -> float:
    """Retrieval-quality heuristic, not something the model reports.

    An exact database row is strong evidence; a semantic match is weaker and
    scales with its score. Deliberately conservative and explainable — it is
    derived only from what was retrieved.
    """
    rows = bool(generated and generated.get("rows"))
    if not records and not chunks and not rows:
        return 0.0
    # A query that actually returned rows is direct evidence, same weight as an
    # exact record lookup.
    score = 0.6 if (records or rows) else 0.0
    if chunks:
        score += 0.4 * min(1.0, max(c["score"] for c in chunks))
    return round(min(score, 0.99), 2)

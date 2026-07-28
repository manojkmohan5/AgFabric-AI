"""Audit Center: persist every AI request, and read it back.

Recording never raises. An audit write failing must not turn a successful answer
into a 500 — the answer was already produced and is owed to the caller. A failed
write is logged loudly instead, because a silently missing audit trail is worse
than a noisy one.

Access rule, matching the rest of the app: finance roles (accountant, exec) read
everything including cost; everyone else reads only their own requests and does
not see cost figures.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db
from .models import FINANCE_ROLES, AuditLog, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/audit", tags=["audit"])

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]

# Answers and questions can be long; cap what is stored so one pathological
# request cannot bloat the table.
MAX_TEXT = 8_000


def record_query(
    db: Session,
    user: User,
    endpoint: str,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist a /query or /search response. Swallows its own failures by design."""
    try:
        explanation = payload.get("explanation") or {}
        model = payload.get("model") or {}
        generated = explanation.get("generated_sql") or {}
        chunks = explanation.get("retrieved_chunks") or []

        db.add(
            AuditLog(
                request_id=request_id,
                user_id=user.id,
                user_email=user.email,
                user_role=user.role,
                endpoint=endpoint,
                question=str(payload.get("question", ""))[:MAX_TEXT],
                answer=(
                    str(payload["answer"])[:MAX_TEXT] if payload.get("answer") else None
                ),
                confidence=payload.get("confidence"),
                provider=model.get("provider"),
                chat_model=model.get("chat_model"),
                embedding_model=model.get("embedding_model"),
                input_tokens=int(model.get("input_tokens") or 0),
                output_tokens=int(model.get("output_tokens") or 0),
                cost_usd=float(model.get("cost_usd") or 0.0),
                # Identifiers only, not the full text — the chunks themselves are
                # still in Postgres and can be re-read by id.
                sources={
                    "chunks": [
                        {
                            "chunk_id": c["source"].get("chunk_id"),
                            "document_id": c["source"].get("document_id"),
                            "filename": c["source"].get("filename"),
                            "sha256": c["source"].get("sha256"),
                            "score": c.get("score"),
                        }
                        for c in chunks
                    ],
                    "resolved": explanation.get("resolved"),
                    "records": [
                        {
                            k: v
                            for k, v in r.items()
                            if k in ("kind", "number", "name", "ticket")
                        }
                        for r in (explanation.get("sql_evidence") or [])
                    ],
                },
                generated_sql=generated.get("sql"),
                sql_rejected=generated.get("rejected"),
                record_count=len(explanation.get("sql_evidence") or []),
                chunk_count=len(chunks),
                graph_edge_count=len(explanation.get("graph_relationships") or []),
                took_ms=float(payload.get("took_ms") or 0.0),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to write audit log for request %s", request_id)


@router.get("")
def list_audit(
    db: DbDep,
    user: UserDep,
    endpoint: str | None = None,
    since_hours: Annotated[int | None, Query(ge=1, le=8760)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    scoped = user.role not in FINANCE_ROLES
    if scoped:
        # Non-finance roles see only their own requests.
        query = query.where(AuditLog.user_id == user.id)
    if endpoint:
        query = query.where(AuditLog.endpoint == endpoint)
    if since_hours:
        query = query.where(
            AuditLog.created_at >= datetime.now(UTC) - timedelta(hours=since_hours)
        )

    rows = db.scalars(query.limit(limit).offset(offset)).all()
    return {
        "count": len(rows),
        "scoped_to_self": scoped,
        "entries": [_serialise(r, show_cost=not scoped) for r in rows],
    }


@router.get("/summary")
def summary(
    db: DbDep,
    user: Annotated[User, Depends(current_user)],
    since_hours: Annotated[int, Query(ge=1, le=8760)] = 24,
) -> dict:
    """Spend and volume rollup. Cost is a finance concern, so it is gated."""
    if user.role not in FINANCE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "audit summary requires accountant or exec"
        )
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)

    totals = db.execute(
        select(
            func.count(AuditLog.id),
            func.coalesce(func.sum(AuditLog.input_tokens), 0),
            func.coalesce(func.sum(AuditLog.output_tokens), 0),
            func.coalesce(func.sum(AuditLog.cost_usd), 0.0),
            func.coalesce(func.avg(AuditLog.took_ms), 0.0),
        ).where(AuditLog.created_at >= cutoff)
    ).one()

    by_user = db.execute(
        select(
            AuditLog.user_email,
            func.count(AuditLog.id),
            func.coalesce(func.sum(AuditLog.cost_usd), 0.0),
        )
        .where(AuditLog.created_at >= cutoff)
        .group_by(AuditLog.user_email)
        .order_by(func.sum(AuditLog.cost_usd).desc())
    ).all()

    return {
        "since_hours": since_hours,
        "requests": totals[0],
        "input_tokens": int(totals[1]),
        "output_tokens": int(totals[2]),
        "cost_usd": round(float(totals[3]), 6),
        "avg_took_ms": round(float(totals[4]), 1),
        "by_user": [
            {"user": email, "requests": n, "cost_usd": round(float(cost), 6)}
            for email, n, cost in by_user
        ],
        "by_endpoint": [
            {"endpoint": ep, "requests": n}
            for ep, n in db.execute(
                select(AuditLog.endpoint, func.count(AuditLog.id))
                .where(AuditLog.created_at >= cutoff)
                .group_by(AuditLog.endpoint)
            ).all()
        ],
    }


@router.get("/{entry_id}")
def get_entry(entry_id: int, db: DbDep, user: UserDep) -> dict:
    entry = db.get(AuditLog, entry_id)
    scoped = user.role not in FINANCE_ROLES
    # Same 404 for absent and forbidden, so the response does not confirm that
    # another user's request exists.
    if entry is None or (scoped and entry.user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such audit entry")
    return _serialise(entry, show_cost=not scoped, full=True)


def _serialise(entry: AuditLog, *, show_cost: bool, full: bool = False) -> dict:
    payload: dict[str, Any] = {
        "id": entry.id,
        "request_id": entry.request_id,
        "user": entry.user_email,
        "role": entry.user_role,
        "endpoint": entry.endpoint,
        "question": entry.question,
        "confidence": entry.confidence,
        "provider": entry.provider,
        "chat_model": entry.chat_model,
        "record_count": entry.record_count,
        "chunk_count": entry.chunk_count,
        "graph_edge_count": entry.graph_edge_count,
        "took_ms": entry.took_ms,
        "created_at": entry.created_at.isoformat(),
    }
    if show_cost:
        payload |= {
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "cost_usd": entry.cost_usd,
        }
    if full:
        payload |= {
            "answer": entry.answer,
            "sources": entry.sources,
            "generated_sql": entry.generated_sql,
            "sql_rejected": entry.sql_rejected,
            "embedding_model": entry.embedding_model,
        }
    return payload

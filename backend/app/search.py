"""Semantic search over document chunks.

Every result traces back to its source: document id, filename, content hash, and
chunk ordinal. The response also reports provider, model and latency, which is
the Explainable AI envelope the plan asks for.
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import indexing, vectors
from .auth import current_user, require_role
from .config import settings
from .db import get_db
from .embed import get_embedder
from .models import Document, DocumentChunk, User

router = APIRouter(tags=["search"])

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]


class SearchRequest(BaseModel):
    # Bounds enforced by the schema, so a hostile body is rejected before any
    # embedding call is paid for.
    query: str = Field(min_length=1, max_length=settings.max_query_chars)
    limit: int = Field(default=10, ge=1, le=50)
    document_id: int | None = Field(default=None, ge=1)


@router.post("/search")
def semantic_search(body: SearchRequest, db: DbDep, user: UserDep) -> dict:
    if not body.query.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "query cannot be only whitespace"
        )

    embedder = get_embedder()
    started = time.perf_counter()
    try:
        vectors.ensure_collection(embedder.dimensions)
        vector = embedder.embed([body.query])[0]
        hits = vectors.search(vector, body.limit, body.document_id)
    except RuntimeError as exc:  # misconfiguration, not a client error
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    took_ms = round((time.perf_counter() - started) * 1000, 1)

    # Full chunk text comes from PostgreSQL rather than the Qdrant payload, which
    # only stores a preview. Keeps the index small and the evidence complete.
    chunk_ids = [h["chunk_id"] for h in hits]
    texts = (
        dict(
            db.execute(
                select(DocumentChunk.id, DocumentChunk.text).where(
                    DocumentChunk.id.in_(chunk_ids)
                )
            ).all()
        )
        if chunk_ids
        else {}
    )

    return {
        "query": body.query,
        "provider": embedder.name,
        "model": embedder.model,
        "dimensions": embedder.dimensions,
        "took_ms": took_ms,
        "count": len(hits),
        "results": [
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
        ],
    }


@router.post("/search/reindex")
def reindex(
    db: DbDep, user: Annotated[User, Depends(require_role("ops", "exec"))]
) -> dict:
    """Embed anything not yet embedded. Safe to re-run; it skips finished chunks."""
    return indexing.reindex(db)


@router.get("/search/status")
def status_(db: DbDep, user: UserDep) -> dict:
    embedder = get_embedder()
    pending = db.scalar(
        select(DocumentChunk.id).where(DocumentChunk.embedded.is_(False)).limit(1)
    )
    return {
        "provider": embedder.name,
        "model": embedder.model,
        "dimensions": embedder.dimensions,
        "collection": settings.qdrant_collection,
        "vectors": vectors.count(),
        "documents": db.scalar(select(Document.id).limit(1)) is not None,
        "chunks_pending": pending is not None,
    }

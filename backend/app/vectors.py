"""Qdrant collection for document chunks.

Point ids are chunk primary keys, so re-embedding a chunk overwrites its vector
instead of duplicating it. PostgreSQL stays the source of truth; Qdrant holds
only what is needed to rank and to trace a hit back to its document.
"""

import logging
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from qdrant_client import QdrantClient, models

from .config import settings

# Payload copied alongside each vector so a search result is self-describing
# without a second round trip.
PREVIEW_CHARS = 400


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> QdrantClient:
    if not settings.qdrant_url:
        # A blank QDRANT_URL means this deployment runs without vector search.
        # Said plainly here rather than as a refused connection to localhost.
        raise RuntimeError("document search is not configured on this deployment")
    return QdrantClient(url=settings.qdrant_url, timeout=30)


def ensure_collection(dimensions: int) -> None:
    """Create the collection if absent. Idempotent.

    A dimension mismatch is raised rather than silently recreated — dropping a
    populated index because a config value changed would be data loss.
    """
    qdrant = client()
    name = settings.qdrant_collection
    if not qdrant.collection_exists(name):
        qdrant.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=dimensions, distance=models.Distance.COSINE
            ),
        )
        return
    existing = qdrant.get_collection(name).config.params.vectors.size
    if existing != dimensions:
        raise RuntimeError(
            f"Qdrant collection {name!r} has {existing} dimensions but the current "
            f"embedder produces {dimensions}. Switching embedding models needs a "
            f"reindex: delete the collection and re-run POST /search/reindex."
        )


def upsert(points: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]) -> None:
    """`points` carries at least {id, document_id, ordinal, filename, text}."""
    if not points:
        return
    client().upsert(
        collection_name=settings.qdrant_collection,
        points=[
            models.PointStruct(
                id=point["id"],
                vector=list(vector),
                payload={
                    "document_id": point["document_id"],
                    "ordinal": point["ordinal"],
                    "filename": point["filename"],
                    "sha256": point["sha256"],
                    "preview": point["text"][:PREVIEW_CHARS],
                },
            )
            for point, vector in zip(points, vectors, strict=True)
        ],
        wait=True,  # so an upload is searchable the moment it returns
    )


def search(
    vector: Sequence[float], limit: int, document_id: int | None = None
) -> list[dict[str, Any]]:
    query_filter = None
    if document_id is not None:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            ]
        )
    hits = (
        client()
        .query_points(
            collection_name=settings.qdrant_collection,
            query=list(vector),
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        .points
    )
    return [
        {"chunk_id": hit.id, "score": hit.score, **(hit.payload or {})} for hit in hits
    ]


def delete_collection() -> None:
    qdrant = client()
    if qdrant.collection_exists(settings.qdrant_collection):
        qdrant.delete_collection(settings.qdrant_collection)


def count() -> int | None:
    """Vectors stored, or None when the store is off or unreachable.

    Only ever reported, never acted on, so a missing store must not turn the
    monitoring and embedding agents' runs into failures.
    """
    try:
        qdrant = client()
        if not qdrant.collection_exists(settings.qdrant_collection):
            return 0
        return qdrant.count(settings.qdrant_collection, exact=True).count
    except Exception:
        logger.warning("vector count unavailable", exc_info=True)
        return None

"""Embed chunks into Qdrant and record that it happened.

Shared by upload (inline) and the reindex route (backfill and retry). Embedding
is the step most likely to fail — no API key, rate limit, Qdrant down — so a
failure here must never lose an already-stored document. Callers keep the
document and surface the reason instead.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import vectors
from .embed import get_embedder
from .models import Document, DocumentChunk


def index_document(db: Session, document: Document) -> int:
    """Embed this document's un-embedded chunks. Returns how many were indexed."""
    chunks = db.scalars(
        select(DocumentChunk)
        .where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.embedded.is_(False),
        )
        .order_by(DocumentChunk.ordinal)
    ).all()
    if not chunks:
        return 0

    embedder = get_embedder()
    vectors.ensure_collection(embedder.dimensions)
    embeddings = embedder.embed([c.text for c in chunks])

    vectors.upsert(
        [
            {
                "id": c.id,
                "document_id": document.id,
                "ordinal": c.ordinal,
                "filename": document.filename,
                "sha256": document.sha256,
                "text": c.text,
            }
            for c in chunks
        ],
        embeddings,
    )

    # Only mark embedded after Qdrant confirmed the write, so a failure leaves
    # the chunks retryable rather than silently missing from search.
    db.execute(
        update(DocumentChunk)
        .where(DocumentChunk.id.in_([c.id for c in chunks]))
        .values(embedded=True)
    )
    document.status = "embedded"
    db.commit()
    return len(chunks)


def reindex(db: Session) -> dict:
    """Embed every document that has un-embedded chunks."""
    pending = db.scalars(
        select(Document)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(DocumentChunk.embedded.is_(False))
        .distinct()
    ).all()

    indexed = 0
    failures: list[dict] = []
    for document in pending:
        try:
            indexed += index_document(db, document)
        except Exception as exc:  # one bad document must not abort the batch
            db.rollback()
            failures.append({"document_id": document.id, "error": str(exc)})
    return {
        "documents_seen": len(pending),
        "chunks_indexed": indexed,
        "failures": failures,
        "vectors_in_collection": vectors.count(),
    }

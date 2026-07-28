"""Document upload and retrieval.

Processing is inline rather than queued.
ponytail: synchronous extract + chunk on the request thread. Fine for the 25MB
ceiling and pure-Python parsers; move `process` into a Celery task (Redis is
already in docker-compose) when uploads get large enough to time out a request.
"""

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import extract, indexing, storage
from .auth import current_user
from .chunk import chunk_text
from .config import settings
from .db import get_db
from .extract import ALLOWED_SUFFIXES, ExtractionError, safe_suffix
from .models import Document, DocumentChunk, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]

READ_CHUNK = 1024 * 1024


def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read the upload, refusing anything past `limit`.

    Streamed in slices and checked as it goes, so an oversized or lying
    Content-Length cannot make the process buffer the whole thing first.
    """
    buffer = bytearray()
    while piece := upload.file.read(READ_CHUNK):
        buffer += piece
        if len(buffer) > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"file exceeds the {limit // (1024 * 1024)}MB limit",
            )
    if not buffer:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file is empty")
    return bytes(buffer)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload(
    db: DbDep,
    user: UserDep,
    file: Annotated[UploadFile, File()],
) -> dict:
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "filename is required")

    suffix = safe_suffix(filename)
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported type {suffix or '(none)'}; "
            f"allowed: {', '.join(sorted(ALLOWED_SUFFIXES))}",
        )

    data = _read_limited(file, settings.max_upload_bytes)
    digest = hashlib.sha256(data).hexdigest()

    # Exact duplicate: same bytes already stored. Return the original instead of
    # storing it twice.
    existing = db.scalar(select(Document).where(Document.sha256 == digest))
    if existing is not None:
        return {
            "duplicate": True,
            "document": _serialise(existing),
            "message": f"identical content already uploaded as {existing.filename}",
        }

    # Same name, different bytes: a new version of that document.
    prior = db.scalar(
        select(func.max(Document.version)).where(Document.filename == filename)
    )
    version = (prior or 0) + 1

    try:
        text, note = extract.extract(data, filename)
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # Key off the content hash, never the supplied filename — a hostile name
    # cannot reach storage at all.
    object_key = f"documents/{digest}{suffix}"
    storage.ensure_bucket()
    storage.put(object_key, data, file.content_type or "application/octet-stream")

    chunks = chunk_text(text, settings.chunk_size, settings.chunk_overlap)
    document = Document(
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        sha256=digest,
        object_key=object_key,
        version=version,
        text_chars=len(text),
        chunk_count=len(chunks),
        # "extracted" means text is chunked and waiting on Phase 3 embedding.
        status="extracted" if chunks else "empty",
        extract_note=note,
        uploaded_by=user.id,
    )
    db.add(document)
    db.flush()
    db.add_all(
        DocumentChunk(
            document_id=document.id,
            ordinal=i,
            text=chunk,
            char_count=len(chunk),
        )
        for i, chunk in enumerate(chunks)
    )
    db.commit()

    # Embedding is the step most likely to fail (no API key, rate limit, Qdrant
    # down). The document is already committed, so a failure downgrades it to
    # retryable rather than discarding an upload the user just made.
    indexed, index_error = 0, None
    if chunks:
        try:
            indexed = indexing.index_document(db, document)
        except Exception as exc:
            db.rollback()
            index_error = str(exc)
            logger.warning(
                "indexing failed for document %s: %s", document.id, exc, exc_info=True
            )

    return {
        "duplicate": False,
        "document": _serialise(document),
        "chunks_indexed": indexed,
        # Non-null means the file is stored and searchable only after a reindex.
        "index_error": index_error,
    }


@router.get("")
def list_documents(db: DbDep, user: UserDep, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows = db.scalars(
        select(Document).order_by(Document.uploaded_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "total": db.scalar(select(func.count()).select_from(Document)) or 0,
        "limit": limit,
        "offset": offset,
        "documents": [_serialise(d) for d in rows],
    }


@router.get("/{document_id}")
def get_document(document_id: int, db: DbDep, user: UserDep) -> dict:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such document")
    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.ordinal)
    ).all()
    return {
        **_serialise(document),
        "chunks": [
            {
                "ordinal": c.ordinal,
                "char_count": c.char_count,
                "embedded": c.embedded,
                "preview": c.text[:200],
            }
            for c in chunks
        ],
    }


def _serialise(document: Document) -> dict:
    return {
        "id": document.id,
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "version": document.version,
        "text_chars": document.text_chars,
        "chunk_count": document.chunk_count,
        "status": document.status,
        "note": document.extract_note,
        "uploaded_at": document.uploaded_at.isoformat(),
    }

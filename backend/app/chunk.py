"""Text chunking for the embedding pipeline.

Character-based rather than token-based on purpose: no tokenizer dependency, and
at these sizes the difference does not change retrieval quality. Paragraph
boundaries are preserved where they fit, so chunks stay readable when shown as
evidence in the Explainable AI payload.
"""

import re

_PARAGRAPH = re.compile(r"\n\s*\n")
_JOIN = "\n\n"


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split `text` into chunks of at most `size` characters.

    Paragraphs are packed together while they fit. A single paragraph longer than
    `size` is hard-split into overlapping windows, which is the only place
    `overlap` applies — packed paragraphs need no overlap because no semantic
    unit was cut.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and < size")

    text = text.strip()
    if not text:
        return []

    step = size - overlap  # > 0 given the guard above, so windowing terminates
    pieces: list[str] = []
    for para in (p.strip() for p in _PARAGRAPH.split(text)):
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
        else:
            pieces.extend(para[i : i + size] for i in range(0, len(para), step))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + len(_JOIN) + len(piece) <= size:
            current += _JOIN + piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks

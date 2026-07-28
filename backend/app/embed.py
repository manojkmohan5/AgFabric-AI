"""Embedding providers behind one seam.

Two real implementations, which is why the seam exists:

- `OpenAIEmbedder` is what runs in production.
- `FakeEmbedder` lets the whole check suite and CI run with no API key, no
  network, and no cost.

The fake is not random padding. It is the hashing trick over word tokens, so
texts sharing vocabulary genuinely score higher against each other. That means
the search smoke test exercises real ranking instead of asserting a tautology.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

from .config import settings

_TOKEN = re.compile(r"[a-z0-9]+")
# OpenAI's embeddings endpoint caps inputs per call; batch below it.
BATCH = 128


class Embedder(Protocol):
    name: str
    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic lexical embedding. No network, stable across processes."""

    name = "fake"

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.model = f"hashing-trick-{dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN.findall(text.lower()):
            # blake2b, not hash(): the built-in is salted per process, which
            # would make vectors differ between the writer and the reader.
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            n = int.from_bytes(digest, "big")
            # Signed buckets cancel some collisions instead of compounding them.
            vector[n % self.dimensions] += 1.0 if n & 1 else -1.0
        return _normalise(vector)


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, api_key: str, model: str, dimensions: int) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = [t or " " for t in texts[start : start + BATCH]]
            response = self._client.embeddings.create(
                model=self.model, input=batch, dimensions=self.dimensions
            )
            # The API documents order preservation, but sorting by index makes
            # a silent mismatch impossible rather than merely unlikely.
            vectors.extend(
                item.embedding for item in sorted(response.data, key=lambda d: d.index)
            )
        return vectors


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        # Empty or punctuation-only text: return a fixed unit vector rather than
        # dividing by zero and poisoning the index with NaNs.
        zero = [0.0] * len(vector)
        zero[0] = 1.0
        return zero
    return [v / norm for v in vector]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    provider = settings.embedding_provider.lower()
    if provider == "auto":
        provider = "openai" if settings.openai_api_key else "fake"
    if provider == "fake":
        return FakeEmbedder(settings.embedding_dimensions)
    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIEmbedder(
            settings.openai_api_key,
            settings.openai_embedding_model,
            settings.embedding_dimensions,
        )
    raise RuntimeError(f"unknown EMBEDDING_PROVIDER {provider!r}; use auto|openai|fake")

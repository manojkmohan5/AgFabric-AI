"""OCR for uploaded images, behind the same provider seam as embeddings and chat.

Why the vision API rather than a local engine — the whole reason is weight:

  easyocr / paddleocr   pull PyTorch, ~2GB
  rapidocr-onnxruntime  ~65MB of wheels plus a model download
  pytesseract           needs the tesseract *binary* installed in the image
  gpt-4o-mini vision    zero new dependencies, and better on skewed photos

An OpenAI key is already required for embeddings and answers, so this adds no new
install footprint at all. `FakeOCR` keeps the check suite offline and free.

Images are validated by magic bytes, never by the claimed extension or
content-type — both are attacker-controlled, and these bytes get sent to a paid
API and stored.
"""

import base64
import logging
from functools import lru_cache
from typing import Protocol

from .config import settings

logger = logging.getLogger(__name__)

# suffix -> (magic prefix, mime). WEBP needs a second check at offset 8.
IMAGE_SIGNATURES: dict[str, tuple[bytes, str]] = {
    ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
    ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    ".webp": (b"RIFF", "image/webp"),
    ".gif": (b"GIF8", "image/gif"),
}
IMAGE_SUFFIXES = frozenset(IMAGE_SIGNATURES)

PROMPT = (
    "Transcribe all text visible in this image exactly as written. This is an "
    "agricultural business document — a scale ticket, contract, invoice or "
    "delivery note. Preserve numbers, dates, ticket numbers and units precisely; "
    "they are the reason the image is being read. Lay out tabular data as rows "
    "with ' | ' between cells. Return only the transcription. If the image "
    "contains no legible text, return exactly: NO_TEXT"
)


class OCRError(Exception):
    """The image could not be read."""


def sniff_image(data: bytes, suffix: str) -> str:
    """Return the real mime type, or raise if the bytes are not that image type.

    A file called `photo.png` containing a PE executable is rejected here rather
    than being uploaded, billed for, and stored.
    """
    signature = IMAGE_SIGNATURES.get(suffix)
    if signature is None:
        raise OCRError(f"{suffix} is not a supported image type")
    prefix, mime = signature
    if not data.startswith(prefix):
        raise OCRError(f"file claims to be {suffix} but its contents are not {mime}")
    # RIFF also fronts WAV and AVI, so confirm the WEBP form specifically.
    if suffix == ".webp" and data[8:12] != b"WEBP":
        raise OCRError("RIFF container is not WEBP")
    return mime


class Reader(Protocol):
    name: str
    model: str

    def read(self, data: bytes, mime: str) -> str: ...


class FakeOCR:
    """Deterministic stand-in. No network, no cost, no key."""

    name = "fake"
    model = "stub"

    def read(self, data: bytes, mime: str) -> str:
        # Reports what it received so a pipeline test can assert the image
        # reached the reader, without pretending to have transcribed anything.
        return f"[fake-ocr] {mime} image, {len(data)} bytes, no transcription performed"


class OpenAIVisionOCR:
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model

    def read(self, data: bytes, mime: str) -> str:
        encoded = base64.b64encode(data).decode()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=settings.ocr_max_output_tokens,
                temperature=0.0,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{encoded}",
                                    # "high" costs several times more per image
                                    # and buys little on document photos.
                                    "detail": "auto",
                                },
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:
            raise OCRError(f"vision request failed: {exc}") from exc

        text = (response.choices[0].message.content or "").strip()
        return "" if text == "NO_TEXT" else text


@lru_cache(maxsize=1)
def get_reader() -> Reader:
    provider = settings.ocr_provider.lower()
    if provider == "auto":
        provider = "openai" if settings.openai_api_key else "fake"
    if provider == "fake":
        return FakeOCR()
    if provider == "openai":
        if not settings.openai_api_key:
            raise OCRError("OCR_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIVisionOCR(settings.openai_api_key, settings.openai_vision_model)
    raise OCRError(f"unknown OCR_PROVIDER {provider!r}; use auto|openai|fake")

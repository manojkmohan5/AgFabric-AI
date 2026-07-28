"""S3-compatible object storage.

Uses the minio client rather than boto3: same S3 API at a fraction of the install
size. Pointing `S3_ENDPOINT_URL` at real AWS S3 needs no code change.
"""

import io
from functools import lru_cache
from urllib.parse import urlparse

from minio import Minio

from .config import settings


@lru_cache(maxsize=1)
def client() -> Minio:
    """Cached client. lru_cache keeps one connection pool per process."""
    parsed = urlparse(settings.s3_endpoint_url or "https://s3.amazonaws.com")
    return Minio(
        parsed.netloc or parsed.path,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=parsed.scheme == "https",
        region=settings.s3_region,
    )


def ensure_bucket() -> None:
    """Create the bucket if absent. Idempotent, safe to call on every startup."""
    s3 = client()
    if not s3.bucket_exists(settings.s3_bucket):
        s3.make_bucket(settings.s3_bucket)


def put(key: str, data: bytes, content_type: str) -> None:
    client().put_object(
        settings.s3_bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def get(key: str) -> bytes:
    response = client().get_object(settings.s3_bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def delete(key: str) -> None:
    client().remove_object(settings.s3_bucket, key)

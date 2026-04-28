"""Cloudflare R2 storage helpers (S3-compatible via boto3)."""

import io
from functools import lru_cache
from typing import BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import get_settings

# Use multipart upload for objects > 100 MB (R2 PutObject hard limit is 5 GB)
_MULTIPART_THRESHOLD = 100 * 1024 * 1024   # 100 MB
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=_MULTIPART_THRESHOLD,
    multipart_chunksize=64 * 1024 * 1024,   # 64 MB parts
    max_concurrency=4,
)


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def r2_key(source_id: str, period: str, filename: str) -> str:
    """Build the canonical R2 object key for a raw source file."""
    return f"raw/{source_id}/{period}/{filename}"


def upload(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload raw bytes to R2, using multipart for objects > 100 MB.

    Cloudflare R2 PutObject is limited to 5 GB; managed multipart handles
    up to 5 TB.  boto3's upload_fileobj applies multipart automatically when
    the object exceeds _MULTIPART_THRESHOLD.
    """
    settings = get_settings()
    _client().upload_fileobj(
        io.BytesIO(data),
        settings.r2_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
        Config=_TRANSFER_CONFIG,
    )


def upload_fileobj(key: str, fileobj: BinaryIO, content_type: str = "application/octet-stream") -> None:
    """Upload a file-like object to R2 with multipart support."""
    settings = get_settings()
    _client().upload_fileobj(
        fileobj,
        settings.r2_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
        Config=_TRANSFER_CONFIG,
    )


def exists(key: str) -> bool:
    """Return True if the key already exists in R2."""
    settings = get_settings()
    try:
        _client().head_object(Bucket=settings.r2_bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def presigned_url(key: str, expires_in: int = 3600) -> str:
    """Return a presigned GET URL valid for `expires_in` seconds."""
    settings = get_settings()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


CONTENT_TYPES = {
    "csv": "text/csv",
    "xml": "application/xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "html": "text/html",
    "json": "application/json",
    "pdf": "application/pdf",
}

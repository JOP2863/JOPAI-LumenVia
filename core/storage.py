from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from google.cloud import storage


@dataclass(frozen=True)
class UploadResult:
    bucket: str
    path: str
    content_type: Optional[str]


def upload_bytes(
    *,
    gcs: storage.Client,
    bucket_name: str,
    path: str,
    data: bytes,
    content_type: str | None = None,
) -> UploadResult:
    bucket = gcs.bucket(bucket_name)
    blob = bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    return UploadResult(bucket=bucket_name, path=path, content_type=content_type)


def upload_text(
    *,
    gcs: storage.Client,
    bucket_name: str,
    path: str,
    text: str,
    content_type: str = "text/plain; charset=utf-8",
) -> UploadResult:
    return upload_bytes(
        gcs=gcs,
        bucket_name=bucket_name,
        path=path,
        data=text.encode("utf-8"),
        content_type=content_type,
    )


def download_bytes(*, gcs: storage.Client, bucket_name: str, path: str) -> bytes:
    bucket = gcs.bucket(bucket_name)
    blob = bucket.blob(path)
    return blob.download_as_bytes()


def blob_exists(*, gcs: storage.Client, bucket_name: str, path: str) -> bool:
    bucket = gcs.bucket(bucket_name)
    blob = bucket.blob(path)
    return bool(blob.exists())


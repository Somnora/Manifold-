"""Persistent-filesystem access via Lambda's Filesystem S3 Adapter.

Lets the dashboard browse and delete files on a persistent filesystem with
NO instance running. Facts from the Lambda docs:
- Regional endpoints: https://files.<region>.lambda.ai
- Each filesystem's UUID is its bucket name.
- boto3 needs checksum calculation/validation set to "when_required" or the
  adapter returns NotImplemented errors.

boto3 is synchronous; API routes call these methods via a worker thread
(starlette's run_in_threadpool) so the event loop is never blocked.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import boto3
from botocore.config import Config


@dataclass
class StoredFile:
    key: str
    size_bytes: int
    last_modified: str  # ISO 8601


class StorageClient(abc.ABC):
    """Browse/delete files on one persistent filesystem."""

    @abc.abstractmethod
    def list_files(self, prefix: str = "") -> list[StoredFile]: ...

    @abc.abstractmethod
    def delete_file(self, key: str) -> None: ...


class S3AdapterStorage(StorageClient):
    def __init__(self, *, region: str, bucket: str, access_key_id: str,
                 secret_access_key: str):
        if not access_key_id or not secret_access_key:
            raise ValueError("S3 adapter credentials are not configured in .env")
        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=f"https://files.{region}.lambda.ai",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def list_files(self, prefix: str = "") -> list[StoredFile]:
        files: list[StoredFile] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                files.append(StoredFile(
                    key=obj["Key"],
                    size_bytes=obj["Size"],
                    last_modified=obj["LastModified"].isoformat(),
                ))
        return files

    def delete_file(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)


class MockStorage(StorageClient):
    """In-memory stand-in for tests and mock-mode dashboard development."""

    def __init__(self, files: dict[str, int] | None = None):
        # key -> size_bytes
        self.files = files if files is not None else {
            "models/llama-3-8b/model.safetensors": 16_060_522_496,
            "datasets/interviews/day1.wav": 412_318_720,
            "outputs/whisper/day1.srt": 48_211,
        }

    def list_files(self, prefix: str = "") -> list[StoredFile]:
        return [
            StoredFile(key=k, size_bytes=v, last_modified="2026-07-10T00:00:00+00:00")
            for k, v in sorted(self.files.items())
            if k.startswith(prefix)
        ]

    def delete_file(self, key: str) -> None:
        if key not in self.files:
            raise KeyError(key)
        del self.files[key]

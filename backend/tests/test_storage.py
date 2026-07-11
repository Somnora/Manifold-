"""Storage endpoints (no instance required) and the S3 adapter wrapper."""

from datetime import datetime, timezone

import app.storage as storage_module
from app.storage import S3AdapterStorage


def test_list_files(client):
    resp = client.get("/storage/files", params={"filesystem": "manifold-data"})
    assert resp.status_code == 200
    body = resp.json()
    keys = [f["key"] for f in body["files"]]
    assert "models/llama-3-8b/model.safetensors" in keys
    assert all(f["size_bytes"] > 0 for f in body["files"])


def test_list_files_with_prefix(client):
    resp = client.get("/storage/files",
                      params={"filesystem": "manifold-data", "prefix": "models/"})
    keys = [f["key"] for f in resp.json()["files"]]
    assert keys and all(k.startswith("models/") for k in keys)


def test_list_files_unknown_filesystem(client):
    resp = client.get("/storage/files", params={"filesystem": "nope"})
    assert resp.status_code == 404


def test_delete_file(client, mock_storage):
    key = "outputs/whisper/day1.srt"
    resp = client.delete(f"/storage/files/{key}",
                         params={"filesystem": "manifold-data"})
    assert resp.status_code == 200
    assert key not in mock_storage.files


def test_delete_missing_file_is_404(client):
    resp = client.delete("/storage/files/no/such/key",
                         params={"filesystem": "manifold-data"})
    assert resp.status_code == 404


def test_filesystems_endpoint(client):
    resp = client.get("/filesystems")
    assert resp.status_code == 200
    fs = resp.json()["filesystems"][0]
    assert fs["name"] == "manifold-data"
    assert fs["region"] == "us-east-1"
    assert fs["mount_point"] == "/lambda/nfs/manifold-data"


# -- S3AdapterStorage against a fake boto3 client --------------------------------


class FakeS3Client:
    """Duck-typed stand-in for boto3's S3 client: two pages + deletes."""

    def __init__(self):
        self.deleted: list[tuple[str, str]] = []
        ts = datetime(2026, 7, 10, tzinfo=timezone.utc)
        self._pages = [
            {"Contents": [
                {"Key": "a.bin", "Size": 100, "LastModified": ts},
                {"Key": "b/c.bin", "Size": 200, "LastModified": ts},
            ]},
            {"Contents": [
                {"Key": "d.bin", "Size": 300, "LastModified": ts},
            ]},
        ]

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        pages = self._pages

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                assert Bucket == "fs-uuid-123"
                return iter(pages)

        return _Paginator()

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))


def test_s3_adapter_lists_across_pages(monkeypatch):
    fake = FakeS3Client()
    captured = {}

    def fake_boto3_client(service, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(storage_module.boto3, "client", fake_boto3_client)
    s3 = S3AdapterStorage(region="us-east-3", bucket="fs-uuid-123",
                          access_key_id="AK", secret_access_key="SK")

    files = s3.list_files()
    assert [f.key for f in files] == ["a.bin", "b/c.bin", "d.bin"]
    assert sum(f.size_bytes for f in files) == 600
    # Dialed the regional adapter endpoint, not AWS.
    assert captured["endpoint_url"] == "https://files.us-east-3.lambda.ai"

    s3.delete_file("b/c.bin")
    assert fake.deleted == [("fs-uuid-123", "b/c.bin")]

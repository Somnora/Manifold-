"""Sidecar unit tests with mocked pynvml and a temp ephemeral root.

The sidecar lives outside the backend package (it ships to the instance),
so we import it by path.
"""

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

SIDECAR_PATH = (
    Path(__file__).resolve().parent.parent.parent / "sidecar" / "manifold_sidecar.py"
)
spec = importlib.util.spec_from_file_location("manifold_sidecar", SIDECAR_PATH)
sidecar = importlib.util.module_from_spec(spec)
sys.modules["manifold_sidecar"] = sidecar
spec.loader.exec_module(sidecar)


class FakeMemory:
    used = 8 * 1024**3
    total = 24 * 1024**3


class FakeUtil:
    gpu = 87


class FakeNvml:
    """Duck-typed pynvml: one GPU, fixed readings."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self, fail_init=False):
        self.fail_init = fail_init
        self.init_calls = 0

    def nvmlInit(self):
        self.init_calls += 1
        if self.fail_init:
            raise RuntimeError("NVML: driver not loaded")

    def nvmlDeviceGetCount(self):
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):
        return f"handle-{i}"

    def nvmlDeviceGetMemoryInfo(self, handle):
        return FakeMemory()

    def nvmlDeviceGetUtilizationRates(self, handle):
        return FakeUtil()

    def nvmlDeviceGetTemperature(self, handle, kind):
        return 71

    def nvmlDeviceGetName(self, handle):
        return b"NVIDIA A10"


def make_client(tmp_path, nvml=None):
    app = sidecar.create_app(
        nvml=nvml or FakeNvml(),
        ephemeral_root=tmp_path / "ephemeral",
        persistent_root=tmp_path / "nfs",
    )
    return TestClient(app)


def test_metrics_reads_gpu_via_pynvml(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/metrics").json()
    assert body["available"] is True
    gpu = body["gpus"][0]
    assert gpu == {
        "index": 0,
        "name": "NVIDIA A10",
        "vram_used_mib": 8192,
        "vram_total_mib": 24576,
        "utilization_pct": 87,
        "temperature_c": 71,
    }


def test_metrics_degrades_without_driver(tmp_path):
    client = make_client(tmp_path, nvml=FakeNvml(fail_init=True))
    body = client.get("/metrics").json()
    assert body["available"] is False
    assert body["gpus"] == []


def test_nvml_initialized_once(tmp_path):
    nvml = FakeNvml()
    client = make_client(tmp_path, nvml=nvml)
    client.get("/metrics")
    client.get("/metrics")
    assert nvml.init_calls == 1


def test_metrics_stream_websocket(tmp_path):
    client = make_client(tmp_path)
    with client.websocket_connect("/metrics/stream") as ws:
        payload = ws.receive_json()
    assert payload["available"] is True
    assert payload["gpus"][0]["utilization_pct"] == 87


def test_unpersisted_finds_valuable_files(tmp_path):
    root = tmp_path / "ephemeral"
    (root / "checkpoints").mkdir(parents=True)
    big = root / "checkpoints" / "model.safetensors"
    big.write_bytes(b"x" * 2048)
    (root / "notes.txt").write_text("not valuable")          # not matched
    (root / "sample.png").write_bytes(b"y" * 100)

    client = make_client(tmp_path)
    body = client.get("/storage/unpersisted").json()
    paths = [f["path"] for f in body["files"]]
    assert paths == ["checkpoints/model.safetensors", "sample.png"]  # size desc
    assert body["files"][0]["size_bytes"] == 2048


def test_unpersisted_empty_when_no_root(tmp_path):
    client = make_client(tmp_path)          # ephemeral dir never created
    body = client.get("/storage/unpersisted").json()
    assert body["files"] == []


def test_fs_list_one_level_dirs_first(tmp_path):
    nfs = tmp_path / "nfs" / "manifold-data"
    (nfs / "datasets" / "raw").mkdir(parents=True)
    (nfs / "outputs").mkdir()
    (nfs / "zebra.txt").write_bytes(b"z" * 42)
    (nfs / "datasets" / "raw" / "day1.wav").write_bytes(b"w" * 100)

    client = make_client(tmp_path)
    body = client.get("/fs/list", params={
        "root_name": "persistent", "path": "manifold-data",
    }).json()
    names = [(e["name"], e["is_dir"]) for e in body["entries"]]
    # Directories first, alphabetical; files after.
    assert names == [("datasets", True), ("outputs", True), ("zebra.txt", False)]
    zebra = body["entries"][2]
    assert zebra["size_bytes"] == 42
    # Descend one level.
    body = client.get("/fs/list", params={
        "root_name": "persistent", "path": "manifold-data/datasets",
    }).json()
    assert [e["name"] for e in body["entries"]] == ["raw"]


def test_fs_list_errors(tmp_path):
    (tmp_path / "nfs").mkdir()
    client = make_client(tmp_path)
    assert client.get("/fs/list", params={
        "root_name": "persistent", "path": "nope",
    }).status_code == 404
    assert client.get("/fs/list", params={
        "root_name": "bogus", "path": "",
    }).status_code == 400
    # Traversal out of the root is refused.
    assert client.get("/fs/list", params={
        "root_name": "persistent", "path": "../../etc",
    }).status_code == 400


def test_fs_usage_recursive_sizes_heaviest_first(tmp_path):
    nfs = tmp_path / "nfs" / "fs"
    (nfs / "research" / "scrapes").mkdir(parents=True)
    (nfs / "small").mkdir()
    (nfs / "research" / "scrapes" / "dump1.jsonl").write_bytes(b"x" * 5000)
    (nfs / "research" / "notes.txt").write_bytes(b"n" * 100)
    (nfs / "small" / "tiny.txt").write_bytes(b"t" * 10)
    (nfs / "loose.bin").write_bytes(b"l" * 300)

    client = make_client(tmp_path)
    body = client.get("/fs/usage", params={
        "root_name": "persistent", "path": "fs",
    }).json()
    by_name = {c["name"]: c for c in body["children"]}
    assert by_name["research"]["total_bytes"] == 5100    # recursive
    assert by_name["research"]["file_count"] == 2
    assert by_name["loose.bin"]["total_bytes"] == 300
    assert by_name["small"]["total_bytes"] == 10
    # Heaviest first — the cleanup view.
    assert [c["name"] for c in body["children"]] == [
        "research", "loose.bin", "small",
    ]
    assert body["truncated"] is False


def test_fs_delete_file_and_dir(tmp_path):
    nfs = tmp_path / "nfs" / "fs"
    (nfs / "old-scrapes").mkdir(parents=True)
    (nfs / "old-scrapes" / "dump.jsonl").write_bytes(b"x" * 10)
    (nfs / "keep.txt").write_bytes(b"k")

    client = make_client(tmp_path)
    # Directory without recursive: refused with guidance.
    resp = client.post("/fs/delete", json={
        "root_name": "persistent", "path": "fs/old-scrapes",
    })
    assert resp.status_code == 409
    assert "recursive" in resp.json()["detail"]
    # With recursive: gone.
    resp = client.post("/fs/delete", json={
        "root_name": "persistent", "path": "fs/old-scrapes", "recursive": True,
    })
    assert resp.status_code == 200
    assert not (nfs / "old-scrapes").exists()
    assert (nfs / "keep.txt").exists()          # neighbors untouched
    # Plain file delete.
    client.post("/fs/delete", json={
        "root_name": "persistent", "path": "fs/keep.txt",
    })
    assert not (nfs / "keep.txt").exists()


def test_fs_delete_falls_back_to_sudo_when_unlink_is_denied(tmp_path, monkeypatch):
    # Job outputs are root-owned; the ubuntu sidecar's plain remove is denied.
    nfs = tmp_path / "nfs" / "outputs"
    (nfs / "job-out").mkdir(parents=True)
    (nfs / "job-out" / "model.bin").write_bytes(b"x")

    def denied(*a, **k):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(sidecar.shutil, "rmtree", denied)

    calls = {}

    def fake_run(argv, **k):
        calls["argv"] = argv
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(sidecar.subprocess, "run", fake_run)

    client = make_client(tmp_path)
    resp = client.post("/fs/delete", json={
        "root_name": "persistent", "path": "outputs/job-out", "recursive": True,
    })
    assert resp.status_code == 200
    # Escalated via a jail-confined, shell-free sudo rm.
    assert calls["argv"][:4] == ["sudo", "-n", "rm", "-rf"]
    assert calls["argv"][-2] == "--"                       # stops option parsing
    assert calls["argv"][-1] == str((nfs / "job-out").resolve())


def test_fs_delete_reports_when_even_sudo_cannot_remove(tmp_path, monkeypatch):
    nfs = tmp_path / "nfs" / "outputs"
    (nfs / "stuck").mkdir(parents=True)
    monkeypatch.setattr(sidecar.shutil, "rmtree",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(sidecar.subprocess, "run",
                        lambda *a, **k: type("R", (), {
                            "returncode": 1, "stderr": "rm: cannot remove"})())
    client = make_client(tmp_path)
    resp = client.post("/fs/delete", json={
        "root_name": "persistent", "path": "outputs/stuck", "recursive": True,
    })
    assert resp.status_code == 500
    assert "elevated privileges" in resp.json()["detail"]


def test_fs_delete_refuses_roots_and_escapes(tmp_path):
    (tmp_path / "nfs").mkdir()
    (tmp_path / "ephemeral").mkdir()
    client = make_client(tmp_path)
    for path in ("", "/", "."):
        resp = client.post("/fs/delete", json={
            "root_name": "persistent", "path": path, "recursive": True,
        })
        assert resp.status_code == 400, f"root delete allowed via {path!r}"
    resp = client.post("/fs/delete", json={
        "root_name": "ephemeral", "path": "../nfs", "recursive": True,
    })
    assert resp.status_code == 400
    assert (tmp_path / "nfs").exists()


def test_recent_walks_both_roots_newest_first(tmp_path):
    import os
    import time as time_module

    eph = tmp_path / "ephemeral" / "run"
    nfs = tmp_path / "nfs" / "manifold-data" / "outputs"
    eph.mkdir(parents=True)
    nfs.mkdir(parents=True)
    old = nfs / "old.bin"
    old.write_bytes(b"x")
    os.utime(old, (time_module.time() - 90000,) * 2)   # >24h old
    (eph / "scratch.pt").write_bytes(b"y" * 10)
    (nfs / "result.srt").write_bytes(b"z" * 20)

    client = make_client(tmp_path)
    body = client.get("/storage/recent").json()
    paths = {(f["root"], f["path"]) for f in body["files"]}
    assert ("ephemeral", "run/scratch.pt") in paths
    assert ("persistent", "manifold-data/outputs/result.srt") in paths
    # The >24h-old file is excluded by the default window.
    assert not any(f["path"].endswith("old.bin") for f in body["files"])
    assert body["truncated"] is False

    # A wider window includes it.
    body = client.get("/storage/recent", params={"hours": 48}).json()
    assert any(f["path"].endswith("old.bin") for f in body["files"])

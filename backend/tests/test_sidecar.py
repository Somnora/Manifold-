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
        nvml=nvml or FakeNvml(), ephemeral_root=tmp_path / "ephemeral"
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

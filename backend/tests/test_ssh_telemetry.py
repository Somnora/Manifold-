"""Sidecar-free GPU telemetry: instances launched outside Manifold never
got our cloud-init, so no sidecar runs on them - metrics fall back to
nvidia-smi over the managed SSH connection."""

from app.config import TelemetrySettings
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from app.sidecar_client import SidecarError
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn

CSV_ONE_GPU = "NVIDIA A10, 15872, 23028, 87\n"
CSV_TWO_GPUS = "NVIDIA H100, 40100, 81559, 92\nNVIDIA H100, 39000, 81559, 88\n"


class FakeConn:
    """The two things telemetry needs from a ManagedConnection."""

    state = ConnectionState.CONNECTED

    def __init__(self, exit_code=0, stdout=CSV_ONE_GPU):
        self.exit_code = exit_code
        self.stdout = stdout

    async def run(self, command, **kwargs):
        return self.exit_code, self.stdout, ""


class DeadSidecar:
    async def metrics(self):
        raise SidecarError("no sidecar listening on this box")


async def test_gpu_metrics_via_ssh_parses_nvidia_smi(tmp_path, db):
    orch = Orchestrator(make_settings(tmp_path), MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    orch.connections["i-ext"] = FakeConn()

    payload = await orch.gpu_metrics_via_ssh("i-ext")
    assert payload == {
        "available": True,
        "source": "ssh",
        "gpus": [{"name": "NVIDIA A10", "vram_used_mib": 15872,
                  "vram_total_mib": 23028, "utilization_pct": 87}],
    }

    orch.connections["i-multi"] = FakeConn(stdout=CSV_TWO_GPUS)
    multi = await orch.gpu_metrics_via_ssh("i-multi")
    assert len(multi["gpus"]) == 2


async def test_gpu_metrics_via_ssh_none_on_failure(tmp_path, db):
    orch = Orchestrator(make_settings(tmp_path), MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    assert await orch.gpu_metrics_via_ssh("i-unknown") is None

    orch.connections["i-nogpu"] = FakeConn(
        exit_code=127, stdout="nvidia-smi: command not found")
    assert await orch.gpu_metrics_via_ssh("i-nogpu") is None

    orch.connections["i-garbage"] = FakeConn(stdout="mock output of: stuff")
    assert await orch.gpu_metrics_via_ssh("i-garbage") is None


async def test_telemetry_loop_falls_back_when_sidecar_dead(tmp_path, db):
    """The sampling loop records a sample for a box whose sidecar raises -
    exactly what an adopted external instance looks like."""
    settings = make_settings(
        tmp_path, telemetry=TelemetrySettings(sample_seconds=0.01))
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn,
                        sidecar_factory=lambda conn: DeadSidecar())
    orch.connections["i-ext"] = FakeConn()
    d = Dispatcher(settings, orch, SQLiteTaskQueue(db), {}, db,
                   MockLambdaClient())

    await d._sample_telemetry_once()

    summary = db.telemetry_summary("i-ext")
    assert summary["sample_count"] == 1
    assert summary["gpu_name"] == "NVIDIA A10"
    assert summary["peak_vram_used_mib"] == 15872


async def test_telemetry_loop_skips_box_with_neither_source(tmp_path, db):
    """Sidecar dead AND nvidia-smi absent (CPU box): no sample, no crash."""
    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn,
                        sidecar_factory=lambda conn: DeadSidecar())
    orch.connections["i-cpu"] = FakeConn(exit_code=127, stdout="")
    d = Dispatcher(settings, orch, SQLiteTaskQueue(db), {}, db,
                   MockLambdaClient())

    await d._sample_telemetry_once()
    assert db.telemetry_summary("i-cpu")["sample_count"] == 0

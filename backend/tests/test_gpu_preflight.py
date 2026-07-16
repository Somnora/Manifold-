"""First-job GPU preflight: don't dispatch onto a GPU that can't run CUDA yet.

Field case (sprite-to-3d test pass): an A100 SXM4 job dispatched 2.5 minutes
after cloud-init finished died with "No CUDA GPUs are available" and burned
~5 billed minutes - nvidia-fabricmanager was still initializing, while
nvidia-smi looked perfectly healthy to every hand-check. The dispatcher now
probes `nvidia-smi -q` before the FIRST job on each instance and waits for
the fabric state to settle (bounded, fail-open).
"""

import asyncio

import pytest

from app.dispatcher import Dispatcher, GPU_PROBE_COMMAND, gpu_readiness
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


# A trimmed real-world `nvidia-smi -q` fabric section, both phases.
SXM_BOOTING = """\
==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:07:00.0
    Product Name                          : NVIDIA A100-SXM4-40GB
    Persistence Mode                      : Enabled
    Fabric
        State                             : In Progress
        Status                            : N/A
    Performance State                     : P0
"""

SXM_READY = SXM_BOOTING.replace("In Progress", "Completed")

PCIE_BOX = """\
==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:06:00.0
    Product Name                          : NVIDIA A10
    Persistence Mode                      : Enabled
    Performance State                     : P8
"""


# -- pure parsing ----------------------------------------------------------------

def test_probe_failure_means_not_ready():
    ready, reason = gpu_readiness(127, "nvidia-smi: command not found")
    assert not ready
    assert "driver" in reason


def test_fabric_in_progress_means_not_ready():
    ready, reason = gpu_readiness(0, SXM_BOOTING)
    assert not ready
    assert "fabric manager" in reason


def test_fabric_completed_means_ready():
    ready, reason = gpu_readiness(0, SXM_READY)
    assert ready
    assert "completed" in reason


def test_pcie_box_without_fabric_is_ready():
    ready, reason = gpu_readiness(0, PCIE_BOX)
    assert ready


def test_mock_shell_output_is_ready():
    # The mock connection answers every command with this shape; existing
    # dispatch tests must sail through the preflight without waiting.
    ready, _ = gpu_readiness(0, f"mock output of: {GPU_PROBE_COMMAND}")
    assert ready


# -- the dispatcher gate ---------------------------------------------------------

class ScriptedConn:
    """Duck-types the one ManagedConnection method the preflight uses.
    Answers run() from a script of (exit_code, stdout) pairs; the last
    entry repeats forever."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def run(self, command, **kwargs):
        self.calls += 1
        exit_code, stdout = (
            self.script.pop(0) if len(self.script) > 1 else self.script[0])
        return exit_code, stdout, ""


@pytest.fixture
def dispatcher(tmp_path, db):
    from app.config import TaskSettings
    # Instant polling so a wait loop runs in milliseconds.
    settings = make_settings(tmp_path, tasks=TaskSettings(
        poll_seconds=0.02,
        gpu_ready_poll_seconds=0.001,
        gpu_ready_timeout_seconds=0.05,
    ))
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    return d


def task_log(dispatcher, task_id):
    return [row["line"] for row in dispatcher.queue.get_logs(task_id)]


async def test_gate_waits_until_fabric_settles(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_BOOTING), (0, SXM_BOOTING), (0, SXM_READY)])
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 3
    log = task_log(dispatcher, task_id)
    assert any("waiting for the GPU" in ln for ln in log)
    assert any("GPU ready" in ln for ln in log)
    assert "i-sxm" in dispatcher._gpu_ready


async def test_gate_skipped_for_later_jobs(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_READY)])
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 1
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 1                    # cached; no second probe
    # A ready box logs nothing: silence when there is nothing to say.
    assert task_log(dispatcher, task_id) == []


async def test_gate_fails_open_after_timeout(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_BOOTING)])   # never settles
    await dispatcher._ensure_gpu_ready(conn, "i-stuck", task_id)
    log = task_log(dispatcher, task_id)
    assert any("dispatching anyway" in ln for ln in log)
    assert "i-stuck" in dispatcher._gpu_ready  # later jobs not re-gated


async def test_gate_fails_open_on_probe_error(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})

    class BrokenConn:
        async def run(self, command, **kwargs):
            raise ConnectionError("no SSH connection")

    await dispatcher._ensure_gpu_ready(BrokenConn(), "i-down", task_id)
    log = task_log(dispatcher, task_id)
    assert any("preflight skipped" in ln for ln in log)
    assert "i-down" in dispatcher._gpu_ready

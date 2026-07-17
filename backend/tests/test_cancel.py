"""Cancelling jobs in any state, including running servers.

Field gap (distill-loop test): /tasks/{id}/cancel only accepted auto-managed
jobs, so a vllm-serve started from the Jobs page could not be stopped
through Manifold at all - the documented serve-then-train flow required a
manual `docker stop` over SSH.
"""

import pytest

from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.orchestrator import LaunchRejected, Orchestrator
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


class StopConn:
    def __init__(self):
        self.commands: list[str] = []
        self.state = ConnectionState.CONNECTED

    async def run(self, command, **kwargs):
        self.commands.append(command)
        return 0, "", ""


@pytest.fixture
def rig(tmp_path, db):
    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    return d, orch, queue


async def test_cancel_queued_manual_task(rig):
    d, _, queue = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    result = await d.cancel_task(task_id)
    assert result == {"cancelled": task_id}
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert task["error"] == "cancelled by user"


async def test_cancel_running_task_stops_the_container(rig):
    d, orch, queue = rig
    task_id = queue.enqueue(template="vllm-serve", parameters={})
    queue.mark_running(task_id, "i-serve")
    conn = StopConn()
    orch.connections["i-serve"] = conn

    result = await d.cancel_task(task_id)
    assert result == {"cancelled": task_id}
    # The stop command covers a live container (rm -f) AND a job still in
    # image-pull (pkill of the docker client, self-match-proof brackets).
    stop = conn.commands[0]
    assert f"docker rm -f manifold-task-{task_id}" in stop
    assert f"pkill -f '[m]anifold-task-{task_id}'" in stop
    # Task is still 'running' until the container's death settles it through
    # the normal funnel - which now labels it as a user cancel.
    assert queue.get(task_id)["status"] == "running"
    d._finish_task(task_id, exit_code=137, output_paths=[],
                   error="container exited 137")
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert task["error"] == "cancelled by user"
    log = " ".join(r["line"] for r in queue.get_logs(task_id))
    assert "stop requested by user" in log


async def test_cancel_running_without_connection_is_409(rig):
    d, _, queue = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, "i-gone")
    with pytest.raises(LaunchRejected) as exc:
        await d.cancel_task(task_id)
    assert exc.value.status_code == 409


async def test_cancel_finished_task_is_409(rig):
    d, _, queue = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, "i-x")
    queue.mark_finished(task_id, exit_code=0, output_paths=[])
    with pytest.raises(LaunchRejected) as exc:
        await d.cancel_task(task_id)
    assert exc.value.status_code == 409


async def test_cancel_unknown_task_is_404(rig):
    d, _, _ = rig
    with pytest.raises(LaunchRejected) as exc:
        await d.cancel_task("nope")
    assert exc.value.status_code == 404


async def test_uncancelled_failures_keep_their_real_error(rig):
    # The cancel label must ONLY apply to jobs the user actually stopped.
    d, _, queue = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, "i-x")
    d._finish_task(task_id, exit_code=1, output_paths=[],
                   error="container exited 1")
    assert queue.get(task_id)["error"] == "container exited 1"

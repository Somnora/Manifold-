"""Backend restart mid-job: running tasks are re-adopted, not orphaned.

Found live (2026-07-16): a --reload restart killed the SSH stream; the
container kept running but the task sat 'running' forever with frozen logs.
The wrap now persists the exit code to task-logs/<id>.exit and the
dispatcher re-adopts running tasks at startup by polling for it.
"""

import pytest

from app.dispatcher import Dispatcher, wrap_remote_command
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


def test_wrap_persists_the_exit_code():
    wrapped = wrap_remote_command(
        "docker run --rm x", "/lambda/nfs/fs/task-logs/abc.log",
        ensure_dirs=["/workspace/ephemeral"])
    assert "/lambda/nfs/fs/task-logs/abc.exit" in wrapped
    assert "nohup bash -c" in wrapped          # container detached from session
    assert "exit $rc" in wrapped               # code still propagates


class ScriptedConn:
    """Answers run() from a script; last entry repeats."""

    def __init__(self, script):
        self.script = list(script)
        from app.connections import ConnectionState
        self.state = ConnectionState.CONNECTED

    async def run(self, command, **kwargs):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return item


@pytest.fixture
def rig(tmp_path, db):
    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    return d, orch, queue, db


def plant_running_task(queue, db, instance_id="i-test"):
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, instance_id)
    launch_id = db.create_launch(requested_type="gpu_1x_a10",
                                 region="us-east-1",
                                 filesystem="manifold-data",
                                 connection_mode="direct-ssh",
                                 hourly_rate_cents=129)
    db.update_launch(launch_id, status="active",
                     lambda_instance_id=instance_id)
    return task_id


async def test_readopt_finishes_with_persisted_exit_code(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "0\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "succeeded"
    assert task["exit_code"] == 0
    log = " ".join(r["line"] for r in queue.get_logs(task_id))
    assert "backend restarted; reattached" in log


async def test_readopt_gone_container_fails_honestly(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "gone\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert "result unknown" in task["error"]


async def test_readopt_nonzero_exit_is_a_failure(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "137\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert task["exit_code"] == 137


async def test_readopt_ignores_finished_tasks(rig):
    d, orch, queue, db = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, "i-test")
    queue.mark_finished(task_id, exit_code=0, output_paths=[])
    await d._readopt_running_tasks()   # no running tasks: returns instantly
    assert queue.get(task_id)["status"] == "succeeded"

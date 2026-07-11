"""Gate 4 end-to-end: enqueue -> dispatch -> logs -> completion -> idle
timeout -> termination request -> safety hook -> sync -> terminated.

Runs over HTTP against the real app wiring with fast test intervals.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings, WatchSettings
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status


@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=0.4, poll_seconds=0.05),
        watches=WatchSettings(poll_seconds=0.05),
    )
    app = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    return app


def wait_until(predicate, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def test_full_task_and_idle_lifecycle(fast_app, mock_client, mock_sidecar):
    with TestClient(fast_app) as client:
        # 1. Launch an instance and wait for the managed connection.
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        assert resp.status_code == 202
        launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
        instance_id = launch["lambda_instance_id"]
        wait_until(
            lambda: next(
                i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id
            )["connection_state"] == "connected",
            message="SSH connected",
        )

        # 2. Enqueue a whisper-batch task.
        resp = client.post("/tasks", json={
            "template": "whisper-batch",
            "parameters": {"input_dir": "interviews", "model_size": "small"},
        })
        assert resp.status_code == 202
        task_id = resp.json()["task"]["id"]

        # 3. Dispatcher pushes it over SSH; completion is recorded.
        task = wait_until(
            lambda: (t := client.get(f"/tasks/{task_id}").json())["status"]
            not in ("queued", "running") and t,
            message="task completion",
        )
        assert task["status"] == "succeeded"
        assert task["exit_code"] == 0
        assert task["instance_id"] == instance_id
        assert task["output_paths"] == [
            "/lambda/nfs/manifold-data/transcripts",
            "/lambda/nfs/manifold-data/cache/huggingface",
        ]

        # 4. Logs streamed: dispatch banner, rendered docker command, exit.
        lines = [
            l["line"]
            for l in client.get(f"/tasks/{task_id}/logs").json()["lines"]
        ]
        assert any("dispatching to" in l for l in lines)
        docker_line = next(l for l in lines if "$ docker run" in l)
        assert "--gpus all" in docker_line
        assert "-v /lambda/nfs/manifold-data/interviews:/data/input:ro" in docker_line
        assert any("exited 0" in l for l in lines)
        # The command actually traveled over the (mock) SSH connection.
        assert any("mock output of:" in l for l in lines)

        # 5. Idle timeout fires -> standard termination flow -> safety hook
        #    blocks (mock sidecar reports unpersisted files) -> dispatcher
        #    syncs -> terminates. The instance ends up gone.
        wait_until(
            lambda: mock_client.instances[instance_id].status == "terminated",
            timeout=10.0,
            message="idle auto-termination",
        )

        # The trail is in the audit log: idle trigger, sync, and the hook's
        # evidence was acted on rather than bypassed silently.
        # (Direct DB read: the audit endpoint arrives with Phase 6.)
        db = fast_app.state.orchestrator.db
        actions = [r["action"] for r in db._execute(
            "SELECT action FROM audit_log ORDER BY id"
        ).fetchall()]
        assert "task_dispatch" in actions
        assert "idle_termination" in actions
        assert "idle_sync" in actions          # hook fired, sync ran

        # History records the termination.
        row = db.find_launch_by_instance(instance_id)
        assert row["status"] == "terminated"


def test_task_waits_for_an_instance(fast_app):
    """A task enqueued with no instance connected stays queued (never lost,
    never errored) until a launch provides a connection."""
    with TestClient(fast_app) as client:
        resp = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
        })
        task_id = resp.json()["task"]["id"]
        time.sleep(0.2)              # several dispatcher cycles
        assert client.get(f"/tasks/{task_id}").json()["status"] == "queued"

        # Launch; the queued task should then run to completion.
        client.post("/instances", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        task = wait_until(
            lambda: (t := client.get(f"/tasks/{task_id}").json())["status"]
            == "succeeded" and t,
            message="queued task ran after launch",
        )
        assert task["exit_code"] == 0


def test_running_task_prevents_idle_termination(tmp_path, mock_client,
                                                mock_storage, mock_sidecar):
    """While a task runs, the idle loop must not touch the instance."""
    from app.connections import MockSSHConnection
    import asyncio

    # An SSH mock whose commands take longer than the idle timeout.
    class SlowSSH(MockSSHConnection):
        async def run(self, command):
            await asyncio.sleep(1.2)
            return await super().run(command)

    def slow_connect_fn(host):
        async def _dial():
            return SlowSSH()
        return _dial

    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=0.3, poll_seconds=0.05),
    )
    app = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=slow_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as client:
        client.post("/instances", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        resp = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
        })
        task_id = resp.json()["task"]["id"]
        task = wait_until(
            lambda: (t := client.get(f"/tasks/{task_id}").json())["status"]
            == "succeeded" and t,
            message="slow task completion",
        )
        # The task ran for ~1.2s with a 0.3s idle timeout, yet the instance
        # survived to finish it: running tasks pin the instance alive.
        instance_id = task["instance_id"]
        assert mock_client.instances[instance_id].status == "active"

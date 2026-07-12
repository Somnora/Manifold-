"""Image preflight (Phase 25): a template whose image does not exist in its
registry must fail the job from the backend — with ZERO instances launched on
the auto-manage path, and zero docker commands on the manual path. Anything
undetermined fails OPEN (the check must never wall off launches).

Motivated by a live failure: whisper-batch's third-party image had vanished
from ghcr, and the job burned a GPU boot just to die at `docker pull`.
"""

import time

from fastapi.testclient import TestClient

from app.image_checker import MockImageChecker, parse_image_ref
from app.sidecar_client import MockSidecarClient
from tests.test_auto_manage import _app, _fast, _queue_auto, _wait_lifecycle

GPU_SMOKE_IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"
WHISPER_IMAGE = "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime"


# -- ref parsing (the part every registry call depends on) -----------------------


def test_parse_image_ref_covers_the_template_registries():
    # docker.io official image -> library/ prefix + registry-1 API host.
    assert parse_image_ref("python:3.11-slim") == (
        "registry-1.docker.io", "library/python", "3.11-slim")
    # docker.io user repo.
    assert parse_image_ref("vllm/vllm-openai:latest") == (
        "registry-1.docker.io", "vllm/vllm-openai", "latest")
    # Explicit registries pass through (ghcr, nvcr with nested path).
    assert parse_image_ref("ghcr.io/org/app:cuda") == (
        "ghcr.io", "org/app", "cuda")
    assert parse_image_ref("nvcr.io/nvidia/tao/tao-toolkit:5.5.0") == (
        "nvcr.io", "nvidia/tao/tao-toolkit", "5.5.0")
    # No tag -> latest; digest refs split on @.
    assert parse_image_ref("nvidia/cuda")[2] == "latest"
    assert parse_image_ref("python@sha256:abc")[2] == "sha256:abc"


# -- auto-manage path: missing image -> failed, ZERO launches --------------------


def test_auto_managed_job_with_missing_image_never_launches(tmp_path):
    checker = MockImageChecker(missing={WHISPER_IMAGE})
    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]),
                     image_checker=checker)
    with TestClient(app) as client:
        r = _queue_auto(client)   # whisper-batch
        task_id = r.json()["task"]["id"]
        failed = _wait_lifecycle(client, task_id, ("failed",))
        assert "image not found" in (failed["lifecycle_detail"] or "")
        assert failed["status"] == "failed"

        # The whole point: no launch attempt, no instance, no spend.
        assert mock.launch_calls == []
        assert mock.instances == {}
        assert WHISPER_IMAGE in checker.checked


# -- manual dispatch path: missing image -> failed before any docker run ---------


def test_manual_task_with_missing_image_fails_before_dispatch(tmp_path):
    from tests.test_reconcile import launch_connected

    checker = MockImageChecker(missing={GPU_SMOKE_IMAGE})
    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]),
                     image_checker=checker)
    with TestClient(app) as client:
        _, instance_id = launch_connected(client)
        r = client.post("/tasks", json={"template": "gpu-smoke",
                                        "parameters": {}})
        task_id = r.json()["task"]["id"]

        deadline = time.monotonic() + 5
        task = r.json()["task"]
        while time.monotonic() < deadline:
            task = client.get(f"/tasks/{task_id}").json()
            if task["status"] in ("failed", "succeeded"):
                break
            time.sleep(0.02)
        assert task["status"] == "failed"
        assert "image not found" in (task["error"] or "")
        assert task["started_at"] is None      # never marked running

        # No docker command ever reached the instance for this task.
        conn = client.app.state.orchestrator.connections[instance_id]
        ssh = conn.ssh_connection()
        assert not any(task_id in c for c in ssh.commands)

        # And it is audited.
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "task_image_missing" in actions


# -- fail-open: undetermined must not block ---------------------------------------


def test_undetermined_image_fails_open_and_job_completes(tmp_path):
    checker = MockImageChecker(undetermined={WHISPER_IMAGE})
    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]),
                     image_checker=checker)
    with TestClient(app) as client:
        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]
        done = _wait_lifecycle(client, task_id, ("done", "failed"))
        assert done["lifecycle"] == "done", done["lifecycle_detail"]


# -- teardown still happens when dispatch-time failure hits at 'ready' -----------


def test_auto_job_torn_down_after_dispatch_time_failure(tmp_path):
    # Image passes at launch-time, then goes missing before dispatch (checker
    # flips). The task fails at dispatch while the job sits at 'ready' — the
    # lifecycle must STILL sync and terminate the box, not leave it billing
    # (the idle loop deliberately skips auto-owned instances).
    checker = MockImageChecker()
    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]),
                     image_checker=checker)
    with TestClient(app) as client:
        # Make dispatch fail: mark the image missing as soon as it is queued.
        # Launch preflight and dispatch preflight both consult the checker,
        # so flip it after the first (launch-time) check.
        orig = checker.image_exists

        async def flip_after_first(image):
            result = await orig(image)
            checker.missing.add(WHISPER_IMAGE)   # subsequent checks fail
            return result

        checker.image_exists = flip_after_first

        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]
        done = _wait_lifecycle(client, task_id, ("done", "failed"))

        task = client.get(f"/tasks/{task_id}").json()
        assert task["status"] == "failed"
        assert "image not found" in (task["error"] or "")
        # The box was launched (preflight passed then), but it is NOT left
        # running: the lifecycle tore it down after the dispatch failure.
        assert len(mock.launch_calls) == 1
        assert [i for i in mock.instances.values() if i.is_running] == []

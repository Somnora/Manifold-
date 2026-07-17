"""Capacity-queued auto-manage jobs: no capacity parks the job, it never
fails for scarcity, and it launches on its own the moment capacity appears.

The fire-and-forget promise: queue a job against a full region, walk away,
and Manifold launches -> runs -> syncs -> terminates when the GPU frees up.
"""

import time
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import (
    AutoManageSettings,
    IdleSettings,
    TaskSettings,
    WatchSettings,
)
from app.lambda_api import MockLambdaClient, capacity_error
from app.sidecar_client import MockSidecarClient
from tests.conftest import make_settings
from tests.test_auto_manage import _app, _queue_auto, _wait_lifecycle


def _fast(tmp_path, **overrides):
    base = dict(
        tasks=TaskSettings(poll_seconds=0.02),
        auto_manage=AutoManageSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=1800, poll_seconds=0.02),
        # Fast snapshot refresh so tests see capacity flips immediately.
        watches=WatchSettings(poll_seconds=0.01),
    )
    base.update(overrides)
    return make_settings(tmp_path, **base)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_job_parks_on_no_capacity_then_launches_when_it_appears(tmp_path):
    # gpu_1x_gh200 ships with NO capacity anywhere in the mock catalog.
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        r = _queue_auto(client, gpu="gpu_1x_gh200")
        task_id = r.json()["task"]["id"]

        waiting = _wait_lifecycle(client, task_id, ("waiting", "failed"))
        assert waiting["lifecycle"] == "waiting", waiting["lifecycle_detail"]
        assert "capacity" in waiting["lifecycle_detail"]
        # Parked BEFORE any launch attempt: nothing hit the API.
        assert mock.launch_calls == []
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "auto_manage_waiting_capacity" in actions

        # The park was announced once (capacity_available kind).
        notes = client.get("/notifications").json()["notifications"]
        parked = [n for n in notes if "parked" in n["title"].lower()]
        assert len(parked) == 1

        # Capacity appears; the parked job launches and completes on its own.
        info = mock.instance_types["gpu_1x_gh200"]
        mock.instance_types["gpu_1x_gh200"] = replace(
            info, regions_with_capacity=["us-east-1"])
        done = _wait_lifecycle(client, task_id, ("done", "failed"))
        assert done["lifecycle"] == "done", done["lifecycle_detail"]
        assert done["status"] == "succeeded"
        # And the box is gone: full launch -> run -> terminate ride.
        assert [i for i in mock.instances.values() if i.is_running] == []


def test_lost_launch_race_reparks_instead_of_failing(tmp_path):
    """Catalog says capacity, but every launch attempt hits the capacity
    wall (someone grabbed the boxes). The exhausted launch must park the
    job again, not fail it - and it completes once launches succeed."""
    # 5 scripted capacity errors = exactly one exhausted launch round
    # (max_attempts=5 in test settings), then attempts succeed.
    mock = MockLambdaClient(
        scripted_launch_errors=[capacity_error() for _ in range(5)])
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar, mock=mock)
    with TestClient(app) as client:
        r = _queue_auto(client)          # a10: capacity listed in catalog
        task_id = r.json()["task"]["id"]
        done = _wait_lifecycle(client, task_id, ("done", "failed"), timeout=15)
        assert done["lifecycle"] == "done", done["lifecycle_detail"]
        # The exhausted first launch is on record, and the job re-parked
        # (capacity audit row) rather than failing.
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "auto_manage_waiting_capacity" in actions


def test_unknown_capacity_fails_open_to_the_launch_path(tmp_path):
    """An unreachable catalog must never park a job forever: an UNKNOWN
    pre-check verdict proceeds to the real launch path (which succeeds or
    produces its own honest error)."""
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        dispatcher = client.app.state.dispatcher

        async def unknown(gpu_type, region):
            return None
        dispatcher._capacity_status = unknown

        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]
        done = _wait_lifecycle(client, task_id, ("done", "failed"))
        assert done["lifecycle"] == "done", done["lifecycle_detail"]


def test_parked_job_can_be_cancelled(tmp_path):
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        r = _queue_auto(client, gpu="gpu_1x_gh200")   # no capacity: parks
        task_id = r.json()["task"]["id"]
        _wait_lifecycle(client, task_id, ("waiting",))

        assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
        done = _wait_lifecycle(client, task_id, ("cancelled", "failed"))
        assert done["lifecycle"] == "cancelled"
        assert mock.launch_calls == []                # never spent a cent

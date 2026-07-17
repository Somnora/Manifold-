"""Capacity watches: notify when a wanted type/region gains capacity, and
optionally auto-launch through the guarded pipeline."""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings, WatchSettings
from app.lambda_api import MockLambdaClient
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn


def wait_until(predicate, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                   auto_launch_enabled=False):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=60, poll_seconds=10),  # inert here
        watches=WatchSettings(poll_seconds=0.05,
                              auto_launch_enabled=auto_launch_enabled),
    )
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )


def test_watch_flips_to_available_when_capacity_appears(
    tmp_path, mock_client, mock_storage, mock_sidecar
):
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar)
    with TestClient(app) as client:
        # gh200 starts out of capacity everywhere (empty region list).
        resp = client.post("/watches", json={
            "instance_type": "gpu_1x_gh200", "region": "us-east-1",
        })
        assert resp.status_code == 201
        watch_id = resp.json()["watch"]["id"]

        # A few polls: still watching.
        time.sleep(0.2)
        watch = next(w for w in client.get("/watches").json()["watches"]
                     if w["id"] == watch_id)
        assert watch["status"] == "watching"
        assert watch["last_checked"] is not None

        # Capacity appears (Lambda-side event, simulated on the mock).
        mock_client.instance_types["gpu_1x_gh200"].regions_with_capacity = [
            "us-east-1"
        ]
        watch = wait_until(
            lambda: (w := next(
                w for w in client.get("/watches").json()["watches"]
                if w["id"] == watch_id
            ))["status"] == "available" and w,
            message="watch to flip to available",
        )
        assert watch["triggered_at"] is not None

        # A watch WITHOUT auto-launch IS this notification (it was silent
        # before: the hook existed but was never wired to the bell).
        notes = client.get("/notifications").json()["notifications"]
        assert any(n["kind"] == "capacity_available" for n in notes)


def test_watch_wrong_region_does_not_trigger(
    tmp_path, mock_client, mock_storage, mock_sidecar
):
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar)
    with TestClient(app) as client:
        watch_id = client.post("/watches", json={
            "instance_type": "gpu_1x_gh200", "region": "us-west-1",
        }).json()["watch"]["id"]
        # Capacity appears — but only in us-east-1.
        mock_client.instance_types["gpu_1x_gh200"].regions_with_capacity = [
            "us-east-1"
        ]
        time.sleep(0.3)
        watch = next(w for w in client.get("/watches").json()["watches"]
                     if w["id"] == watch_id)
        assert watch["status"] == "watching"     # region must match exactly


def test_auto_launch_goes_through_guards(
    tmp_path, mock_storage, mock_sidecar
):
    """Auto-launch uses request_launch, so the budget guard applies: a watch
    on an over-budget type sees capacity but is refused the launch."""
    mock_client = MockLambdaClient()
    mock_client.instance_types["gpu_8x_h100_sxm5"].regions_with_capacity = []
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                         auto_launch_enabled=True)
    with TestClient(app) as client:
        watch_id = client.post("/watches", json={
            "instance_type": "gpu_8x_h100_sxm5",    # $31.92/hr >> $4 budget
            "region": "us-east-1",
            "filesystem": "manifold-data",
            "auto_launch": True,
        }).json()["watch"]["id"]

        mock_client.instance_types["gpu_8x_h100_sxm5"].regions_with_capacity = [
            "us-east-1"
        ]
        wait_until(
            lambda: next(
                w for w in client.get("/watches").json()["watches"]
                if w["id"] == watch_id
            )["status"] == "available",
            message="watch trigger",
        )
        time.sleep(0.2)  # give any (wrong) launch a chance to happen
        # The guard held: capacity was seen, nothing launched.
        assert mock_client.launch_calls == []
        actions = [r["action"] for r in app.state.orchestrator.db._execute(
            "SELECT action FROM audit_log ORDER BY id"
        ).fetchall()]
        assert "watch_auto_launch_rejected" in actions


def test_auto_launch_launches_within_budget(
    tmp_path, mock_storage, mock_sidecar
):
    mock_client = MockLambdaClient()
    mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = []
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                         auto_launch_enabled=True)
    with TestClient(app) as client:
        watch_id = client.post("/watches", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
            "auto_launch": True,
        }).json()["watch"]["id"]

        mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = [
            "us-east-1"
        ]
        wait_until(
            lambda: next(
                w for w in client.get("/watches").json()["watches"]
                if w["id"] == watch_id
            )["status"] == "launched",
            message="auto-launch",
        )
        assert len(mock_client.launch_calls) == 1
        assert mock_client.launch_calls[0]["instance_type"] == "gpu_1x_a10"


def test_auto_launch_config_kill_switch(
    tmp_path, mock_storage, mock_sidecar
):
    """auto_launch on the watch but disabled in config -> notify only."""
    mock_client = MockLambdaClient()
    mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = []
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                         auto_launch_enabled=False)
    with TestClient(app) as client:
        watch_id = client.post("/watches", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
            "auto_launch": True,
        }).json()["watch"]["id"]
        mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = [
            "us-east-1"
        ]
        wait_until(
            lambda: next(
                w for w in client.get("/watches").json()["watches"]
                if w["id"] == watch_id
            )["status"] == "available",
            message="watch trigger",
        )
        time.sleep(0.15)
        assert mock_client.launch_calls == []


def test_watch_validation(client):
    assert client.post("/watches", json={
        "instance_type": "gpu_1x_nonsense", "region": "us-east-1",
    }).status_code == 400
    # auto_launch needs a filesystem, and its region must match.
    assert client.post("/watches", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "auto_launch": True,
    }).status_code == 400
    assert client.post("/watches", json={
        "instance_type": "gpu_1x_a10", "region": "us-west-1",
        "filesystem": "manifold-data", "auto_launch": True,
    }).status_code == 400


def test_cancel_watch(client):
    watch_id = client.post("/watches", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
    }).json()["watch"]["id"]
    resp = client.delete(f"/watches/{watch_id}")
    assert resp.json()["watch"]["status"] == "cancelled"

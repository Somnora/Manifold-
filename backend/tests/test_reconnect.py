"""Reconnect-on-startup: a backend restart re-adopts live Lambda instances."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.connections import ConnectionState, ManagedConnection
from app.lambda_api import InstanceInfo, MockLambdaClient
from app.main import create_app
from app.orchestrator import Orchestrator
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status


async def wait_state(conn: ManagedConnection, state: ConnectionState, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if conn.state == state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"never reached {state}, stuck at {conn.state}")


def running_instance(instance_id="i-live", name="manifold-live", region="us-east-1"):
    return InstanceInfo(
        id=instance_id, name=name, status="active", ip="203.0.113.50",
        region=region, instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )


async def test_adopt_reconnects_to_running_instance(tmp_path, db):
    """An instance already running on Lambda, unknown to this fresh
    orchestrator, gets a managed connection on adopt."""
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-live"] = running_instance()
    # A launch row records how it was connected (direct-ssh).
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(launch_id, lambda_instance_id="i-live", status="active")

    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    assert "i-live" not in orch.connections

    adopted = await orch.adopt_running_instances()
    assert adopted == 1
    assert "i-live" in orch.connections
    # It actually connects (mock connect_fn succeeds).
    await wait_state(orch.connections["i-live"], ConnectionState.CONNECTED)


async def test_adopt_skips_already_tracked_and_non_active(tmp_path, db):
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-live"] = running_instance()
    mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="booting", status="booting", ip=None,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)

    first = await orch.adopt_running_instances()
    assert first == 1                       # only i-live; i-boot skipped
    second = await orch.adopt_running_instances()
    assert second == 0                      # i-live already tracked

    assert "i-boot" not in orch.connections


async def test_adopt_defaults_mode_for_instances_without_launch_row(tmp_path, db):
    """An instance with no launch history (launched outside Manifold) still
    reconnects, using the default connection mode."""
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-orphan"] = running_instance(instance_id="i-orphan")
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)

    adopted = await orch.adopt_running_instances()
    assert adopted == 1
    assert "i-orphan" in orch.connections


async def test_adopt_is_best_effort_when_lambda_unreachable(tmp_path, db):
    """An unconfigured/unreachable Lambda client must not raise — the backend
    has to start regardless."""
    from app.lambda_api import UnconfiguredLambdaClient
    settings = make_settings(tmp_path, lambda_api_key="")
    orch = Orchestrator(settings, UnconfiguredLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    adopted = await orch.adopt_running_instances()
    assert adopted == 0                     # no crash, just nothing adopted


def test_restart_readopts_over_http(tmp_path, mock_storage, mock_sidecar):
    """End to end: launch on one app instance, then a SECOND app (same DB +
    same live Lambda mock, fresh connections) re-adopts on startup and the
    instance shows connected, terminal-ready — no relaunch."""
    settings = make_settings(tmp_path)
    shared_mock = MockLambdaClient()

    def build_app():
        return create_app(
            settings,
            lambda_client=shared_mock,
            storage_factory=lambda fs: mock_storage,
            connect_fn=mock_connect_fn,
            sidecar_factory=lambda conn: mock_sidecar,
        )

    # First "process": launch and reach active.
    with TestClient(build_app()) as client:
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
        instance_id = launch["lambda_instance_id"]
        assert shared_mock.instances[instance_id].status == "active"

    # Second "process": brand-new app over the SAME db file and SAME live
    # Lambda mock. On startup it must re-adopt the still-running instance.
    with TestClient(build_app()) as client:
        instances = client.get("/instances").json()["instances"]
        inst = next(i for i in instances if i["id"] == instance_id)
        assert inst["connection_state"] in ("connecting", "connected")

        # And the audit log records the reconnect.
        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "reconnect_on_startup" for e in audit)

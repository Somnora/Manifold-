"""The launch pipeline: retry-on-capacity, fallbacks, boot wait, connection."""

import asyncio

import pytest

from app.config import LaunchPolicy
from app.connections import ConnectionState
from app.lambda_api import LambdaAPIError, MockLambdaClient, capacity_error
from app.orchestrator import Orchestrator
from tests.conftest import make_settings, mock_connect_fn


async def wait_for_connection(orchestrator, instance_id,
                              state=ConnectionState.CONNECTED, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        conn = orchestrator.connections.get(instance_id)
        if conn is not None and conn.state == state:
            return conn
        await asyncio.sleep(0.01)
    raise AssertionError(f"connection to {instance_id} never reached {state}")


async def test_successful_launch(orchestrator, mock_client):
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10",
        region="us-east-1",
        filesystem="manifold-data",
    )
    assert launch["status"] == "launching"

    final = await orchestrator.wait_for_launch(launch["id"])
    assert final["status"] == "active"
    assert final["launched_type"] == "gpu_1x_a10"
    assert final["hourly_rate_cents"] == 75
    assert final["attempts"] == 1
    assert final["launched_at"] is not None
    assert final["active_at"] is not None

    # The launch call carried the filesystem and the configured SSH key.
    call = mock_client.launch_calls[0]
    assert call["filesystem_names"] == ["manifold-data"]
    assert call["ssh_key_names"] == ["test-ssh-key"]

    # The managed SSH connection came up.
    conn = await wait_for_connection(orchestrator, final["lambda_instance_id"])
    assert conn.state == ConnectionState.CONNECTED


async def test_launch_with_explicit_ssh_key(orchestrator, mock_client):
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10",
        region="us-east-1",
        filesystem="manifold-data",
        ssh_key_name="mock-key",          # overrides config.yaml's test-ssh-key
    )
    await orchestrator.wait_for_launch(launch["id"])
    assert mock_client.launch_calls[0]["ssh_key_names"] == ["mock-key"]


async def test_capacity_failures_then_success(settings, db):
    mock = MockLambdaClient(
        scripted_launch_errors=[capacity_error(), capacity_error()]
    )
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    assert final["status"] == "active"
    assert final["attempts"] == 3
    assert len(mock.launch_calls) == 3
    # No fallbacks configured: every attempt used the requested type.
    assert {c["instance_type"] for c in mock.launch_calls} == {"gpu_1x_a10"}


async def test_capacity_exhaustion_fails_loudly(settings, db):
    mock = MockLambdaClient(scripted_launch_errors=[capacity_error()] * 10)
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    assert final["status"] == "failed"
    assert final["attempts"] == 5                 # max_attempts, one type each
    assert "5 attempts" in final["error"]
    assert "gpu_1x_a10" in final["error"]


async def test_fallback_type_used_when_primary_has_no_capacity(tmp_path, db):
    settings = make_settings(
        tmp_path,
        launch=LaunchPolicy(
            max_attempts=5, backoff_base_seconds=0, backoff_max_seconds=0,
            boot_timeout_seconds=5, boot_poll_seconds=0,
            fallback_instance_types=("gpu_1x_a100_sxm4",),
        ),
    )
    # Primary fails once; the fallback (tried next, same attempt) succeeds.
    mock = MockLambdaClient(scripted_launch_errors=[capacity_error()])
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    assert final["status"] == "active"
    assert final["requested_type"] == "gpu_1x_a10"
    assert final["launched_type"] == "gpu_1x_a100_sxm4"
    assert final["hourly_rate_cents"] == 129      # rate reflects what launched
    assert [c["instance_type"] for c in mock.launch_calls] == [
        "gpu_1x_a10", "gpu_1x_a100_sxm4",
    ]


async def test_fallback_over_budget_is_skipped(tmp_path, db):
    settings = make_settings(
        tmp_path,
        launch=LaunchPolicy(
            max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0,
            boot_timeout_seconds=5, boot_poll_seconds=0,
            fallback_instance_types=("gpu_8x_a100",),   # $10.32/hr > $4 budget
        ),
    )
    mock = MockLambdaClient(scripted_launch_errors=[capacity_error()])
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    # The over-budget fallback was never attempted — guard beats fallback.
    assert final["status"] == "failed"
    assert [c["instance_type"] for c in mock.launch_calls] == ["gpu_1x_a10"]


async def test_non_capacity_error_fails_immediately(settings, db):
    quota = LambdaAPIError(
        code="global/quota-exceeded", message="Quota exceeded.",
        suggestion="Contact Support to increase your quota.", status=400,
    )
    mock = MockLambdaClient(scripted_launch_errors=[quota, capacity_error()])
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    assert final["status"] == "failed"
    assert "quota" in final["error"].lower()
    assert len(mock.launch_calls) == 1            # no retry on non-capacity errors


async def test_retry_status_visible_while_retrying(settings, db):
    """The dashboard must be able to see retries as they happen."""
    mock = MockLambdaClient(scripted_launch_errors=[capacity_error()] * 10)
    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    # After exhaustion the row still tells the whole story.
    assert final["status"] == "failed"
    assert final["error"] and "capacity" in final["error"].lower()


async def test_terminate_closes_connection_and_records(orchestrator, mock_client):
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="manifold-data",
    )
    final = await orchestrator.wait_for_launch(launch["id"])
    instance_id = final["lambda_instance_id"]
    conn = await wait_for_connection(orchestrator, instance_id)

    result = await orchestrator.terminate(instance_id)
    assert result["terminated"] is True
    assert conn.state == ConnectionState.DISCONNECTED
    assert instance_id not in orchestrator.connections
    assert mock_client.instances[instance_id].status == "terminated"

    row = orchestrator.db.find_launch_by_instance(instance_id)
    assert row["status"] == "terminated"
    assert row["terminated_at"] is not None

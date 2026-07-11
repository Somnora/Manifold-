"""Out-of-band termination: Lambda is the source of truth, Manifold follows.

James terminated an instance from outside Manifold and the dashboard kept
showing it. These tests pin the reconcile behavior that fixes that.
"""

import time

from app.connections import ConnectionState
from tests.conftest import wait_for_launch_status


def launch_connected(client, timeout=5.0):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        if inst["connection_state"] == "connected":
            return launch["id"], instance_id
        time.sleep(0.02)
    raise AssertionError("never connected")


def test_externally_terminated_instance_disappears(client, mock_client):
    launch_id, instance_id = launch_connected(client)

    # Terminate BEHIND Manifold's back (console/API), as James did. Lambda
    # keeps reporting the instance as 'terminated' for a while.
    mock_client.instances[instance_id].status = "terminated"

    # The very next poll drops the card...
    assert client.get("/instances").json()["instances"] == []

    # ...reaps the SSH supervisor (no reconnect-looping at a dead host)...
    orch = client.app.state.orchestrator
    assert instance_id not in orch.connections

    # ...and closes the history row so cost stops accruing.
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"
    assert row["terminated_at"] is not None

    # The reconciliation is audited.
    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "external_termination_detected" for e in audit)


def test_instance_deleted_entirely_from_lambda(client, mock_client):
    """Same, but the instance vanished from the list altogether."""
    launch_id, instance_id = launch_connected(client)
    del mock_client.instances[instance_id]

    assert client.get("/instances").json()["instances"] == []
    assert instance_id not in client.app.state.orchestrator.connections
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"


def test_orphaned_active_row_closed_without_connection(client, mock_client, db):
    """A launch row left 'active' from a session where the instance was
    terminated while the backend was down still gets closed."""
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(launch_id, status="active",
                     lambda_instance_id="i-long-gone")

    client.get("/instances")   # any poll reconciles
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"


def test_healthy_instances_untouched_by_reconcile(client, mock_client):
    _, instance_id = launch_connected(client)
    # Repeated polls must not disturb a live instance or its connection.
    for _ in range(3):
        instances = client.get("/instances").json()["instances"]
    assert [i["id"] for i in instances] == [instance_id]
    conn = client.app.state.orchestrator.connections[instance_id]
    assert conn.state == ConnectionState.CONNECTED

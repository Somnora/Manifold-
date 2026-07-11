"""End-to-end over HTTP: launch -> visible state -> terminate -> history."""

import time

from tests.conftest import wait_for_launch_status


def test_full_lifecycle_over_http(client):
    # Launch is admitted and returns 202 with a trackable launch id.
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
        "name": "lifecycle-test",
    })
    assert resp.status_code == 202
    launch = resp.json()["launch"]
    assert launch["status"] == "launching"

    # The background pipeline settles into "active".
    final = wait_for_launch_status(client, launch["id"])
    assert final["status"] == "active"
    instance_id = final["lambda_instance_id"]

    # The instance list shows live status, mode, and SSH connection state.
    deadline = time.monotonic() + 5
    inst = None
    while time.monotonic() < deadline:
        instances = client.get("/instances").json()["instances"]
        inst = next(i for i in instances if i["id"] == instance_id)
        if inst["connection_state"] == "connected":
            break
        time.sleep(0.02)
    assert inst["status"] == "active"
    assert inst["connection_mode"] == "direct-ssh"
    assert inst["connection_state"] == "connected"
    assert inst["hourly_rate_usd"] == 0.75

    # Terminate and confirm history records it with timestamps for costing.
    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 200

    history = client.get("/launches").json()["launches"]
    row = next(l for l in history if l["id"] == launch["id"])
    assert row["status"] == "terminated"
    assert row["launched_at"] is not None
    assert row["terminated_at"] is not None
    assert row["hourly_rate_cents"] == 75

    assert client.get("/instances").json()["instances"] == []


def test_health_and_instance_types(client):
    assert client.get("/health").json()["status"] == "ok"
    types = client.get("/instance-types").json()
    assert types["gpu_1x_a10"]["price_usd_per_hour"] == 0.75
    assert "us-east-1" in types["gpu_1x_a10"]["regions_with_capacity"]


def test_launch_status_404(client):
    assert client.get("/launches/nonexistent").status_code == 404

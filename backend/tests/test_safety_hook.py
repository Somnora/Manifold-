"""The pre-termination safety hook and sync flow, end to end over HTTP."""

from tests.conftest import wait_for_launch_status


def launch_and_wait(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 202
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    return launch["lambda_instance_id"]


def wait_connected(client, instance_id, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(
            i for i in client.get("/instances").json()["instances"]
            if i["id"] == instance_id
        )
        if inst["connection_state"] == "connected":
            return
        time.sleep(0.02)
    raise AssertionError("never connected")


def test_terminate_blocked_by_unpersisted_files(client, mock_sidecar, mock_client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["blocked"] is True
    paths = [f["path"] for f in body["unpersisted_files"]]
    assert "checkpoints/step-2000.safetensors" in paths
    # And the instance is genuinely still running.
    assert mock_client.instances[instance_id].status == "active"


def test_force_terminate_bypasses_hook(client, mock_sidecar, mock_client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    resp = client.delete(f"/instances/{instance_id}", params={"force": "true"})
    assert resp.status_code == 200
    assert mock_client.instances[instance_id].status == "terminated"


def test_sync_then_terminate(client, mock_sidecar, mock_client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    # Sync runs rsync over the managed connection...
    resp = client.post(f"/instances/{instance_id}/sync")
    assert resp.status_code == 200
    assert resp.json()["synced_to"] == "/lambda/nfs/manifold-data/ephemeral-backup/"

    # ...after which the sidecar reports nothing unpersisted (the mock
    # simulates the ephemeral dir now being backed up), so terminate passes.
    mock_sidecar.clear_unpersisted()
    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 200
    assert mock_client.instances[instance_id].status == "terminated"


def test_terminate_proceeds_when_no_connection(client, mock_client, mock_sidecar):
    """An instance with no managed connection (e.g. found already running at
    startup) must still be terminable — the hook is evidence, not a wedge."""
    from app.lambda_api import InstanceInfo
    mock_client.instances["orphan"] = InstanceInfo(
        id="orphan", name="orphan", status="active", ip="192.0.2.99",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    resp = client.delete("/instances/orphan")
    assert resp.status_code == 200
    assert mock_client.instances["orphan"].status == "terminated"


def test_metrics_endpoint_relays_sidecar(client, mock_sidecar):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    body = client.get(f"/instances/{instance_id}/metrics").json()
    assert body["available"] is True
    assert body["gpus"][0]["name"] == "Mock A10"


def test_metrics_stream_websocket_relays(client, mock_sidecar):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    with client.websocket_connect(f"/instances/{instance_id}/metrics/stream") as ws:
        payload = ws.receive_json()
    assert payload["gpus"][0]["vram_total_mib"] == 24564

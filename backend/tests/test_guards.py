"""Validation and guardrails — every rejection happens BEFORE any launch call."""

from app.lambda_api import InstanceInfo


def test_region_mismatch_rejected(client, mock_client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-west-1",               # filesystem lives in us-east-1
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "us-east-1" in detail and "us-west-1" in detail
    assert "region" in detail.lower()
    assert mock_client.launch_calls == []    # never reached the Lambda API


def test_budget_guard_rejects_expensive_type(client, mock_client):
    # gpu_8x_a100 is $10.32/hr; the default limit is $4.00/hr.
    resp = client.post("/instances", json={
        "instance_type": "gpu_8x_a100",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "$10.32" in detail and "$4.00" in detail
    assert mock_client.launch_calls == []


def test_budget_guard_counts_running_instances(tmp_path, mock_client, mock_storage):
    """Spend is cumulative: an already-running instance eats the budget."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    from app.config import Guardrails

    settings = make_settings(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=5, max_hourly_spend_usd=1.00),
    )
    mock_client.instances["existing"] = InstanceInfo(
        id="existing", name="already-here", status="active", ip="192.0.2.9",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=75,
    )
    app = create_app(settings, lambda_client=mock_client,
                     storage_factory=lambda fs: mock_storage,
                     connect_fn=mock_connect_fn)
    with TestClient(app) as client:
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10",       # $0.75 + $0.75 > $1.00
            "region": "us-east-1",
            "filesystem": "manifold-data",
        })
    assert resp.status_code == 409
    assert "$1.50" in resp.json()["detail"]
    assert mock_client.launch_calls == []


def test_concurrency_guard(client, mock_client):
    mock_client.instances["existing"] = InstanceInfo(
        id="existing", name="already-here", status="active", ip="192.0.2.9",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=75,
    )
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 409
    assert "limit is 1" in resp.json()["detail"]
    assert mock_client.launch_calls == []


def test_unknown_instance_type_rejected(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_nonsense",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 400
    assert "gpu_1x_nonsense" in resp.json()["detail"]


def test_unknown_filesystem_rejected(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "does-not-exist",
    })
    assert resp.status_code == 400
    assert "does-not-exist" in resp.json()["detail"]


def test_tailscale_mode_unavailable_without_authkey(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
        "connection_mode": "tailscale",
    })
    assert resp.status_code == 400
    assert "TAILSCALE_AUTHKEY" in resp.json()["detail"]


def test_unknown_connection_mode_rejected(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
        "connection_mode": "carrier-pigeon",
    })
    assert resp.status_code == 400
    assert "direct-ssh" in resp.json()["detail"]

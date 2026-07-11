"""First-run setup: graceful unconfigured mode + Settings endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.lambda_api import (
    LambdaAPIError,
    MockLambdaClient,
    SwappableLambdaClient,
    UnconfiguredLambdaClient,
)
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn


def make_unconfigured_app(tmp_path, *, lambda_client_factory=None):
    """Real-mode app with NO API key — must start and stay explanatory."""
    settings = make_settings(tmp_path, lambda_api_key="")
    return create_app(
        settings,
        lambda_client=SwappableLambdaClient(UnconfiguredLambdaClient()),
        storage_factory=lambda fs: None,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: None,
        lambda_client_factory=lambda_client_factory,
        env_path=tmp_path / ".env",
    )


def test_unconfigured_backend_starts_and_explains(tmp_path):
    app = make_unconfigured_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"

        status = client.get("/settings/status").json()
        assert status["lambda_configured"] is False
        assert status["mock"] is False

        # Every Lambda-backed endpoint returns the actionable message,
        # not a crash or a blank.
        resp = client.get("/instance-types")
        assert resp.status_code == 503
        assert "Settings" in resp.json()["detail"]
        resp = client.get("/ssh-keys")
        assert resp.status_code == 503

        # Launching is refused with the same guidance.
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        assert resp.status_code == 503


def test_invalid_key_rejected_and_not_saved(tmp_path):
    def rejecting_factory(api_key):
        client = MockLambdaClient()

        async def fail():
            raise LambdaAPIError(
                code="global/unauthorized", message="Invalid API key.",
                status=401,
            )
        client.list_instance_types = fail
        return client

    app = make_unconfigured_app(tmp_path, lambda_client_factory=rejecting_factory)
    with TestClient(app) as client:
        resp = client.post("/settings/lambda-key", json={"api_key": "bad-key-123"})
        assert resp.status_code == 400
        assert "Invalid API key" in resp.json()["detail"]
        # Nothing persisted.
        env = tmp_path / ".env"
        assert not env.exists() or "bad-key-123" not in env.read_text()
        assert client.get("/settings/status").json()["lambda_configured"] is False


def test_valid_key_saved_and_hot_swapped(tmp_path):
    app = make_unconfigured_app(
        tmp_path, lambda_client_factory=lambda key: MockLambdaClient()
    )
    with TestClient(app) as client:
        resp = client.post(
            "/settings/lambda-key",
            json={"api_key": "secret_valid_key_abc123"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["applied_live"] is True

        # Persisted to .env...
        env_text = (tmp_path / ".env").read_text()
        assert "LAMBDA_API_KEY=secret_valid_key_abc123" in env_text

        # ...status flips...
        assert client.get("/settings/status").json()["lambda_configured"] is True

        # ...and the running client now serves the catalog with NO restart.
        resp = client.get("/instance-types")
        assert resp.status_code == 200
        assert "gpu_1x_a10" in resp.json()

        # The secret never appears in the audit log.
        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "settings_lambda_key" for e in audit)
        assert "secret_valid_key_abc123" not in str(audit)


def test_update_env_file_preserves_comments(tmp_path):
    from app.config import update_env_file
    env = tmp_path / ".env"
    env.write_text(
        "# Lambda Cloud API key\nLAMBDA_API_KEY=\n\n# S3 creds\nS3_ACCESS_KEY_ID=old\n"
    )
    update_env_file(env, {"LAMBDA_API_KEY": "newkey", "TAILSCALE_AUTHKEY": "ts1"})
    text = env.read_text()
    assert "# Lambda Cloud API key" in text          # comments preserved
    assert "LAMBDA_API_KEY=newkey" in text            # updated in place
    assert "S3_ACCESS_KEY_ID=old" in text              # untouched
    assert text.rstrip().endswith("TAILSCALE_AUTHKEY=ts1")   # appended


def test_s3_keys_saved_without_validation_when_unconfigured(tmp_path):
    app = make_unconfigured_app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/settings/s3-keys", json={
            "access_key_id": "AKIA_TEST", "secret_access_key": "s3secret99",
        })
        assert resp.status_code == 200
        assert resp.json() == {"saved": True, "validated": False}
        env_text = (tmp_path / ".env").read_text()
        assert "S3_ACCESS_KEY_ID=AKIA_TEST" in env_text
        assert client.get("/settings/status").json()["s3_configured"] is True


def test_mock_mode_saves_key_but_keeps_demo_catalog(tmp_path, mock_storage,
                                                    mock_sidecar):
    settings = make_settings(tmp_path, lambda_api_key="")
    app = create_app(
        settings,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        lambda_client_factory=lambda key: MockLambdaClient(),
        env_path=tmp_path / ".env",
        mock=True,
    )
    with TestClient(app) as client:
        resp = client.post("/settings/lambda-key",
                           json={"api_key": "real_key_for_later"})
        assert resp.json()["applied_live"] is False   # mock stays mock
        assert "real_key_for_later" in (tmp_path / ".env").read_text()
        assert client.get("/settings/status").json()["mock"] is True

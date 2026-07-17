"""Adoption sweep: an instance launched outside Manifold mid-session gets
a managed connection without a backend restart (Files/chat/jobs need one)."""

import time

from fastapi.testclient import TestClient

from app.config import LaunchPolicy
from app.lambda_api import InstanceInfo, MockLambdaClient
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn


def external_instance(instance_id="i-external"):
    return InstanceInfo(
        id=instance_id, name="launched-elsewhere", status="active",
        ip="203.0.113.77", region="us-east-1", instance_type="gpu_1x_a10",
        hourly_rate_cents=129,
    )


def sweep_settings(tmp_path, adopt_poll_seconds):
    return make_settings(tmp_path, launch=LaunchPolicy(
        max_attempts=5, backoff_base_seconds=0, backoff_max_seconds=0,
        boot_timeout_seconds=5, boot_poll_seconds=0,
        adopt_poll_seconds=adopt_poll_seconds,
    ))


def test_sweep_adopts_instance_launched_mid_session(
        tmp_path, mock_storage, mock_sidecar):
    """The instance appears on Lambda AFTER the app started - startup
    adoption never saw it - and the sweep connects to it anyway."""
    settings = sweep_settings(tmp_path, adopt_poll_seconds=0.02)
    shared_mock = MockLambdaClient()

    app = create_app(
        settings,
        lambda_client=shared_mock,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as client:
        # Simulate a launch from the Lambda console / a raw API script.
        shared_mock.instances["i-external"] = external_instance()

        deadline = time.monotonic() + 2.0
        state = ""
        while time.monotonic() < deadline:
            instances = client.get("/instances").json()["instances"]
            inst = next(
                (i for i in instances if i["id"] == "i-external"), None)
            state = (inst or {}).get("connection_state", "")
            if state in ("connecting", "connected"):
                break
            time.sleep(0.02)
        assert state in ("connecting", "connected"), (
            f"sweep never adopted the external instance (state={state!r})")

        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "instance_adopted" for e in audit)


def test_sweep_disabled_when_poll_is_zero(tmp_path, mock_storage, mock_sidecar):
    settings = sweep_settings(tmp_path, adopt_poll_seconds=0)
    app = create_app(
        settings,
        lambda_client=MockLambdaClient(),
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as client:
        client.get("/health")
        loops = app.state.dispatcher._loops
        names = {t.get_coro().__qualname__ for t in loops}
        assert not any("_adopt_loop" in n for n in names)

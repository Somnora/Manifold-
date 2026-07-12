"""Shared fixtures. Everything runs against mocks — zero live spend."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Guardrails, LaunchPolicy, Settings, SSHSettings
from app.connections import MockSSHConnection
from app.db import Database
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.orchestrator import Orchestrator
from app.storage import MockStorage


def make_settings(tmp_path, **overrides) -> Settings:
    """Test settings: instant backoff and polling so retries run in ms."""
    defaults = dict(
        lambda_api_key="test-key-not-real",
        guardrails=Guardrails(max_concurrent_instances=1, max_hourly_spend_usd=4.00),
        launch=LaunchPolicy(
            max_attempts=5,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
            boot_timeout_seconds=5,
            boot_poll_seconds=0,
        ),
        ssh=SSHSettings(
            key_name="test-ssh-key",
            reconnect_base_seconds=0.01,
            reconnect_max_seconds=0.05,
        ),
        db_path=str(tmp_path / "test.db"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def mock_connect_fn(host: str):
    async def _dial():
        return MockSSHConnection()
    return _dial


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def mock_client() -> MockLambdaClient:
    return MockLambdaClient()


@pytest.fixture
def db(settings):
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def orchestrator(settings, mock_client, db) -> Orchestrator:
    return Orchestrator(settings, mock_client, db, connect_fn=mock_connect_fn)


@pytest.fixture
def mock_storage() -> MockStorage:
    return MockStorage()


@pytest.fixture
def mock_sidecar():
    from app.sidecar_client import MockSidecarClient
    return MockSidecarClient()


@pytest.fixture
def mock_model():
    from app.model_client import MockModelClient
    return MockModelClient()


@pytest.fixture
def client(settings, mock_client, mock_storage, mock_sidecar, mock_model):
    """TestClient over the real app wiring, with mocks injected."""
    from app.image_checker import MockImageChecker
    app = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: mock_model,
        image_checker=MockImageChecker(),   # offline: no registry calls in tests
    )
    with TestClient(app) as test_client:
        yield test_client


def wait_for_launch_status(client: TestClient, launch_id: str,
                           statuses=("active", "failed"), timeout=5.0) -> dict:
    """Poll the launch endpoint until the background pipeline settles."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        launch = client.get(f"/launches/{launch_id}").json()
        if launch["status"] in statuses:
            return launch
        time.sleep(0.02)
    raise AssertionError(
        f"launch {launch_id} did not reach {statuses} within {timeout}s; "
        f"last state: {launch}"
    )

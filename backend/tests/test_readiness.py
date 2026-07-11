"""Model readiness: 'serving' (container running) vs 'ready' (API answers).

A vllm-serve task goes 'running' the instant its container launches, but
vLLM needs minutes to load before it answers. These tests pin the gate
that stops chat/proxy/autopilot from hitting a not-yet-ready model.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.model_client import MockModelClient, ModelClientError
from tests.conftest import make_settings, mock_connect_fn
from tests.test_safety_hook import launch_and_wait, wait_connected


class LoadingThenReadyModel(MockModelClient):
    """model_info raises until `ready` is flipped — simulates vLLM starting
    up (connection refused on the loopback API) and then finishing."""

    def __init__(self):
        super().__init__()
        self.ready = False
        self.probes = 0

    async def model_info(self, port):
        self.probes += 1
        if not self.ready:
            raise ModelClientError("connection refused (model still loading)")
        return await super().model_info(port)


@pytest.fixture
def loading_model():
    return LoadingThenReadyModel()


@pytest.fixture
def loading_app(tmp_path, mock_client, mock_storage, mock_sidecar, loading_model):
    # Tiny readiness TTL so the test can flip ready without waiting.
    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: loading_model,
    )
    app.state.dispatcher.LOADING_TTL = 0.0   # always re-probe while loading
    app.state.dispatcher.READY_TTL = 0.0
    return app


def serve(client, model_id="Qwen/Qwen2.5-7B-Instruct"):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    resp = client.post("/tasks", json={
        "template": "vllm-serve", "parameters": {"model_id": model_id},
    })
    client.app.state.queue.mark_running(resp.json()["task"]["id"], instance_id)
    return instance_id


def test_model_endpoint_reports_loading_then_ready(loading_app, loading_model):
    with TestClient(loading_app) as client:
        instance_id = serve(client)

        body = client.get(f"/instances/{instance_id}/model").json()
        assert body["serving"] is True
        assert body["ready"] is False
        assert "loading" in body["status_detail"]

        loading_model.ready = True     # vLLM finished loading
        body = client.get(f"/instances/{instance_id}/model").json()
        assert body["ready"] is True
        assert body["status_detail"] == ""


def test_chat_blocked_while_loading(loading_app, loading_model):
    with TestClient(loading_app) as client:
        instance_id = serve(client)
        resp = client.post(f"/instances/{instance_id}/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 503
        assert "still loading" in resp.json()["detail"]

        loading_model.ready = True
        resp = client.post(f"/instances/{instance_id}/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 200


def test_proxy_hides_loading_model_and_503s(loading_app, loading_model):
    with TestClient(loading_app) as client:
        serve(client)
        # Not listed while loading...
        assert client.get("/v1/models").json()["data"] == []
        # ...and chat completions is a clean 503, not a connection error.
        resp = client.post("/v1/chat/completions", json={
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [{"role": "user", "content": "x"}],
        })
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "model_loading"

        loading_model.ready = True
        assert len(client.get("/v1/models").json()["data"]) == 1
        resp = client.post("/v1/chat/completions", json={
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [{"role": "user", "content": "x"}],
        })
        assert resp.status_code == 200


def test_autopilot_refuses_loading_brain(loading_app, loading_model):
    with TestClient(loading_app) as client:
        instance_id = serve(client)
        resp = client.post("/autopilot/runs", json={
            "goal": "do something useful", "brain_instance_id": instance_id,
        })
        assert resp.status_code == 409
        assert "still loading" in resp.json()["detail"]


def test_readiness_is_cached(loading_app, loading_model):
    """With a non-zero TTL, a ready verdict is not re-probed every call."""
    with TestClient(loading_app) as client:
        instance_id = serve(client)
        loading_model.ready = True
        loading_app.state.dispatcher.READY_TTL = 60.0
        client.get(f"/instances/{instance_id}/model")
        probes_after_first = loading_model.probes
        client.get(f"/instances/{instance_id}/model")
        client.get(f"/instances/{instance_id}/model")
        # No new probes within the TTL window.
        assert loading_model.probes == probes_after_first

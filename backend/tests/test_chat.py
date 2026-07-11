"""Chat with a served model: discovery, SSE relay, and guardrails."""

import json

from app.model_client import MockModelClient


def make_serving_task(client, instance_id):
    """Enqueue a vllm-serve task and mark it running on the instance —
    what the dispatcher does when the container is up and serving."""
    resp = client.post("/tasks", json={
        "template": "vllm-serve",
        "parameters": {"model_id": "meta-llama/Llama-3.1-8B-Instruct"},
    })
    assert resp.status_code == 202
    task_id = resp.json()["task"]["id"]
    queue = client.app.state.queue
    queue.mark_running(task_id, instance_id)
    return task_id


def test_model_endpoint_reports_not_serving(client):
    assert client.get("/instances/whatever/model").json() == {"serving": False}


def test_model_endpoint_reports_served_model(client):
    make_serving_task(client, "i-serve")
    body = client.get("/instances/i-serve/model").json()
    assert body["serving"] is True
    assert body["model_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert body["port"] == 8080          # host side of the template mapping
    assert body["template"] == "vllm-serve"
    # A different instance is not serving.
    assert client.get("/instances/i-other/model").json() == {"serving": False}


def test_non_serving_running_task_is_not_a_model(client):
    """A running whisper-batch (no ports) must not be mistaken for a model."""
    resp = client.post("/tasks", json={"template": "whisper-batch",
                                       "parameters": {}})
    client.app.state.queue.mark_running(resp.json()["task"]["id"], "i-whisper")
    assert client.get("/instances/i-whisper/model").json() == {"serving": False}


def test_chat_409_when_nothing_served(client):
    resp = client.post("/instances/i-none/chat", json={
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 409
    assert "vllm-serve" in resp.json()["detail"]


def test_chat_streams_sse_and_audits(tmp_path, mock_client, mock_storage,
                                     mock_sidecar):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    from tests.test_safety_hook import launch_and_wait, wait_connected

    mock_model = MockModelClient()
    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: mock_model,
    )
    with TestClient(app) as client:
        instance_id = launch_and_wait(client)
        wait_connected(client, instance_id)
        make_serving_task(client, instance_id)

        with client.stream(
            "POST", f"/instances/{instance_id}/chat",
            json={"messages": [{"role": "user", "content": "ping"}],
                  "temperature": 0.2},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())

        # SSE chunks assemble into the mock's reply, terminated by [DONE].
        chunks = [json.loads(l[len("data: "):]) for l in body.splitlines()
                  if l.startswith("data: ") and "[DONE]" not in l]
        text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
        assert text == "Mock reply to: ping"
        assert "data: [DONE]" in body

        # The relay passed model + knobs through to the endpoint.
        sent = mock_model.requests[0]["payload"]
        assert sent["model"] == "meta-llama/Llama-3.1-8B-Instruct"
        assert sent["temperature"] == 0.2
        assert mock_model.requests[0]["port"] == 8080

        # Audited, and counted as activity for idle detection.
        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "chat" for e in audit)
        assert instance_id in client.app.state.dispatcher.last_activity


def test_chat_no_connection_409(client):
    """Serving task exists but no managed connection -> clear 409."""
    make_serving_task(client, "i-gone")
    resp = client.post("/instances/i-gone/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 409
    assert "no managed connection" in resp.json()["detail"]

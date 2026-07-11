"""OpenAI-compatible /v1 proxy: point any OpenAI client at Manifold and it
talks to a model served on one of your instances."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Guardrails
from app.main import create_app
from app.model_client import MockModelClient
from tests.conftest import make_settings, mock_connect_fn
from tests.test_safety_hook import launch_and_wait, wait_connected


def serve_model(client, model_id="Qwen/Qwen2.5-7B-Instruct"):
    """Launch an instance, connect it, and mark a vllm-serve job running —
    the state in which a model is reachable through the proxy."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    resp = client.post("/tasks", json={
        "template": "vllm-serve", "parameters": {"model_id": model_id},
    })
    client.app.state.queue.mark_running(resp.json()["task"]["id"], instance_id)
    return instance_id


def proxy_app(tmp_path, mock_client, mock_storage, mock_sidecar, *,
              mock_model=None, **settings_overrides):
    return create_app(
        make_settings(tmp_path, **settings_overrides),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=(lambda conn: mock_model) if mock_model else None,
    )


# -- model listing -------------------------------------------------------------------


def test_models_empty_when_nothing_served(client):
    body = client.get("/v1/models").json()
    assert body == {"object": "list", "data": []}


def test_models_lists_served(client):
    serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["Qwen/Qwen2.5-7B-Instruct"]
    assert body["data"][0]["owned_by"].startswith("manifold:")


# -- non-streaming completion (the shape OpenAI clients expect) -----------------------


def test_chat_completion_returns_openai_object(client):
    serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
    resp = client.post("/v1/chat/completions", json={
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "Mock reply to: hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "usage" in body
    # The response reports the actually-served model.
    assert body["model"] == "Qwen/Qwen2.5-7B-Instruct"


def test_params_pass_through_and_model_is_forced(tmp_path, mock_client,
                                                 mock_storage, mock_sidecar):
    mock_model = MockModelClient()
    app = proxy_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                    mock_model=mock_model)
    with TestClient(app) as client:
        serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
        client.post("/v1/chat/completions", json={
            "model": "whatever-the-client-hardcoded",   # lenient single route
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3, "max_tokens": 64, "top_p": 0.9,
        })
        sent = mock_model.requests[-1]["payload"]
        assert sent["temperature"] == 0.3       # passed through untouched
        assert sent["max_tokens"] == 64
        assert sent["top_p"] == 0.9
        # Model rewritten to what vLLM actually serves, so it accepts it.
        assert sent["model"] == "Qwen/Qwen2.5-7B-Instruct"
        assert "stream" not in sent


# -- streaming -----------------------------------------------------------------------


def test_chat_completion_streams_sse(client):
    serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "messages": [{"role": "user", "content": "stream please"}],
        "stream": True,
    }) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    chunks = [json.loads(l[6:]) for l in body.splitlines()
              if l.startswith("data: ") and "[DONE]" not in l]
    text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
    assert text == "Mock reply to: stream please"
    assert "data: [DONE]" in body


# -- routing -------------------------------------------------------------------------


def test_route_by_instance_id(client):
    instance_id = serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
    resp = client.post("/v1/chat/completions", json={
        "model": instance_id,                    # pin by instance
        "messages": [{"role": "user", "content": "x"}],
    })
    assert resp.status_code == 200


def test_no_model_served_is_503(client):
    resp = client.post("/v1/chat/completions", json={
        "model": "anything", "messages": [{"role": "user", "content": "x"}],
    })
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_model_served"


def test_unknown_model_among_many_is_404(tmp_path, mock_client, mock_storage,
                                         mock_sidecar):
    app = proxy_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                    guardrails=Guardrails(max_concurrent_instances=2,
                                          max_hourly_spend_usd=4.00))
    with TestClient(app) as client:
        serve_model(client, "Qwen/Qwen2.5-7B-Instruct")
        serve_model(client, "meta-llama/Llama-3.1-8B-Instruct")
        # Two models served: an unknown name can't be resolved leniently.
        resp = client.post("/v1/chat/completions", json={
            "model": "gpt-4o", "messages": [{"role": "user", "content": "x"}],
        })
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"
        # Both real models are named in the error.
        assert "Qwen/Qwen2.5-7B-Instruct" in resp.json()["error"]["message"]


def test_missing_messages_is_400(client):
    serve_model(client)
    resp = client.post("/v1/chat/completions", json={"model": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_messages"


# -- audit + activity ----------------------------------------------------------------


def test_proxy_use_is_audited_and_counts_as_activity(client):
    instance_id = serve_model(client)
    client.post("/v1/chat/completions", json={
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "messages": [{"role": "user", "content": "x"}],
    })
    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "openai_proxy" for e in audit)
    assert instance_id in client.app.state.dispatcher.last_activity


# -- optional auth -------------------------------------------------------------------


def test_proxy_key_gates_when_set(tmp_path, mock_client, mock_storage,
                                  mock_sidecar):
    app = proxy_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                    proxy_api_key="s3cret-token")
    with TestClient(app) as client:
        serve_model(client)
        # No / wrong bearer: rejected on both endpoints.
        assert client.get("/v1/models").status_code == 401
        assert client.get(
            "/v1/models", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        r = client.post("/v1/chat/completions", json={
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [{"role": "user", "content": "x"}],
        })
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "authentication_error"
        # Correct bearer: allowed.
        ok = client.get("/v1/models",
                        headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200


def test_proxy_open_when_no_key(client):
    serve_model(client)
    assert client.get("/v1/models").status_code == 200   # no auth needed

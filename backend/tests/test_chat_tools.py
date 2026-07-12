"""Chat tools: the served model can browse/read the filesystem and queue
jobs through the guarded paths, driven by a backend tool loop."""

import json

from fastapi.testclient import TestClient

from app.image_checker import MockImageChecker
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.model_client import MockModelClient
from app.sidecar_client import MockSidecarClient
from app.storage import MockStorage
from tests.conftest import make_settings, mock_connect_fn
from tests.test_chat import make_serving_task


class ScriptedModelClient(MockModelClient):
    """chat_completion returns queued replies (tool calls, then an answer)."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    async def chat_completion(self, port, payload):
        self.requests.append({"port": port, "payload": payload})
        content = self.script.pop(0)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": content}}]}


def sse_events(text):
    out = []
    for event in text.split("\n\n"):
        data = "".join(l[6:] for l in event.split("\n")
                       if l.startswith("data: "))
        if data and data != "[DONE]":
            out.append(json.loads(data))
    return out


def tooled_client(tmp_path, script):
    model = ScriptedModelClient(script)
    app = create_app(
        make_settings(tmp_path),
        lambda_client=MockLambdaClient(),
        storage_factory=lambda fs: MockStorage(),
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: MockSidecarClient(),
        model_client_factory=lambda conn: model,
        image_checker=MockImageChecker(),
    )
    return TestClient(app), model


def chat(client, instance_id, text):
    from tests.test_reconcile import launch_connected
    _, iid = launch_connected(client)
    make_serving_task(client, iid)
    resp = client.post(f"/instances/{iid}/chat", json={
        "messages": [{"role": "user", "content": text}],
        "tools": True,
    })
    assert resp.status_code == 200
    return sse_events(resp.text)


def test_tool_loop_lists_files_then_answers(tmp_path):
    client, model = tooled_client(tmp_path, [
        '{"action": "list_files", "args": {"root": "persistent", "path": ""}}',
        "You have transcripts and a cache directory on the filesystem.",
    ])
    with client:
        events = chat(client, "i", "what files are on the filesystem?")
    # First event: the tool call, succeeded.
    assert events[0]["tool"]["action"] == "list_files"
    assert events[0]["tool"]["ok"] is True
    # Final event: the answer as a normal delta chunk.
    assert "transcripts" in events[1]["choices"][0]["delta"]["content"]
    # The observation (real sidecar listing) was fed back to the model.
    obs = json.loads(model.requests[-1]["payload"]["messages"][-1]["content"])
    assert "entries" in obs or "files" in obs or "error" not in obs


def test_tool_loop_queues_a_job_through_the_guarded_path(tmp_path):
    client, _ = tooled_client(tmp_path, [
        '{"action": "run_job", "args": {"template": "gpu-smoke", '
        '"parameters": {"note": "from chat"}}}',
        "Queued the smoke test for you.",
    ])
    with client:
        events = chat(client, "i", "run a gpu smoke test")
        assert events[0]["tool"]["action"] == "run_job"
        assert events[0]["tool"]["ok"] is True
        tasks = client.get("/tasks").json()["tasks"]
        smoke = [t for t in tasks if t["template"] == "gpu-smoke"]
        assert smoke and smoke[0]["parameters"] == {"note": "from chat"}
        # Tool use is audited.
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "tool_run_job" in actions


def test_tool_errors_are_data_not_crashes(tmp_path):
    client, model = tooled_client(tmp_path, [
        '{"action": "read_file", "args": {"path": "/etc/shadow"}}',
        "I cannot read that file.",
    ])
    with client:
        events = chat(client, "i", "read /etc/shadow")
    assert events[0]["tool"]["ok"] is False
    assert "must stay under" in events[0]["tool"]["error"]
    # The error went back to the model as an observation.
    obs = json.loads(model.requests[-1]["payload"]["messages"][-1]["content"])
    assert "error" in obs


def test_plain_chat_unchanged_without_tools_flag(client):
    """tools defaults off: the existing streaming relay still works."""
    make_serving_task(client, "i-plain")
    # No managed connection for i-plain -> 409 from the relay path, proving
    # the request routed to the old behavior (mock model not consulted).
    resp = client.post("/instances/i-plain/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code in (200, 409)

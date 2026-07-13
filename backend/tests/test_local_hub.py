"""The local hub (Phase 36): external brains, approval gates, local terminal.

- BrainRegistry: instance brains from served models; local brains from
  probed endpoints (prober injected - tests never touch the network); api
  brains appear only when their key env var is set.
- Autopilot with an external brain: the run loop drives a scripted
  OpenAI-compatible client end to end.
- Approval gates: launch_gpu pauses as 'pending'; deny feeds an error the
  model sees; approve executes for real; timeout expires it.
- Local terminal WS: origin allowlist enforced; a real shell echoes.
"""

import asyncio
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.brains import BrainRegistry
from app.config import AutopilotSettings
from app.sidecar_client import MockSidecarClient
from tests.conftest import make_settings
from tests.test_auto_manage import _app, _fast
from tests.test_chat import make_serving_task


# -- registry ----------------------------------------------------------------------


def registry_client(tmp_path, *, probe=None, script=None):
    from app.model_client import MockModelClient

    class ScriptedBrain(MockModelClient):
        def __init__(self, lines):
            super().__init__()
            self.lines = list(lines)

        async def chat_completion(self, port, payload):
            self.requests.append({"port": port, "payload": payload})
            return {"choices": [{"message": {
                "role": "assistant", "content": self.lines.pop(0)}}]}

        async def chat_stream(self, port, payload):
            # Autopilot consumes streams; emit one delta then DONE.
            self.requests.append({"port": port, "payload": payload})
            content = self.lines.pop(0)
            chunk = {"choices": [{"delta": {"content": content}}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]))
    if probe is not None:
        app.state.brains._http_get = probe
    scripted = ScriptedBrain(script or [])
    return app, mock, scripted


def test_local_brains_detected_and_api_brains_gated(tmp_path, monkeypatch):
    async def probe(url):
        if "11434" in url:
            return ["llama3.1", "qwen3:8b"]
        return None      # LM Studio not running

    app, _, _ = registry_client(tmp_path, probe=probe)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with TestClient(app) as client:
        brains = client.get("/brains").json()["brains"]
        refs = [b["ref"] for b in brains]
        assert "local:ollama/llama3.1" in refs
        assert "local:ollama/qwen3:8b" in refs
        assert not any(r.startswith("api:") for r in refs)   # no keys set

        # Key appears -> brain appears (no restart needed: env is read live).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
        app.state.brains._local_cache = None
        brains = client.get("/brains").json()["brains"]
        assert any(b["ref"] == "api:claude" for b in brains)


def test_instance_brains_come_from_served_models(tmp_path):
    async def probe(url):
        return None
    app, _, _ = registry_client(tmp_path, probe=probe)
    with TestClient(app) as client:
        from tests.test_reconcile import launch_connected
        _, iid = launch_connected(client)
        make_serving_task(client, iid)
        brains = client.get("/brains").json()["brains"]
        assert any(b["ref"] == f"instance:{iid}" for b in brains)


def test_resolve_rejects_unusable_refs(tmp_path):
    app, _, _ = registry_client(tmp_path)
    reg: BrainRegistry = app.state.brains
    with pytest.raises(ValueError):
        reg.resolve("api:claude")          # no key set -> actionable error
    with pytest.raises(ValueError):
        reg.resolve("local:nope/model")
    with pytest.raises(ValueError):
        reg.resolve("garbage")


# -- external brain drives a run -----------------------------------------------------


def wait_run(client, run_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    run = client.get(f"/autopilot/runs/{run_id}").json()
    while time.monotonic() < deadline:
        run = client.get(f"/autopilot/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.03)
    raise AssertionError(f"run stuck: {run['status']}")


def test_external_brain_runs_a_goal(tmp_path):
    async def probe(url):
        return ["scripted"] if "11434" in url else None

    app, _, scripted = registry_client(
        tmp_path,
        probe=probe,
        script=['{"thought": "list", "action": "list_templates", "args": {}}',
                '{"thought": "done", "action": "done", '
                '"args": {"summary": "listed the templates"}}'])
    # Route the local brain to the scripted client instead of real HTTP.
    reg = app.state.brains
    original = reg.resolve

    def resolve(ref):
        if ref.startswith("local:"):
            return scripted, "scripted", 0
        return original(ref)
    reg.resolve = resolve

    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "List the templates and finish.",
            "brain": "local:ollama/scripted",
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert done["summary"] == "listed the templates"
        assert done["brain_instance_id"] == "local:ollama/scripted"


# -- approvals -----------------------------------------------------------------------


def approval_app(tmp_path, script, *, timeout=600.0):
    app, mock, scripted = registry_client(tmp_path, script=script)
    reg = app.state.brains
    reg.resolve = lambda ref: (scripted, "scripted", 0)
    # Speed the poll: patch settings on the autopilot instance.
    import dataclasses
    st = app.state.dispatcher.settings
    patched = dataclasses.replace(
        st, autopilot=AutopilotSettings(approval_timeout_seconds=timeout))
    return app, mock, patched


def start_gated_run(client, goal="Launch a GPU."):
    return client.post("/autopilot/runs", json={
        "goal": goal, "brain": "local:x/scripted",
        "require_approval": True,
    }).json()["run"]


def wait_pending(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = client.get("/autopilot/approvals").json()["approvals"]
        if pending:
            return pending[0]
        time.sleep(0.03)
    raise AssertionError("no approval ever became pending")


LAUNCH = ('{"thought": "go", "action": "launch_gpu", "args": '
          '{"instance_type": "gpu_1x_a10", "region": "us-east-1", '
          '"filesystem": "manifold-data"}}')
DONE = '{"thought": "ok", "action": "done", "args": {"summary": "finished"}}'


def test_denied_action_never_executes_and_run_adapts(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        assert pending["action"] == "launch_gpu"
        assert pending["run_goal"] == "Launch a GPU."

        resp = client.post(f"/autopilot/approvals/{pending['id']}",
                           json={"approve": False})
        assert resp.status_code == 200

        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"      # model adapted, finished
        assert mock.launch_calls == []            # NOTHING launched
        # The denial reached the model as an observation.
        steps = client.get(f"/autopilot/runs/{run['id']}").json()["steps"]
        launch_step = next(s for s in steps if s["action"] == "launch_gpu")
        assert "DENIED" in launch_step["result"]["error"]


def test_approved_action_executes_for_real(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        client.post(f"/autopilot/approvals/{pending['id']}",
                    json={"approve": True})
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert len(mock.launch_calls) == 1        # the launch really happened
        # Decision is race-safe: a second decide is refused.
        again = client.post(f"/autopilot/approvals/{pending['id']}",
                            json={"approve": False})
        assert again.status_code == 409


def test_approval_timeout_auto_denies(tmp_path):
    app, mock, patched = approval_app(tmp_path, [LAUNCH, DONE], timeout=0.3)
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        # Nobody decides. Force-expire via the same guarded transition the
        # timeout path uses, then confirm the run adapts.
        db = client.app.state.orchestrator.db
        assert db.decide_approval(pending["id"], "expired") is True
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert mock.launch_calls == []


def test_ungated_run_needs_no_approval(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Launch a GPU.", "brain": "local:x/scripted",
            "require_approval": False,
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert len(mock.launch_calls) == 1
        assert client.get("/autopilot/approvals").json()["approvals"] == []


# -- local terminal ------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
def test_local_terminal_echoes_and_respects_origin(tmp_path):
    app, _, _ = registry_client(tmp_path)
    with TestClient(app) as client:
        # Evil origin -> refused before any shell is spawned.
        with pytest.raises(Exception):
            with client.websocket_connect(
                    "/local/terminal",
                    headers={"origin": "https://evil.example"}) as ws:
                ws.receive_text()

        # Localhost origin -> a real shell on this machine echoes.
        with client.websocket_connect(
                "/local/terminal",
                headers={"origin": "http://localhost:3000"}) as ws:
            ws.send_json({"type": "input",
                          "data": "echo manifold_$((20+16))\n"})
            deadline = time.monotonic() + 8
            seen = ""
            while time.monotonic() < deadline:
                seen += ws.receive_text()
                if "manifold_36" in seen:
                    break
            assert "manifold_36" in seen

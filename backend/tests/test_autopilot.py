"""Autopilot: a model served on one instance drives Manifold's guarded ops.

A ScriptedBrain stands in for the vllm-served model: it replays canned
JSON-action replies through the same ModelClient seam production uses, so
the loop, the action surface, the guards, and the persistence are all the
real code paths.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import parse_action
from app.config import Guardrails
from app.model_client import ModelClient
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn
from tests.test_safety_hook import launch_and_wait, wait_connected


class ScriptedBrain(ModelClient):
    """Replays canned replies as OpenAI-style SSE, recording every call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def model_info(self, port):
        return {"data": [{"id": "scripted"}]}

    async def chat_stream(self, port, payload):
        # Snapshot: the loop keeps mutating its live messages list after
        # this call returns (real httpx serializes immediately, so only the
        # mock needs the copy).
        self.calls.append({**payload, "messages": [dict(m) for m in payload["messages"]]})
        reply = (self.replies.pop(0) if self.replies
                 else '{"action": "done", "args": {"summary": "script exhausted"}}')
        chunk = {"choices": [{"delta": {"content": reply}}]}
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"


def build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain,
              **settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: brain,
    )


def make_brain_instance(client):
    """Launch an instance and mark a vllm-serve task running on it — the
    state the dispatcher produces when a model server is up."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    resp = client.post("/tasks", json={
        "template": "vllm-serve",
        "parameters": {"model_id": "Qwen/Qwen2.5-7B-Instruct"},
    })
    client.app.state.queue.mark_running(resp.json()["task"]["id"], instance_id)
    return instance_id


def wait_run(client, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/autopilot/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError(f"run stuck: {run}")


# -- parse_action unit tests --------------------------------------------------------


def test_parse_action_plain():
    parsed, err = parse_action(
        '{"thought": "x", "action": "wait", "args": {"seconds": 1}}'
    )
    assert err is None
    assert parsed["action"] == "wait"
    assert parsed["args"] == {"seconds": 1}


def test_parse_action_fenced_with_prose():
    text = 'Sure! Here is my move:\n```json\n{"action": "list_instances"}\n```'
    parsed, err = parse_action(text)
    assert err is None
    assert parsed["action"] == "list_instances"
    assert parsed["args"] == {}          # args defaulted


def test_parse_action_rejects_no_json():
    parsed, err = parse_action("I think we should launch a GPU!")
    assert parsed is None
    assert "JSON" in err


def test_parse_action_skips_non_action_json():
    text = 'data: {"foo": 1} then {"action": "done", "args": {"summary": "s"}}'
    parsed, err = parse_action(text)
    assert parsed["action"] == "done"


# -- full runs over HTTP ---------------------------------------------------------------


def test_golden_run_brain_manages_second_gpu(tmp_path, mock_client,
                                             mock_storage, mock_sidecar):
    """GPU A (brain) launches GPU B, verifies it, and finishes."""
    brain = ScriptedBrain([
        '{"thought": "see what is available", "action": "list_instance_types", "args": {}}',
        '{"thought": "a10 is cheapest with capacity", "action": "launch_gpu",'
        ' "args": {"instance_type": "gpu_1x_a10", "region": "us-east-1",'
        ' "filesystem": "manifold-data"}}',
        '{"thought": "give it a moment", "action": "wait", "args": {"seconds": 0.2}}',
        '{"thought": "confirm both instances", "action": "list_instances", "args": {}}',
        '{"thought": "goal met", "action": "done",'
        ' "args": {"summary": "Launched a second A10 and verified it."}}',
    ])
    app = build_app(
        tmp_path, mock_client, mock_storage, mock_sidecar, brain,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00),
    )
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        resp = client.post("/autopilot/runs", json={
            "goal": "Launch a second A10 in us-east-1 and confirm it is up.",
            "brain_instance_id": brain_id,
        })
        assert resp.status_code == 202
        run_id = resp.json()["run"]["id"]

        run = wait_run(client, run_id)
        assert run["status"] == "succeeded"
        assert run["summary"] == "Launched a second A10 and verified it."
        assert run["steps_taken"] == 5
        assert run["brain_model"] == "Qwen/Qwen2.5-7B-Instruct"

        # The second GPU really launched through the guarded pipeline.
        assert len(mock_client.launch_calls) == 2

        # Steps persisted in order with observations.
        actions = [s["action"] for s in run["steps"]]
        assert actions == ["list_instance_types", "launch_gpu", "wait",
                           "list_instances", "done"]
        launch_step = run["steps"][1]
        assert launch_step["result"]["launch"]["status"] == "launching"
        instances_step = run["steps"][3]
        assert len(instances_step["result"]["instances"]) == 2

        # The observation loop actually fed results back to the brain.
        second_turn_user = brain.calls[1]["messages"][-1]
        assert "gpu_1x_a10" in second_turn_user["content"]

        # Audited under its own actor.
        audit = client.get("/audit").json()["entries"]
        autopilot_actions = [e["action"] for e in audit
                             if e["actor"] == "autopilot"]
        assert "run_start" in autopilot_actions
        assert "launch_gpu" in autopilot_actions
        assert "run_succeeded" in autopilot_actions


def test_guards_bind_the_autopilot(tmp_path, mock_client, mock_storage,
                                   mock_sidecar):
    """An over-budget launch is refused; the refusal is fed back as data
    and the brain adapts. The guard's message reaches the model verbatim."""
    brain = ScriptedBrain([
        '{"action": "launch_gpu", "args": {"instance_type":'
        ' "gpu_8x_a100_80gb_sxm4", "region": "us-east-1",'
        ' "filesystem": "manifold-data"}}',
        '{"action": "done", "args": {"summary": "Cannot afford that GPU;'
        ' budget guard refused."}}',
    ])
    app = build_app(
        tmp_path, mock_client, mock_storage, mock_sidecar, brain,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00),
    )
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Launch the biggest GPU you can.",
            "brain_instance_id": brain_id,
        }).json()["run"]["id"]

        run = wait_run(client, run_id)
        assert run["status"] == "succeeded"     # agent ended gracefully
        refusal = run["steps"][0]["result"]["error"]
        assert "Budget guard" in refusal
        # The refusal text reached the brain's next turn.
        assert "Budget guard" in brain.calls[1]["messages"][-1]["content"]
        # Only the brain instance exists; nothing else launched.
        assert len(mock_client.launch_calls) == 1


def test_step_limit_exhausts_run(tmp_path, mock_client, mock_storage,
                                 mock_sidecar):
    brain = ScriptedBrain(
        ['{"action": "wait", "args": {"seconds": 0}}'] * 10
    )
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Loop forever.",
            "brain_instance_id": brain_id,
            "max_steps": 3,
        }).json()["run"]["id"]
        run = wait_run(client, run_id)
        assert run["status"] == "exhausted"
        assert run["steps_taken"] == 3
        assert "step limit" in run["error"]


def test_malformed_output_bounced_then_recovers(tmp_path, mock_client,
                                                mock_storage, mock_sidecar):
    brain = ScriptedBrain([
        "I should probably list the instances first!",     # no JSON
        '{"action": "done", "args": {"summary": "recovered"}}',
    ])
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Anything.", "brain_instance_id": brain_id,
        }).json()["run"]["id"]
        run = wait_run(client, run_id)
        assert run["status"] == "succeeded"
        assert run["steps"][0]["action"] == "__invalid__"
        # The correction hint went back to the model.
        assert "JSON" in brain.calls[1]["messages"][-1]["content"]


def test_persistent_garbage_fails_run(tmp_path, mock_client, mock_storage,
                                      mock_sidecar):
    brain = ScriptedBrain(["nonsense"] * 5)
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Anything.", "brain_instance_id": brain_id,
        }).json()["run"]["id"]
        run = wait_run(client, run_id)
        assert run["status"] == "failed"
        assert "unparseable" in run["error"]
        assert run["steps_taken"] == 3          # MAX_CONSECUTIVE_FAILURES


def test_cancel_mid_run(tmp_path, mock_client, mock_storage, mock_sidecar):
    brain = ScriptedBrain(
        ['{"action": "wait", "args": {"seconds": 30}}'] * 5
    )
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Wait around.", "brain_instance_id": brain_id,
        }).json()["run"]["id"]
        time.sleep(0.3)          # let it enter the long wait
        resp = client.post(f"/autopilot/runs/{run_id}/cancel")
        assert resp.status_code == 200
        run = wait_run(client, run_id, timeout=5.0)
        assert run["status"] == "cancelled"


def test_run_requires_a_served_brain(client):
    resp = client.post("/autopilot/runs", json={
        "goal": "Do something.", "brain_instance_id": "i-nothing",
    })
    assert resp.status_code == 409
    assert "vllm-serve" in resp.json()["detail"]


def test_unknown_action_becomes_observation(tmp_path, mock_client,
                                            mock_storage, mock_sidecar):
    brain = ScriptedBrain([
        '{"action": "rm_rf_slash", "args": {}}',
        '{"action": "done", "args": {"summary": "ok, no such tool"}}',
    ])
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Anything.", "brain_instance_id": brain_id,
        }).json()["run"]["id"]
        run = wait_run(client, run_id)
        assert run["status"] == "succeeded"
        assert "unknown action" in run["steps"][0]["result"]["error"]


def test_orphaned_runs_marked_failed_on_restart(tmp_path, mock_client,
                                                mock_storage, mock_sidecar):
    """A run left 'running' by a dead process is failed at next startup."""
    brain = ScriptedBrain([])
    settings = make_settings(tmp_path)

    def fresh_app():
        return create_app(
            settings,
            lambda_client=mock_client,
            storage_factory=lambda fs: mock_storage,
            connect_fn=mock_connect_fn,
            sidecar_factory=lambda conn: mock_sidecar,
            model_client_factory=lambda conn: brain,
        )

    app1 = fresh_app()
    with TestClient(app1) as client:
        # Simulate a run that a previous process never finished.
        app1.state.orchestrator.db.create_agent_run(
            goal="ghost", brain_instance_id="i-dead",
            brain_model="m", max_steps=5,
        )
    with TestClient(fresh_app()) as client:
        runs = client.get("/autopilot/runs").json()["runs"]
        ghost = next(r for r in runs if r["goal"] == "ghost")
        assert ghost["status"] == "failed"
        assert "restarted" in ghost["error"]

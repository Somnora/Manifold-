"""Autopilot project brief: persistent project context in every run's
system prompt, so a goal reads as a step in the project."""

from fastapi.testclient import TestClient

from tests.test_autopilot import (
    ScriptedBrain,
    build_app,
    make_brain_instance,
    wait_run,
)


def test_brief_roundtrip_and_audit(client):
    assert client.get("/project-brief").json()["content"] == ""
    resp = client.put("/project-brief",
                      json={"content": "Red Hope: 3D colony game."})
    assert resp.status_code == 200
    body = client.get("/project-brief").json()
    assert body["content"] == "Red Hope: 3D colony game."
    assert body["updated_at"] is not None
    actions = [e["action"] for e in client.get("/audit").json()["entries"]]
    assert "project_brief_updated" in actions


def test_brief_lands_in_the_run_system_prompt(tmp_path, mock_client,
                                              mock_storage, mock_sidecar):
    brain = ScriptedBrain([
        '{"thought": "done", "action": "done", "args": {"summary": "ok"}}',
    ])
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        client.put("/project-brief",
                   json={"content": "Assets live under red_hope/."})
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Survey the account.",
            "brain_instance_id": brain_id,
            "approve_actions": [],
        }).json()["run"]["id"]
        wait_run(client, run_id)

        system = brain.calls[0]["messages"][0]["content"]
        assert "Project context" in system
        assert "Assets live under red_hope/." in system
        # The brief frames the goal: it appears BEFORE the goal line.
        assert system.index("red_hope") < system.index(
            "Goal: Survey the account.")


def test_empty_brief_leaves_prompt_untouched(tmp_path, mock_client,
                                             mock_storage, mock_sidecar):
    brain = ScriptedBrain([
        '{"thought": "done", "action": "done", "args": {"summary": "ok"}}',
    ])
    app = build_app(tmp_path, mock_client, mock_storage, mock_sidecar, brain)
    with TestClient(app) as client:
        brain_id = make_brain_instance(client)
        run_id = client.post("/autopilot/runs", json={
            "goal": "Survey the account.",
            "brain_instance_id": brain_id,
            "approve_actions": [],
        }).json()["run"]["id"]
        wait_run(client, run_id)
        assert "Project context" not in brain.calls[0]["messages"][0]["content"]

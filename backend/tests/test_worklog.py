"""Worklog: settled jobs and autopilot runs become markdown entries other
agents can read (the cross-agent memory from the platform vision)."""

from pathlib import Path

from app.worklog import Worklog


class _Prefs:
    """Minimal PreferenceStore stand-in."""

    def __init__(self, mirror_dir=""):
        from app.preferences import Preferences, WorklogPrefs
        self._p = Preferences(worklog=WorklogPrefs(mirror_dir=mirror_dir))

    def get(self):
        return self._p


def test_record_and_tail_roundtrip(tmp_path):
    wl = Worklog(tmp_path / "worklog.md")
    wl.record("job whisper-batch succeeded", ["task abc", "cost $0.26"])
    wl.record("autopilot run done", ["run xyz, 4 step(s)"])

    entries = wl.tail(10)
    assert len(entries) == 2
    assert "whisper-batch" in entries[0] and "- cost $0.26" in entries[0]
    assert "autopilot run done" in entries[1]
    # tail(1) returns only the newest.
    assert wl.tail(1) == entries[1:]


def test_mirror_dir_receives_the_same_entries(tmp_path):
    vault = tmp_path / "vault"
    wl = Worklog(tmp_path / "worklog.md", _Prefs(str(vault)))
    wl.record("job done", ["line"])
    mirrored = (vault / "manifold-worklog.md").read_text()
    assert "job done" in mirrored


def test_mirror_failure_never_breaks_the_write(tmp_path):
    # Point the mirror at a location that cannot be a directory.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir")
    wl = Worklog(tmp_path / "worklog.md", _Prefs(str(blocker / "sub")))
    wl.record("job done", ["line"])        # must not raise
    assert "job done" in (tmp_path / "worklog.md").read_text()


def test_tail_of_missing_file_is_empty(tmp_path):
    assert Worklog(tmp_path / "nope.md").tail() == []


# -- funnel integration: a settled job writes an entry ---------------------------


def test_settled_job_lands_in_worklog_and_route(client):
    resp = client.post("/tasks", json={"template": "gpu-smoke",
                                       "parameters": {}})
    task_id = resp.json()["task"]["id"]
    queue = client.app.state.queue
    queue.mark_running(task_id, "i-x")
    dispatcher = client.app.state.dispatcher
    dispatcher._finish_task(task_id, exit_code=0,
                            output_paths=["/lambda/nfs/x/outputs"])

    body = client.get("/worklog").json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert "job gpu-smoke succeeded" in entry
    assert task_id in entry
    assert "/lambda/nfs/x/outputs" in entry
    assert Path(body["path"]).exists()


def test_autopilot_finish_lands_in_worklog(client, db):
    run_id = db.create_agent_run(
        goal="survey the account", brain_instance_id="local:test",
        brain_model="m", max_steps=5)
    client.app.state.autopilot._finish(
        run_id, 3, "done", summary="surveyed 2 instances")

    entries = client.get("/worklog").json()["entries"]
    assert any("autopilot run done" in e and "survey the account" in e
               for e in entries)

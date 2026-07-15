"""Phase 40 field hardening: launches survive --reload, boots get a real
timeout window, launch progress is structured, and progress-bar log churn is
collapsed. All against mocks - zero spend."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import LaunchPolicy, load_settings
from app.connections import ConnectionState
from app.lambda_api import InstanceInfo, MockLambdaClient
from app.main import create_app
from app.orchestrator import Orchestrator, launch_progress
from app.task_queue import SQLiteTaskQueue, collapse_progress
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status


# -- collapse_progress (pure) ----------------------------------------------------

def test_collapse_progress_keeps_last_segment_of_a_bar():
    # A progress bar redraws one line with \r; only the final frame is shown.
    line = "10%\r 40%\r 80%\r100% done"
    assert collapse_progress(line) == "100% done"


def test_collapse_progress_strips_trailing_cr_from_crlf():
    assert collapse_progress("finished\r") == "finished"


def test_collapse_progress_leaves_plain_lines_untouched():
    assert collapse_progress("[manifold] $ docker run ...") == \
        "[manifold] $ docker run ..."


def test_collapse_progress_empty_stays_empty():
    assert collapse_progress("") == ""


def test_job_logs_collapse_carriage_returns_end_to_end(db):
    """A line captured with embedded \r lands in the store already collapsed,
    so get_job_logs (and the tokens an agent spends reading it) stay small."""
    queue = SQLiteTaskQueue(db)
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.append_log(task_id, "epoch 1\repoch 2\repoch 3\repoch 4 final")
    queue.append_log(task_id, "plain line")
    lines = [row["line"] for row in queue.get_logs(task_id)]
    assert "epoch 4 final" in lines
    assert "plain line" in lines
    # None of the intermediate frames were stored.
    assert not any("\r" in ln for ln in lines)
    assert not any("epoch 1" in ln for ln in lines)


# -- launch_progress (pure) ------------------------------------------------------

def test_launch_progress_ready_is_settled():
    out = launch_progress({"status": "active"}, 2400.0, "2026-07-14T00:00:00+00:00")
    assert out["phase"] == "ready"
    assert out["settled"] is True


def test_launch_progress_failed_is_settled():
    out = launch_progress({"status": "failed"}, 2400.0, "2026-07-14T00:00:00+00:00")
    assert out["phase"] == "failed"
    assert out["settled"] is True


def test_launch_progress_booting_has_countdown():
    launch = {
        "status": "booting",
        "lambda_instance_id": "i-boot",
        "launched_at": "2026-07-14T00:00:00+00:00",
    }
    out = launch_progress(launch, 2400.0, "2026-07-14T00:05:00+00:00")  # 300s later
    assert out["phase"] == "waiting_for_active"
    assert out["settled"] is False
    assert out["boot_elapsed_seconds"] == 300
    assert out["boot_timeout_seconds"] == 2400
    assert out["boot_remaining_seconds"] == 2100
    assert "i-boot" in out["phase_detail"]


def test_launch_progress_booting_clamps_when_past_timeout():
    launch = {
        "status": "booting",
        "lambda_instance_id": "i-boot",
        "launched_at": "2026-07-14T00:00:00+00:00",
    }
    out = launch_progress(launch, 60.0, "2026-07-14T01:00:00+00:00")  # 1h later
    assert out["boot_remaining_seconds"] == 0     # never negative


# -- boot timeout default --------------------------------------------------------

def test_boot_timeout_default_is_generous():
    """The shipped default must cover a slow SXM4 boot, not cut it off."""
    assert LaunchPolicy().boot_timeout_seconds == 2400.0


def test_config_yaml_boot_timeout_loaded(tmp_path, monkeypatch):
    settings = load_settings()
    # Repo config.yaml carries the hardened value.
    assert settings.launch.boot_timeout_seconds >= 2400.0


# -- resume pending launches (survive --reload) ---------------------------------

def _booting_launch(db, instance_id="i-boot"):
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(
        launch_id, status="booting", lambda_instance_id=instance_id,
        launched_type="gpu_1x_a10", launched_at="2026-07-14T00:00:00+00:00",
    )
    return launch_id


async def test_resume_finishes_a_still_booting_launch(tmp_path, db):
    """A launch orphaned mid-boot (its waiter died with the old process) gets
    a fresh waiter that carries it to active + connected."""
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="booting", ip=None,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    launch_id = _booting_launch(db)

    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    resumed = await orch.resume_pending_launches()
    assert resumed == 1

    settled = await orch.wait_for_launch(launch_id, timeout=3.0)
    assert settled["status"] == "active"
    assert "i-boot" in orch.connections


async def test_resume_marks_active_when_adopt_already_reconnected(tmp_path, db):
    """If the instance finished booting during the downtime, adopt reconnects
    it and resume just closes out the still-'booting' launch record."""
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="active", ip="203.0.113.9",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    launch_id = _booting_launch(db)

    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    await orch.adopt_running_instances()          # reconnects the active one
    assert "i-boot" in orch.connections
    resumed = await orch.resume_pending_launches()
    assert resumed == 1
    assert db.get_launch(launch_id)["status"] == "active"


async def test_resume_fails_booting_launch_with_no_instance(tmp_path, db):
    """A 'booting' row with no instance id can never settle on its own; resume
    fails it rather than leaving a zombie."""
    settings = make_settings(tmp_path)
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(launch_id, status="booting")   # no lambda_instance_id
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    await orch.resume_pending_launches()
    assert db.get_launch(launch_id)["status"] == "failed"


def test_restart_resumes_mid_boot_over_http(tmp_path, mock_storage, mock_sidecar):
    """End to end: a launch left 'booting' in the DB (as --reload would) is
    carried to active by a fresh app on startup, with an audit trail."""
    from app.db import Database

    settings = make_settings(tmp_path)
    shared_mock = MockLambdaClient()
    shared_mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="booting", ip=None,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )

    # Seed a booting launch row BEFORE the app starts, so startup's
    # resume_pending_launches sees it (mirrors a --reload mid-boot).
    seed_db = Database(settings.db_path)
    launch_id = _booting_launch(seed_db)
    seed_db.close()

    app = create_app(
        settings, lambda_client=shared_mock,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as client:
        settled = wait_for_launch_status(client, launch_id, timeout=3.0)
        assert settled["status"] == "active"
        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "resume_pending_launches" for e in audit)


# -- wait long-poll + structured phase over HTTP --------------------------------

def test_launch_route_returns_structured_phase(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch_id = resp.json()["launch"]["id"]
    settled = wait_for_launch_status(client, launch_id)
    body = client.get(f"/launches/{launch_id}").json()
    assert body["phase"] == "ready"
    assert body["settled"] is True
    assert "phase_detail" in body


def test_wait_route_blocks_until_settled(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch_id = resp.json()["launch"]["id"]
    body = client.get(f"/launches/{launch_id}/wait", params={"timeout": 5}).json()
    assert body["settled"] is True
    assert body["status"] in ("active", "failed")


def test_wait_route_404_for_unknown_launch(client):
    resp = client.get("/launches/nope/wait", params={"timeout": 1})
    assert resp.status_code == 404

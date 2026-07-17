"""Per-job actual cost: wall time at the launch's hourly rate.

Closes the estimate feedback loop: the Jobs page shows what a finished job
actually cost so the user can check the pre-launch estimates against
reality. Unknown stays unknown: a task on an adopted instance (no launch
row, no rate) gets a runtime but a null cost.
"""


def _finish_task(client, db, instance_id, minutes=30):
    """Queue a task, mark it running on `instance_id`, finish it, and
    stretch its wall time to `minutes` by rewriting the timestamps."""
    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {}})
    task_id = resp.json()["task"]["id"]
    queue = client.app.state.queue
    queue.mark_running(task_id, instance_id)
    queue.mark_finished(task_id, exit_code=0, output_paths=[])
    # Rewrite started_at in the same ISO format the app writes, so the
    # cost query's timestamp parse sees exactly production-shaped rows.
    from datetime import datetime, timedelta
    row = db._execute("SELECT finished_at FROM tasks WHERE id = ?",
                      (task_id,)).fetchone()
    start = (datetime.fromisoformat(row["finished_at"])
             - timedelta(minutes=minutes)).isoformat()
    db._execute("UPDATE tasks SET started_at = ? WHERE id = ?",
                (start, task_id))
    return task_id


def test_finished_task_reports_runtime_and_cost(client, db):
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1", filesystem=None,
        connection_mode="direct-ssh", hourly_rate_cents=129)
    db.update_launch(launch_id, lambda_instance_id="i-launched")

    task_id = _finish_task(client, db, "i-launched", minutes=30)
    task = next(t for t in client.get("/tasks").json()["tasks"]
                if t["id"] == task_id)
    assert task["runtime_seconds"] == 1800.0
    # 0.5h at $1.29/h = 64.5 cents; Python's round() is banker's rounding.
    assert task["actual_cost_cents"] == 64


def test_task_on_adopted_instance_has_runtime_but_no_cost(client, db):
    """No launch row means no rate; the cost must be null, not a guess."""
    task_id = _finish_task(client, db, "i-adopted", minutes=10)
    task = next(t for t in client.get("/tasks").json()["tasks"]
                if t["id"] == task_id)
    assert task["runtime_seconds"] == 600.0
    assert task["actual_cost_cents"] is None


def test_unfinished_task_has_no_runtime(client):
    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {}})
    task_id = resp.json()["task"]["id"]
    task = next(t for t in client.get("/tasks").json()["tasks"]
                if t["id"] == task_id)
    assert task["runtime_seconds"] is None
    assert task["actual_cost_cents"] is None

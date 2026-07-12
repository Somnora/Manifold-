"""Job History: finished jobs persist and can be removed individually or
cleared in bulk, while active jobs are protected. Also the model presets
endpoint. Driven by a live-test report where old jobs cluttered the queue
with no way to clear them."""


def _enqueue(client, note):
    r = client.post("/tasks", json={"template": "gpu-smoke",
                                    "parameters": {"note": note}})
    assert r.status_code == 202
    return r.json()["task"]["id"]


def test_clear_finished_removes_only_finished(client):
    db = client.app.state.orchestrator.db
    a, b, c = (_enqueue(client, "a"), _enqueue(client, "b"),
               _enqueue(client, "c"))
    db.update_task(a, status="succeeded", exit_code=0)
    db.update_task(b, status="failed", exit_code=1, error="boom")
    # c stays queued (no connected instance, so the dispatcher can't run it).

    resp = client.delete("/tasks/finished")
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 2

    remaining = {t["id"] for t in client.get("/tasks").json()["tasks"]}
    assert remaining == {c}
    # Logs of cleared tasks are gone too.
    assert client.get(f"/tasks/{a}/logs").status_code == 404


def test_delete_one_finished_task(client):
    db = client.app.state.orchestrator.db
    a = _enqueue(client, "a")
    db.update_task(a, status="succeeded", exit_code=0)
    assert client.delete(f"/tasks/{a}").status_code == 200
    assert client.get(f"/tasks/{a}").status_code == 404


def test_cannot_delete_running_task(client):
    db = client.app.state.orchestrator.db
    a = _enqueue(client, "a")
    db.update_task(a, status="running", instance_id="i-1")
    resp = client.delete(f"/tasks/{a}")
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"]
    # Still present.
    assert client.get(f"/tasks/{a}").status_code == 200


def test_delete_unknown_task_404(client):
    assert client.delete("/tasks/nope").status_code == 404


def test_finished_route_not_shadowed_by_task_id(client):
    """DELETE /tasks/finished must hit the clear endpoint, not be parsed as a
    task whose id is 'finished'."""
    resp = client.delete("/tasks/finished")
    assert resp.status_code == 200
    assert "cleared" in resp.json()


def test_model_presets_are_ungated_and_tiered(client):
    resp = client.get("/model-presets")
    assert resp.status_code == 200
    presets = resp.json()["presets"]
    assert len(presets) >= 3
    ids = [p["model_id"] for p in presets]
    assert "Qwen/Qwen2.5-7B-Instruct" in ids
    for p in presets:
        assert {"label", "model_id", "vram_gib", "tier", "note"} <= set(p)
        # Ungated only: no known gated repos slip into the presets.
        assert "meta-llama" not in p["model_id"]
        assert "google/gemma" not in p["model_id"]

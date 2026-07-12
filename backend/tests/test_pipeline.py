"""Data-pipeline templates: script-run + llm-synthesize, and the
network: host rendering they rely on."""

from pathlib import Path

import pytest

from app.dispatcher import coerce_parameters, render_docker_command
from app.templates import TemplateError, load_templates, parse_template

REPO_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"
TEMPLATES, ERRORS = load_templates(REPO_TEMPLATES)


def test_pipeline_templates_load_cleanly():
    assert ERRORS == {}
    assert "script-run" in TEMPLATES
    assert "llm-synthesize" in TEMPLATES


def test_network_field_validation():
    base = """
name: t
description: d
image: alpine
command: "true"
"""
    with pytest.raises(TemplateError, match="network must be"):
        parse_template(base + "network: bridge2\n")
    # host + ports is contradictory: host networking has no port mappings.
    with pytest.raises(TemplateError, match="mutually exclusive"):
        parse_template(base + "network: host\nports:\n  - host: 80\n    container: 80\n")
    assert parse_template(base + "network: host\n").network == "host"
    assert parse_template(base).network == ""


def test_llm_synthesize_renders_host_network_no_ports():
    t = TEMPLATES["llm-synthesize"]
    params = coerce_parameters(t, {
        "input_path": "research/scrapes/candidates-2026-raw.jsonl",
        "instruction": "Extract candidate name, district, and funding total as JSON.",
        "limit": 5,
    })
    cmd = render_docker_command(t, params, filesystem="manifold-data",
                                task_id="syn1")
    assert "--network host" in cmd
    assert "-p 127.0.0.1" not in cmd
    # Whole persistent filesystem mounted at /data.
    assert "-v /lambda/nfs/manifold-data:/data" in cmd
    # The instruction travels shell-quoted (spaces intact, injection inert).
    assert "'Extract candidate name, district, and funding total as JSON.'" in cmd
    # Defaults applied: port 8080, output name.
    assert " 8080 " in cmd
    assert "synthesized" in cmd


def test_script_run_renders_quoted_args():
    t = TEMPLATES["script-run"]
    params = coerce_parameters(t, {
        "script": "scrape_candidates.py",
        "args": "--state TX --cycle 2026; rm -rf /",
    })
    cmd = render_docker_command(t, params, filesystem="manifold-data",
                                task_id="scr1")
    # Args arrive as ONE quoted token — the injection attempt is inert text.
    assert "'--state TX --cycle 2026; rm -rf /'" in cmd
    assert "-v /lambda/nfs/manifold-data:/data" in cmd
    assert "scrape_candidates.py" in cmd
    # No served ports, default bridge network.
    assert "--network host" not in cmd


def test_script_run_caches_pip_on_persistent_storage():
    """pip downloads cache under /data (persistent NFS) so re-runs and other
    instances don't re-download the same wheels."""
    t = TEMPLATES["script-run"]
    cmd = render_docker_command(
        t, coerce_parameters(t, {"script": "x.py"}),
        filesystem="manifold-data", task_id="scr2")
    assert "--cache-dir /data/.cache/pip" in cmd


# -- script-run: execute the REAL runner against a temp filesystem ----------------
#
# The runner lives in the RUNNER env var and takes script + args as $1/$2,
# exactly as the container invokes it. Run it verbatim (with /data pointed at
# a temp dir) to prove the preflight fires, a present script runs, and args
# with spaces survive as ONE argv[1] (the quoting bug the mock never caught).

import subprocess


def _run_runner(data, script, args):
    runner = TEMPLATES["script-run"].env["RUNNER"].replace("/data", str(data))
    return subprocess.run(
        ["bash", "-c", runner, "manifold", script, args],
        capture_output=True, text=True, timeout=30,
    )


def test_script_run_preflights_missing_script(tmp_path):
    """A typo'd script name fails fast (exit 2) with a clear message, instead
    of a confusing crash inside the container — the reported symptom."""
    data = tmp_path / "data"
    (data / "scripts").mkdir(parents=True)
    result = _run_runner(data, "typo.py", "")
    assert result.returncode == 2
    assert "script not found" in result.stderr
    assert "typo.py" in result.stderr


def test_script_run_executes_present_script(tmp_path):
    """A present script runs and receives args-with-spaces as ONE argv[1]."""
    data = tmp_path / "data"
    (data / "scripts").mkdir(parents=True)
    (data / "scripts" / "hello.py").write_text(
        "import shlex, sys\n"
        "print('argv1=' + repr(sys.argv[1]))\n"
        "print('split=' + repr(shlex.split(sys.argv[1])))\n")
    result = _run_runner(data, "hello.py", "--state TX --cycle 2026")
    assert result.returncode == 0, result.stderr
    # The whole args string arrives as a single argv[1], intact.
    assert "argv1='--state TX --cycle 2026'" in result.stdout
    assert "split=['--state', 'TX', '--cycle', '2026']" in result.stdout


# -- llm-synthesize: execute the REAL embedded script against a stub vLLM ---------
#
# These run the template's PYCODE verbatim in a subprocess (the never-run
# guard) and cover the Phase 17 hardening: preflight wait, transient retry,
# malformed-input tolerance, JSON parsing, and clean failure messages.

import http.server
import json as _json
import subprocess
import sys
import threading


def _make_stub(*, model_content, models_ready_after=0, chat_fail_once=()):
    """Build a configurable stub vLLM handler class.

    models_ready_after: return 503 on /v1/models this many times before the
    model 'comes up' (exercises the readiness wait).
    chat_fail_once: record names that should get one 500 before succeeding
    (exercises the per-record retry).
    """
    state = {"models_calls": 0, "chat_fails": {}}

    class Stub(http.server.BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = _json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["models_calls"] += 1
            if state["models_calls"] <= models_ready_after:
                self._send(503, {"error": "loading"})
            else:
                self._send(200, {"data": [{"id": "stub/qwen-7b"}]})

        def do_POST(self):
            req = _json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))
            record = _json.loads(req["messages"][1]["content"])
            name = record.get("name")
            if name in chat_fail_once and not state["chat_fails"].get(name):
                state["chat_fails"][name] = True
                self._send(500, {"error": "transient"})
                return
            self._send(200, {"choices": [{"message": {
                "content": model_content(record)}}]})

        def log_message(self, *a):
            pass

    return Stub, state


def _run_synthesize(tmp_path, records_text, *, model_content,
                    models_ready_after=0, chat_fail_once=(),
                    limit="0", input_name="research/raw.jsonl",
                    ready_timeout=None, write_input=True):
    """Run the template's PYCODE against a stub, return (result, data_dir)."""
    handler, _state = _make_stub(
        model_content=model_content,
        models_ready_after=models_ready_after,
        chat_fail_once=chat_fail_once,
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    data = tmp_path / "data"
    if write_input:
        (data / input_name).parent.mkdir(parents=True, exist_ok=True)
        (data / input_name).write_text(records_text)
    else:
        data.mkdir(parents=True, exist_ok=True)

    pycode = TEMPLATES["llm-synthesize"].env["PYCODE"]
    # The container mounts the filesystem at /data; locally we substitute
    # the temp dir. Everything else runs verbatim.
    pycode = pycode.replace('"/data', f'"{data}').replace('f"/data', f'f"{data}')
    env = None
    if ready_timeout is not None:
        import os
        env = {**os.environ, "MANIFOLD_SYNTH_READY_TIMEOUT": str(ready_timeout)}
    try:
        result = subprocess.run(
            [sys.executable, "-c", pycode, input_name, "points",
             "Extract name and district as JSON.", str(port), limit],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        server.shutdown()
    return result, data


def _output(data):
    return [_json.loads(l) for l in
            (data / "synthesized" / "points.jsonl").read_text().splitlines()]


def test_llm_synthesize_pycode_actually_runs(tmp_path):
    """Happy path: JSONL in, structured JSONL out. The model returns JSON,
    which is parsed into ready-to-use points (synthesis_json)."""
    rows = (_json.dumps({"name": "Jane Doe", "district": "TX-07"}) + "\n"
            + _json.dumps({"name": "Bob Roe", "district": "AZ-01"}) + "\n")
    result, data = _run_synthesize(
        tmp_path, rows,
        model_content=lambda r: _json.dumps(
            {"name": r["name"], "district": r["district"]}),
    )
    assert result.returncode == 0, result.stderr
    assert "synthesizing with stub/qwen-7b" in result.stdout
    assert "2 synthesized, 0 failed" in result.stdout
    out = _output(data)
    assert out[0]["record"] == {"name": "Jane Doe", "district": "TX-07"}
    # The JSON reply is parsed into usable points, not left double-encoded.
    assert out[0]["synthesis_json"] == {"name": "Jane Doe", "district": "TX-07"}
    assert out[1]["synthesis_json"]["district"] == "AZ-01"


def test_llm_synthesize_parses_fenced_json(tmp_path):
    """Models love ```json fences; those still parse into points."""
    rows = _json.dumps({"name": "Jane", "district": "TX-07"}) + "\n"
    result, data = _run_synthesize(
        tmp_path, rows,
        model_content=lambda r: f"```json\n{_json.dumps({'d': r['district']})}\n```",
    )
    assert result.returncode == 0, result.stderr
    assert _output(data)[0]["synthesis_json"] == {"d": "TX-07"}


def test_llm_synthesize_marks_non_json_output(tmp_path):
    """A prose reply is kept verbatim and flagged, never silently dropped."""
    rows = _json.dumps({"name": "Jane", "district": "TX-07"}) + "\n"
    result, data = _run_synthesize(
        tmp_path, rows, model_content=lambda r: "Jane represents TX-07.")
    assert result.returncode == 0, result.stderr
    row = _output(data)[0]
    assert row["synthesis"] == "Jane represents TX-07."
    assert row["parse_error"] is True
    assert "synthesis_json" not in row


def test_llm_synthesize_waits_for_model(tmp_path):
    """If synthesize starts before vLLM is ready, it waits and then runs —
    instead of crashing on the first /v1/models call (the live-test bug)."""
    rows = _json.dumps({"name": "Jane", "district": "TX-07"}) + "\n"
    result, data = _run_synthesize(
        tmp_path, rows,
        model_content=lambda r: _json.dumps({"d": r["district"]}),
        models_ready_after=1,   # 503 once, then ready
    )
    assert result.returncode == 0, result.stderr
    assert "waiting for a model" in result.stdout
    assert "1 synthesized, 0 failed" in result.stdout


def test_llm_synthesize_retries_transient_errors(tmp_path):
    """One transient 500 on a record is retried, not counted as a loss."""
    rows = _json.dumps({"name": "Jane", "district": "TX-07"}) + "\n"
    result, data = _run_synthesize(
        tmp_path, rows,
        model_content=lambda r: _json.dumps({"d": r["district"]}),
        chat_fail_once=("Jane",),
    )
    assert result.returncode == 0, result.stderr
    assert "1 synthesized, 0 failed" in result.stdout
    assert _output(data)[0]["synthesis_json"] == {"d": "TX-07"}


def test_llm_synthesize_skips_malformed_input(tmp_path):
    """A broken JSONL line is skipped (counted failed); the run continues."""
    rows = ("{not valid json\n"
            + _json.dumps({"name": "Bob", "district": "AZ-01"}) + "\n")
    result, data = _run_synthesize(
        tmp_path, rows,
        model_content=lambda r: _json.dumps({"d": r["district"]}),
    )
    assert result.returncode == 0, result.stderr
    assert "skipped malformed input" in result.stdout
    assert "1 synthesized, 1 failed" in result.stdout
    assert _output(data)[0]["record"] == {"name": "Bob", "district": "AZ-01"}


def test_llm_synthesize_missing_input_fails_clearly(tmp_path):
    """A typo'd input path fails with the path, not a Python traceback."""
    result, _ = _run_synthesize(
        tmp_path, "", model_content=lambda r: "x", write_input=False)
    assert result.returncode != 0
    assert "input file not found" in result.stderr


def test_llm_synthesize_fails_fast_when_no_model(tmp_path):
    """If no model ever answers, the job fails with an actionable message
    (bounded by the readiness timeout, not hung forever)."""
    rows = _json.dumps({"name": "Jane", "district": "TX-07"}) + "\n"
    result, _ = _run_synthesize(
        tmp_path, rows, model_content=lambda r: "x",
        models_ready_after=10_000,   # never becomes ready
        ready_timeout=1,
    )
    assert result.returncode != 0
    assert "did not become ready" not in result.stderr  # message is precise
    assert "no model answered" in result.stderr
    assert "vllm-serve" in result.stderr


def test_script_run_end_to_end_over_http(client):
    """Enqueue -> dispatch over (mock) SSH -> succeeded, with the rendered
    docker command visible in the logs."""
    import time
    from tests.test_reconcile import launch_connected

    launch_connected(client)
    resp = client.post("/tasks", json={
        "template": "script-run",
        "parameters": {"script": "scrape.py", "args": "--limit 10"},
    })
    assert resp.status_code == 202
    task_id = resp.json()["task"]["id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert task["status"] == "succeeded"
    lines = [l["line"] for l in
             client.get(f"/tasks/{task_id}/logs").json()["lines"]]
    docker_line = next(l for l in lines if "docker run" in l)
    assert "scrape.py" in docker_line
    assert "'--limit 10'" in docker_line

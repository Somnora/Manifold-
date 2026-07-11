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


def test_llm_synthesize_pycode_actually_runs(tmp_path):
    """Execute the template's embedded Python for real: a stub OpenAI server
    stands in for vLLM, a JSONL input goes in, structured JSONL comes out.
    This is the guard against shipping a never-executed script."""
    import http.server
    import json
    import subprocess
    import sys
    import threading

    class StubVLLM(http.server.BaseHTTPRequestHandler):
        def _send(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == "/v1/models"
            self._send({"data": [{"id": "stub/qwen-7b"}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))
            record = json.loads(req["messages"][1]["content"])
            self._send({"choices": [{"message": {
                "content": f"POINT: {record['name']} ({record['district']})"
            }}]})

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), StubVLLM)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    data = tmp_path / "data"
    (data / "research").mkdir(parents=True)
    with open(data / "research" / "raw.jsonl", "w") as f:
        f.write(json.dumps({"name": "Jane Doe", "district": "TX-07"}) + "\n")
        f.write(json.dumps({"name": "Bob Roe", "district": "AZ-01"}) + "\n")

    pycode = TEMPLATES["llm-synthesize"].env["PYCODE"]
    # The container mounts the filesystem at /data; locally we substitute
    # the temp dir. Everything else runs verbatim.
    pycode = pycode.replace('"/data', f'"{data}').replace("f\"/data", f"f\"{data}")
    result = subprocess.run(
        [sys.executable, "-c", pycode, "research/raw.jsonl", "points",
         "Extract name and district.", str(port), "0"],
        capture_output=True, text=True, timeout=30,
    )
    server.shutdown()
    assert result.returncode == 0, result.stderr
    assert "synthesizing with stub/qwen-7b" in result.stdout
    assert "2 synthesized, 0 failed" in result.stdout

    lines = [json.loads(l) for l in
             (data / "synthesized" / "points.jsonl").read_text().splitlines()]
    assert lines[0]["record"] == {"name": "Jane Doe", "district": "TX-07"}
    assert lines[0]["synthesis"] == "POINT: Jane Doe (TX-07)"
    assert lines[1]["synthesis"] == "POINT: Bob Roe (AZ-01)"


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

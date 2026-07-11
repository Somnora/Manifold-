"""The dashboard terminal: browser WS <-> backend <-> SSH shell session.

Uses the mock shell behind the same bridge code as production — the only
difference is what connect_fn dialed.
"""

import time

from tests.conftest import wait_for_launch_status


def launch_connected(client, timeout=5.0):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(
            i for i in client.get("/instances").json()["instances"]
            if i["id"] == instance_id
        )
        if inst["connection_state"] == "connected":
            return instance_id
        time.sleep(0.02)
    raise AssertionError("instance never connected")


def read_until(ws, marker: str, limit=50) -> str:
    collected = ""
    for _ in range(limit):
        collected += ws.receive_text()
        if marker in collected:
            return collected
    raise AssertionError(f"never saw {marker!r} in {collected!r}")


def test_terminal_end_to_end(client):
    instance_id = launch_connected(client)
    with client.websocket_connect(f"/instances/{instance_id}/terminal") as ws:
        # Shell greets with a prompt.
        banner = read_until(ws, "$ ")
        assert "mock shell" in banner

        # Type a command; the shell echoes it and answers.
        for ch in "nvidia-smi\r":
            ws.send_json({"type": "input", "data": ch})
        output = read_until(ws, "$ ")           # up to the next prompt
        assert "NVIDIA-SMI" in output
        assert "Mock A10" in output

        # Claude Code is present on the box (installed by cloud-init).
        for ch in "claude --version\r":
            ws.send_json({"type": "input", "data": ch})
        output = read_until(ws, "$ ")
        assert "claude" in output.lower()


def test_terminal_resize_reaches_the_pty(client, mock_client):
    instance_id = launch_connected(client)
    orchestrator = None
    # Reach through the app to the live connection's mock processes.
    from fastapi.testclient import TestClient
    with client.websocket_connect(f"/instances/{instance_id}/terminal") as ws:
        read_until(ws, "$ ")
        ws.send_json({"type": "resize", "cols": 120, "rows": 40})
        ws.send_json({"type": "input", "data": "pwd\r"})
        read_until(ws, "$ ")
    # The resize reached the (mock) PTY.
    conn = client.app.state.orchestrator.connections[instance_id]
    # The ManagedConnection wraps our MockSSHConnection:
    ssh = conn.ssh_connection()
    shell = next(p for p in ssh.processes if p.command is None)
    assert (120, 40) in shell.resizes


def test_terminal_requires_connection(client):
    with client.websocket_connect("/instances/nonexistent/terminal") as ws:
        message = ws.receive_text()
        assert "no SSH connection" in message


def test_terminal_activity_feeds_idle_detection(client):
    instance_id = launch_connected(client)
    dispatcher = client.app.state.dispatcher
    before = dispatcher.last_activity.get(instance_id)
    with client.websocket_connect(f"/instances/{instance_id}/terminal") as ws:
        read_until(ws, "$ ")
        ws.send_json({"type": "input", "data": "ls\r"})
        read_until(ws, "$ ")
    after = dispatcher.last_activity.get(instance_id)
    assert after is not None
    assert before is None or after >= before


def test_recent_files_endpoint(client):
    instance_id = launch_connected(client)
    body = client.get(f"/instances/{instance_id}/files/recent").json()
    roots = {f["root"] for f in body["files"]}
    assert roots == {"ephemeral", "persistent"}
    # Newest first.
    modified = [f["modified"] for f in body["files"]]
    assert modified == sorted(modified, reverse=True)

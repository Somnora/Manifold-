"""Sidecar diagnosis: classify why the sidecar is silent from read-only
probes over the (known-good) SSH connection. Driven by a live-test report
where telemetry showed 'not reachable yet' 13 minutes after boot with no
way to tell whether cloud-init, the service, or the forward was at fault."""

import asyncio

from app.diagnostics import diagnose_sidecar


def _fake_run(mapping):
    """Return a run(cmd) coroutine that matches cmd by substring; unmatched
    commands return an empty success."""
    async def run(cmd):
        for needle, value in mapping.items():
            if needle in cmd:
                return value          # (exit, stdout, stderr)
        return (0, "", "")
    return run


def _diagnose(mapping):
    return asyncio.run(diagnose_sidecar(_fake_run(mapping)))


def test_cloud_init_still_running():
    d = _diagnose({
        "cloud-init status": (0, "status: running", ""),
        "is-active": (0, "activating", ""),
        "ss -ltn": (0, "no", ""),
    })
    assert d["cause"] == "cloud-init-running"
    assert "first-boot setup" in d["summary"]
    # The evidence is carried for the user to inspect.
    labels = [c["label"] for c in d["checks"]]
    assert any("cloud-init" in l for l in labels)


def test_sidecar_crashed_surfaces_log():
    d = _diagnose({
        "cloud-init status": (0, "status: done", ""),
        "is-active": (0, "failed", ""),
        "ss -ltn": (0, "no", ""),
        "journalctl": (0, "Traceback: ImportError: pynvml", ""),
    })
    assert d["cause"] == "sidecar-crashed"
    log = next(c for c in d["checks"] if "log" in c["label"])
    assert "ImportError" in log["output"]


def test_sidecar_starting_when_active_but_not_listening():
    d = _diagnose({
        "cloud-init status": (0, "status: done", ""),
        "is-active": (0, "active", ""),
        "ss -ltn": (0, "no", ""),
    })
    assert d["cause"] == "sidecar-starting"
    assert "9411" in d["summary"]


def test_forward_transient_when_healthy_on_instance():
    d = _diagnose({
        "cloud-init status": (0, "status: done", ""),
        "is-active": (0, "active", ""),
        "ss -ltn": (0, "yes", ""),
    })
    assert d["cause"] == "forward-transient"
    assert "port-forward" in d["summary"]


def test_probe_failure_never_sinks_the_report():
    async def run(cmd):
        if "is-active" in cmd:
            raise ConnectionError("ssh dropped mid-probe")
        return (0, "status: done", "")
    d = asyncio.run(diagnose_sidecar(run))
    # Still returns a structured report; the failed probe is captured inline
    # and the classification says the channel died - it must not guess a
    # sidecar state from the partial answers (a dead SSH session used to
    # read as "sidecar-starting").
    assert d["cause"] == "probe-error"
    assert "unknown" in d["summary"]
    svc = next(c for c in d["checks"] if "service" in c["label"])
    assert "probe failed" in svc["output"]


def test_probe_loss_stops_probing_the_dead_channel():
    calls = []

    async def run(cmd):
        calls.append(cmd)
        raise ConnectionError("Connection reset")

    d = asyncio.run(diagnose_sidecar(run))
    assert d["cause"] == "probe-error"
    # One failure aborts the sweep: each further probe would only burn its
    # own timeout against a channel we already know is dead.
    assert len(calls) == 1
    outputs = [c["output"] for c in d["checks"]]
    assert sum("probe skipped" in o for o in outputs) == 3
    assert len(d["checks"]) == 4                 # shape preserved for the UI


def test_diagnose_route_is_wired(client):
    """Over the app: the endpoint runs the probes over the (mock) SSH
    connection and returns the structured shape."""
    from tests.test_reconcile import launch_connected

    _, instance_id = launch_connected(client)
    resp = client.get(f"/instances/{instance_id}/sidecar/diagnose")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"cause", "summary", "port", "checks"}
    assert body["port"] == 9411
    assert len(body["checks"]) == 4


def test_diagnose_requires_a_connection(client):
    resp = client.get("/instances/does-not-exist/sidecar/diagnose")
    assert resp.status_code == 409

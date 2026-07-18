"""Sidecar reachability diagnosis.

The sidecar binds to 127.0.0.1:9411 on the instance and is reached only
through an SSH local port forward. When it does not answer, the managed SSH
connection itself is almost always fine, so we can ask the instance directly
WHY: read-only shell probes over the known-good SSH channel, classified into
an actionable cause instead of a dead-end "sidecar not reachable yet".

Pure and injectable: diagnose_sidecar takes a `run(cmd) -> (exit, out, err)`
coroutine (ManagedConnection.run in production, a fake in tests).
"""

from __future__ import annotations

from .sidecar_client import SIDECAR_PORT

# (key, human label, read-only command). Ordered; all are best-effort and
# must never fail the whole probe, hence the `|| true` / echo fallbacks.
def _checks(port: int):
    return [
        ("cloud_init", "first-boot setup (cloud-init)",
         "cloud-init status 2>/dev/null || echo 'status: unknown'"),
        ("service", "sidecar service state",
         "systemctl is-active manifold-sidecar 2>/dev/null || echo unknown"),
        ("listening", f"listening on 127.0.0.1:{port}",
         f"( ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null ) "
         f"| grep -q ':{port} ' && echo yes || echo no"),
        ("logs", "recent sidecar log",
         "journalctl -u manifold-sidecar --no-pager -n 15 2>/dev/null "
         "| tail -n 15 || echo '(no journal)'"),
    ]


async def diagnose_sidecar(run, *, port: int = SIDECAR_PORT) -> dict:
    """Probe the instance over SSH and classify why the sidecar is silent.

    Returns {cause, summary, port, checks:[{label, command, output}]}.
    """
    results: dict[str, dict] = {}
    probe_lost = False
    checks = _checks(port)
    for i, (key, label, cmd) in enumerate(checks):
        try:
            _exit, out, err = await run(cmd)
            output = (out or "").strip() or (err or "").strip()
        except Exception as exc:   # a probe failing must not sink the report
            # The SSH channel itself just failed. Don't keep probing over a
            # dead channel (each attempt would burn its own timeout), and
            # don't let the partial answers below masquerade as a diagnosis.
            probe_lost = True
            results[key] = {"label": label, "command": cmd,
                            "output": f"probe failed: {exc}"}
            for skipped_key, skipped_label, skipped_cmd in checks[i + 1:]:
                results[skipped_key] = {
                    "label": skipped_label, "command": skipped_cmd,
                    "output": "probe skipped: connection lost mid-diagnosis",
                }
            break
        results[key] = {"label": label, "command": cmd, "output": output}

    if probe_lost:
        return {
            "cause": "probe-error",
            "summary": (
                "The SSH connection failed while probing the instance, so "
                "the sidecar state is unknown (not necessarily broken). "
                "Wait for the connection to recover and retry."
            ),
            "port": port,
            "checks": [results[k] for k in
                       ("cloud_init", "service", "listening", "logs")],
        }

    cloud = results["cloud_init"]["output"].lower()
    service = results["service"]["output"].strip()
    listening = results["listening"]["output"].strip() == "yes"

    if "status: running" in cloud:
        cause = "cloud-init-running"
        summary = (
            "The instance is still running first-boot setup (cloud-init). "
            "The sidecar starts only after Docker and the NVIDIA toolkit "
            "finish installing, which can take a few minutes on first boot. "
            "Recheck shortly."
        )
    elif "status: error" in cloud:
        cause = "cloud-init-error"
        summary = (
            "First-boot setup (cloud-init) reported an error, so the sidecar "
            "may never have been installed. See the recent log below."
        )
    elif service == "failed":
        cause = "sidecar-crashed"
        summary = (
            "The sidecar service is installed but crashed. The recent log "
            "below usually names the reason."
        )
    elif service in ("activating", "inactive") or (
        service == "active" and not listening
    ):
        cause = "sidecar-starting"
        summary = (
            "The sidecar service is up but not yet listening on "
            f"127.0.0.1:{port}. Give it a moment and recheck."
        )
    elif service == "active" and listening:
        cause = "forward-transient"
        summary = (
            "The sidecar is healthy on the instance (running and listening). "
            "The dashboard's SSH port-forward likely failed transiently; "
            "retry the telemetry or files action."
        )
    else:
        cause = "unknown"
        summary = (
            "Could not classify the sidecar state from the instance. See the "
            "raw checks below."
        )

    return {
        "cause": cause,
        "summary": summary,
        "port": port,
        "checks": [results[k] for k in
                   ("cloud_init", "service", "listening", "logs")],
    }

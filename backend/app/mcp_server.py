"""Manifold MCP server — stdio bridge from any MCP client to the backend.

HARD RULE (enforced by a test that scans this file's imports): this module
talks to the backend over HTTP only. It imports nothing from the rest of
the application — no orchestrator, no database, no Lambda client — so there
is structurally no path around the backend's guards. An agent calling
launch_gpu hits the exact same budget/concurrency/region walls as the
dashboard's Launch button.

Every tool accepts an optional `note` (why the agent is doing this); each
call is recorded in the backend audit log (tool, args, note, result) and
shown on the dashboard's Agent Activity page.

Run: `uv run manifold-mcp` from backend/ (stdio transport).
Config: MANIFOLD_API_URL (default http://localhost:8000).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("MANIFOLD_API_URL", "http://localhost:8000")

mcp = FastMCP(
    "manifold",
    instructions=(
        "Manifold orchestrates Lambda Cloud GPU instances through a guarded "
        "local backend. Launches are asynchronous: launch_gpu returns a "
        "launch id immediately; then call wait_for_launch to block until it is "
        "'active' or 'failed' (one call, not a poll loop - large GPU instances "
        "can take 15-40 min to boot), or get_launch_status for a single "
        "snapshot. Termination may be blocked by a safety hook "
        "if unsaved files exist on the instance; sync_outputs saves them. "
        "Pass a short `note` with each call saying why — it lands in the "
        "audit log the user reviews."
    ),
)

# Injectable for tests: tests replace this with an ASGI-transport client
# aimed at an in-process app instance.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=API_URL, timeout=60.0)
    return _client


async def _audit(tool: str, args: dict, note: str, result: str) -> None:
    """Best-effort audit post; an unreachable backend already failed the
    real call, so audit failures must not mask the original error."""
    try:
        await _http().post("/audit/agent", json={
            "tool": tool, "args": args, "note": note, "result": result[:500],
        })
    except httpx.HTTPError:
        pass


async def _call(
    tool: str,
    method: str,
    path: str,
    *,
    note: str,
    args: dict[str, Any] | None = None,
    body: dict | None = None,
    params: dict | None = None,
    request_timeout: float | None = None,
) -> dict:
    """One guarded backend call + its audit entry. Backend rejections come
    back as {"error": <the backend's own message>} so the agent sees the
    same truth a human sees in the dashboard. Transport failures (backend
    down or restarting) additionally carry `unreachable: true`, so a caller
    can tell "the backend said no" from "the backend didn't answer".

    `request_timeout` overrides the client's default 60s ceiling for calls
    that legitimately hold the socket longer (the wait_for_launch long-poll
    parks server-side for up to 300s)."""
    args = args or {}
    try:
        resp = await _http().request(
            method, path, json=body, params=params,
            **({"timeout": request_timeout} if request_timeout else {}),
        )
        # A healthy backend answers JSON. A 500 that escaped the route,
        # though, is a Starlette plain-text "Internal Server Error" page —
        # calling .json() on that raises a cryptic "Expecting value" decode
        # error that would surface to the agent instead of the real status.
        # Fall back to the body text so the status-code branch below can
        # report something actionable.
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"detail": (resp.text or "").strip()[:300]
                       or f"HTTP {resp.status_code} (non-JSON response)"}
    except httpx.HTTPError as exc:
        result = {
            "error": f"Manifold backend unreachable at {API_URL}: {exc}",
            "unreachable": True,
        }
        await _audit(tool, args, note, result["error"])
        return result
    if resp.status_code >= 400:
        result = {"error": payload.get("detail", f"HTTP {resp.status_code}")}
        # Termination safety hook: return the evidence, not just the error.
        if payload.get("blocked"):
            result["blocked"] = True
            result["unpersisted_files"] = payload.get("unpersisted_files", [])
        await _audit(tool, args, note, f"rejected: {result['error']}")
        return result
    await _audit(tool, args, note, "ok")
    return payload


# -- instances -------------------------------------------------------------------


@mcp.tool()
async def list_launch_options(note: str = "") -> dict:
    """Launchable {instance_type, region, filesystem} targets Lambda can
    satisfy RIGHT NOW, ranked best-first. CALL THIS BEFORE launch_gpu: it is
    the only way to see which instance types have capacity in which regions,
    and it keeps you from guessing a region that has no capacity or no
    filesystem.

    A launch needs the three to line up — the type must have capacity in the
    region, and a persistent filesystem is region-locked, so it can only be
    used from its own region. Each returned target is a combination that lines
    up, so you can copy one straight into launch_gpu.

    `targets` is ranked: co-located with your EXISTING data first (a filesystem
    that already holds files, so a job runs next to what it reads/writes), then
    co-located with an empty filesystem, then scratch-only (capacity but no
    filesystem there — everything on it is ephemeral), and cheaper first within
    each band. A target's `filesystem` is null for a scratch-only launch; pass
    "" as launch_gpu's filesystem for those. `unavailable` lists types with no
    capacity anywhere right now (retry later or pick another from `targets`)."""
    return await _call(
        "list_launch_options", "GET", "/launch-options", note=note,
    )


@mcp.tool()
async def launch_gpu(
    instance_type: str,
    region: str,
    filesystem: str,
    connection_mode: str | None = None,
    note: str = "",
) -> dict:
    """Launch a GPU instance. Flows through ALL backend guards (budget,
    concurrency, region-filesystem match); a rejection returns the guard's
    message in `error`. Returns a launch record — poll get_launch_status
    with its `id` until status is 'active' (SSH up) or 'failed'.

    Call list_launch_options FIRST and pass one of its targets: it returns
    only {type, region, filesystem} combinations that have capacity right now
    and are co-located with your data, which avoids a blind region guess that
    fails on capacity or a region-filesystem mismatch."""
    body = {
        "instance_type": instance_type,
        "region": region,
        "filesystem": filesystem,
    }
    if connection_mode:
        body["connection_mode"] = connection_mode
    return await _call(
        "launch_gpu", "POST", "/instances",
        note=note, args=body, body=body,
    )


@mcp.tool()
async def get_launch_status(launch_id: str, note: str = "") -> dict:
    """Progress of an asynchronous launch: launching -> retrying (capacity)
    -> booting -> active | failed. Returns a stable `phase`
    (requesting_capacity | retrying_capacity | waiting_for_active | ready |
    failed | terminated), a human `phase_detail`, and `settled` (true once
    nothing more will change). While booting it also returns
    boot_elapsed_seconds / boot_timeout_seconds / boot_remaining_seconds.
    For a slow boot, prefer wait_for_launch: one blocking call instead of a
    poll loop."""
    return await _call(
        "get_launch_status", "GET", f"/launches/{launch_id}",
        note=note, args={"launch_id": launch_id},
    )


@mcp.tool()
async def wait_for_launch(launch_id: str, timeout: float = 120,
                          note: str = "") -> dict:
    """Block until a launch settles (active | failed | terminated) or up to
    `timeout` seconds (max 300) pass, then return the same enriched record as
    get_launch_status. This is the efficient way to await a slow SXM4 boot:
    ONE call parks server-side instead of dozens of get_launch_status polls.
    If it returns still booting (settled=false), the instance is fine - just
    call again to keep waiting. A backend restart mid-wait (dev --reload) is
    absorbed: the wait reconnects and keeps parking; the launch itself is
    resumed by the backend and keeps booting either way."""
    timeout = max(1.0, min(float(timeout), 300.0))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = max(1.0, deadline - loop.time())
        result = await _call(
            "wait_for_launch", "GET", f"/launches/{launch_id}/wait",
            note=note, args={"launch_id": launch_id, "timeout": timeout},
            params={"timeout": remaining},
            # The server parks up to `remaining`; give the socket headroom
            # past that so a legitimate long park is not misread as death.
            request_timeout=remaining + 15.0,
        )
        # A restart mid-park drops the socket. The launch is unharmed (the
        # backend resumes it on startup), so reconnect and keep waiting
        # instead of alarming the caller with a transport error.
        if not result.get("unreachable"):
            return result
        if loop.time() >= deadline:
            return {
                "status": "unknown",
                "settled": False,
                "phase": "backend_restarting",
                "phase_detail": (
                    "The Manifold backend was unreachable for the whole wait "
                    "window (it may be restarting). The launch itself is not "
                    "affected - the backend resumes in-flight boots on "
                    "startup. Call wait_for_launch again."
                ),
            }
        await asyncio.sleep(2.0)


@mcp.tool()
async def list_instances(note: str = "") -> dict:
    """Running instances with live status, SSH connection state, GPU type,
    region, and hourly rate."""
    return await _call("list_instances", "GET", "/instances", note=note)


@mcp.tool()
async def terminate_instance(
    instance_id: str, force: bool = False, note: str = ""
) -> dict:
    """Terminate an instance. With force=false the safety hook checks for
    unsaved files in ephemeral scratch first: if any exist, this returns
    blocked=true with the file list INSTEAD of terminating. Either call
    sync_outputs then retry, or pass force=true to accept the loss."""
    return await _call(
        "terminate_instance", "DELETE", f"/instances/{instance_id}",
        note=note, args={"instance_id": instance_id, "force": force},
        params={"force": str(force).lower()},
    )


@mcp.tool()
async def sync_outputs(instance_id: str, note: str = "") -> dict:
    """rsync the instance's ephemeral scratch to the persistent filesystem
    (ephemeral-backup/), over the managed SSH connection."""
    return await _call(
        "sync_outputs", "POST", f"/instances/{instance_id}/sync",
        note=note, args={"instance_id": instance_id},
    )


# -- jobs -----------------------------------------------------------------------


@mcp.tool()
async def list_templates(note: str = "") -> dict:
    """Job templates with parameter schemas (name, type, default, required).
    Templates run as Docker containers on the instance with the GPU attached."""
    return await _call("list_templates", "GET", "/templates", note=note)


@mcp.tool()
async def run_job(template: str, parameters: dict, note: str = "") -> dict:
    """Enqueue a job from a template. Parameters are validated against the
    template schema immediately. The job runs on the connected instance;
    poll get_job_status. Logs stream to get_job_logs."""
    return await _call(
        "run_job", "POST", "/tasks",
        note=note, args={"template": template, "parameters": parameters},
        body={"template": template, "parameters": parameters},
    )


@mcp.tool()
async def save_template(yaml_text: str, note: str = "") -> dict:
    """Create or update a CUSTOM job template from raw YAML, so a workflow
    you have proven by hand becomes a one-click recipe the user can rerun
    from the Jobs page without any agent involved. Validated exactly like
    bundled templates (image, command with {{param}} placeholders, parameter
    schema; volume mounts only under /workspace/ephemeral or {persistent};
    ports always loopback-bound). Returns the parsed template or the
    validation error. Prefer parameterizing over hardcoding: a template with
    good parameters serves the user forever."""
    return await _call(
        "save_template", "POST", "/templates/custom",
        note=note, args={"yaml": f"({len(yaml_text)} chars)"},
        body={"yaml": yaml_text},
    )


@mcp.tool()
async def delete_template(name: str, note: str = "") -> dict:
    """Delete a CUSTOM template by name. Bundled templates cannot be
    deleted; if the custom one was overriding a bundled name, the bundled
    version is restored."""
    return await _call(
        "delete_template", "DELETE", f"/templates/custom/{name}",
        note=note, args={"name": name},
    )


@mcp.tool()
async def run_command(instance_id: str, command: str, timeout: float = 120,
                      note: str = "") -> dict:
    """Run ONE shell command on the instance over the managed SSH connection
    and return {exit_code, stdout, stderr}. Full shell parity, but audited:
    every command lands in the user's activity log with its exit code, which
    raw SSH would not. Bounded by `timeout` (max 600s) - long-running work
    belongs in a job (run_job streams logs and survives backend restarts).
    Use this for the quick real commands in between: inspecting files,
    checking nvidia-smi, preparing directories."""
    return await _call(
        "run_command", "POST", f"/instances/{instance_id}/run",
        note=note, args={"instance_id": instance_id, "command": command[:200]},
        body={"command": command, "timeout": timeout},
    )


@mcp.tool()
async def get_job_status(task_id: str, note: str = "") -> dict:
    """Job state (queued|running|succeeded|failed), exit code, and the
    persistent output paths it writes to."""
    return await _call(
        "get_job_status", "GET", f"/tasks/{task_id}",
        note=note, args={"task_id": task_id},
    )


@mcp.tool()
async def get_job_logs(task_id: str, tail: int = 100, note: str = "") -> dict:
    """The last `tail` log lines of a job (live while it runs)."""
    return await _call(
        "get_job_logs", "GET", f"/tasks/{task_id}/logs",
        note=note, args={"task_id": task_id, "tail": tail},
        params={"tail": tail},
    )


# -- storage ---------------------------------------------------------------------


@mcp.tool()
async def list_filesystems(note: str = "") -> dict:
    """Persistent filesystems with their regions. Filesystems are
    region-locked: an instance can only mount one in its own region."""
    return await _call("list_filesystems", "GET", "/filesystems", note=note)


async def _connected_instance_for_fs(filesystem: str | None) -> tuple | None:
    """A connected instance that mounts `filesystem` (or any, if None), as
    (instance_id, filesystem_name). None when nothing suitable is connected.
    Lets file browsing ride the SSH connection with no S3 keys."""
    listing = await _call("list_persistent_files", "GET", "/instances", note="")
    for inst in listing.get("instances", []):
        if inst.get("connection_state") != "connected":
            continue
        mounts = inst.get("filesystems") or []
        if filesystem is None:
            if mounts:
                return inst["id"], mounts[0]
        elif filesystem in mounts:
            return inst["id"], filesystem
    return None


@mcp.tool()
async def list_persistent_files(
    prefix: str = "", filesystem: str | None = None, note: str = ""
) -> dict:
    """Browse one directory level of a persistent filesystem.

    Prefers a RUNNING instance: if one is connected and mounts the target
    filesystem, this browses over its SSH connection (via the sidecar, at
    local-disk speed and needing NO S3 keys) — the same path the dashboard's
    per-instance Files panel uses. It returns {source, filesystem, root, path,
    entries:[{name, is_dir, size_bytes, modified}]}. `prefix` is relative to
    the filesystem, e.g. "outputs/images".

    Only when no connected instance mounts the filesystem does it fall back to
    Lambda's S3 "Files" API — which CAN browse with nothing running, but needs
    S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY in .env and returns {filesystem,
    files:[...]}. If `filesystem` is omitted and exactly one exists (or one
    instance is connected), it is used."""
    # 1) Keyless path: a connected instance that mounts this filesystem.
    hit = await _connected_instance_for_fs(filesystem)
    if hit is not None:
        instance_id, fs_name = hit
        # The sidecar's persistent root is /lambda/nfs (the filesystem's
        # PARENT), so its first path segment is the filesystem name; prepend
        # it to keep `prefix` filesystem-relative like the S3 path.
        sub = "/".join(p for p in [fs_name, prefix.strip("/")] if p)
        result = await _call(
            "list_persistent_files", "GET",
            f"/instances/{instance_id}/files/list",
            note=note, args={"filesystem": fs_name, "prefix": prefix},
            params={"root_name": "persistent", "path": sub},
        )
        if "error" not in result:
            result["filesystem"] = fs_name
            result["source"] = f"instance:{instance_id} (ssh, no S3 keys)"
        return result

    # 2) S3 fallback (needs keys; works with no instance running).
    if filesystem is None:
        listing = await _call(
            "list_persistent_files", "GET", "/filesystems", note="",
        )
        names = [f["name"] for f in listing.get("filesystems", [])]
        if len(names) != 1:
            result = {
                "error": f"Specify `filesystem`; available: {', '.join(names) or '(none)'}"
            }
            await _audit("list_persistent_files",
                         {"prefix": prefix}, note, result["error"])
            return result
        filesystem = names[0]
    result = await _call(
        "list_persistent_files", "GET", "/storage/files",
        note=note, args={"filesystem": filesystem, "prefix": prefix},
        params={"filesystem": filesystem, "prefix": prefix},
    )
    # No S3 keys AND no connected instance: point at the keyless route.
    if result.get("error") and "credential" in result["error"].lower():
        result["hint"] = (
            "No S3 Files keys configured. Launch or connect an instance that "
            "mounts this filesystem, then this browses it over SSH with no keys."
        )
    return result


async def _pick_instance(instance_id: str | None, tool: str, note: str,
                         args: dict) -> str | dict:
    """Use the given instance, or auto-select when exactly one is connected."""
    if instance_id:
        return instance_id
    listing = await _call(tool, "GET", "/instances", note="", args={})
    connected = [i["id"] for i in listing.get("instances", [])
                 if i.get("connection_state") == "connected"]
    if len(connected) != 1:
        result = {"error": f"Specify `instance_id`; connected instances: "
                           f"{', '.join(connected) or '(none)'}"}
        await _audit(tool, args, note, result["error"])
        return result
    return connected[0]


@mcp.tool()
async def upload_file(local_path: str, remote_path: str = "inbox/",
                      instance_id: str | None = None, note: str = "") -> dict:
    """Upload a file from THIS machine to an instance over the managed SSH
    connection. remote_path ending in '/' keeps the filename; relative
    paths land on the persistent filesystem (surviving termination).
    If instance_id is omitted and exactly one instance is connected, it is
    used."""
    args = {"local_path": local_path, "remote_path": remote_path,
            "instance_id": instance_id}
    if not os.path.isfile(local_path):
        result = {"error": f"local file not found: {local_path}"}
        await _audit("upload_file", args, note, result["error"])
        return result
    target = await _pick_instance(instance_id, "upload_file", note, args)
    if isinstance(target, dict):
        return target
    try:
        with open(local_path, "rb") as fh:
            resp = await _http().post(
                f"/instances/{target}/files/upload",
                files={"file": (os.path.basename(local_path), fh)},
                data={"dest": remote_path},
            )
        payload = resp.json()
    except httpx.HTTPError as exc:
        result = {"error": f"upload failed: {exc}"}
        await _audit("upload_file", args, note, result["error"])
        return result
    if resp.status_code >= 400:
        result = {"error": payload.get("detail", f"HTTP {resp.status_code}")}
        await _audit("upload_file", args, note, f"rejected: {result['error']}")
        return result
    await _audit("upload_file", args, note,
                 f"ok: {payload.get('bytes', 0)} bytes -> {payload.get('path')}")
    return payload


@mcp.tool()
async def download_file(remote_path: str, local_path: str,
                        instance_id: str | None = None, note: str = "") -> dict:
    """Download a file from an instance to THIS machine over the managed
    SSH connection. Relative remote paths read from the persistent
    filesystem. If instance_id is omitted and exactly one instance is
    connected, it is used."""
    args = {"remote_path": remote_path, "local_path": local_path,
            "instance_id": instance_id}
    target = await _pick_instance(instance_id, "download_file", note, args)
    if isinstance(target, dict):
        return target
    try:
        async with _http().stream(
            "GET", f"/instances/{target}/files/download",
            params={"path": remote_path},
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                result = {"error": body[:300] or f"HTTP {resp.status_code}"}
                await _audit("download_file", args, note,
                             f"rejected: {result['error'][:150]}")
                return result
            parent = os.path.dirname(os.path.abspath(local_path))
            os.makedirs(parent, exist_ok=True)
            written = 0
            with open(local_path, "wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
                    written += len(chunk)
    except httpx.HTTPError as exc:
        result = {"error": f"download failed: {exc}"}
        await _audit("download_file", args, note, result["error"])
        return result
    await _audit("download_file", args, note,
                 f"ok: {written} bytes -> {local_path}")
    return {"local_path": local_path, "bytes": written}


def main() -> None:
    mcp.run()   # stdio transport


if __name__ == "__main__":
    main()

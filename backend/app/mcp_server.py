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
        "launch id immediately; poll get_launch_status until status is "
        "'active' or 'failed'. Termination may be blocked by a safety hook "
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
) -> dict:
    """One guarded backend call + its audit entry. Backend rejections come
    back as {"error": <the backend's own message>} so the agent sees the
    same truth a human sees in the dashboard."""
    args = args or {}
    try:
        resp = await _http().request(method, path, json=body, params=params)
        payload = resp.json() if resp.content else {}
    except httpx.HTTPError as exc:
        result = {"error": f"Manifold backend unreachable at {API_URL}: {exc}"}
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
    with its `id` until status is 'active' (SSH up) or 'failed'."""
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
    -> booting -> active | failed, with attempts and error detail."""
    return await _call(
        "get_launch_status", "GET", f"/launches/{launch_id}",
        note=note, args={"launch_id": launch_id},
    )


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


@mcp.tool()
async def list_persistent_files(
    prefix: str = "", filesystem: str | None = None, note: str = ""
) -> dict:
    """Browse persistent-filesystem files (works with no instance running).
    If `filesystem` is omitted and exactly one exists, it is used."""
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
    return await _call(
        "list_persistent_files", "GET", "/storage/files",
        note=note, args={"filesystem": filesystem, "prefix": prefix},
        params={"filesystem": filesystem, "prefix": prefix},
    )


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

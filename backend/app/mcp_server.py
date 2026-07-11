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


def main() -> None:
    mcp.run()   # stdio transport


if __name__ == "__main__":
    main()

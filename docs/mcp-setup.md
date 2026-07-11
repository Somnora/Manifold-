# Driving Manifold from an AI agent (MCP setup)

The MCP server lets any MCP-capable client — Claude Desktop, Claude Code,
or anything else that speaks the protocol — launch GPUs, run jobs, browse
storage, and shut instances down. Every tool call flows through the same
guarded backend as the dashboard: the budget cap, the concurrency limit,
the region check, and the termination safety hook all apply identically.
An agent cannot spend what you have not permitted.

## Prerequisites

The backend must be running (`uv run uvicorn app.main:create_default_app
--factory` from `backend/`, or mock mode with `MANIFOLD_MOCK=1`). The MCP
server is a thin bridge to it; if the backend is down, every tool returns
a clear "backend unreachable" error.

## Registering in Claude Code

From the repo root:

```bash
claude mcp add manifold -- uv run --directory "$(pwd)/backend" manifold-mcp
```

Then in any Claude Code session in this project: "launch a 1x A10 in
us-east-1 with the manifold-data filesystem" and watch it use the tools.

## Registering in Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(create the file if it does not exist), with YOUR absolute repo path:

```json
{
  "mcpServers": {
    "manifold": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/jamesmcshane/Desktop/Manifold/backend", "manifold-mcp"]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the hammer icon.

If the backend runs somewhere non-default, set the env var in the same
block: `"env": {"MANIFOLD_API_URL": "http://localhost:8000"}`.

## The tools

| Tool | What it does |
| --- | --- |
| `launch_gpu(instance_type, region, filesystem, connection_mode?)` | Launch through ALL guards; returns a launch id |
| `get_launch_status(launch_id)` | Poll: launching → retrying → booting → active/failed |
| `list_instances()` | Live instances + SSH connection state |
| `terminate_instance(id, force=false)` | force=false returns the unsaved-file list instead of terminating |
| `sync_outputs(instance_id)` | rsync ephemeral scratch → persistent filesystem |
| `list_templates()` | Job templates with parameter schemas |
| `run_job(template, parameters)` | Enqueue a job; validated immediately |
| `get_job_status(id)` / `get_job_logs(id, tail=100)` | Progress and live logs |
| `list_filesystems()` / `list_persistent_files(prefix)` | Persistent storage, no instance needed |

Every tool takes an optional `note` — one line of intent that lands in the
audit log. Everything an agent does is visible on the dashboard's **Agent
Activity** page as it happens.

## Worked example

You say to the agent:

> Transcribe everything in /inbox with whisper-large, then shut down.

A well-behaved session looks like this (all of it visible on Agent
Activity):

1. `list_templates(note="find transcription template")` → sees
   `whisper-batch` with parameters `input_dir`, `model_size`, `language`.
2. `list_filesystems()` → `manifold-data` in `us-east-1`; the inbox lives
   at `<filesystem>/inbox`.
3. `launch_gpu(instance_type="gpu_1x_a10", region="us-east-1",
   filesystem="manifold-data", note="GPU for whisper batch")` → launch id.
   If this had breached the budget or concurrency cap, the tool would have
   returned the guard's message and the agent would have to tell you no.
4. `get_launch_status(...)` polled until `active`.
5. `run_job("whisper-batch", {"input_dir": "inbox", "model_size":
   "large-v3"}, note="transcribe inbox")` → task id.
6. `get_job_status(...)` until `succeeded`; `get_job_logs(...)` to confirm;
   outputs recorded under `<filesystem>/transcripts`.
7. `terminate_instance(id, note="job done")` → **blocked**: the safety hook
   reports unsaved files in ephemeral scratch and the tool returns the list
   instead of terminating.
8. `sync_outputs(id, note="save outputs first")` →
   `terminate_instance(id, force=true, note="all synced")` → terminated.
   Billing stopped.

## Non-MCP agents

Anything that can speak HTTP can use the backend directly — the API the
MCP tools wrap is plain REST on localhost:8000 (see `backend/app/main.py`).
The guards live in the backend, so they hold for those clients too.

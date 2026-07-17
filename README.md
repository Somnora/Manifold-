# Manifold

Local orchestrator + web dashboard for Lambda Cloud GPUs. Treats GPU instances
as ephemeral compute: boot, mount persistent storage, run jobs, self-terminate.
Everything — launch, monitor, jobs, terminals, safe shutdown — runs through one
guarded FastAPI backend, driven from a dashboard or from any MCP-capable agent.

## Layout

- `backend/` — FastAPI orchestrator (Python 3.11+, SQLite, asyncssh, boto3)
- `dashboard/` — Next.js dashboard (Phase 2+)
- `templates/` — YAML job templates (Phase 3+)
- `DECISIONS.md` — running log of architectural decisions and why
- `CLAUDE.md` — build/run/test commands and conventions

## Quick start

```bash
cp .env.example .env   # fill in keys
cd backend
uv sync
uv run pytest          # all tests run against mocks; no live spend
uv run uvicorn app.main:create_default_app --factory --reload
# or, with no credentials and zero spend:
MANIFOLD_MOCK=1 uv run uvicorn app.main:create_default_app --factory --reload
```

See `CLAUDE.md` for the full command reference.

## Onboard your AI agent

Manifold is built to be driven by agents. Two steps:

1. Connect the MCP server (with the app or a dev backend running):

   ```bash
   claude mcp add manifold -- uv run --directory <path-to-repo>/backend manifold-mcp
   ```

2. The agent's first call should be the `get_skill` tool, which returns
   `docs/manifold-skill.md`: task recipes (launch, serve, batch, fine-tune,
   teardown) plus the rules that keep GPU work safe and cheap. The same
   document is served at `http://localhost:8000/skill`, and the desktop app
   bundles it, so agents on a machine with only the .dmg get it too.

The one rule, if the agent reads nothing else: go through Manifold, never
around it. Raw Lambda API calls and hand-rolled SSH lose the budget guards,
the audit trail, data rescue on termination, and job supervision.

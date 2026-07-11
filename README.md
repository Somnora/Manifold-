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

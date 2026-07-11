# Manifold — project conventions

Local orchestrator + dashboard for Lambda Cloud GPUs. FastAPI backend is the
single guarded gateway; dashboard and MCP server are thin clients of it.

## Commands

```bash
# Backend (from backend/)
uv sync                                                    # install deps
uv run pytest -q                                           # full test suite (mocks only, no spend)
uv run uvicorn app.main:create_default_app --factory --reload            # real mode (needs .env)
MANIFOLD_MOCK=1 uv run uvicorn app.main:create_default_app --factory     # mock mode, zero spend
```

Backend runs on :8000; dashboard (from `dashboard/`): `npm run dev` on :3000,
`npm run build` to typecheck/compile.

```bash
# Full mock demo (no credentials, no spend): from backend/ and dashboard/
MANIFOLD_MOCK=1 MANIFOLD_MOCK_CAPACITY_FAILURES=2 uv run uvicorn app.main:create_default_app --factory
npm run dev     # then open http://localhost:3000
```

## Layout

- `backend/app/` — FastAPI backend
  - `config.py` — .env (secrets) + config.yaml (tunables); never mix the two
  - `lambda_api.py` — `LambdaClient` interface + real (httpx) and mock clients
  - `connections.py` — `ConnectionManager` (mode swap point) + `ManagedConnection` (SSH supervisor)
  - `storage.py` — `StorageClient` interface + S3-adapter and mock backends
  - `orchestrator.py` — launch pipeline: validate → guards → retry → persist → connect; termination safety hook; sync
  - `cloud_init.py` — user-data generation (Docker, sidecar, Claude CLI, optional Tailscale)
  - `sidecar_client.py` — `SidecarClient` interface: real (SSH port forward + httpx) and mock
  - `model_client.py` — `ModelClient` interface: chat with a model served on the instance (vllm-serve) over the same forward pattern
  - `templates.py` — job-template registry; mount rules enforced at load
  - `task_queue.py` — `TaskQueue` interface + SQLite implementation
  - `dispatcher.py` — task push over SSH, idle auto-termination, capacity watches
  - `agent.py` — Autopilot: agent loop driven by a model served on an instance; fixed action allowlist
  - `db.py` — SQLite schema and queries
  - `main.py` — app factory + routes only; no business logic in routes
  - `mcp_server.py` — MCP stdio bridge; HTTP-only thin client (AST-enforced), run via `uv run manifold-mcp`
- `backend/tests/` — pytest; everything runs against mocks
- `sidecar/manifold_sidecar.py` — runs ON the instance, 127.0.0.1 only; embedded into cloud-init (metrics, unpersisted/recent files, fs browse/usage/delete)
- `templates/*.yaml` — job templates (vllm-serve, whisper-batch, axolotl-finetune, tao-train, sdxl-generate, gpu-smoke)
- `docs/` — user-facing guides (agent-on-gpu.md, mcp-setup.md, openai-proxy.md)
- `config.yaml` — guardrails, retry policy, SSH settings
- `.env` — secrets only (gitignored; template in `.env.example`)
- `DECISIONS.md` — every non-obvious choice gets an entry (what/alternatives/why)

## Hard rules

- No live spend in development: tests use `MockLambdaClient` exclusively.
  Real-instance testing happens manually at phase gates.
- All guards (budget, concurrency, region match, safety hooks) live in the
  backend/orchestrator. Clients may never contain business logic or a path
  around a guard.
- Nothing on a GPU instance listens on a non-loopback interface except sshd.
  All instance communication rides the managed SSH connection.
- Secrets stay in .env; never hardcode, log, or echo them.
- Connection modes (direct-ssh/tailscale) differ ONLY in the dial target.
  No endpoint, business logic, or UI may branch on mode beyond displaying it.

## Working style

- Phased delivery with hard gates: run verification, show results, wait for
  approval before the next phase. Commit at the end of each phase.
- Work on feature branches (`phase-N-...`), merge to `main` at approved gates.
- Prefer boring, readable code; the owner is learning from this codebase.
- Update DECISIONS.md whenever making a non-obvious choice.

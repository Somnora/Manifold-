# The local hub: brains, approvals, and a terminal on your own machine

Manifold's backend runs on your machine. The hub makes that side of the
system first-class: models running locally (or frontier APIs) can drive
the same guarded operations as a GPU-served model, actions that spend
money can wait for your explicit approval, and the dashboard gets a
terminal on the local box next to the ones on instances.

## Brains

A *brain* is any model that can drive an Autopilot run. Three kinds, one
OpenAI-compatible interface:

| Kind | Example | How it appears |
| --- | --- | --- |
| `instance:` | Qwen3.6 on your H100 | queue vllm-serve; appears when running |
| `local:` | llama3.1 via Ollama on your Mac | start Ollama/LM Studio; auto-detected |
| `cli:` | Claude via your Max subscription | log into the claude/codex/gemini CLI once; auto-detected |
| `api:` | Claude / GPT / Gemini | put the API key in .env; appears instantly |

- Local detection probes `127.0.0.1:11434` (Ollama) and `:1234`
  (LM Studio) for `/v1/models` - nothing to configure, results cached a
  few seconds. Endpoints are editable under `hub.local_endpoints`.
- CLI brains are the subscription path - "OAuth without the API bill".
  You log into each CLI once with the provider's OWN official OAuth
  (`claude`, `codex`, or `gemini` in any terminal); Manifold then invokes
  the CLI as a subprocess per turn and never sees a token. This is the
  ToS-clean way to use a Claude/ChatGPT/Gemini subscription from a
  third-party tool: impersonating the CLIs' OAuth client ids directly
  would violate provider terms and risk the ACCOUNT, so Manifold does not
  do it. (Anthropic and OpenAI have announced sanctioned "sign in with
  your subscription" programs for third-party apps; when those open up,
  a first-party OAuth flow can replace this cleanly.)
- API brains use each provider's OpenAI-compatible endpoint. Keys live in
  .env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) and are
  never stored anywhere else. No key -> the option simply is not offered.
- The Hub page lists everything currently available; the Autopilot page
  picks from the same list.

The safety model does not change with the brain: same fixed action
allowlist, same budget/concurrency/region guards, same step caps, same
audit trail. A frontier model gets no more power than a 4B local one.

## Approval-gated runs

Start a run with **Require my approval** on (the default) and the three
actions that spend money or destroy state - `launch_gpu`, `run_job`,
`terminate_instance` - pause as a pending card (Autopilot and Hub pages)
until you Approve or Deny:

- **Approve** -> the action executes through the normal guarded path.
- **Deny** -> the agent receives "DENIED by the user" as data and adapts.
- **No decision** -> after `autopilot.approval_timeout_seconds` (default
  10 min) it auto-denies, so a forgotten run never spends while you sleep.

Every request and decision is audited (`approval_requested`,
`approval_approved`, `approval_denied`).

## Local terminal

The Hub page embeds a login shell on the machine running the backend -
the same xterm panel the instances use, pointed at your own box. Use it
to prep datasets, run scripts, or drive the MCP tools without leaving the
dashboard.

Security posture: the backend listens on loopback only, and the terminal
WebSocket additionally enforces a strict `Origin` allowlist (localhost
only) because browsers permit cross-origin WebSocket connections that
CORS does not stop. Kill switch: `hub.local_terminal: false` removes the
endpoint entirely. Windows is not supported yet (POSIX pty).

## Pipelines this unlocks

- **Local orchestrator, cloud muscle**: llama3.1 on your Mac launches an
  H100, runs a fine-tune, syncs outputs, terminates - with each spend
  step approved by you.
- **Frontier reviewer**: Claude drives a distillation run end to end
  (docs/distill-your-own-model.md), reading job logs and adapting.
- **GPU to GPU**: a model served on instance A directing jobs on
  instance B (per-instance dispatch, Phase 35).

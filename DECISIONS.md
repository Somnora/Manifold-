# Decisions

A running log of non-obvious architectural and implementation choices: what
was decided, what the alternatives were, and why. Written for someone
learning backend development. Newest entries at the bottom.

---

## 2026-07-10 — Verify the Lambda API from its OpenAPI spec, not blog posts

**Decided:** Implement `RealLambdaClient` against the machine-readable spec at
`https://cloud.lambda.ai/api/v1/openapi.json` (v1.10.0), and record the facts
we depend on in `lambda_api.py`'s docstring.

**Alternatives:** Trust tutorials/HOWTOs, or the older
`cloud.lambdalabs.com` domain (it now 301-redirects; the spec marks it
deprecated).

**Why:** API details drift, and this project's error handling hangs on exact
strings — e.g. capacity failures are identified by the error code
`instance-operations/launch/insufficient-capacity` inside an
`{"error": {code, message, suggestion}}` envelope. Reading the source of
truth once beats debugging a mystery later. Lesson: when a vendor publishes
an OpenAPI spec, treat it as the contract.

## 2026-07-10 — Interfaces + injected dependencies at every external boundary

**Decided:** Three seams, each an abstract interface with a real and a mock
implementation: `LambdaClient` (Lambda API), `StorageClient` (S3 adapter),
and the `connect_fn` hook inside `ManagedConnection` (SSH dialing).
`create_app()` accepts any of them as arguments.

**Alternatives:** Call `httpx`/`boto3`/`asyncssh` directly where needed and
monkeypatch in tests; or use a mocking library like `responses`/`moto`.

**Why:** The project rule is "no live spend during development" — the whole
test suite and the dashboard must run against fakes. An explicit interface
makes the fake a first-class citizen (the mock dashboard mode uses the same
`MockLambdaClient` as the tests) instead of test-only patch magic. It also
documents exactly which slice of each vendor API we use.

## 2026-07-10 — Launch endpoint returns 202 immediately; the pipeline runs in the background

**Decided:** `POST /instances` does validation and guard checks synchronously
(so rejections are instant and explicit), then persists a `launches` row and
runs retry → boot-wait → SSH-connect as an `asyncio` background task. Clients
poll `GET /launches/{id}` to watch `launching → retrying → booting → active`
(or `failed`, with the error message preserved).

**Alternatives:** (a) Block the HTTP request until the instance is live —
but capacity retries plus booting can take many minutes, and browsers/proxies
time out. (b) A job queue like Celery/Redis — banned by the stack rules and
overkill for a single-user local tool.

**Why:** The status lives in SQLite, so retry progress survives page reloads
and is trivially renderable by the dashboard ("never fail silently"). This is
the standard "accepted for processing" pattern behind HTTP 202.

## 2026-07-10 — Guards compute against LIVE Lambda state, not our database

**Decided:** The concurrency and budget guards list instances from the Lambda
API at request time and sum `price_cents_per_hour` over everything still
billable (`booting`/`active`/`unhealthy`).

**Alternatives:** Track running instances in SQLite and check that.

**Why:** Our DB only knows about launches made through Manifold. If an
instance was launched from Lambda's own console (or a previous Manifold run
crashed), a DB-based guard would happily overspend. The API is the source of
truth for money; our DB is just history. Costs one extra API call per launch.

## 2026-07-10 — Plain stdlib sqlite3 behind a lock, not an ORM or async driver

**Decided:** `db.py` uses `sqlite3` with `check_same_thread=False`, one
`threading.Lock`, WAL mode, and hand-written SQL.

**Alternatives:** SQLAlchemy (ORM), aiosqlite (async driver).

**Why:** This is a single-user local tool writing a few rows per launch;
every statement runs in microseconds, so briefly blocking the event loop is
harmless. An ORM adds a dependency and a layer of indirection to learn for
five queries. If contention ever appears, swapping in aiosqlite is contained
in one file. Boring and readable wins.

## 2026-07-10 — ConnectionManager = "where to dial" and nothing else

**Decided:** The `ConnectionManager` interface has exactly one method,
`dial_target(instance) -> host`. `direct-ssh` returns the public IP;
`tailscale` (Phase 3) will return the tailnet IP. Everything above the dial —
`ManagedConnection`, terminals, forwards, rsync — is shared code that never
branches on mode.

**Alternatives:** Subclass the whole connection stack per mode, or sprinkle
`if mode == "tailscale"` through the codebase.

**Why:** The spec demands the mode be "a swap point only". Making the
interface one method wide makes violations impossible to hide: if a feature
needs anything mode-specific beyond the address, the design is wrong and the
compiler-of-code-review catches it. Small interfaces are how you keep a swap
point honest.

## 2026-07-10 — One supervisor task per SSH connection; reconnect forever with capped backoff

**Decided:** `ManagedConnection.start()` spawns a supervisor coroutine:
connect, wait for the connection to close, reconnect with exponential backoff
(base 1s doubling to a 30s cap), repeat until deliberately closed. Instance
lifecycle ("active" per Lambda) and connection state ("connected" per us) are
tracked and displayed separately.

**Alternatives:** Reconnect only on demand when a command fails; give up
after N reconnect attempts.

**Why:** GPU boxes reboot, networks blip, and sshd comes up a beat after the
instance reports "active". A supervisor gives one place where state
transitions happen, which makes the dashboard's connection badge trustworthy.
We never give up because the fix for "instance is really gone" is
termination, which closes the connection object — not a silent timeout that
leaves a zombie card. The capped backoff keeps a dead host from being hammered.

## 2026-07-10 — Retry semantics: one attempt = one pass through (type + fallbacks)

**Decided:** Per attempt, try the requested type, then each fallback in
order; only `insufficient-capacity` moves to the next candidate. Between
attempts, exponential backoff (5s base, 120s cap), max 5 attempts. Any
non-capacity error fails the launch immediately. Fallbacks that would break
the budget guard are dropped at admission time, before the row is created.

**Alternatives:** Exhaust all 5 retries on the primary type before touching
fallbacks (slower to get a GPU); treat all errors as retryable (hides real
problems like quota or bad parameters behind minutes of pointless retries).

**Why:** The user's intent is "give me a usable GPU soon, prefer this type" —
cycling candidates each round gets capacity fastest while preserving
preference order. Budget-filtering fallbacks up front keeps the guard
absolute: no code path launches an over-budget type. The launched type and
its real hourly rate are recorded separately from the requested type, so cost
history stays honest.

## 2026-07-10 — `known_hosts=None` for instance SSH (trade-off, revisit)

**Decided:** The managed connection accepts the host key of a freshly booted
instance without verification.

**Alternatives:** Pin host keys by fetching them via the Lambda API (not
offered), or TOFU-persist the first-seen key per instance.

**Why:** A brand-new cloud instance has a brand-new host key, so there is
nothing to check it against on first contact; strict checking would just
break every launch. Persisting the first-seen key per instance id (proper
TOFU) is cheap hardening once instance identity matters — noted for Phase 3
when cloud-init could report the key out-of-band.

## 2026-07-10 — Billing timestamps: `launched_at` vs `active_at`

**Decided:** The `launches` table records both when Lambda accepted the
launch (`launched_at` — billing starts here) and when the instance became
reachable (`active_at`). Cost history uses `launched_at → terminated_at`.

**Alternatives:** One timestamp for "started".

**Why:** Lambda bills from launch acceptance, including boot time. Computing
cost from `active_at` would systematically undercount by a few minutes per
launch. Small thing, but the History page's numbers should survive
comparison with the real invoice.

## 2026-07-10 — App is built by a factory; uvicorn runs it with `--factory`

**Decided:** No module-level `app` object. `create_app()` takes settings and
injected clients; `create_default_app()` reads `MANIFOLD_MOCK` and is run as
`uvicorn app.main:create_default_app --factory`.

**Alternatives:** The common `app = FastAPI()` at module scope.

**Why:** A module-level app would construct the real Lambda client at import
time, so merely importing `app.main` (as every test does) would demand
credentials. The factory pattern keeps construction explicit, makes tests
first-class (each test builds its own app with mocks), and gives mock mode a
clean switch. Rule of thumb: side effects at import time eventually bite.

## 2026-07-10 — S3 adapter specifics worth writing down

**Decided:** `S3AdapterStorage` dials `https://files.<region>.lambda.ai`,
uses the filesystem's UUID (`id`) as the bucket name, and sets boto3's
checksum calculation/validation to `when_required`.

**Why:** All three facts come from Lambda's S3-adapter docs and are easy to
get wrong: the bucket is NOT the filesystem's human name, and without the
checksum settings newer boto3 versions send checksum headers the adapter
answers with `NotImplemented`. Recording them here saves the next debugging
session.

## 2026-07-10 — Dashboard polls the backend; no websockets, no data library

**Decided:** Client components fetch from the backend on a 2-5s interval via
one small `usePolling` hook. No SWR, no react-query, no websocket layer for
page state. Plain `fetch` with typed wrappers in `lib/api.ts`.

**Alternatives:** SWR/react-query (dependency for caching we don't need on a
localhost API with tiny payloads); server-sent events or websockets (real
push, but a second transport to build and debug before Phase 3 actually
needs one for telemetry streaming).

**Why:** The backend already persists every state transition, so polling
`GET /instances` + `GET /launches` renders the truth within two seconds —
good enough for a human watching a launch. Fewer moving parts now; the
websocket work arrives in Phase 3 where it pays for itself (live GPU
telemetry). Rule applied: add realtime transport when the data is realtime,
not for status badges.

## 2026-07-10 — The launch form contains zero rules

**Decided:** The form does not pre-validate region matches or budgets. It
auto-fills the region when a filesystem is picked (pure convenience, still
editable) and submits whatever the user chose; backend rejection messages
are displayed verbatim.

**Alternatives:** Duplicate the guard logic client-side for instant feedback.

**Why:** Project rule — guards live beneath all clients, and clients contain
no business logic. Duplicated validation drifts: the day the backend guard
changes, a client-side copy would lie. Showing the backend's own message
also proves at demo time that the dashboard and any future MCP agent hit the
identical wall.

## 2026-07-10 — Capacity-retry demo via env var, not a demo endpoint

**Decided:** `MANIFOLD_MOCK_CAPACITY_FAILURES=N` (mock mode only) scripts N
insufficient-capacity errors into the mock client at startup.

**Alternatives:** A `/debug/fail-next-launch` endpoint; a magic instance
name that always fails.

**Why:** Failure injection stays in process wiring, not in the API surface —
a debug endpoint would be one more thing clients could hit and one more
branch in production code. The env var reuses the same `scripted_launch_errors`
mechanism the tests use, so the demo exercises the exact code path the test
suite covers.

## 2026-07-10 — SSH key is chosen per launch, validated against Lambda's registry

**Decided:** `GET /ssh-keys` lists the account's registered key names; the
launch form offers them in a dropdown, `POST /instances` takes an optional
`ssh_key_name`, and `config.yaml`'s `ssh.key_name` is only the fallback
default. The orchestrator rejects (400) any key name not registered with
Lambda before calling the launch API.

**Alternatives:** Config-file-only (original Phase 1 design — user feedback:
"nowhere to enter the SSH information"); free-text input (invites typos that
would surface minutes later as a launch failure).

**Why:** The key must exist in Lambda's registry for the launch call to
succeed, so the honest UI is a dropdown of exactly those names. Validating
membership at admission keeps failures at the cheap end of the pipeline.
Note the private key path for the backend's own SSH client remains in
config.yaml; only the key NAME travels with a launch.

## 2026-07-10 — No prices on the Instances page (user decision)

**Decided:** The launch form and instance cards show GPU identity
(description like "1x A10 (24 GB PCIe)") and no hourly prices. The budget
guard still runs on live API prices; the History page keeps its cost column
(a spec deliverable) computed from Lambda-reported rates.

**Why:** Owner feedback at Gate 2: the mock-mode canned prices read as wrong
data, and the cards' job is tracking which GPU is which, not accounting.
Prices remain in the API responses so guards and history stay honest; the
dashboard just stops advertising them where they add noise.

## 2026-07-10 — Post-mortem: orphaned dev servers and a stale .next cache

**What happened:** Demo servers started in the background were never
actually killed (shell job tables do not survive between tool invocations),
so port 8000 was still taken when the owner ran the backend ("address
already in use"), and a `next build` ran while the orphaned dev server had
`.next` open — the mixed cache produced "module not found in the React
Client Manifest" errors on every page.

**Rule going forward:** kill dev processes by port (`lsof -ti :8000 :3000 |
xargs kill`), never by job id; and if dev-server behavior looks impossible,
`rm -rf .next` before deeper debugging.

## 2026-07-11 — Sidecar ships inside cloud-init, not fetched at boot

**Decided:** `build_user_data()` embeds the sidecar's source verbatim in the
cloud-init script (heredoc into /opt/manifold), runs it under systemd as a
loopback-only service.

**Alternatives:** Fetch from a URL at boot (needs somewhere public to host
it, plus a supply-chain surface); scp it after SSH comes up (adds a
provisioning step that can race jobs).

**Why:** The sidecar is one file far under Lambda's 1 MB user-data cap. What
an instance runs is exactly what this commit contains, the instance needs no
extra credentials or network fetch, and the version question ("which sidecar
is on that box?") answers itself: the one the launching backend shipped.

## 2026-07-11 — Tailscale dial target is the MagicDNS hostname

**Decided:** A tailscale-mode launch names the instance `manifold-<launch_id>`,
cloud-init joins the tailnet with that hostname (`tailscale up --ssh
--hostname=...`), and `TailscaleConnectionManager.dial_target()` returns the
instance name. The contract test asserts both managers expose exactly
{mode, dial_target} — nowhere for mode-specific logic to hide.

**Alternatives:** Query the tailnet for the node's 100.x.y.z IP via the
local `tailscale` CLI or Tailscale's API (extra dependency and credentials;
the IP is just what MagicDNS resolves anyway).

**Why:** The orchestrator host is on the tailnet, so the hostname resolves
like any other address — dialing a name keeps the swap point one line and
zero new dependencies. asyncssh does not care whether it dials an IP or a
name; everything above the dial stays byte-identical.

## 2026-07-11 — Safety hook is evidence, not a lock

**Decided:** `terminate(force=False)` asks the sidecar for unpersisted
ephemeral files and blocks with the file list (HTTP 409) if any exist. But
if the sidecar is unreachable — instance still booting, connection down,
orphan instance launched outside Manifold — termination proceeds.

**Alternatives:** Refuse to terminate whenever the check cannot run.

**Why:** The hook's job is preventing accidental data loss, not preventing
termination. A hard requirement would make an unhealthy or half-booted
instance unkillable from the dashboard while it bills by the minute — a
worse failure than losing scratch files the user was warned are ephemeral.
force=true remains the explicit override either way, and sync-then-terminate
is the safe path the dashboard offers first.

## 2026-07-11 — Telemetry: WS to the browser, polling over the SSH forward

**Decided:** The browser gets a real WebSocket from the backend
(`/instances/{id}/metrics/stream`). Behind it, `RealSidecarClient` polls the
sidecar's GET /metrics through a per-call SSH local port forward every 2s,
rather than holding a second long-lived WS through the tunnel.

**Alternatives:** Proxy the sidecar's own WS end-to-end through the forward
(same data rate, but a long-lived forward + WS client to supervise through
every SSH reconnect).

**Why:** The payload is a few hundred bytes every 2 seconds; polling over
the already-supervised managed connection delivers identical freshness with
one less stateful thing to babysit. The sidecar keeps its WS endpoint (it
costs nothing and a future client may want it); the browser-facing contract
is a WS either way, so swapping the internals later touches one class.

## 2026-07-11 — Template placeholders validated at load, ports forced to loopback

**Decided:** Templates are validated when loaded, not when run: every
`{{placeholder}}` in a command must be a declared parameter, every host
mount must start with `/workspace/ephemeral` or `{persistent}`, and declared
ports are published on 127.0.0.1 by the dispatcher regardless of what the
template says. Broken templates are surfaced in GET /templates' `errors`
map instead of silently vanishing.

**Why:** Load-time failure puts the error in front of the person editing
YAML, not the person dispatching a job hours later. The loopback rule is
enforced in the dispatcher (one place) rather than trusted to each template,
consistent with "nothing on the instance listens publicly except sshd."

## 2026-07-11 — Tasks validate twice: at enqueue and at dispatch

**Decided:** `POST /tasks` runs the template's parameter validation
immediately (bad requests fail with 422 at the door), and the dispatcher
re-runs it before rendering the docker command.

**Alternatives:** Validate only at dispatch (a typo sits silently in the
queue until an instance connects, maybe minutes later); only at enqueue
(the template YAML can change between enqueue and dispatch).

**Why:** The person is present at enqueue time — that is when an error is
cheap. The dispatch-time recheck covers the gap where a template was edited
or deleted while tasks were queued. Same principle as the launch guards:
fail at the earliest moment the failure is knowable.

## 2026-07-11 — One task at a time; a running task pins the instance

**Decided:** The dispatcher runs a single task at once, and the idle loop
skips entirely while any task is running. Idle = connected + no running
task + no activity (job or terminal) for the timeout, with the clock seeded
at connection time and reset by every dispatch/completion.

**Alternatives:** Concurrent tasks per instance (GPU contention chaos for
no benefit at max_concurrent_instances=1); counting time-since-last-log as
activity (a long quiet training epoch would read as idle — wrong).

**Why:** Serialized tasks match the one-GPU-instance reality and make logs,
idle logic, and failure attribution trivially understandable. The
"running task = alive" rule is the conservative one: better to keep a box
an hour too long than kill a fine-tune at 90%.

## 2026-07-11 — Idle termination reuses the safety hook, then syncs, then forces

**Decided:** Idle auto-termination calls the standard `terminate(force=False)`.
If the Phase 3 hook blocks (unpersisted files), the dispatcher syncs
ephemeral → persistent and retries with force=true; every step lands in the
audit log (demonstrated live at the gate: idle_termination → idle_sync →
terminated in one trace).

**Alternatives:** Idle-terminate with force=true directly (defeats the whole
point of the hook — unattended termination is exactly when data loss
happens); block and wait for a human (the machine bills all night).

**Why:** Unattended is when the safety hook matters most, and sync-then-
terminate is the only resolution that needs no human. The files end up in
`<filesystem>/ephemeral-backup/`, the box stops billing, and the audit log
tells the story next morning.

## 2026-07-11 — Capacity watches: notify by default, auto-launch double-gated

**Decided:** (James's feature request at Gate 3.) `POST /watches` registers
an instance-type + region watch; the dispatcher polls the catalog and flips
the watch to "available" when capacity appears. Auto-launch requires BOTH
`auto_launch` on the watch AND `watches.auto_launch_enabled: true` in
config.yaml, and goes through `request_launch` — budget, concurrency, and
region guards all apply (test-proven: an over-budget auto-launch watch sees
capacity but is refused, with the rejection audited).

**Alternatives:** Notify-only (capacity at 3am is gone by 8am — the user
called this "a game changer" precisely because reacting manually loses the
race); auto-launch by default (spending money unattended should need two
deliberate switches, not one checkbox).

**Why:** The double gate splits "what I want" (per watch) from "what I
permit" (global config), so an experimenting user cannot accidentally
arm unattended spending. Routing through the normal launch pipeline means a
watch can never become a guard bypass.

## 2026-07-11 — Terminal protocol: JSON control frames in, raw text out

**Decided:** The browser terminal WS sends JSON messages
(`{type: "input"|"resize", ...}`) and receives raw text frames of terminal
output. The backend bridges to an asyncssh PTY session
(`create_process(term_type=..., term_size=...)`) on the managed connection.

**Alternatives:** Binary frames both ways with a framing byte (what ttyd
does — more efficient, more code); running ttyd/gotty on the instance
(banned: nothing may listen publicly except sshd, and a web terminal
service is exactly the kind of thing that gets left running).

**Why:** Two message kinds do not justify a binary protocol on a localhost
link. Raw-text-out means the server needs no envelope parsing on the hot
path, and xterm.js consumes it directly. The mock shell sits behind the
identical bridge code — only the dialed connection object differs — so the
gate demo exercises the real WS handler, not a lookalike.

## 2026-07-11 — Mock shell instead of a local Docker container for the gate demo

**Decided:** `MockSSHConnection.create_process()` returns a tiny scripted
shell (prompt, echo, canned nvidia-smi/claude outputs). The Gate 5 demo and
tests drive the real WS bridge against it.

**Alternatives:** Run a local sshd container and have the backend really
SSH into it (closer to production, but adds a Docker dependency to the test
suite and still would not have a GPU, so nvidia-smi would fail anyway).

**Why:** The thing worth testing is Manifold's bridge: WS handling, PTY
plumbing, resize propagation, activity touching, teardown. That code is
byte-identical in mock and real mode. What a real sshd would additionally
prove (asyncssh's own PTY support) is upstream-tested. Real-instance
verification happens at the manual phase gate like every other phase.

## 2026-07-11 — Recent-files view: bounded sidecar walk, not inotify

**Decided:** (For James's "see files being added/moved/produced" ask.) The
sidecar's GET /storage/recent walks ephemeral + persistent roots, returns
files modified in the last N hours (default 24), newest first, hard-capped
at 20k entries scanned / 50 returned, with a `truncated` flag. Dashboard
polls it every 5s while the Files panel is open.

**Alternatives:** inotify/watchdog for true event streaming (persistent
storage is NFS, where inotify does not see remote writes — it would
silently miss exactly the files jobs produce); rsync --list-only diffing
(stateful, more moving parts).

**Why:** A bounded mtime walk is stateless, works identically on NFS, and
5-second freshness is plenty for "is my job producing outputs?". The scan
cap keeps a million-file HF cache from wedging the sidecar; the truncated
flag keeps the cap honest instead of silent.

## 2026-07-11 — TAO support is a template, not a feature

**Decided:** NVIDIA TAO Toolkit support ships as `templates/tao-train.yaml`
(task entrypoint + spec file + results dir, all on persistent storage), not
as dedicated backend/frontend code.

**Why:** This is the Gate 4 design paying rent: "TAO made easy" required
zero code because templates are data. The same holds for the next toolkit
James wants — the answer is a YAML file. If a workflow ever genuinely needs
new capability (e.g. multi-node), that is when code gets written.

## 2026-07-11 — MCP thinness is enforced by an import-allowlist test, not review

**Decided:** `mcp_server.py` may import exactly `os`, `typing`, `httpx`,
and the MCP SDK — a test parses the module's AST and fails on anything
else. The module lives in the same package as the backend for packaging
convenience (`manifold-mcp` console script), but structurally it can only
speak HTTP to the backend.

**Alternatives:** A separate package/venv for true physical isolation
(more honest still, but adds a second lockfile and install step to a
single-user local tool); code review as the enforcement mechanism (decays
the first time someone adds "just one" convenience import).

**Why:** "The MCP server is a thin client with no path around guards" is
the spec's hard rule; a rule is only real if a machine checks it. The AST
test turns an architectural intention into a failing build. Guard parity
is additionally proven behaviorally: the same over-budget launch through
the dashboard's HTTP path and the MCP tool returns byte-identical
rejection text.

## 2026-07-11 — MCP tools return errors as data, not protocol exceptions

**Decided:** Backend rejections come back to the agent as
`{"error": <the backend's exact detail>}` (plus `blocked` +
`unpersisted_files` for the termination hook) rather than raised MCP
errors. Every tool takes an optional `note`, and every call — success or
rejection — is POSTed to `/audit/agent` and shown on the Agent Activity
page. Audit posting is best-effort: an unreachable backend already failed
the real call, so a failed audit write must not mask the real error.

**Alternatives:** Raise protocol-level errors (clients render them
inconsistently, and several truncate the message — the guard's dollar math
is the most useful part); make audit writes mandatory (turns a logging
hiccup into a tool failure the agent then retries, double-logging).

**Why:** An agent that can read "Budget guard: … would bring hourly spend
to $22.32, over the $4.00 limit" can explain to its human exactly why it
stopped, or pick a cheaper GPU. Error-as-data with the backend's own words
keeps agents and humans looking at the same truth; the audit trail makes
the agent's whole session reviewable after the fact.

## 2026-07-11 — Phase 7: first-run setup through the dashboard

**What happened:** James started real mode with no .env; the backend
crashed at import of the real client and the dashboard showed blank
dropdowns with no explanation. Root cause: credentials were file-only and
the failure mode was silent.

**Decided:** Three pieces. (1) Real mode without a key now boots into an
`UnconfiguredLambdaClient` — every Lambda-backed endpoint returns 503 with
"No Lambda API key configured. Open the dashboard's Settings page…", and
the Instances page shows a banner linking to Settings. (2) A Settings page
accepts the key once, the backend VALIDATES it against the live Lambda API
before saving (an invalid key is rejected with Lambda's own message and
never persisted), writes it to .env preserving comments, and (3) hot-swaps
the running client through a `SwappableLambdaClient` wrapper — the launch
form goes live without a restart.

**Alternatives:** Keep .env-only setup with better docs (still fails the
"someone without Lambda knowledge" test); store secrets in SQLite or
browser localStorage (violates the secrets-live-in-.env rule and secret
hygiene — the browser never holds the key, it passes through one POST and
is never echoed back); require a restart after saving (simpler, but the
first-run experience should end with a working launch form, not another
terminal step).

**Why:** The dashboard is the product's front door; the first five minutes
should not require knowing what dotenv is. Validation-before-save turns
"why are my dropdowns empty an hour later" into "Lambda rejected this key"
at paste time. Secret hygiene held: booleans and counts in /settings/status,
key never logged, never audited, never returned.

## Phase 8 — reconnect-on-startup (2026-07-11)

**What:** On startup the backend calls `orchestrator.adopt_running_instances()`,
which lists live Lambda instances and re-establishes a `ManagedConnection` to
any that are `active` with an IP and not already tracked. Connection mode comes
from the launch history row, falling back to `default_connection_mode` for
instances launched outside Manifold.

**Why:** Before this, restarting the backend orphaned every running instance —
it kept billing on Lambda but the dashboard showed it `disconnected` with no way
to reconnect, forcing a terminate-and-relaunch. Surfaced live when a wrong SSH
key (config pointed at `id_ed25519` instead of the launch key) left an instance
stuck `reconnecting`; the only recovery was a restart, which then orphaned it.

**Design:** Best-effort — an unconfigured or unreachable Lambda client logs and
returns 0 rather than blocking boot (a startup hook must never crash the app).
Reuses the exact launch-path connection code via a shared `_open_connection`
helper, so an adopted connection is byte-identical to a freshly launched one
(same terminal, telemetry, idle detection). Adoptions are audited.

**Alternatives considered:** Persisting connection objects across restarts
(impossible — SSH sockets don't survive a process). Storing "last known
instances" in SQLite and trusting it (rejected: Lambda is the source of truth;
an instance may have been terminated out-of-band, so we must re-list live).

## Phase 9 — guided launch form + full region catalog (2026-07-11)

**What:** The launch form now walks GPU -> region -> filesystem. GPUs list
available types first (cheapest to priciest), out-of-capacity ones greyed and
unselectable. Region options are driven by the chosen GPU: regions with
capacity for it are selectable (a region where you already have a filesystem
wins ties), the rest are greyed with "not available for this type". Filesystem
narrows to the selected region. Added the full 12-region NA catalog with human
names (Virginia, Arizona, ...) and a `GET /regions` endpoint serving the whole
region universe so the form can grey out what a GPU can't use.

**Why:** James kept building invalid combinations (a us-east-1 filesystem with a
region the GPU wasn't in, or picking a region blindly) and hitting backend
rejections after the fact. Mirroring Lambda's own console flow — pick the GPU,
then see only the regions that GPU can actually run in — makes the invalid
combination unrepresentable in the UI. The backend guards stay the final
authority; this just stops the user reaching them by accident.

**Design:** `/instance-types` shape is unchanged (WatchPanel still consumes it);
region names live in a separate `/regions` endpoint so nothing breaks. Native
`<select>` with `disabled` options does the greying — no custom dropdown,
matches the console, stays readable. Auto-selection fills sensible defaults
(cheapest available GPU; a region where a filesystem exists) but never fights
an explicit choice.

## Phase 10 — in-dashboard chat with a served model (2026-07-11)

**What:** A Chat button on connected instance cards. `GET
/instances/{id}/model` reports whether a model server is live (a running
task whose template publishes a port — vllm-serve today) and which model.
`POST /instances/{id}/chat` relays an OpenAI-style chat completion to the
model's loopback port over an SSH local port forward and streams the SSE
response straight through to the browser. New `ModelClient` seam
(real = per-call port forward + httpx streaming, mock = canned SSE chunks),
mirroring `SidecarClient` exactly.

**Why:** James's original vision: download a HuggingFace model and talk to
it inside the dashboard. vllm-serve already served the model on the
instance's loopback; this adds the one missing hop, browser -> backend ->
SSH forward -> vLLM, without opening any new listener anywhere.

**Design choices:**
- Discovery, not registration: "a model is being served" is derived from
  the task queue (running task + template with ports), so there is no
  separate serving state to drift out of sync. Kill the job, chat closes.
- The relay passes vLLM's SSE bytes through untouched instead of
  re-encoding: the browser parses the standard OpenAI chunk format, and
  mid-stream failures are surfaced as a data: {"error": ...} event rather
  than a silently truncated reply.
- Chat traffic counts as activity (touch_activity per chunk), and a
  serving task already pins the instance alive via the running-task rule,
  so a conversation can't be idle-terminated mid-reply.
- Every chat call is audited (instance, message count, model) — message
  CONTENT is deliberately not logged.
- Known quirk: vllm-serve's `port` parameter changes the container port
  while the loopback mapping stays 8080:8080; chat uses the host side of
  the mapping, which is what the dispatcher actually publishes, so it
  works regardless. Cleaning up that parameter is cosmetic backlog.

## Phase 11 — Autopilot: a self-hosted model drives Manifold (2026-07-11)

**What:** Agent runs. Pick a brain (any instance serving a model via
vllm-serve), give a goal, and the backend runs the loop: send conversation
to the brain over the managed SSH connection -> expect ONE JSON action ->
execute it against Manifold's own guarded operations -> feed the observation
back. GPU A literally manages GPU B. New Autopilot page shows every run and
step live, with cancel; steps also land in Agent Activity under actor
"autopilot". Also: instances terminated out-of-band are now reconciled away
(card dropped, SSH supervisor reaped, history row closed), and the dashboard
marks stale data as stale when the backend stops answering (James hit both).

**Why this shape (the honest version):**
- A strict one-JSON-action-per-turn protocol instead of OpenAI tool-calls:
  vLLM's native tool-call support varies wildly by model; plain JSON is the
  thing 7B-class open models can reliably produce, and parse errors are
  bounced back as correction hints (3 consecutive failures end the run).
- The loop lives IN the backend, next to the guards, not in a client:
  launch_gpu IS orchestrator.request_launch, so budget/concurrency/region
  guards bind the autopilot with zero new enforcement code. Test-proven:
  an over-budget launch comes back as {"error": "Budget guard: ..."} data
  the model reads and adapts to.
- Caps everywhere: hard step ceiling (config autopilot.max_steps_cap),
  wait cap, per-turn chat timeout, MAX_CONSECUTIVE_FAILURES, fixed action
  allowlist (no shell, no arbitrary HTTP, no self-modification). Runs are
  cancellable; orphaned runs are marked failed at startup.
- Honest limits, stated in docs: a 7B open model is a mediocre long-horizon
  agent. The harness compensates (tiny action space, errors-as-data, hard
  caps), and the same guarded surface is what a heavyweight brain (Claude
  via MCP) uses for hard jobs. Autopilot is the self-sufficient tier, not
  the only tier.

**Alternatives:** LangChain/agent frameworks (dependency ban, and the loop
is ~200 lines); letting the agent shell into instances (unbounded blast
radius — refused); OpenAI-native tool calling (model-dependent, brittle on
small models).

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

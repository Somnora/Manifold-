"""FastAPI application — the single guarded gateway to Lambda Cloud.

Every client (dashboard, MCP server, future tools) is a thin consumer of
these endpoints; business logic and guards live in the Orchestrator, never
in clients.

Run modes:
- Real:  `uv run uvicorn app.main:create_default_app --factory` with .env set.
- Mock:  same, with MANIFOLD_MOCK=1 — canned Lambda API, in-memory storage,
         fake SSH. Zero live spend, works offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .sidecar_client import SidecarError

from .config import Settings, load_settings
from .connections import MockSSHConnection
from .db import Database
from .config import update_env_file
from .lambda_api import (
    FilesystemInfo,
    LambdaAPIError,
    LambdaClient,
    MockLambdaClient,
    RealLambdaClient,
    SwappableLambdaClient,
    UnconfiguredLambdaClient,
    capacity_error,
)
from .agent import Autopilot, find_serving_task
from .dispatcher import Dispatcher, ParameterError, coerce_parameters
from .model_client import MockModelClient, ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
from .sidecar_client import MockSidecarClient
from .storage import MockStorage, S3AdapterStorage, StorageClient
from .task_queue import SQLiteTaskQueue
from .templates import load_templates

logger = logging.getLogger("manifold.main")


class LaunchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str
    connection_mode: str | None = None
    ssh_key_name: str | None = None    # falls back to ssh.key_name in config.yaml
    name: str = Field(default="", max_length=64)


class TaskRequest(BaseModel):
    template: str
    parameters: dict = Field(default_factory=dict)


class WatchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str | None = None      # required only for auto_launch
    auto_launch: bool = False


class AgentAuditRequest(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    note: str = ""                     # caller-supplied session note
    result: str = ""                   # one-line result summary


class AutopilotRequest(BaseModel):
    goal: str = Field(min_length=4, max_length=4000)
    brain_instance_id: str
    max_steps: int | None = Field(default=None, ge=1)


class ChatRequest(BaseModel):
    messages: list[dict] = Field(min_length=1)   # [{role, content}, ...]
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class LambdaKeyRequest(BaseModel):
    api_key: str = Field(min_length=8)


class S3KeysRequest(BaseModel):
    access_key_id: str = Field(min_length=4)
    secret_access_key: str = Field(min_length=8)


def create_app(
    settings: Settings | None = None,
    *,
    lambda_client: LambdaClient | None = None,
    storage_factory=None,          # (FilesystemInfo) -> StorageClient
    connect_fn=None,               # (host) -> coroutine factory, for tests
    sidecar_factory=None,          # (ManagedConnection) -> SidecarClient
    model_client_factory=None,     # (ManagedConnection) -> ModelClient
    lambda_client_factory=None,    # (api_key) -> LambdaClient, for key validation
    env_path=None,                 # where /settings writes secrets (.env)
    templates_dir=None,
    mock: bool = False,
) -> FastAPI:
    settings = settings or load_settings()
    lambda_client_factory = lambda_client_factory or RealLambdaClient
    from .config import REPO_ROOT
    env_file = env_path if env_path is not None else REPO_ROOT / ".env"

    if mock:
        if sidecar_factory is None:
            shared_sidecar = MockSidecarClient()
            sidecar_factory = lambda conn: shared_sidecar  # noqa: E731
        if model_client_factory is None:
            shared_model = MockModelClient()
            model_client_factory = lambda conn: shared_model  # noqa: E731
        lambda_client = lambda_client or MockLambdaClient()
        if storage_factory is None:
            shared = MockStorage()
            storage_factory = lambda fs: shared  # noqa: E731
        if connect_fn is None:
            async def _mock_dial():
                return MockSSHConnection()
            connect_fn = lambda host: _mock_dial  # noqa: E731
        if not settings.ssh.key_name:
            # Mock mode must work without any real configuration.
            settings = replace(
                settings, ssh=replace(settings.ssh, key_name="mock-key")
            )
    elif lambda_client is None:
        # Real mode: never crash on a missing key. Start with a placeholder
        # that returns a clear "configure me" error on every call; the
        # Settings page swaps in a real client once a key is validated.
        if settings.lambda_api_key:
            lambda_client = SwappableLambdaClient(
                RealLambdaClient(settings.lambda_api_key)
            )
        else:
            lambda_client = SwappableLambdaClient(UnconfiguredLambdaClient())

    if storage_factory is None:
        def storage_factory(fs: FilesystemInfo) -> StorageClient:
            return S3AdapterStorage(
                region=fs.region,
                bucket=fs.id,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
            )

    db = Database(settings.db_path)
    orchestrator = Orchestrator(
        settings, lambda_client, db,
        connect_fn=connect_fn, sidecar_factory=sidecar_factory,
        model_client_factory=model_client_factory,
    )
    storage_cache: dict[str, StorageClient] = {}

    templates, template_errors = load_templates(
        templates_dir if templates_dir is not None else REPO_ROOT / "templates"
    )

    queue = SQLiteTaskQueue(db)
    dispatcher = Dispatcher(
        settings, orchestrator, queue, templates, db, lambda_client
    )
    autopilot = Autopilot(settings, orchestrator, queue, templates, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Re-attach to instances still running on Lambda (e.g. after a
        # backend restart) before starting the loops, so the dispatcher and
        # idle watcher see them immediately. Best-effort; never blocks boot.
        adopted = await orchestrator.adopt_running_instances()
        if adopted:
            logger.info("reconnect-on-startup: adopted %d instance(s)", adopted)
        # An agent loop is in-memory; a run left 'running' by a previous
        # process is dead. Say so instead of showing it running forever.
        orphaned = db.fail_orphaned_agent_runs()
        if orphaned:
            logger.info("marked %d orphaned autopilot run(s) failed", orphaned)
        dispatcher.start()
        yield
        await autopilot.stop()
        await dispatcher.stop()
        await orchestrator.shutdown()
        await lambda_client.close()
        db.close()

    app = FastAPI(title="Manifold", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.settings = settings
    app.state.dispatcher = dispatcher
    app.state.queue = queue
    app.state.autopilot = autopilot

    # The dashboard (Phase 2) runs on localhost:3000 and is the only
    # expected browser client; the backend itself binds to localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(LaunchRejected)
    async def _launch_rejected(request, exc: LaunchRejected):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail})

    @app.exception_handler(TerminationBlocked)
    async def _termination_blocked(request, exc: TerminationBlocked):
        from fastapi.responses import JSONResponse
        # 409 with the evidence: clients show the list and offer
        # sync-then-terminate or force=true. Never a silent block.
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "blocked": True,
                "instance_id": exc.instance_id,
                "unpersisted_files": exc.files,
            },
        )

    @app.exception_handler(LambdaAPIError)
    async def _lambda_error(request, exc: LambdaAPIError):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=exc.status if exc.status >= 400 else 502,
            content={"detail": f"Lambda API: {exc.message}",
                     "code": exc.code, "suggestion": exc.suggestion},
        )

    # -- meta -------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok", "mock": mock}

    # -- settings (first-run setup; secrets go to .env, never echoed back) --------

    @app.get("/settings/status")
    async def settings_status():
        """Configuration status only — booleans, never secret values."""
        return {
            "mock": mock,
            "lambda_configured": bool(settings.lambda_api_key),
            "s3_configured": bool(
                settings.s3_access_key_id and settings.s3_secret_access_key
            ),
            "tailscale_available": bool(settings.tailscale_authkey),
            "env_path": str(env_file),
        }

    @app.post("/settings/lambda-key")
    async def set_lambda_key(req: LambdaKeyRequest):
        """Validate a Lambda API key against the live API, persist it to
        .env, and hot-swap the running client. The key is never logged,
        audited, or returned."""
        nonlocal settings
        candidate = lambda_client_factory(req.api_key)
        try:
            types = await candidate.list_instance_types()
        except LambdaAPIError as exc:
            await candidate.close()
            raise HTTPException(
                400, f"Lambda rejected this key: {exc.message}"
            )
        except Exception as exc:
            await candidate.close()
            raise HTTPException(502, f"Could not reach Lambda to validate: {exc}")

        update_env_file(env_file, {"LAMBDA_API_KEY": req.api_key})
        settings = replace(settings, lambda_api_key=req.api_key)
        orchestrator.settings = settings
        dispatcher.settings = settings
        if not mock and isinstance(lambda_client, SwappableLambdaClient):
            old = lambda_client.inner
            lambda_client.inner = candidate
            await old.close()
        else:
            # Mock mode keeps serving the demo catalog; the key is saved
            # for the next real-mode start.
            await candidate.close()
        db.record_audit(
            "api", "settings_lambda_key",
            f"Lambda API key validated ({len(types)} instance types visible) "
            f"and saved to .env",
        )
        return {
            "valid": True,
            "instance_types_visible": len(types),
            "applied_live": not mock,
        }

    @app.post("/settings/s3-keys")
    async def set_s3_keys(req: S3KeysRequest):
        """Persist S3-adapter credentials to .env. Validated against the
        first filesystem when one is visible; saved either way."""
        nonlocal settings
        validated = False
        try:
            filesystems = await lambda_client.list_filesystems()
        except LambdaAPIError:
            filesystems = []
        if filesystems and not mock:
            probe = S3AdapterStorage(
                region=filesystems[0].region,
                bucket=filesystems[0].id,
                access_key_id=req.access_key_id,
                secret_access_key=req.secret_access_key,
            )
            try:
                await run_in_threadpool(probe.list_files, "")
                validated = True
            except Exception as exc:
                raise HTTPException(
                    400,
                    f"S3 adapter rejected these keys against filesystem "
                    f"'{filesystems[0].name}': {str(exc)[:200]}",
                )
        update_env_file(env_file, {
            "S3_ACCESS_KEY_ID": req.access_key_id,
            "S3_SECRET_ACCESS_KEY": req.secret_access_key,
        })
        settings = replace(
            settings,
            s3_access_key_id=req.access_key_id,
            s3_secret_access_key=req.secret_access_key,
        )
        orchestrator.settings = settings
        dispatcher.settings = settings
        storage_cache.clear()   # rebuild storage clients with the new keys
        db.record_audit(
            "api", "settings_s3_keys",
            f"S3 adapter keys saved to .env "
            f"({'validated against a filesystem' if validated else 'not validated: no filesystem visible'})",
        )
        return {"saved": True, "validated": validated}

    # -- instances ----------------------------------------------------------------

    @app.get("/instance-types")
    async def instance_types():
        types = await lambda_client.list_instance_types()
        return {
            name: {
                "description": t.description,
                "gpu_description": t.gpu_description,
                "price_usd_per_hour": t.price_cents_per_hour / 100,
                "specs": t.specs,
                "regions_with_capacity": t.regions_with_capacity,
            }
            for name, t in sorted(types.items())
        }

    @app.get("/regions")
    async def list_regions():
        """The full region universe with human names, so the launch form can
        show every region and grey out the ones a chosen GPU can't use.

        Order: the known NA regions east->west first, then any extra region
        the live catalog reports (named if we know it, else its code). If the
        Lambda client is unconfigured, we still return the static NA set."""
        from .lambda_api import NA_REGIONS, REGION_NAMES
        codes = list(NA_REGIONS)
        try:
            types = await lambda_client.list_instance_types()
            for t in types.values():
                for code in t.regions_with_capacity:
                    if code not in codes:
                        codes.append(code)
        except LambdaAPIError:
            pass  # unconfigured/unreachable: the static NA set is still useful
        return {
            "regions": [
                {"code": c, "name": REGION_NAMES.get(c, c)} for c in codes
            ]
        }

    @app.post("/instances", status_code=202)
    async def launch_instance(req: LaunchRequest):
        launch = await orchestrator.request_launch(
            instance_type=req.instance_type,
            region=req.region,
            filesystem=req.filesystem,
            connection_mode=req.connection_mode,
            ssh_key_name=req.ssh_key_name,
            name=req.name,
        )
        return {"launch": launch}

    @app.get("/ssh-keys")
    async def list_ssh_keys():
        keys = await lambda_client.list_ssh_keys()
        return {
            "ssh_keys": [k.name for k in keys],
            "default": settings.ssh.key_name,
        }

    @app.get("/instances")
    async def list_instances():
        return {"instances": await orchestrator.instances_with_state()}

    @app.delete("/instances/{instance_id}")
    async def terminate_instance(instance_id: str, force: bool = False):
        return await orchestrator.terminate(instance_id, force=force)

    @app.post("/instances/{instance_id}/sync")
    async def sync_instance(instance_id: str):
        return await orchestrator.sync_ephemeral(instance_id)

    @app.get("/instances/{instance_id}/metrics")
    async def instance_metrics(instance_id: str):
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        return await sidecar.metrics()

    @app.websocket("/instances/{instance_id}/terminal")
    async def instance_terminal(ws: WebSocket, instance_id: str):
        """Browser terminal: xterm.js <-> this WS <-> SSH shell session.

        Rides the managed connection — no ttyd, nothing new listening on
        the instance. Protocol: client sends JSON {type: "input"|"resize"},
        server sends raw text frames of terminal output. All traffic counts
        as activity for idle detection.
        """
        await ws.accept()
        conn = orchestrator.connections.get(instance_id)
        ssh = conn.ssh_connection() if conn else None
        if ssh is None:
            await ws.send_text(
                f"\r\n[manifold] no SSH connection to {instance_id} "
                f"(state: {conn.state.value if conn else 'unknown'})\r\n"
            )
            await ws.close()
            return

        process = await ssh.create_process(
            term_type="xterm-256color", term_size=(80, 24)
        )
        dispatcher.touch_activity(instance_id)

        async def pump_output():
            try:
                while True:
                    data = await process.stdout.read(4096)
                    if not data:
                        break
                    dispatcher.touch_activity(instance_id)
                    await ws.send_text(data)
                await ws.send_text("\r\n[manifold] shell exited\r\n")
                await ws.close()
            except (WebSocketDisconnect, RuntimeError):
                pass

        output_task = asyncio.create_task(pump_output())
        try:
            while True:
                msg = await ws.receive_json()
                dispatcher.touch_activity(instance_id)
                if msg.get("type") == "input":
                    process.stdin.write(msg.get("data", ""))
                elif msg.get("type") == "resize":
                    process.change_terminal_size(
                        int(msg.get("cols", 80)), int(msg.get("rows", 24))
                    )
        except (WebSocketDisconnect, KeyError, ValueError):
            pass
        finally:
            output_task.cancel()
            process.close()

    # -- chat with a served model -----------------------------------------------

    def _serving_task(instance_id: str) -> dict | None:
        """A live model server on this instance (see agent.find_serving_task,
        the shared single source of truth)."""
        return find_serving_task(queue, templates, instance_id)

    @app.get("/instances/{instance_id}/model")
    async def instance_model(instance_id: str):
        """Is a model being served on this instance, and which one?"""
        task = _serving_task(instance_id)
        if task is None:
            return {"serving": False}
        return {
            "serving": True,
            "task_id": task["id"],
            "template": task["template"],
            "model_id": task["model_id"],
            "port": task["port"],
        }

    @app.post("/instances/{instance_id}/chat")
    async def instance_chat(instance_id: str, req: ChatRequest):
        """Relay a chat completion to the model served on the instance,
        streaming the OpenAI-style SSE response straight through. The model
        listens on the instance's loopback; this rides the managed SSH
        connection — the chat never touches the public internet unencrypted."""
        task = _serving_task(instance_id)
        if task is None:
            raise HTTPException(
                409,
                "No model is being served on this instance. Queue a "
                "vllm-serve job first (Jobs page), then chat once it is "
                "running.",
            )
        model_client = orchestrator.model_client_for(instance_id)
        if model_client is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")

        payload = {
            "model": task["model_id"],
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        db.record_audit(
            "api", "chat",
            f"{instance_id}: {len(req.messages)} message(s) -> {task['model_id']}",
        )
        dispatcher.touch_activity(instance_id)

        import json
        from fastapi.responses import StreamingResponse

        async def relay():
            try:
                async for line in model_client.chat_stream(task["port"], payload):
                    yield line
                    dispatcher.touch_activity(instance_id)
            except ModelClientError as exc:
                # Mid-stream failure: surface it as an SSE event the panel
                # can render instead of silently truncating the reply.
                yield f'data: {{"error": {json.dumps(str(exc))}}}\n\n'

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- file bridge (upload/download over the managed SSH connection) -------------

    ALLOWED_FILE_ROOTS = ("/lambda/nfs/", "/workspace/ephemeral/")

    def _resolve_remote_path(instance_id: str, path: str) -> str:
        """Resolve a user/agent-supplied path to a safe absolute remote path.

        Relative paths land on the instance's persistent filesystem. The
        result must stay under the same sanctioned roots templates may
        mount — no traversal out of them."""
        import posixpath
        if not path.startswith("/"):
            launch = db.find_launch_by_instance(instance_id)
            filesystem = (launch or {}).get("filesystem")
            if not filesystem:
                raise HTTPException(
                    409,
                    f"No filesystem recorded for {instance_id}; use an "
                    f"absolute path under /lambda/nfs/ or /workspace/ephemeral/.",
                )
            path = f"/lambda/nfs/{filesystem}/{path}"
        resolved = posixpath.normpath(path)
        if not any(resolved.startswith(root) for root in ALLOWED_FILE_ROOTS):
            raise HTTPException(
                400,
                f"Path must stay under {' or '.join(ALLOWED_FILE_ROOTS)} "
                f"(got {resolved!r}).",
            )
        return resolved

    def _connected(instance_id: str):
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(
                409,
                f"No connected instance {instance_id}. Files move over the "
                f"managed SSH connection, so the instance must be running "
                f"and connected.",
            )
        return conn

    @app.post("/instances/{instance_id}/files/upload")
    async def upload_file(instance_id: str, file: UploadFile,
                          dest: str = Form("inbox/")):
        """Upload a local file to the instance over SFTP. `dest` ending in
        '/' is a directory (keeps the original filename); relative paths
        land on the persistent filesystem."""
        conn = _connected(instance_id)
        target = dest + (file.filename or "upload.bin") if dest.endswith("/") \
            else dest
        remote = _resolve_remote_path(instance_id, target)

        async def chunks():
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

        try:
            written = await conn.sftp_write(remote, chunks())
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"upload failed: {exc}")
        dispatcher.touch_activity(instance_id)
        db.record_audit(
            "api", "file_upload",
            f"{file.filename} -> {instance_id}:{remote} ({written} bytes)",
        )
        return {"path": remote, "bytes": written}

    @app.get("/instances/{instance_id}/files/download")
    async def download_file(instance_id: str, path: str):
        """Stream a file down from the instance over SFTP."""
        import posixpath
        from fastapi.responses import StreamingResponse
        conn = _connected(instance_id)
        remote = _resolve_remote_path(instance_id, path)

        # Pull the first chunk BEFORE responding, so missing files are a
        # real 404 instead of a broken 200 stream.
        gen = conn.sftp_read(remote)
        first = b""
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            pass                     # empty file: valid, zero-byte download
        except FileNotFoundError:
            raise HTTPException(404, f"{remote} not found on the instance")
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}")

        dispatcher.touch_activity(instance_id)
        db.record_audit("api", "file_download", f"{instance_id}:{remote}")

        async def stream():
            if first:
                yield first
            async for chunk in gen:
                yield chunk

        filename = posixpath.basename(remote)
        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/instances/{instance_id}/files/recent")
    async def instance_recent_files(instance_id: str, hours: float = 24,
                                    limit: int = 50):
        """Recently changed files on the instance (ephemeral + persistent),
        relayed from the sidecar — the 'what is my job producing?' view."""
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        return await sidecar.recent_files(hours=hours, limit=limit)

    @app.websocket("/instances/{instance_id}/metrics/stream")
    async def instance_metrics_stream(ws: WebSocket, instance_id: str):
        """Relay: sidecar (via SSH forward) -> this WS -> browser chart."""
        await ws.accept()
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            await ws.send_json({"error": f"no managed connection to {instance_id}"})
            await ws.close()
            return
        try:
            async for payload in sidecar.metrics_stream():
                await ws.send_json(payload)
        except (WebSocketDisconnect, SidecarError):
            pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass  # already closed by the client

    # -- job templates --------------------------------------------------------------

    @app.get("/templates")
    async def list_templates():
        """Valid templates with parameter schemas, plus load errors so a
        broken YAML file is visible instead of silently missing."""
        return {
            "templates": [t.to_api() for t in templates.values()],
            "errors": template_errors,
        }

    # -- tasks ------------------------------------------------------------------------

    @app.post("/tasks", status_code=202)
    async def enqueue_task(req: TaskRequest):
        template = templates.get(req.template)
        if template is None:
            raise HTTPException(
                404,
                f"Unknown template '{req.template}'. "
                f"Available: {', '.join(sorted(templates)) or '(none)'}",
            )
        # Validate NOW so a bad request fails at enqueue, not minutes later
        # on the instance. The dispatcher re-validates before running.
        try:
            coerce_parameters(template, req.parameters)
        except ParameterError as exc:
            raise HTTPException(422, str(exc))
        task_id = queue.enqueue(template=req.template, parameters=req.parameters)
        db.record_audit("api", "task_enqueue", f"{task_id} ({req.template})")
        return {"task": queue.get(task_id)}

    @app.get("/tasks")
    async def list_tasks():
        return {"tasks": queue.list()}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        task = queue.get(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id} not found")
        return task

    @app.get("/tasks/{task_id}/logs")
    async def get_task_logs(task_id: str, tail: int | None = None):
        if queue.get(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        return {"task_id": task_id, "lines": queue.get_logs(task_id, tail)}

    # -- capacity watches ---------------------------------------------------------------

    @app.post("/watches", status_code=201)
    async def create_watch(req: WatchRequest):
        types = await lambda_client.list_instance_types()
        if req.instance_type not in types:
            raise HTTPException(
                400,
                f"Unknown instance type '{req.instance_type}'. "
                f"Valid types: {', '.join(sorted(types))}",
            )
        if req.auto_launch:
            if not req.filesystem:
                raise HTTPException(
                    400, "auto_launch requires a filesystem to attach"
                )
            filesystems = {
                fs.name: fs for fs in await lambda_client.list_filesystems()
            }
            fs = filesystems.get(req.filesystem)
            if fs is None:
                raise HTTPException(400, f"Unknown filesystem '{req.filesystem}'")
            if fs.region != req.region:
                raise HTTPException(
                    400,
                    f"Region mismatch: filesystem '{req.filesystem}' lives in "
                    f"{fs.region} but the watch targets {req.region}.",
                )
        watch_id = db.create_watch(
            instance_type=req.instance_type, region=req.region,
            filesystem=req.filesystem, auto_launch=req.auto_launch,
        )
        db.record_audit(
            "api", "watch_create",
            f"{watch_id}: {req.instance_type} in {req.region}"
            f"{' (auto-launch)' if req.auto_launch else ''}",
        )
        return {"watch": db.get_watch(watch_id)}

    @app.get("/watches")
    async def list_watches():
        return {
            "watches": db.list_watches(),
            "auto_launch_enabled": settings.watches.auto_launch_enabled,
        }

    @app.delete("/watches/{watch_id}")
    async def cancel_watch(watch_id: str):
        if db.get_watch(watch_id) is None:
            raise HTTPException(404, f"watch {watch_id} not found")
        db.update_watch(watch_id, status="cancelled")
        return {"watch": db.get_watch(watch_id)}

    # -- autopilot (agent runs driven by a model served on an instance) ------------

    @app.post("/autopilot/runs", status_code=202)
    async def start_autopilot_run(req: AutopilotRequest):
        serving = _serving_task(req.brain_instance_id)
        if serving is None:
            raise HTTPException(
                409,
                f"No model is being served on {req.brain_instance_id}. "
                "Queue a vllm-serve job there first; the running model "
                "becomes the run's brain.",
            )
        if orchestrator.model_client_for(req.brain_instance_id) is None:
            raise HTTPException(
                409, f"no managed connection to {req.brain_instance_id}"
            )
        cap = settings.autopilot.max_steps_cap
        max_steps = min(req.max_steps or settings.autopilot.max_steps_default,
                        cap)
        run_id = autopilot.start_run(
            goal=req.goal,
            brain_instance_id=req.brain_instance_id,
            brain_model=serving["model_id"],
            brain_port=serving["port"],
            max_steps=max_steps,
        )
        return {"run": db.get_agent_run(run_id)}

    @app.get("/autopilot/runs")
    async def list_autopilot_runs():
        return {"runs": db.list_agent_runs()}

    @app.get("/autopilot/runs/{run_id}")
    async def get_autopilot_run(run_id: str):
        run = db.get_agent_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        return {**run, "steps": db.get_agent_steps(run_id)}

    @app.post("/autopilot/runs/{run_id}/cancel")
    async def cancel_autopilot_run(run_id: str):
        run = db.get_agent_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        if run["status"] != "running":
            raise HTTPException(409, f"run is already {run['status']}")
        autopilot.cancel_run(run_id)
        return {"cancelling": True}

    # -- audit (agent activity) -----------------------------------------------------

    @app.post("/audit/agent", status_code=201)
    async def record_agent_call(req: AgentAuditRequest):
        """MCP tool-call audit: tool, args, session note, result. The MCP
        server posts one entry per tool invocation."""
        import json as json_module
        db.record_audit(
            "mcp", req.tool,
            json_module.dumps(
                {"args": req.args, "note": req.note, "result": req.result}
            ),
        )
        return {"recorded": True}

    @app.get("/audit")
    async def list_audit(actor: str | None = None, limit: int = 200):
        return {"entries": db.list_audit(actor=actor, limit=limit)}

    # -- launches (retry status + cost history) ------------------------------------

    @app.get("/launches")
    async def list_launches():
        return {"launches": db.list_launches()}

    @app.get("/launches/{launch_id}")
    async def get_launch(launch_id: str):
        launch = db.get_launch(launch_id)
        if launch is None:
            raise HTTPException(404, f"launch {launch_id} not found")
        return launch

    # -- filesystems & storage ------------------------------------------------------

    @app.get("/filesystems")
    async def list_filesystems():
        return {
            "filesystems": [
                {
                    "name": fs.name,
                    "region": fs.region,
                    "mount_point": fs.mount_point,
                    "is_in_use": fs.is_in_use,
                    "bytes_used": fs.bytes_used,
                }
                for fs in await lambda_client.list_filesystems()
            ]
        }

    async def _storage_for(filesystem: str) -> StorageClient:
        filesystems = {fs.name: fs for fs in await lambda_client.list_filesystems()}
        if filesystem not in filesystems:
            raise HTTPException(
                404,
                f"Unknown filesystem '{filesystem}'. "
                f"Available: {', '.join(sorted(filesystems)) or '(none)'}",
            )
        fs = filesystems[filesystem]
        if fs.id not in storage_cache:
            storage_cache[fs.id] = storage_factory(fs)
        return storage_cache[fs.id]

    @app.get("/storage/files")
    async def list_storage_files(filesystem: str, prefix: str = ""):
        storage = await _storage_for(filesystem)
        files = await run_in_threadpool(storage.list_files, prefix)
        return {
            "filesystem": filesystem,
            "files": [
                {"key": f.key, "size_bytes": f.size_bytes,
                 "last_modified": f.last_modified}
                for f in files
            ],
        }

    @app.delete("/storage/files/{key:path}")
    async def delete_storage_file(key: str, filesystem: str):
        storage = await _storage_for(filesystem)
        try:
            await run_in_threadpool(storage.delete_file, key)
        except KeyError:
            raise HTTPException(404, f"file '{key}' not found")
        return {"deleted": key}

    return app


def create_default_app() -> FastAPI:
    """Uvicorn entry point (run with --factory so importing this module
    never requires credentials): reads MANIFOLD_MOCK to pick the mode.

    In mock mode, MANIFOLD_MOCK_CAPACITY_FAILURES=N scripts N
    insufficient-capacity errors before launches succeed, so the
    dashboard's retry states can be demonstrated end to end.
    """
    mock = os.environ.get("MANIFOLD_MOCK", "") == "1"
    lambda_client = None
    if mock:
        failures = int(os.environ.get("MANIFOLD_MOCK_CAPACITY_FAILURES", "0"))
        if failures:
            lambda_client = MockLambdaClient(
                scripted_launch_errors=[capacity_error() for _ in range(failures)]
            )
    return create_app(mock=mock, lambda_client=lambda_client)

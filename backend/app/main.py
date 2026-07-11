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

import os
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .connections import MockSSHConnection
from .db import Database
from .lambda_api import (
    FilesystemInfo,
    LambdaAPIError,
    LambdaClient,
    MockLambdaClient,
    RealLambdaClient,
)
from .orchestrator import LaunchRejected, Orchestrator
from .storage import MockStorage, S3AdapterStorage, StorageClient


class LaunchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str
    connection_mode: str | None = None
    name: str = Field(default="", max_length=64)


def create_app(
    settings: Settings | None = None,
    *,
    lambda_client: LambdaClient | None = None,
    storage_factory=None,          # (FilesystemInfo) -> StorageClient
    connect_fn=None,               # (host) -> coroutine factory, for tests
    mock: bool = False,
) -> FastAPI:
    settings = settings or load_settings()

    if mock:
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
        lambda_client = RealLambdaClient(settings.lambda_api_key)

    if storage_factory is None:
        def storage_factory(fs: FilesystemInfo) -> StorageClient:
            return S3AdapterStorage(
                region=fs.region,
                bucket=fs.id,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
            )

    db = Database(settings.db_path)
    orchestrator = Orchestrator(settings, lambda_client, db, connect_fn=connect_fn)
    storage_cache: dict[str, StorageClient] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await orchestrator.shutdown()
        await lambda_client.close()
        db.close()

    app = FastAPI(title="Manifold", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.settings = settings

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

    @app.post("/instances", status_code=202)
    async def launch_instance(req: LaunchRequest):
        launch = await orchestrator.request_launch(
            instance_type=req.instance_type,
            region=req.region,
            filesystem=req.filesystem,
            connection_mode=req.connection_mode,
            name=req.name,
        )
        return {"launch": launch}

    @app.get("/instances")
    async def list_instances():
        return {"instances": await orchestrator.instances_with_state()}

    @app.delete("/instances/{instance_id}")
    async def terminate_instance(instance_id: str):
        return await orchestrator.terminate(instance_id)

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
    never requires credentials): reads MANIFOLD_MOCK to pick the mode."""
    return create_app(mock=os.environ.get("MANIFOLD_MOCK", "") == "1")

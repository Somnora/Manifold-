"""Backend-side access to the sidecar running on an instance.

The sidecar binds to 127.0.0.1 on the instance, so the only road in is the
managed SSH connection: `RealSidecarClient` opens an SSH local port forward
(loopback on this machine -> loopback on the instance) and speaks HTTP/WS
across it. The telemetry path end to end:

    pynvml -> sidecar (127.0.0.1:9411 on instance)
           -> SSH local port forward (managed connection)
           -> backend relay
           -> browser WebSocket (dashboard chart)

`MockSidecarClient` serves canned metrics and unpersisted files for tests
and mock mode — including the termination-interception demo.
"""

from __future__ import annotations

import abc
import asyncio
import math
from typing import AsyncIterator

import httpx

SIDECAR_PORT = 9411


class SidecarError(Exception):
    """Sidecar unreachable or returned an error."""


class SidecarClient(abc.ABC):
    @abc.abstractmethod
    async def unpersisted_files(self) -> dict:
        """The sidecar's /storage/unpersisted payload."""

    @abc.abstractmethod
    async def metrics(self) -> dict: ...

    @abc.abstractmethod
    def metrics_stream(self) -> AsyncIterator[dict]:
        """Yield metrics payloads until cancelled."""


class RealSidecarClient(SidecarClient):
    """Talks to the sidecar through an SSH local port forward.

    The forward is created lazily per call and closed after; the managed
    connection itself stays up. asyncssh returns a listener whose
    get_port() gives the ephemeral local port.
    """

    def __init__(self, managed_connection):
        self._mc = managed_connection

    async def _forward(self):
        conn = self._mc.ssh_connection()
        if conn is None:
            raise SidecarError(
                f"no SSH connection to {self._mc.host} "
                f"(state: {self._mc.state.value})"
            )
        try:
            return await conn.forward_local_port(
                "127.0.0.1", 0, "127.0.0.1", SIDECAR_PORT
            )
        except Exception as exc:
            raise SidecarError(f"could not forward sidecar port: {exc}") from exc

    async def _get(self, path: str) -> dict:
        listener = await self._forward()
        try:
            port = listener.get_port()
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(f"http://127.0.0.1:{port}{path}")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            raise SidecarError(f"sidecar request {path} failed: {exc}") from exc
        finally:
            listener.close()

    async def unpersisted_files(self) -> dict:
        return await self._get("/storage/unpersisted")

    async def metrics(self) -> dict:
        return await self._get("/metrics")

    async def metrics_stream(self) -> AsyncIterator[dict]:
        # Poll GET /metrics over the forward at the sidecar's own cadence.
        # (The sidecar offers a WS, but polling over the forward is
        # equivalent data at the same rate with far less plumbing; the
        # browser-facing side is still a real WebSocket.)
        while True:
            yield await self.metrics()
            await asyncio.sleep(2.0)


class MockSidecarClient(SidecarClient):
    """Canned sidecar for tests and mock mode.

    `unpersisted` starts with plausible fake files so the termination
    safety hook has something to intercept; `clear_unpersisted()` simulates
    a successful sync.
    """

    def __init__(self, unpersisted: list[dict] | None = None):
        self.unpersisted = unpersisted if unpersisted is not None else [
            {"path": "checkpoints/step-2000.safetensors",
             "size_bytes": 4_294_967_296, "modified": "2026-07-10T22:11:00+00:00"},
            {"path": "outputs/samples/grid-final.png",
             "size_bytes": 8_912_896, "modified": "2026-07-10T22:14:00+00:00"},
        ]
        self._tick = 0

    def clear_unpersisted(self) -> None:
        self.unpersisted = []

    async def unpersisted_files(self) -> dict:
        return {
            "root": "/workspace/ephemeral",
            "patterns": ["*.safetensors", "*.png"],
            "files": list(self.unpersisted),
        }

    async def metrics(self) -> dict:
        # A gentle sine wave so the dashboard chart visibly moves.
        self._tick += 1
        wave = (math.sin(self._tick / 5) + 1) / 2
        return {
            "available": True,
            "gpus": [{
                "index": 0,
                "name": "Mock A10",
                "vram_used_mib": int(4096 + 14000 * wave),
                "vram_total_mib": 24564,
                "utilization_pct": int(25 + 70 * wave),
                "temperature_c": int(45 + 25 * wave),
            }],
        }

    async def metrics_stream(self) -> AsyncIterator[dict]:
        while True:
            yield await self.metrics()
            await asyncio.sleep(1.0)

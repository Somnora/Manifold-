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
    async def recent_files(self, *, hours: float = 24, limit: int = 50) -> dict:
        """Recently modified files across ephemeral + persistent mounts."""

    @abc.abstractmethod
    async def metrics(self) -> dict: ...

    @abc.abstractmethod
    def metrics_stream(self) -> AsyncIterator[dict]:
        """Yield metrics payloads until cancelled."""

    # -- file navigator ---------------------------------------------------------

    @abc.abstractmethod
    async def list_dir(self, root_name: str, path: str) -> dict:
        """One directory level: {root, path, entries}."""

    @abc.abstractmethod
    async def usage(self, root_name: str, path: str) -> dict:
        """Recursive child sizes: {root, path, children, truncated}."""

    @abc.abstractmethod
    async def delete_path(self, root_name: str, path: str,
                          recursive: bool) -> dict:
        """Delete a file/directory. Raises SidecarError with the sidecar's
        message on refusal (root delete, non-recursive dir, missing)."""


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

    async def _request(self, method: str, path: str, *,
                       params: dict | None = None,
                       json_body: dict | None = None,
                       timeout: float = 10.0) -> dict:
        listener = await self._forward()
        try:
            port = listener.get_port()
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.request(
                    method, f"http://127.0.0.1:{port}{path}",
                    params=params, json=json_body,
                )
                if resp.status_code >= 400:
                    # Surface the sidecar's own message (jail refusals,
                    # non-recursive dir deletes) instead of a bare status.
                    try:
                        detail = resp.json().get("detail", resp.text)
                    except Exception:
                        detail = resp.text
                    raise SidecarError(str(detail)[:300])
                return resp.json()
        except httpx.HTTPError as exc:
            raise SidecarError(f"sidecar request {path} failed: {exc}") from exc
        finally:
            listener.close()

    async def _get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def unpersisted_files(self) -> dict:
        return await self._get("/storage/unpersisted")

    async def recent_files(self, *, hours: float = 24, limit: int = 50) -> dict:
        return await self._get(f"/storage/recent?hours={hours}&limit={limit}")

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

    async def list_dir(self, root_name: str, path: str) -> dict:
        return await self._request(
            "GET", "/fs/list", params={"root_name": root_name, "path": path},
            timeout=30.0,   # NFS metadata can be slow on big directories
        )

    async def usage(self, root_name: str, path: str) -> dict:
        return await self._request(
            "GET", "/fs/usage", params={"root_name": root_name, "path": path},
            timeout=120.0,  # a bounded recursive walk, but still a walk
        )

    async def delete_path(self, root_name: str, path: str,
                          recursive: bool) -> dict:
        return await self._request(
            "POST", "/fs/delete",
            json_body={"root_name": root_name, "path": path,
                       "recursive": recursive},
            timeout=120.0,  # recursive rmtree on NFS takes time
        )


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
        import copy
        self.tree = copy.deepcopy(self.DEMO_TREE)   # mutable per instance

    def clear_unpersisted(self) -> None:
        self.unpersisted = []

    async def recent_files(self, *, hours: float = 24, limit: int = 50) -> dict:
        files = [
            {"root": "ephemeral", "path": f["path"],
             "size_bytes": f["size_bytes"], "modified": f["modified"]}
            for f in self.unpersisted
        ] + [
            {"root": "persistent", "path": "transcripts/day1.srt",
             "size_bytes": 48_211, "modified": "2026-07-11T05:58:00+00:00"},
            {"root": "persistent", "path": "cache/huggingface/blobs/a1b2c3",
             "size_bytes": 2_147_483_648, "modified": "2026-07-11T05:41:00+00:00"},
        ]
        files.sort(key=lambda f: f["modified"], reverse=True)
        return {"files": files[:limit], "truncated": False, "hours": hours}

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

    # -- file navigator (an in-memory tree: dict = dir, int = file size) -----------

    DEMO_TREE = {
        "persistent": {
            "manifold-data": {
                "research": {
                    "scrapes": {
                        "candidates-2026-raw.jsonl": 3_221_225_472,
                        "donors-dump.csv": 1_073_741_824,
                    },
                    "notes.md": 4_096,
                },
                "datasets": {"interviews": {"day1.wav": 412_000_000}},
                "models": {"llama-3-8b": {"model.safetensors": 16_060_000_000}},
                "outputs": {"transcripts": {"day1.srt": 48_211}},
                "cache": {"huggingface": {"blobs": {"a1b2c3": 2_147_483_648}}},
            },
        },
        "ephemeral": {
            "checkpoints": {"step-2000.safetensors": 4_294_967_296},
            "outputs": {"samples": {"grid-final.png": 8_912_896}},
        },
    }

    def _node(self, root_name: str, path: str):
        if root_name not in self.tree:
            raise SidecarError(f"unknown root '{root_name}'; "
                               f"use ephemeral or persistent")
        node = self.tree[root_name]
        parts = [p for p in path.strip("/").split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise SidecarError(f"path escapes the {root_name} root")
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise SidecarError(f"{root_name}:{path} not found")
            node = node[part]
        return node, parts

    @staticmethod
    def _tree_size(node) -> tuple[int, int]:
        if isinstance(node, int):
            return node, 1
        total = files = 0
        for child in node.values():
            size, count = MockSidecarClient._tree_size(child)
            total += size
            files += count
        return total, files

    async def list_dir(self, root_name: str, path: str) -> dict:
        node, _ = self._node(root_name, path)
        if isinstance(node, int):
            raise SidecarError(f"{root_name}:{path} is a file, not a directory")
        entries = [
            {"name": name, "is_dir": isinstance(child, dict),
             "size_bytes": 0 if isinstance(child, dict) else child,
             "modified": "2026-07-11T06:00:00+00:00"}
            for name, child in node.items()
        ]
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"root": root_name, "path": path, "entries": entries}

    async def usage(self, root_name: str, path: str) -> dict:
        node, _ = self._node(root_name, path)
        if isinstance(node, int):
            raise SidecarError(f"{root_name}:{path} is not a directory")
        children = []
        for name, child in node.items():
            size, count = self._tree_size(child)
            children.append({"name": name, "is_dir": isinstance(child, dict),
                             "total_bytes": size, "file_count": count})
        children.sort(key=lambda c: c["total_bytes"], reverse=True)
        return {"root": root_name, "path": path, "children": children,
                "truncated": False}

    async def delete_path(self, root_name: str, path: str,
                          recursive: bool) -> dict:
        node, parts = self._node(root_name, path)
        if not parts:
            raise SidecarError("refusing to delete a filesystem root")
        if isinstance(node, dict) and not recursive:
            raise SidecarError(
                f"{path} is a directory; pass recursive=true to delete it "
                f"and everything inside")
        parent, _ = self._node(root_name, "/".join(parts[:-1]))
        del parent[parts[-1]]
        return {"deleted": f"{root_name}:{path}"}

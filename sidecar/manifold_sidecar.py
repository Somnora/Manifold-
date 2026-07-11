"""Manifold sidecar — runs ON the GPU instance, binds to 127.0.0.1 ONLY.

The backend reaches it exclusively through an SSH local port forward over
the managed connection; it is never exposed on a public interface. sshd
stays the only public listener on the box.

Endpoints:
    GET /health                liveness
    GET /metrics               VRAM / utilization / temperature via pynvml
    WS  /metrics/stream        the same payload pushed every interval
    GET /storage/unpersisted   files under /workspace/ephemeral matching
                               "valuable" patterns — the termination safety
                               hook's evidence list

Single file, minimal dependencies (fastapi, uvicorn, pynvml), installed by
cloud-init. Run: python3 manifold_sidecar.py
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

try:
    import pynvml
except ImportError:  # pragma: no cover - tests inject a fake
    pynvml = None

EPHEMERAL_ROOT = Path(os.environ.get("MANIFOLD_EPHEMERAL_ROOT", "/workspace/ephemeral"))

# Files worth warning about before termination. Overridable via env:
# MANIFOLD_VALUABLE_PATTERNS="*.safetensors,*.pt,*.bin"
DEFAULT_VALUABLE_PATTERNS = [
    "*.safetensors", "*.pt", "*.ckpt", "*.bin", "*.gguf",
    "*.png", "*.jpg", "*.jsonl", "*.csv", "*.srt", "*.wav", "*.mp4",
]
STREAM_INTERVAL_SECONDS = float(os.environ.get("MANIFOLD_STREAM_INTERVAL", "2.0"))


def valuable_patterns() -> list[str]:
    raw = os.environ.get("MANIFOLD_VALUABLE_PATTERNS", "")
    if raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    return DEFAULT_VALUABLE_PATTERNS


PERSISTENT_ROOT = Path(os.environ.get("MANIFOLD_PERSISTENT_ROOT", "/lambda/nfs"))
# Bound the recent-files walk so a huge model cache cannot stall the sidecar.
MAX_SCAN_ENTRIES = 20_000


def create_app(nvml=None, ephemeral_root: Path | None = None,
               persistent_root: Path | None = None) -> FastAPI:
    """App factory; tests pass a fake nvml module and temp roots."""
    nvml = nvml if nvml is not None else pynvml
    root = ephemeral_root if ephemeral_root is not None else EPHEMERAL_ROOT
    p_root = persistent_root if persistent_root is not None else PERSISTENT_ROOT
    app = FastAPI(title="manifold-sidecar")
    state = {"nvml_ready": False}

    def ensure_nvml() -> bool:
        if nvml is None:
            return False
        if not state["nvml_ready"]:
            try:
                nvml.nvmlInit()
                state["nvml_ready"] = True
            except Exception:
                return False
        return True

    def read_metrics() -> dict:
        if not ensure_nvml():
            return {"available": False, "gpus": [],
                    "error": "pynvml unavailable or no NVIDIA driver"}
        gpus = []
        for i in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(i)
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            util = nvml.nvmlDeviceGetUtilizationRates(handle)
            temp = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            name = nvml.nvmlDeviceGetName(handle)
            gpus.append({
                "index": i,
                "name": name.decode() if isinstance(name, bytes) else str(name),
                "vram_used_mib": mem.used // (1024 * 1024),
                "vram_total_mib": mem.total // (1024 * 1024),
                "utilization_pct": util.gpu,
                "temperature_c": temp,
            })
        return {"available": True, "gpus": gpus,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return read_metrics()

    @app.websocket("/metrics/stream")
    async def metrics_stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(read_metrics())
                await asyncio.sleep(STREAM_INTERVAL_SECONDS)
        except WebSocketDisconnect:
            pass

    @app.get("/storage/unpersisted")
    async def unpersisted():
        """Valuable files in ephemeral scratch that would die with the box."""
        patterns = valuable_patterns()
        files = []
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if any(fnmatch.fnmatch(path.name, p) for p in patterns):
                    stat = path.stat()
                    files.append({
                        "path": str(path.relative_to(root)),
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                    })
        files.sort(key=lambda f: f["size_bytes"], reverse=True)
        return {"root": str(root), "patterns": patterns, "files": files}

    @app.get("/storage/recent")
    async def recent(hours: float = 24, limit: int = 50):
        """Files changed in the last N hours across ephemeral scratch and
        the persistent mounts — a live view of what jobs are producing."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        results = []
        scanned = 0
        truncated = False
        for base, kind in ((root, "ephemeral"), (p_root, "persistent")):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                results.append({
                    "root": kind,
                    "path": str(path.relative_to(base)),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                })
        results.sort(key=lambda f: f["modified"], reverse=True)
        return {"files": results[:limit], "truncated": truncated,
                "hours": hours}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    # Loopback only — this is a hard security rule, not a default.
    uvicorn.run(app, host="127.0.0.1", port=9411)

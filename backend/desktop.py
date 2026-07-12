"""Desktop entrypoint: the whole product as one process.

This is what PyInstaller freezes into the sidecar binary the Tauri shell
spawns (and what a double-click runs standalone). It boots the same app
factory as development, binds strictly to loopback, and serves the bundled
dashboard at /.

MANIFOLD_MOCK=1 works here exactly as in development - the packaged app can
be demoed with zero credentials and zero spend.
"""

from __future__ import annotations

import os
import sys
import threading

import uvicorn

from app.main import create_default_app

HOST = "127.0.0.1"
PORT = int(os.environ.get("MANIFOLD_PORT", "8000"))


def _watch_parent() -> None:
    """Exit when the shell that spawned us dies.

    PyInstaller --onefile runs as bootloader -> real process; the Tauri
    shell can only kill the bootloader, which would orphan this process
    and leave :8000 held forever (found live at the Phase 28 gate). Our
    stdin is a pipe from the shell, so EOF on it means the shell is gone
    - the reliable cross-platform death signal. Opt-in via env so a
    terminal run (stdin may be closed or a TTY) never self-terminates.
    """
    def watch() -> None:
        try:
            sys.stdin.buffer.read()   # blocks until the pipe closes
        except Exception:
            pass
        print("manifold: shell gone (stdin EOF); shutting down", flush=True)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def main() -> None:
    if os.environ.get("MANIFOLD_PARENT_WATCHDOG") == "1":
        _watch_parent()
    app = create_default_app()
    print(f"manifold: serving on http://{HOST}:{PORT}", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except SystemExit:
        raise
    except OSError as exc:
        # The one predictable failure: the port is taken. Say so plainly.
        print(f"manifold: cannot bind {HOST}:{PORT} ({exc}). "
              f"Set MANIFOLD_PORT to a free port and relaunch.",
              file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

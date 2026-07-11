"""SQLite persistence for orchestrator metadata.

One table per concern; jobs and benchmarks tables arrive with Phase 4.
Uses the stdlib sqlite3 driver guarded by a lock — this is a single-user
local tool and every statement here runs in well under a millisecond.
(See DECISIONS.md: "Plain sqlite3 instead of an async driver".)
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,          -- ISO 8601 UTC
    requested_type      TEXT NOT NULL,          -- what the user asked for
    launched_type       TEXT,                   -- what actually launched (may be a fallback)
    region              TEXT NOT NULL,
    filesystem          TEXT,
    connection_mode     TEXT NOT NULL,
    hourly_rate_cents   INTEGER,
    status              TEXT NOT NULL,          -- launching|retrying|booting|failed|active|terminated
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,                   -- last error message, for the dashboard
    lambda_instance_id  TEXT,
    launched_at         TEXT,                   -- when Lambda accepted the launch (billing starts)
    active_at           TEXT,                   -- when the instance reached "active"
    terminated_at       TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,      -- "dashboard" | "mcp" | ...
    action      TEXT NOT NULL,
    detail      TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # -- launches ------------------------------------------------------------

    def create_launch(
        self,
        *,
        requested_type: str,
        region: str,
        filesystem: str | None,
        connection_mode: str,
        hourly_rate_cents: int,
    ) -> str:
        launch_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO launches
               (id, created_at, requested_type, region, filesystem,
                connection_mode, hourly_rate_cents, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'launching')""",
            (launch_id, utcnow(), requested_type, region, filesystem,
             connection_mode, hourly_rate_cents),
        )
        return launch_id

    def update_launch(self, launch_id: str, **fields: Any) -> None:
        allowed = {
            "status", "attempts", "error", "lambda_instance_id",
            "launched_type", "hourly_rate_cents",
            "launched_at", "active_at", "terminated_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown launch fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE launches SET {cols} WHERE id = ?",
            (*fields.values(), launch_id),
        )

    def get_launch(self, launch_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM launches WHERE id = ?", (launch_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_launches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM launches ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def find_launch_by_instance(self, lambda_instance_id: str) -> dict | None:
        row = self._execute(
            """SELECT * FROM launches WHERE lambda_instance_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (lambda_instance_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- audit log -----------------------------------------------------------

    def record_audit(self, actor: str, action: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (utcnow(), actor, action, detail),
        )

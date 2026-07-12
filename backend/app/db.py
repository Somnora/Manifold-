"""SQLite persistence for orchestrator metadata.

One table per concern; jobs and benchmarks tables arrive with Phase 4.
Uses the stdlib sqlite3 driver guarded by a lock — this is a single-user
local tool and every statement here runs in well under a millisecond.
(See DECISIONS.md: "Plain sqlite3 instead of an async driver".)
"""

from __future__ import annotations

import json
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
    terminated_at       TEXT,
    keep_alive          INTEGER NOT NULL DEFAULT 0   -- idle auto-termination switched off
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,      -- "dashboard" | "mcp" | ...
    action      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    template        TEXT NOT NULL,
    parameters      TEXT NOT NULL,      -- JSON of user-supplied values
    status          TEXT NOT NULL,      -- queued|running|succeeded|failed
    instance_id     TEXT,               -- where it ran
    started_at      TEXT,
    finished_at     TEXT,
    exit_code       INTEGER,
    error           TEXT,               -- dispatcher-level error, if any
    output_paths    TEXT                -- JSON list of persistent paths
);

CREATE TABLE IF NOT EXISTS task_logs (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,       -- ordering within a task
    at          TEXT NOT NULL,
    line        TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    goal                TEXT NOT NULL,
    brain_instance_id   TEXT NOT NULL,   -- instance serving the model
    brain_model         TEXT,            -- model id driving the run
    status              TEXT NOT NULL,   -- running|succeeded|failed|cancelled|exhausted
    max_steps           INTEGER NOT NULL,
    steps_taken         INTEGER NOT NULL DEFAULT 0,
    summary             TEXT,            -- the agent's own closing summary
    error               TEXT,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    at          TEXT NOT NULL,
    thought     TEXT,
    action      TEXT NOT NULL,
    args        TEXT NOT NULL,           -- JSON
    result      TEXT NOT NULL,           -- JSON observation fed back
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS watches (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    instance_type   TEXT NOT NULL,
    region          TEXT NOT NULL,
    filesystem      TEXT,               -- needed only for auto-launch
    auto_launch     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,      -- watching|available|launched|cancelled
    last_checked    TEXT,
    triggered_at    TEXT                -- when capacity was first seen
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
        # Additive migrations for databases created before a column existed
        # (CREATE TABLE IF NOT EXISTS does not alter existing tables).
        self._ensure_column("launches", "keep_alive",
                            "INTEGER NOT NULL DEFAULT 0")
        self._lock = threading.Lock()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self._conn.commit()

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
            "launched_at", "active_at", "terminated_at", "keep_alive",
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

    def list_audit(self, actor: str | None = None, limit: int = 200) -> list[dict]:
        if actor:
            rows = self._execute(
                "SELECT * FROM audit_log WHERE actor = ? ORDER BY id DESC LIMIT ?",
                (actor, limit),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- tasks -----------------------------------------------------------------

    def create_task(self, *, template: str, parameters: dict) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO tasks (id, created_at, template, parameters, status)
               VALUES (?, ?, ?, ?, 'queued')""",
            (task_id, utcnow(), template, json.dumps(parameters)),
        )
        return task_id

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "instance_id", "started_at", "finished_at",
                   "exit_code", "error", "output_paths"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {unknown}")
        if "output_paths" in fields and not isinstance(fields["output_paths"], str):
            fields["output_paths"] = json.dumps(fields["output_paths"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
        )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict:
        task = dict(row)
        task["parameters"] = json.loads(task["parameters"])
        task["output_paths"] = json.loads(task["output_paths"] or "[]")
        return task

    def get_task(self, task_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM tasks ORDER BY created_at DESC, id"
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def next_queued_task(self) -> dict | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        return self._task_row(row) if row else None

    def running_task_count(self) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
        ).fetchone()
        return row["n"]

    # -- task logs ---------------------------------------------------------------

    def append_task_log(self, task_id: str, line: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM task_logs WHERE task_id = ?",
                (task_id,),
            )
            seq = cur.fetchone()["seq"]
            self._conn.execute(
                "INSERT INTO task_logs (task_id, seq, at, line) VALUES (?, ?, ?, ?)",
                (task_id, seq, utcnow(), line),
            )
            self._conn.commit()

    def get_task_logs(self, task_id: str, tail: int | None = None) -> list[dict]:
        if tail is not None:
            rows = self._execute(
                """SELECT * FROM (
                       SELECT * FROM task_logs WHERE task_id = ?
                       ORDER BY seq DESC LIMIT ?
                   ) ORDER BY seq""",
                (task_id, tail),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM task_logs WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- autopilot runs ---------------------------------------------------------------

    def create_agent_run(self, *, goal: str, brain_instance_id: str,
                         brain_model: str, max_steps: int) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO agent_runs
               (id, created_at, goal, brain_instance_id, brain_model,
                status, max_steps)
               VALUES (?, ?, ?, ?, ?, 'running', ?)""",
            (run_id, utcnow(), goal, brain_instance_id, brain_model, max_steps),
        )
        return run_id

    def update_agent_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "steps_taken", "summary", "error", "finished_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown agent_run fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE agent_runs SET {cols} WHERE id = ?",
            (*fields.values(), run_id),
        )

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_agent_runs(self, limit: int = 50) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM agent_runs ORDER BY created_at DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_agent_step(self, run_id: str, seq: int, *, thought: str,
                       action: str, args: dict, result: dict) -> None:
        self._execute(
            """INSERT INTO agent_steps (run_id, seq, at, thought, action,
               args, result) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, seq, utcnow(), thought, action,
             json.dumps(args), json.dumps(result)),
        )

    def get_agent_steps(self, run_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        steps = []
        for r in rows:
            step = dict(r)
            step["args"] = json.loads(step["args"])
            step["result"] = json.loads(step["result"])
            steps.append(step)
        return steps

    def fail_orphaned_agent_runs(self) -> int:
        """Mark runs left 'running' by a dead process as failed. Called at
        startup: an in-memory agent loop cannot survive a restart, and a
        row that claims to be running forever would be a lie."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE agent_runs
                   SET status = 'failed', finished_at = ?,
                       error = 'backend restarted mid-run'
                   WHERE status = 'running'""",
                (utcnow(),),
            )
            self._conn.commit()
            return cur.rowcount

    # -- capacity watches -----------------------------------------------------------

    def create_watch(self, *, instance_type: str, region: str,
                     filesystem: str | None, auto_launch: bool) -> str:
        watch_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO watches
               (id, created_at, instance_type, region, filesystem,
                auto_launch, status)
               VALUES (?, ?, ?, ?, ?, ?, 'watching')""",
            (watch_id, utcnow(), instance_type, region, filesystem,
             int(auto_launch)),
        )
        return watch_id

    def update_watch(self, watch_id: str, **fields: Any) -> None:
        allowed = {"status", "last_checked", "triggered_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown watch fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE watches SET {cols} WHERE id = ?", (*fields.values(), watch_id)
        )

    def get_watch(self, watch_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM watches WHERE id = ?", (watch_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_watches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM watches ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def active_watches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM watches WHERE status = 'watching'"
        ).fetchall()
        return [dict(r) for r in rows]

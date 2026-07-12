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
    output_paths    TEXT,               -- JSON list of persistent paths
    -- Auto-manage (Phase 24): a job that owns its own instance lifecycle.
    -- When auto_manage=1 the dispatcher launches a dedicated instance for
    -- this job (gpu_type/region/filesystem), runs it, syncs, and terminates.
    auto_manage     INTEGER NOT NULL DEFAULT 0,
    gpu_type        TEXT,               -- requested instance type (auto-manage)
    region          TEXT,
    filesystem      TEXT,
    launch_id       TEXT,               -- the launch this job's lifecycle created
    lifecycle       TEXT,               -- queued|waiting|launching|ready|running|
                                        -- syncing|terminating|done|failed|cancelled
    lifecycle_detail TEXT,              -- human "why" for the current state
    lifecycle_events TEXT               -- JSON {state: iso-ts}, one stamp per state
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

-- Periodic GPU telemetry, sampled by the dispatcher while an instance is
-- connected. Backs the post-run utilization verdict and the right-size hint.
-- Purely advisory; nothing on the launch path reads or writes this.
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL,
    at              TEXT NOT NULL,
    gpu_name        TEXT,
    vram_used_mib   INTEGER,
    vram_total_mib  INTEGER,
    util_pct        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_telemetry_instance
    ON telemetry_samples(instance_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _interval(start_iso: str | None, end_iso: str | None) -> float | None:
    """Seconds between two ISO timestamps, or None if either is missing."""
    if not start_iso or not end_iso:
        return None
    try:
        return (datetime.fromisoformat(end_iso)
                - datetime.fromisoformat(start_iso)).total_seconds()
    except (TypeError, ValueError):
        return None


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
        # Auto-manage columns (Phase 24) for databases created earlier.
        self._ensure_column("tasks", "auto_manage",
                            "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("tasks", "gpu_type", "TEXT")
        self._ensure_column("tasks", "region", "TEXT")
        self._ensure_column("tasks", "filesystem", "TEXT")
        self._ensure_column("tasks", "launch_id", "TEXT")
        self._ensure_column("tasks", "lifecycle", "TEXT")
        self._ensure_column("tasks", "lifecycle_detail", "TEXT")
        self._ensure_column("tasks", "lifecycle_events", "TEXT")
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

    # -- cost/utilization intelligence (read-only; off the launch path) --------

    def task_durations(self, template: str, gpu_type: str) -> list[float]:
        """Runtimes (seconds) of PAST successful runs of `template` on
        `gpu_type`, joining each task to the launch it ran on to recover the
        GPU. Feeds the pre-launch estimate; grows more accurate as history
        accumulates. Excludes rows without both timestamps."""
        rows = self._execute(
            """SELECT t.started_at AS s, t.finished_at AS f
                 FROM tasks t
                 JOIN launches l ON t.instance_id = l.lambda_instance_id
                WHERE t.template = ?
                  AND l.launched_type = ?
                  AND t.status = 'succeeded'
                  AND t.started_at IS NOT NULL
                  AND t.finished_at IS NOT NULL""",
            (template, gpu_type),
        ).fetchall()
        out = []
        for r in rows:
            try:
                start = datetime.fromisoformat(r["s"])
                finish = datetime.fromisoformat(r["f"])
            except (TypeError, ValueError):
                continue
            secs = (finish - start).total_seconds()
            if secs >= 0:
                out.append(secs)
        return out

    def record_telemetry_sample(self, instance_id: str, *, gpu_name: str,
                                vram_used_mib: int, vram_total_mib: int,
                                util_pct: int) -> None:
        self._execute(
            """INSERT INTO telemetry_samples
                   (instance_id, at, gpu_name, vram_used_mib,
                    vram_total_mib, util_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (instance_id, utcnow(), gpu_name, vram_used_mib,
             vram_total_mib, util_pct),
        )

    def telemetry_summary(self, instance_id: str) -> dict:
        """Aggregate an instance's samples: sample count, PEAK vram used, the
        card's total vram, and average utilization. Peak (not average) vram is
        the OOM-relevant figure the right-size hint keys on."""
        row = self._execute(
            """SELECT COUNT(*) AS n,
                      MAX(vram_used_mib) AS peak_used,
                      MAX(vram_total_mib) AS total,
                      AVG(util_pct) AS avg_util,
                      MAX(gpu_name) AS gpu_name
                 FROM telemetry_samples WHERE instance_id = ?""",
            (instance_id,),
        ).fetchone()
        return {
            "sample_count": row["n"] or 0,
            "peak_vram_used_mib": row["peak_used"] or 0,
            "vram_total_mib": row["total"] or 0,
            "avg_util_pct": float(row["avg_util"] or 0.0),
            "gpu_name": row["gpu_name"] or "",
        }

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

    def create_task(self, *, template: str, parameters: dict,
                    auto_manage: bool = False, gpu_type: str | None = None,
                    region: str | None = None,
                    filesystem: str | None = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        # Auto-managed jobs start in lifecycle 'queued' with a first event
        # stamp; manual jobs leave lifecycle NULL (the field is unused for
        # them, so the dispatcher and UI treat them exactly as before).
        lifecycle = "queued" if auto_manage else None
        events = json.dumps({"queued": utcnow()}) if auto_manage else None
        self._execute(
            """INSERT INTO tasks
               (id, created_at, template, parameters, status,
                auto_manage, gpu_type, region, filesystem,
                lifecycle, lifecycle_events)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (task_id, utcnow(), template, json.dumps(parameters),
             1 if auto_manage else 0, gpu_type, region, filesystem,
             lifecycle, events),
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

    # Auto-manage lifecycle states that still hold the single-instance slot
    # (the job is mid-flight). 'queued' is not-yet-started; the terminal
    # states (done/failed/cancelled) release the slot.
    ACTIVE_LIFECYCLE = ("waiting", "launching", "ready", "running",
                        "syncing", "terminating")
    # In-flight states that actually have an instance attached (excludes
    # 'waiting', which is pre-launch). Used to find instances an auto-managed
    # job owns.
    _OWNING_LIFECYCLE = ("launching", "ready", "running",
                         "syncing", "terminating")

    def set_task_lifecycle(self, task_id: str, lifecycle: str, *,
                           detail: str | None = None,
                           launch_id: str | None = None,
                           stamp: bool = True) -> None:
        """Move an auto-managed task to a new lifecycle state.

        Records a single timestamp per state in lifecycle_events (stamp=False
        updates only the detail, e.g. re-describing a blocked termination
        without re-stamping). Optionally attaches the launch this job created.
        """
        row = self._execute(
            "SELECT lifecycle_events FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        events = json.loads(row["lifecycle_events"]) if row and row["lifecycle_events"] else {}
        if stamp and lifecycle not in events:
            events[lifecycle] = utcnow()
        fields: dict[str, Any] = {
            "lifecycle": lifecycle,
            "lifecycle_events": json.dumps(events),
        }
        if detail is not None:
            fields["lifecycle_detail"] = detail
        if launch_id is not None:
            fields["launch_id"] = launch_id
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
        )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict:
        task = dict(row)
        task["parameters"] = json.loads(task["parameters"])
        task["output_paths"] = json.loads(task["output_paths"] or "[]")
        task["auto_manage"] = bool(task.get("auto_manage"))
        events = json.loads(task["lifecycle_events"]) if task.get("lifecycle_events") else {}
        task["lifecycle_events"] = events
        # Launch-to-ready instrumentation: how long from kicking off the
        # launch to a connected, ready-to-run GPU. The zero-waste headline
        # number, surfaced on the job card.
        task["launch_to_ready_seconds"] = _interval(
            events.get("launching"), events.get("ready"))
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

    def queued_tasks(self) -> list[dict]:
        """All queued tasks, oldest first. The dispatcher scans these to find
        the first one with an eligible instance (auto-managed jobs bind to
        their own launched instance; manual jobs take any free one)."""
        rows = self._execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at, id"
        ).fetchall()
        return [self._task_row(r) for r in rows]

    # -- auto-manage lifecycle queries -----------------------------------------

    def next_pending_auto_managed_task(self) -> dict | None:
        """Oldest auto-managed job that has not started its lifecycle yet."""
        row = self._execute(
            """SELECT * FROM tasks
                WHERE auto_manage = 1 AND lifecycle = 'queued'
                ORDER BY created_at, id LIMIT 1"""
        ).fetchone()
        return self._task_row(row) if row else None

    def active_auto_managed_task(self) -> dict | None:
        """The auto-managed job currently holding the instance slot, if any.

        v1 is sequential (one in flight at a time); if more than one ever
        exists, the oldest is returned so it drains first."""
        placeholders = ", ".join("?" for _ in self.ACTIVE_LIFECYCLE)
        row = self._execute(
            f"""SELECT * FROM tasks
                 WHERE auto_manage = 1 AND lifecycle IN ({placeholders})
                 ORDER BY created_at, id LIMIT 1""",
            self.ACTIVE_LIFECYCLE,
        ).fetchone()
        return self._task_row(row) if row else None

    def auto_managed_instance_ids(self) -> set[str]:
        """Instance ids owned by an in-flight auto-managed job. The idle loop
        skips these (their lifecycle owns teardown) and manual jobs never
        dispatch onto them."""
        placeholders = ", ".join("?" for _ in self._OWNING_LIFECYCLE)
        rows = self._execute(
            f"""SELECT DISTINCT l.lambda_instance_id AS iid
                  FROM tasks t JOIN launches l ON t.launch_id = l.id
                 WHERE t.auto_manage = 1
                   AND t.lifecycle IN ({placeholders})
                   AND l.lambda_instance_id IS NOT NULL""",
            self._OWNING_LIFECYCLE,
        ).fetchall()
        return {r["iid"] for r in rows}

    def running_task_count(self) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
        ).fetchone()
        return row["n"]

    def delete_task(self, task_id: str) -> None:
        """Remove one task and its logs (used by the Job History 'remove')."""
        with self._lock:
            self._conn.execute("DELETE FROM task_logs WHERE task_id = ?",
                               (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    def delete_finished_tasks(self) -> int:
        """Clear all finished (succeeded/failed) tasks and their logs. Active
        jobs (queued/running) are left untouched. Returns the count removed."""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM tasks WHERE status IN ('succeeded', 'failed')"
            ).fetchall()]
            for tid in ids:
                self._conn.execute("DELETE FROM task_logs WHERE task_id = ?",
                                   (tid,))
            self._conn.execute(
                "DELETE FROM tasks WHERE status IN ('succeeded', 'failed')")
            self._conn.commit()
            return len(ids)

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

"""TaskQueue interface, backed by SQLite.

The spec requires task dispatch to live behind a single interface so
alternative backends (Redis, cloud queues) could be swapped in later
without touching callers. SQLiteTaskQueue is the only implementation today:
tasks, their state transitions, and their logs all live in the same
database as everything else, so history survives restarts for free.
"""

from __future__ import annotations

import abc

from .db import Database, utcnow


class TaskQueue(abc.ABC):
    @abc.abstractmethod
    def enqueue(self, *, template: str, parameters: dict,
                auto_manage: bool = False, gpu_type: str | None = None,
                region: str | None = None,
                filesystem: str | None = None) -> str:
        """Add a task; returns its id.

        When auto_manage is set, the dispatcher owns the instance lifecycle
        for this job (launch -> run -> sync -> terminate) using the supplied
        gpu_type/region/filesystem."""

    @abc.abstractmethod
    def next_queued(self) -> dict | None:
        """The oldest queued task, or None."""

    @abc.abstractmethod
    def mark_running(self, task_id: str, instance_id: str) -> None: ...

    @abc.abstractmethod
    def mark_finished(self, task_id: str, *, exit_code: int,
                      output_paths: list[str], error: str = "") -> None: ...

    @abc.abstractmethod
    def append_log(self, task_id: str, line: str) -> None: ...

    @abc.abstractmethod
    def get_logs(self, task_id: str, tail: int | None = None) -> list[dict]: ...

    @abc.abstractmethod
    def get(self, task_id: str) -> dict | None: ...

    @abc.abstractmethod
    def list(self) -> list[dict]: ...

    @abc.abstractmethod
    def running_count(self) -> int: ...

    @abc.abstractmethod
    def delete(self, task_id: str) -> None: ...

    @abc.abstractmethod
    def clear_finished(self) -> int: ...


class SQLiteTaskQueue(TaskQueue):
    def __init__(self, db: Database):
        self._db = db

    def enqueue(self, *, template: str, parameters: dict,
                auto_manage: bool = False, gpu_type: str | None = None,
                region: str | None = None,
                filesystem: str | None = None) -> str:
        return self._db.create_task(
            template=template, parameters=parameters, auto_manage=auto_manage,
            gpu_type=gpu_type, region=region, filesystem=filesystem)

    def next_queued(self) -> dict | None:
        return self._db.next_queued_task()

    def mark_running(self, task_id: str, instance_id: str) -> None:
        self._db.update_task(
            task_id, status="running", instance_id=instance_id,
            started_at=utcnow(),
        )

    def mark_finished(self, task_id: str, *, exit_code: int,
                      output_paths: list[str], error: str = "") -> None:
        self._db.update_task(
            task_id,
            status="succeeded" if exit_code == 0 and not error else "failed",
            finished_at=utcnow(),
            exit_code=exit_code,
            output_paths=output_paths,
            error=error or None,
        )

    def append_log(self, task_id: str, line: str) -> None:
        self._db.append_task_log(task_id, line)

    def get_logs(self, task_id: str, tail: int | None = None) -> list[dict]:
        return self._db.get_task_logs(task_id, tail)

    def get(self, task_id: str) -> dict | None:
        return self._db.get_task(task_id)

    def list(self) -> list[dict]:
        return self._db.list_tasks()

    def running_count(self) -> int:
        return self._db.running_task_count()

    def delete(self, task_id: str) -> None:
        self._db.delete_task(task_id)

    def clear_finished(self) -> int:
        return self._db.delete_finished_tasks()

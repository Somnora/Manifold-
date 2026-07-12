"""Dispatcher: pushes queued tasks to a connected instance over SSH.

Flow per task:
1. Wait until a queued task AND a connected instance exist (poll loop).
2. Resolve the template; validate + coerce the stored parameter values.
3. Render the docker invocation: substitute {{parameters}} in the command,
   replace {persistent} in mounts with /lambda/nfs/<filesystem>, publish
   declared ports on 127.0.0.1 only, add --gpus all.
4. Run it over the managed SSH connection, streaming stdout/stderr lines
   into task_logs as they arrive (visible live via GET /tasks/{id}/logs).
   A persistent copy is written on the instance too (docker logs also
   retains them until the container is pruned).
5. Record exit code + output paths (the template's persistent mounts).

Idle auto-termination lives here as a second loop: if no task is running
and no terminal has been active for idle.timeout_seconds, request the
STANDARD termination flow — the Phase 3 safety hook still applies.

The capacity watcher is a third loop: polls the instance-type catalog and
flips watches to "available" (or auto-launches through the guarded path
when enabled and configured on the watch).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time

from .config import Settings
from .connections import ConnectionState, ManagedConnection
from .db import Database, utcnow
from .lambda_api import LambdaClient
from .model_client import ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
from .task_queue import TaskQueue
from .templates import JobTemplate, PERSISTENT_TOKEN

logger = logging.getLogger("manifold.dispatcher")


class ParameterError(Exception):
    """User-supplied task parameters don't satisfy the template schema."""


def coerce_parameters(template: JobTemplate, values: dict) -> dict:
    """Validate user values against the template schema; apply defaults.

    Returns the complete parameter dict. Raises ParameterError with a
    message naming every problem at once (nicer than one-at-a-time).
    """
    problems = []
    declared = {p.name for p in template.parameters}
    for extra in sorted(set(values) - declared):
        problems.append(f"unknown parameter '{extra}'")

    result: dict = {}
    for p in template.parameters:
        if p.name in values:
            raw = values[p.name]
            try:
                if p.type == "integer":
                    result[p.name] = int(raw)
                elif p.type == "number":
                    result[p.name] = float(raw)
                elif p.type == "boolean":
                    if isinstance(raw, bool):
                        result[p.name] = raw
                    elif str(raw).lower() in ("true", "1", "yes"):
                        result[p.name] = True
                    elif str(raw).lower() in ("false", "0", "no"):
                        result[p.name] = False
                    else:
                        raise ValueError(raw)
                else:
                    result[p.name] = str(raw)
            except (TypeError, ValueError):
                problems.append(
                    f"parameter '{p.name}' must be {p.type}, got {raw!r}"
                )
        elif p.required:
            problems.append(f"missing required parameter '{p.name}'")
        else:
            result[p.name] = p.default
    if problems:
        raise ParameterError("; ".join(problems))
    return result


def render_docker_command(
    template: JobTemplate, parameters: dict, *, filesystem: str, task_id: str
) -> str:
    """Build the docker run invocation for a task.

    Every substituted value is shell-quoted. Ports are ALWAYS published on
    127.0.0.1 — a template cannot open a public listener no matter what it
    declares (see CLAUDE.md hard rules).
    """
    persistent_root = f"/lambda/nfs/{filesystem}"

    command = template.command
    for name, value in parameters.items():
        command = command.replace("{{" + name + "}}", shlex.quote(str(value)))

    parts = [
        "docker run --rm",
        f"--name manifold-task-{task_id}",
        "--gpus all",
    ]
    if template.network == "host":
        # Loopback-consumer jobs (llm-synthesize) dial servers other jobs
        # publish on the host's 127.0.0.1. Mutually exclusive with ports
        # (enforced at template load).
        parts.append("--network host")
    for volume in template.volumes:
        host = volume.host.replace(PERSISTENT_TOKEN, persistent_root)
        # Parameters may appear inside mount paths too (e.g. input_dir).
        for name, value in parameters.items():
            host = host.replace("{{" + name + "}}", str(value))
        suffix = ":ro" if volume.read_only else ""
        parts.append(f"-v {shlex.quote(host)}:{shlex.quote(volume.container)}{suffix}")
    for port in template.ports:
        parts.append(f"-p 127.0.0.1:{port.host}:{port.container}")
    for key, value in template.env.items():
        parts.append(f"-e {shlex.quote(f'{key}={value}')}")
    parts.append(template.image)
    parts.append(command)
    return " ".join(parts)


def wrap_remote_command(docker_cmd: str, remote_log: str, *,
                        ensure_dirs: list[str]) -> str:
    """Wrap a docker command for remote dispatch: create the dirs it needs,
    tee all output to a persistent log, and — critically — propagate the
    CONTAINER's exit code through the pipe.

    Without `set -o pipefail` a pipeline's exit code is the LAST command's
    (tee, which always exits 0), so every job would report "succeeded" no
    matter what the container did. Found on real hardware at the Phase 15
    gate: two crashed vllm-serve jobs showed green.
    """
    mkdirs = " ".join(shlex.quote(d) for d in ensure_dirs)
    return (
        f"mkdir -p {mkdirs} && set -o pipefail && "
        f"({docker_cmd}) 2>&1 | tee {shlex.quote(remote_log)}"
    )


def output_paths_for(template: JobTemplate, parameters: dict,
                     filesystem: str) -> list[str]:
    """The persistent host paths a task writes to (its writable mounts)."""
    persistent_root = f"/lambda/nfs/{filesystem}"
    paths = []
    for volume in template.volumes:
        if volume.read_only or not volume.host.startswith(PERSISTENT_TOKEN):
            continue
        host = volume.host.replace(PERSISTENT_TOKEN, persistent_root)
        for name, value in parameters.items():
            host = host.replace("{{" + name + "}}", str(value))
        paths.append(host)
    return paths


class Dispatcher:
    """Owns the three background loops: tasks, idle, capacity watches."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        queue: TaskQueue,
        templates: dict[str, JobTemplate],
        db: Database,
        lambda_client: LambdaClient,
        *,
        clock=time.monotonic,
    ):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        self.db = db
        self.client = lambda_client
        self._clock = clock
        self._loops: list[asyncio.Task] = []
        # Terminal activity is reported by the terminal WS handler (Phase 5);
        # jobs update it too, so "idle" means neither jobs nor shells.
        self.last_activity: dict[str, float] = {}
        # Instances whose idle auto-termination the user switched off; also
        # persisted on the launch row (see keep_alive_enabled).
        self._keep_alive_mem: set[str] = set()
        self.on_capacity_available = None   # hook for notifications (set by app)
        # Model-readiness cache: task_id -> {ready, error, checked_at}. A
        # served model's task goes 'running' the instant its container is
        # launched, but vLLM needs minutes to pull the image, download
        # weights, and load the GPU before its API answers. This tracks
        # "actually answering", probed via GET /v1/models with a TTL.
        self._readiness: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._loops = [
            asyncio.create_task(self._task_loop()),
            asyncio.create_task(self._idle_loop()),
            asyncio.create_task(self._watch_loop()),
        ]

    async def stop(self) -> None:
        for loop in self._loops:
            loop.cancel()
        for loop in self._loops:
            try:
                await loop
            except asyncio.CancelledError:
                pass
        self._loops = []

    def touch_activity(self, instance_id: str) -> None:
        """Record activity (job start/end, terminal traffic) on an instance."""
        self.last_activity[instance_id] = self._clock()

    # -- idle keep-alive ---------------------------------------------------------------

    def keep_alive_enabled(self, instance_id: str) -> bool:
        """Whether the user has switched idle auto-termination off for this
        instance. Persisted on the launch row when one exists, so it survives
        a backend restart; in-memory otherwise (adopted external instances)."""
        if instance_id in self._keep_alive_mem:
            return True
        launch = self.db.find_launch_by_instance(instance_id)
        return bool(launch and launch.get("keep_alive"))

    def set_keep_alive(self, instance_id: str, enabled: bool) -> dict:
        if enabled:
            self._keep_alive_mem.add(instance_id)
        else:
            self._keep_alive_mem.discard(instance_id)
        launch = self.db.find_launch_by_instance(instance_id)
        if launch:
            self.db.update_launch(launch["id"], keep_alive=1 if enabled else 0)
        self.db.record_audit(
            "dashboard", "keep_alive",
            f"{instance_id} idle auto-termination {'off' if enabled else 'on'}",
        )
        return {"instance_id": instance_id, "keep_alive": enabled}

    def idle_status(self, instance_id: str) -> dict:
        """Idle countdown info for the instance card. idle_seconds counts
        from the last job/terminal activity (0 if none recorded yet)."""
        last = self.last_activity.get(instance_id)
        idle = max(0.0, self._clock() - last) if last is not None else 0.0
        return {
            "idle_seconds": round(idle),
            "timeout_seconds": round(self.settings.idle.timeout_seconds),
            "keep_alive": self.keep_alive_enabled(instance_id),
        }

    # -- model readiness ---------------------------------------------------------------

    # Re-probe cadence: a model confirmed ready is rechecked rarely; one
    # that's still loading is rechecked often so the UI flips promptly.
    READY_TTL = 30.0
    LOADING_TTL = 3.0

    async def model_ready(self, instance_id: str, task_id: str,
                          port: int) -> dict:
        """Whether the model served by `task_id` actually answers yet.

        Probes GET /v1/models on the instance at most once per TTL and
        caches the verdict. Returns {"ready": bool, "error": str}. The
        error carries the probe failure ('connection refused' while vLLM is
        still starting) so callers can show a helpful loading message."""
        now = self._clock()
        cached = self._readiness.get(task_id)
        ttl = self.READY_TTL if (cached and cached["ready"]) else self.LOADING_TTL
        if cached and now - cached["checked_at"] < ttl:
            return {"ready": cached["ready"], "error": cached["error"]}

        client = self.orchestrator.model_client_for(instance_id)
        if client is None:
            result = {"ready": False, "error": "no managed connection"}
        else:
            try:
                await client.model_info(port)
                result = {"ready": True, "error": ""}
            except ModelClientError as exc:
                result = {"ready": False, "error": str(exc)}
            except Exception as exc:   # never let a probe raise into a caller
                result = {"ready": False, "error": str(exc)}
        self._readiness[task_id] = {**result, "checked_at": now}
        return result

    # -- task loop -----------------------------------------------------------------

    def _connected_instance(self) -> tuple[str, ManagedConnection] | None:
        for instance_id, conn in self.orchestrator.connections.items():
            if conn.state == ConnectionState.CONNECTED:
                return instance_id, conn
        return None

    async def _task_loop(self) -> None:
        while True:
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task loop iteration failed")
            await asyncio.sleep(self.settings.tasks.poll_seconds)

    async def _dispatch_once(self) -> None:
        if self.queue.running_count() > 0:
            return  # one task at a time per instance; keep it simple
        task = self.queue.next_queued()
        if task is None:
            return
        target = self._connected_instance()
        if target is None:
            return  # stays queued until an instance is connected
        instance_id, conn = target
        await self._run_task(task, instance_id, conn)

    async def _run_task(self, task: dict, instance_id: str,
                        conn: ManagedConnection) -> None:
        task_id = task["id"]
        template = self.templates.get(task["template"])
        if template is None:
            self.queue.mark_finished(
                task_id, exit_code=-1, output_paths=[],
                error=f"template '{task['template']}' no longer exists",
            )
            return

        launch = self.db.find_launch_by_instance(instance_id)
        filesystem = (launch or {}).get("filesystem")
        if not filesystem:
            self.queue.mark_finished(
                task_id, exit_code=-1, output_paths=[],
                error=f"no filesystem recorded for instance {instance_id}",
            )
            return

        try:
            parameters = coerce_parameters(template, task["parameters"])
        except ParameterError as exc:
            self.queue.mark_finished(
                task_id, exit_code=-1, output_paths=[], error=str(exc)
            )
            return

        docker_cmd = render_docker_command(
            template, parameters, filesystem=filesystem, task_id=task_id
        )
        outputs = output_paths_for(template, parameters, filesystem)

        self.queue.mark_running(task_id, instance_id)
        self.touch_activity(instance_id)
        self.queue.append_log(task_id, f"[manifold] dispatching to {instance_id}")
        self.queue.append_log(task_id, f"[manifold] $ {docker_cmd}")
        self.db.record_audit("backend", "task_dispatch",
                             f"{task_id} ({task['template']}) -> {instance_id}")

        # Also keep a persistent copy of the log on the filesystem.
        remote_log = f"/lambda/nfs/{filesystem}/task-logs/{task_id}.log"
        wrapped = wrap_remote_command(
            docker_cmd, remote_log,
            ensure_dirs=["/workspace/ephemeral",
                         f"/lambda/nfs/{filesystem}/task-logs"],
        )

        try:
            exit_code, stdout, stderr = await self._stream_run(
                conn, wrapped, task_id
            )
        except ConnectionError as exc:
            self.queue.append_log(task_id, f"[manifold] connection lost: {exc}")
            self.queue.mark_finished(
                task_id, exit_code=-1, output_paths=[],
                error=f"SSH connection lost during task: {exc}",
            )
            return
        finally:
            self.touch_activity(instance_id)

        for line in stderr.splitlines():
            self.queue.append_log(task_id, f"[stderr] {line}")
        self.queue.append_log(
            task_id,
            f"[manifold] exited {exit_code}; log archived at {remote_log}",
        )
        self.queue.mark_finished(
            task_id,
            exit_code=exit_code,
            output_paths=outputs,
            error="" if exit_code == 0 else f"container exited {exit_code}",
        )

    async def _stream_run(self, conn: ManagedConnection, command: str,
                          task_id: str) -> tuple[int, str, str]:
        """Run a command, streaming stdout lines into the task log.

        Uses the connection's streaming API when available (real asyncssh:
        create_process); falls back to run() for simple mocks.
        """
        ssh = conn.ssh_connection()
        if ssh is None:
            raise ConnectionError(f"no SSH connection (state: {conn.state.value})")
        create_process = getattr(ssh, "create_process", None)
        if create_process is None:
            exit_code, stdout, stderr = await conn.run(command)
            for line in stdout.splitlines():
                self.queue.append_log(task_id, line)
            return exit_code, stdout, stderr

        process = await create_process(command)
        stdout_lines: list[str] = []
        async for line in process.stdout:
            line = line.rstrip("\n")
            stdout_lines.append(line)
            self.queue.append_log(task_id, line)
        stderr = await process.stderr.read()
        await process.wait()
        exit_code = process.exit_status if process.exit_status is not None else -1
        return exit_code, "\n".join(stdout_lines), stderr or ""

    # -- idle loop -----------------------------------------------------------------

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.idle.poll_seconds)
            try:
                await self._check_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle loop iteration failed")

    async def _check_idle(self) -> None:
        """Terminate instances idle past the timeout, via the STANDARD flow.

        Idle = connected, no running task, and no activity (job or terminal)
        for idle.timeout_seconds. The clock starts when the connection comes
        up (we seed last_activity then), so a freshly booted instance gets a
        full quiet period before it is eligible.
        """
        if self.queue.running_count() > 0:
            # A running job keeps every instance alive (single-instance
            # guardrail today; revisit when concurrency > 1).
            return
        now = self._clock()
        timeout = self.settings.idle.timeout_seconds
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if conn.state != ConnectionState.CONNECTED:
                # Not reachable: don't count unreachable time as idle.
                self.last_activity.pop(instance_id, None)
                continue
            if self.keep_alive_enabled(instance_id):
                continue
            last = self.last_activity.setdefault(instance_id, now)
            if now - last < timeout:
                continue
            logger.info("instance %s idle for %.0fs; requesting termination",
                        instance_id, now - last)
            self.db.record_audit(
                "backend", "idle_termination",
                f"{instance_id} idle {now - last:.0f}s (limit {timeout:.0f}s)",
            )
            try:
                # force=False: the Phase 3 safety hook still applies.
                await self.orchestrator.terminate(instance_id, force=False)
                self.last_activity.pop(instance_id, None)
            except TerminationBlocked as exc:
                # Unpersisted files: sync them, then try once more. If that
                # still fails, leave the instance alone and try next cycle.
                logger.info("idle termination blocked by %d files; syncing",
                            len(exc.files))
                self.db.record_audit(
                    "backend", "idle_sync",
                    f"{instance_id}: {len(exc.files)} unpersisted files",
                )
                try:
                    await self.orchestrator.sync_ephemeral(instance_id)
                    await self.orchestrator.terminate(instance_id, force=True)
                    self.last_activity.pop(instance_id, None)
                except (LaunchRejected, TerminationBlocked) as sync_exc:
                    logger.warning("idle sync+terminate failed for %s: %s",
                                   instance_id, sync_exc)

    # -- capacity watch loop ----------------------------------------------------------

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.watches.poll_seconds)
            try:
                await self._check_watches()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watch loop iteration failed")

    async def _check_watches(self) -> None:
        watches = self.db.active_watches()
        if not watches:
            return
        types = await self.client.list_instance_types()
        now = utcnow()
        for watch in watches:
            self.db.update_watch(watch["id"], last_checked=now)
            info = types.get(watch["instance_type"])
            if info is None or watch["region"] not in info.regions_with_capacity:
                continue
            # Capacity found.
            self.db.update_watch(watch["id"], status="available", triggered_at=now)
            self.db.record_audit(
                "backend", "capacity_available",
                f"{watch['instance_type']} in {watch['region']} (watch {watch['id']})",
            )
            if self.on_capacity_available:
                try:
                    self.on_capacity_available(watch)
                except Exception:
                    logger.exception("capacity notification hook failed")
            if (
                watch["auto_launch"]
                and self.settings.watches.auto_launch_enabled
                and watch["filesystem"]
            ):
                try:
                    # Straight through the guarded pipeline: budget,
                    # concurrency, and region-match all still apply.
                    await self.orchestrator.request_launch(
                        instance_type=watch["instance_type"],
                        region=watch["region"],
                        filesystem=watch["filesystem"],
                    )
                    self.db.update_watch(watch["id"], status="launched")
                    self.db.record_audit(
                        "backend", "watch_auto_launch",
                        f"watch {watch['id']}: launched {watch['instance_type']}",
                    )
                except LaunchRejected as exc:
                    # Guards said no. The watch stays "available" so the
                    # user sees capacity exists and why we did not launch.
                    self.db.record_audit(
                        "backend", "watch_auto_launch_rejected",
                        f"watch {watch['id']}: {exc.detail}",
                    )

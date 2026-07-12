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
            asyncio.create_task(self._telemetry_loop()),
            asyncio.create_task(self._auto_manage_loop()),
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

    def _pick_dispatchable(
        self,
    ) -> tuple[dict, str, ManagedConnection] | None:
        """First queued task that has an eligible connected instance.

        Binding keeps the two job kinds from stepping on each other:
        - an auto-managed job runs ONLY on the instance its own lifecycle
          launched, and only once that instance is 'ready' (connected);
        - a manual job runs on any connected instance NOT owned by an
          auto-managed lifecycle, so it never lands on a box about to be
          torn down.
        Scanning (rather than taking only the oldest) stops a waiting manual
        job from blocking a ready auto-managed one, and vice versa.
        """
        connected = {
            iid: conn
            for iid, conn in self.orchestrator.connections.items()
            if conn.state == ConnectionState.CONNECTED
        }
        if not connected:
            return None
        auto_owned = self.db.auto_managed_instance_ids()
        for task in self.db.queued_tasks():
            if task["auto_manage"]:
                if task["lifecycle"] != "ready" or not task["launch_id"]:
                    continue
                launch = self.db.get_launch(task["launch_id"])
                iid = launch["lambda_instance_id"] if launch else None
                if iid and iid in connected:
                    return task, iid, connected[iid]
            else:
                for iid, conn in connected.items():
                    if iid not in auto_owned:
                        return task, iid, conn
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
        pick = self._pick_dispatchable()
        if pick is None:
            return  # nothing queued, or no eligible instance yet
        task, instance_id, conn = pick
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
        if task.get("auto_manage"):
            # The lifecycle loop launched this box and will sync+terminate it
            # once the job settles; mark the running phase for the job card.
            self.db.set_task_lifecycle(task_id, "running",
                                       detail=f"running on {instance_id}")
            self.db.record_audit("backend", "auto_manage_running",
                                 f"job {task_id} instance {instance_id}: dispatched")
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
        # Instances an auto-managed job owns are governed by that job's
        # lifecycle, which owns teardown (sync -> terminate). The idle loop
        # must not race it: skip them entirely, keep-alive or not. If the
        # lifecycle is ever lost (its job reached a terminal state), the
        # instance drops out of this set and the idle loop resumes as backstop.
        auto_owned = self.db.auto_managed_instance_ids()
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if instance_id in auto_owned:
                continue
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

    # -- auto-manage lifecycle loop -----------------------------------------------------

    async def _auto_manage_loop(self) -> None:
        """Drive auto-managed jobs through their whole instance lifecycle:

            waiting -> launching -> ready -> running -> syncing -> terminating -> done

        Sequential (v1): at most one auto-managed job holds the single-instance
        slot at a time; the next waits its turn. Every guarded step routes
        through the SAME orchestrator functions the dashboard uses
        (request_launch, sync_ephemeral, terminate) — no guard is duplicated
        or bypassed. The loop is stateless across ticks (it reads the job's
        lifecycle from the DB each time), so a backend restart resumes wherever
        the job left off.
        """
        while True:
            await asyncio.sleep(self.settings.auto_manage.poll_seconds)
            try:
                await self._auto_manage_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-manage loop iteration failed")

    async def _auto_manage_once(self) -> None:
        # One in-flight job at a time; only promote the next pending job when
        # the slot is free (the current one reached a terminal state).
        job = self.db.active_auto_managed_task()
        if job is None:
            job = self.db.next_pending_auto_managed_task()
            if job is None:
                return
        lc = job["lifecycle"]
        if lc in ("queued", "waiting"):
            await self._auto_launch(job)
        elif lc == "launching":
            self._auto_check_boot(job)
        elif lc == "ready":
            return  # the task loop dispatches it; nothing to do here
        elif lc == "running":
            self._auto_check_run_done(job)
        elif lc == "syncing":
            await self._auto_sync(job)
        elif lc == "terminating":
            await self._auto_terminate(job)

    def _job_instance_id(self, job: dict,
                         launch_id: str | None = None) -> str | None:
        lid = launch_id or job.get("launch_id")
        if not lid:
            return None
        launch = self.db.get_launch(lid)
        return launch["lambda_instance_id"] if launch else None

    def _transition(self, job: dict, lifecycle: str, detail: str = "", *,
                    launch_id: str | None = None,
                    instance_id: str | None = None,
                    audit_action: str | None = None) -> None:
        """Move a job to a new lifecycle state and audit the change once.

        Every transition writes an audit row carrying the job id and (when the
        instance exists) the instance id, per the spec. Re-entering the same
        state (e.g. staying in 'waiting') does not re-audit."""
        changed = job.get("lifecycle") != lifecycle
        self.db.set_task_lifecycle(job["id"], lifecycle, detail=detail or None,
                                   launch_id=launch_id, stamp=changed)
        if changed:
            iid = instance_id or self._job_instance_id(job, launch_id)
            loc = f"job {job['id']}" + (f" instance {iid}" if iid else "")
            self.db.record_audit(
                "backend", audit_action or f"auto_manage_{lifecycle}",
                loc + (f": {detail}" if detail else ""))

    def _fail(self, job: dict, reason: str) -> None:
        task = self.queue.get(job["id"])
        if task and task["status"] == "queued":
            # Never dispatched (guard rejection or boot failure): close it out
            # so it leaves the active list with the reason attached.
            self.queue.mark_finished(job["id"], exit_code=-1, output_paths=[],
                                     error=reason)
        self._transition(job, "failed", detail=reason,
                         audit_action="auto_manage_failed")

    async def _auto_launch(self, job: dict) -> None:
        """queued/waiting -> launching, through the guarded launch path."""
        try:
            launch = await self.orchestrator.request_launch(
                instance_type=job["gpu_type"], region=job["region"],
                filesystem=job["filesystem"])
        except LaunchRejected as exc:
            if exc.reason_code == "concurrency":
                # The single slot is busy (a manual/external instance is up).
                # Wait and retry next tick; do NOT fail the job.
                self._transition(
                    job, "waiting",
                    detail=f"waiting for a free instance slot ({exc.detail})",
                    audit_action="auto_manage_waiting")
                return
            # budget / validation / mode: can never admit -> fail with reason.
            self._fail(job, exc.detail)
            return
        self._transition(job, "launching", launch_id=launch["id"],
                         detail=f"launching {job['gpu_type']} in {job['region']}")

    def _auto_check_boot(self, job: dict) -> None:
        """launching -> ready once the launch is active and connected."""
        launch = self.db.get_launch(job["launch_id"]) if job["launch_id"] else None
        if launch is None:
            self._fail(job, "launch record missing")
            return
        if launch["status"] == "failed":
            self._fail(job, launch["error"] or "launch failed")
            return
        if launch["status"] == "active":
            iid = launch["lambda_instance_id"]
            conn = self.orchestrator.connections.get(iid) if iid else None
            if iid and conn and conn.state == ConnectionState.CONNECTED:
                self._transition(job, "ready", instance_id=iid,
                                 detail="instance connected; ready to run")
        # else still booting/retrying: wait for the next tick

    def _auto_check_run_done(self, job: dict) -> None:
        """running -> syncing once the dispatched task settles."""
        task = self.queue.get(job["id"])
        if task and task["status"] in ("succeeded", "failed"):
            self._transition(
                job, "syncing", instance_id=self._job_instance_id(job),
                detail=f"job {task['status']}; syncing outputs to persistent")

    async def _auto_sync(self, job: dict) -> None:
        """syncing -> terminating. Always sync ephemeral scratch first."""
        iid = self._job_instance_id(job)
        if iid:
            try:
                await self.orchestrator.sync_ephemeral(iid)
            except LaunchRejected as exc:
                # Sync could not run (no connection, rsync error). Record it and
                # still attempt the guarded terminate; the safety hook blocks
                # below if data is genuinely at risk.
                self.db.record_audit(
                    "backend", "auto_manage_sync_failed",
                    f"job {job['id']} instance {iid}: {exc.detail}")
        self._transition(job, "terminating", instance_id=iid,
                         detail="sync done; terminating (safety hook applies)")

    async def _auto_terminate(self, job: dict) -> None:
        """terminating -> done, via the guarded terminate. Never force."""
        iid = self._job_instance_id(job)
        launch = self.db.get_launch(job["launch_id"]) if job["launch_id"] else None
        if not iid or (launch and launch["status"] == "terminated"):
            # Already gone (user resolved a block, or reconcile closed it).
            self._transition(job, "done", instance_id=iid,
                             detail="instance terminated")
            return
        try:
            await self.orchestrator.terminate(iid, force=False)
            self._transition(job, "done", instance_id=iid,
                             detail="synced and terminated")
        except TerminationBlocked as exc:
            # Spec: if files REMAIN after the intended sync, do NOT force.
            # Surface it exactly like the manual flow and leave the box up for
            # review. The loop keeps retrying force=False, so the moment the
            # user syncs/clears the files (or terminates manually) the job
            # completes on its own.
            msg = (f"termination blocked: {len(exc.files)} unpersisted "
                   f"file(s) remain after sync; instance {iid} left running "
                   f"for review")
            if job.get("lifecycle_detail") != msg:
                self.db.set_task_lifecycle(job["id"], "terminating",
                                           detail=msg, stamp=False)
                self.db.record_audit(
                    "backend", "auto_manage_terminate_blocked",
                    f"job {job['id']} instance {iid}: {msg}")

    async def cancel_auto_managed(self, task_id: str) -> dict:
        """Cancel an auto-managed job that has not started running.

        Allowed while queued/waiting/launching/ready. If a box was already
        launched, tear it down through the guarded path (nothing ran, so the
        hook passes; if it somehow blocks, surface rather than force). Running
        or tearing-down jobs are left to finish."""
        task = self.queue.get(task_id)
        if task is None:
            raise LaunchRejected(404, f"task {task_id} not found")
        if not task["auto_manage"]:
            raise LaunchRejected(400, f"task {task_id} is not auto-managed")
        lc = task["lifecycle"]
        if lc in ("done", "failed", "cancelled"):
            raise LaunchRejected(409, f"job is already {lc}")
        if lc in ("running", "syncing", "terminating"):
            raise LaunchRejected(
                409, f"cannot cancel a job that is {lc}; let it finish or "
                f"terminate the instance from the instance card")
        iid = self._job_instance_id(task)
        if iid:
            try:
                await self.orchestrator.terminate(iid, force=False)
            except TerminationBlocked as exc:
                raise LaunchRejected(
                    409, f"instance {iid} has {len(exc.files)} unpersisted "
                    f"file(s); resolve them before cancelling")
        self.db.set_task_lifecycle(task_id, "cancelled",
                                   detail="cancelled by user")
        if task["status"] == "queued":
            self.queue.mark_finished(task_id, exit_code=-1, output_paths=[],
                                     error="cancelled by user")
        self.db.record_audit(
            "backend", "auto_manage_cancelled",
            f"job {task_id}" + (f" instance {iid}" if iid else "")
            + ": cancelled by user")
        return {"cancelled": task_id}

    # -- telemetry sampling loop -------------------------------------------------------

    async def _telemetry_loop(self) -> None:
        """Record one GPU telemetry sample per connected instance on a slow
        cadence, so a post-run utilization verdict and right-size hint can be
        computed from real data. Best-effort and fully off the launch path:
        a probe failure just skips that tick."""
        while True:
            await asyncio.sleep(self.settings.telemetry.sample_seconds)
            try:
                await self._sample_telemetry_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("telemetry loop iteration failed")

    async def _sample_telemetry_once(self) -> None:
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if conn.state != ConnectionState.CONNECTED:
                continue
            sidecar = self.orchestrator.sidecar_for(instance_id)
            if sidecar is None:
                continue
            try:
                metrics = await sidecar.metrics()
            except Exception:
                continue   # sidecar not up yet / transient: skip this tick
            gpus = metrics.get("gpus") if metrics.get("available") else None
            if not gpus:
                continue
            g = gpus[0]
            self.db.record_telemetry_sample(
                instance_id,
                gpu_name=g.get("name", ""),
                vram_used_mib=int(g.get("vram_used_mib", 0)),
                vram_total_mib=int(g.get("vram_total_mib", 0)),
                util_pct=int(g.get("utilization_pct", 0)),
            )

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

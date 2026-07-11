"""Launch orchestration: validation -> guards -> retry -> persistence -> connection.

This module is the single path to a running instance. Every client — the
dashboard, the MCP server (Phase 6), anything else — goes through
Orchestrator.request_launch, so the guards here cannot be bypassed.

Control flow for a launch:

1. VALIDATE   connection mode is available; instance type exists; the
              filesystem exists and its region matches the requested region
              (filesystems are region-locked and attach only at launch).
2. GUARDS     max concurrent instances, then max hourly spend, both computed
              against LIVE instances from the Lambda API. Violations reject
              the request with 409 — nothing is ever queued silently.
3. PERSIST    a launches row is created; every later transition (retrying,
              booting, active, failed) updates it, so the dashboard can
              always show what is happening. The API returns 202 here.
4. RETRY      in the background: one "attempt" tries the requested type and
              then each budget-safe fallback type once. Capacity errors move
              to the next candidate; any other error fails the launch
              immediately. Between attempts: exponential backoff, capped.
5. BOOT       poll the instance until it is "active" with an IP (or time out).
6. CONNECT    ask the launch's ConnectionManager for the dial target (the
              only mode-specific step) and start a ManagedConnection, which
              keeps itself alive with reconnect+backoff from then on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .cloud_init import build_user_data
from .config import Settings
from .connections import (
    CONNECTION_MODES,
    ConnectionManager,
    ConnectionState,
    DirectSSHConnectionManager,
    ManagedConnection,
    TailscaleConnectionManager,
    backoff_delay,
)
from .db import Database, utcnow
from .lambda_api import InstanceInfo, LambdaAPIError, LambdaClient
from .sidecar_client import RealSidecarClient, SidecarClient, SidecarError

logger = logging.getLogger("manifold.orchestrator")


class LaunchRejected(Exception):
    """A launch request refused before any Lambda API launch call."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class TerminationBlocked(Exception):
    """The pre-termination safety hook found unpersisted files.

    Carries the file list so clients can show it and offer sync or force.
    """

    def __init__(self, instance_id: str, files: list[dict]):
        super().__init__(
            f"{len(files)} unpersisted file(s) on {instance_id}; "
            f"sync them or pass force=true"
        )
        self.instance_id = instance_id
        self.files = files


@dataclass
class LaunchPlan:
    """Everything _run_launch needs, resolved and validated up front."""
    launch_id: str
    region: str
    filesystem: str
    connection_mode: str
    ssh_key_name: str
    types_to_try: list[str]          # requested type first, then fallbacks
    prices: dict[str, int]           # cents/hour per candidate type
    name: str


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        lambda_client: LambdaClient,
        db: Database,
        *,
        connect_fn: Callable[[str], Callable[[], Awaitable]] | None = None,
        sidecar_factory: Callable[[ManagedConnection], SidecarClient] | None = None,
    ):
        self.settings = settings
        self.client = lambda_client
        self.db = db
        # connect_fn(host) returns the coroutine factory a ManagedConnection
        # uses to dial; tests and mock mode inject one, real mode uses asyncssh.
        self._connect_fn = connect_fn
        # sidecar_factory(conn) builds the sidecar client for an instance;
        # mock mode injects MockSidecarClient.
        self._sidecar_factory = sidecar_factory or RealSidecarClient
        self.managers: dict[str, ConnectionManager] = {
            m.mode: m
            for m in (DirectSSHConnectionManager(), TailscaleConnectionManager())
        }
        self.connections: dict[str, ManagedConnection] = {}   # lambda instance id -> conn
        self._launch_tasks: dict[str, asyncio.Task] = {}

    # -- public API ------------------------------------------------------------

    async def request_launch(
        self,
        *,
        instance_type: str,
        region: str,
        filesystem: str,
        connection_mode: str | None = None,
        ssh_key_name: str | None = None,
        name: str = "",
    ) -> dict:
        """Validate and admit a launch; returns the persisted launch row.

        Raises LaunchRejected (with an HTTP status) on any validation or
        guardrail failure. On success the retry/boot/connect pipeline runs
        as a background task; poll GET /launches/{id} for progress.
        """
        mode = connection_mode or self.settings.default_connection_mode
        self._validate_mode(mode)

        types = await self.client.list_instance_types()
        if instance_type not in types:
            raise LaunchRejected(
                400,
                f"Unknown instance type '{instance_type}'. "
                f"Valid types: {', '.join(sorted(types))}",
            )

        filesystems = {fs.name: fs for fs in await self.client.list_filesystems()}
        if filesystem not in filesystems:
            raise LaunchRejected(
                400,
                f"Unknown filesystem '{filesystem}'. "
                f"Available: {', '.join(sorted(filesystems)) or '(none)'}",
            )
        fs = filesystems[filesystem]
        if fs.region != region:
            raise LaunchRejected(
                400,
                f"Region mismatch: filesystem '{filesystem}' lives in "
                f"{fs.region} but the launch requests {region}. Lambda "
                f"filesystems are region-locked and can only be attached at "
                f"launch — launch in {fs.region} instead.",
            )

        # SSH key: per-launch choice wins, config.yaml is the fallback. The
        # name must be registered with Lambda or the launch call would fail
        # minutes later — catch typos now.
        resolved_key = ssh_key_name or self.settings.ssh.key_name
        registered = [k.name for k in await self.client.list_ssh_keys()]
        if not resolved_key:
            raise LaunchRejected(
                400,
                "No SSH key selected. Pick one of your Lambda SSH keys "
                f"({', '.join(registered) or 'none registered yet'}) or set "
                "ssh.key_name in config.yaml.",
            )
        if resolved_key not in registered:
            raise LaunchRejected(
                400,
                f"SSH key '{resolved_key}' is not registered with Lambda. "
                f"Registered keys: {', '.join(registered) or '(none)'}.",
            )

        # Guards run against LIVE state, not our database, so instances
        # launched outside Manifold still count toward the limits.
        running = [i for i in await self.client.list_instances() if i.is_running]
        limit = self.settings.guardrails.max_concurrent_instances
        if len(running) + 1 > limit:
            raise LaunchRejected(
                409,
                f"Concurrency guard: {len(running)} instance(s) already "
                f"running, limit is {limit}. Terminate one first or raise "
                f"guardrails.max_concurrent_instances in config.yaml.",
            )

        current_spend = sum(i.hourly_rate_cents for i in running)
        budget_cents = round(self.settings.guardrails.max_hourly_spend_usd * 100)
        price = types[instance_type].price_cents_per_hour
        if current_spend + price > budget_cents:
            raise LaunchRejected(
                409,
                f"Budget guard: launching {instance_type} "
                f"(${price / 100:.2f}/hr) would bring hourly spend to "
                f"${(current_spend + price) / 100:.2f}, over the "
                f"${budget_cents / 100:.2f} limit "
                f"(guardrails.max_hourly_spend_usd in config.yaml).",
            )

        # Fallback types must exist and independently fit the budget;
        # ones that don't are skipped rather than failing the launch.
        candidates = [instance_type]
        for fb in self.settings.launch.fallback_instance_types:
            if fb == instance_type or fb in candidates or fb not in types:
                continue
            if current_spend + types[fb].price_cents_per_hour <= budget_cents:
                candidates.append(fb)

        launch_id = self.db.create_launch(
            requested_type=instance_type,
            region=region,
            filesystem=filesystem,
            connection_mode=mode,
            hourly_rate_cents=price,
        )
        plan = LaunchPlan(
            launch_id=launch_id,
            region=region,
            filesystem=filesystem,
            connection_mode=mode,
            ssh_key_name=resolved_key,
            types_to_try=candidates,
            prices={t: types[t].price_cents_per_hour for t in candidates},
            name=name or f"manifold-{launch_id}",
        )
        self._launch_tasks[launch_id] = asyncio.create_task(self._run_launch(plan))
        return self.db.get_launch(launch_id)

    def sidecar_for(self, instance_id: str) -> SidecarClient | None:
        conn = self.connections.get(instance_id)
        if conn is None:
            return None
        return self._sidecar_factory(conn)

    async def terminate(self, instance_id: str, *, force: bool = False) -> dict:
        """Terminate an instance, guarded by the unpersisted-files hook.

        Unless force=true, ask the sidecar (over the managed connection)
        what valuable files sit in ephemeral scratch; if any, raise
        TerminationBlocked with the list instead of terminating. If the
        sidecar is unreachable (still booting, connection down), proceed —
        the hook is best-effort evidence, not a way to wedge termination.
        """
        if not force:
            sidecar = self.sidecar_for(instance_id)
            if sidecar is not None:
                try:
                    report = await sidecar.unpersisted_files()
                    files = report.get("files", [])
                except SidecarError as exc:
                    logger.warning(
                        "sidecar check skipped for %s: %s", instance_id, exc
                    )
                    files = []
                if files:
                    raise TerminationBlocked(instance_id, files)

        conn = self.connections.pop(instance_id, None)
        if conn is not None:
            await conn.close()
        await self.client.terminate_instance(instance_id)
        launch = self.db.find_launch_by_instance(instance_id)
        if launch:
            self.db.update_launch(
                launch["id"], status="terminated", terminated_at=utcnow()
            )
        return {"instance_id": instance_id, "terminated": True}

    async def sync_ephemeral(self, instance_id: str) -> dict:
        """rsync ephemeral scratch to the persistent filesystem, over the
        managed connection. Destination: <mount>/ephemeral-backup/."""
        conn = self.connections.get(instance_id)
        if conn is None:
            raise LaunchRejected(409, f"no managed connection to {instance_id}")
        launch = self.db.find_launch_by_instance(instance_id)
        filesystem = launch["filesystem"] if launch else None
        if not filesystem:
            raise LaunchRejected(
                409, f"no filesystem recorded for {instance_id}; cannot sync"
            )
        dest = f"/lambda/nfs/{filesystem}/ephemeral-backup/"
        exit_status, stdout, stderr = await conn.run(
            f"mkdir -p {dest} && rsync -a --info=stats1 /workspace/ephemeral/ {dest}"
        )
        if exit_status != 0:
            raise LaunchRejected(
                502, f"sync failed (rsync exit {exit_status}): {stderr[:300]}"
            )
        self.db.record_audit("backend", "sync_ephemeral", f"{instance_id} -> {dest}")
        return {"instance_id": instance_id, "synced_to": dest,
                "rsync_stats": stdout.strip()[:500]}

    async def instances_with_state(self) -> list[dict]:
        """Live Lambda instances joined with connection state + launch info."""
        result = []
        for inst in await self.client.list_instances():
            conn = self.connections.get(inst.id)
            launch = self.db.find_launch_by_instance(inst.id)
            result.append({
                "id": inst.id,
                "name": inst.name,
                "status": inst.status,
                "ip": inst.ip,
                "region": inst.region,
                "instance_type": inst.instance_type,
                "gpu_description": inst.gpu_description,
                "hourly_rate_usd": inst.hourly_rate_cents / 100,
                "filesystems": inst.file_system_names,
                "connection_mode": launch["connection_mode"] if launch else None,
                "connection_state": conn.state.value if conn else "disconnected",
                "connection_error": conn.last_error if conn else "",
                "launch_id": launch["id"] if launch else None,
            })
        return result

    async def shutdown(self) -> None:
        """Close background tasks and connections (instances keep running)."""
        for task in self._launch_tasks.values():
            task.cancel()
        for conn in list(self.connections.values()):
            await conn.close()
        self.connections.clear()

    # -- launch pipeline ---------------------------------------------------------

    def _validate_mode(self, mode: str) -> None:
        if mode not in CONNECTION_MODES:
            raise LaunchRejected(
                400,
                f"Unknown connection mode '{mode}'. "
                f"Valid modes: {', '.join(CONNECTION_MODES)}",
            )
        if mode == "tailscale" and not self.settings.tailscale_authkey:
            raise LaunchRejected(
                400,
                "Connection mode 'tailscale' is unavailable: TAILSCALE_AUTHKEY "
                "is not set in .env. Add one or use direct-ssh.",
            )

    async def _run_launch(self, plan: LaunchPlan) -> None:
        try:
            launched = await self._launch_with_retry(plan)
            if launched is None:
                return  # already marked failed
            instance_id, launched_type = launched
            instance = await self._wait_until_active(plan, instance_id)
            if instance is None:
                return
            await self._establish_connection(plan, instance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("launch %s crashed", plan.launch_id)
            self.db.update_launch(
                plan.launch_id, status="failed",
                error=f"internal error: {exc}",
            )

    async def _launch_with_retry(self, plan: LaunchPlan) -> tuple[str, str] | None:
        """Try candidates with backoff. Returns (instance_id, type) or None."""
        policy = self.settings.launch
        attempts = 0
        for round_no in range(policy.max_attempts):
            for candidate in plan.types_to_try:
                attempts += 1
                self.db.update_launch(plan.launch_id, attempts=attempts)
                try:
                    instance_id = await self.client.launch_instance(
                        instance_type=candidate,
                        region=plan.region,
                        ssh_key_names=[plan.ssh_key_name],
                        filesystem_names=[plan.filesystem],
                        name=plan.name,
                        user_data=build_user_data(
                            # Only a tailscale-mode launch carries the key.
                            tailscale_authkey=(
                                self.settings.tailscale_authkey
                                if plan.connection_mode == "tailscale" else ""
                            ),
                            hostname=plan.name,
                        ),
                    )
                except LambdaAPIError as err:
                    if err.is_capacity_error:
                        logger.info(
                            "launch %s: no capacity for %s (attempt %d)",
                            plan.launch_id, candidate, attempts,
                        )
                        self.db.update_launch(
                            plan.launch_id, status="retrying",
                            error=f"attempt {attempts}: no capacity for "
                                  f"{candidate} in {plan.region}",
                        )
                        continue
                    # Non-capacity errors are not retryable: fail loudly.
                    self.db.update_launch(
                        plan.launch_id, status="failed",
                        error=f"{err.code}: {err.message}",
                    )
                    return None
                self.db.update_launch(
                    plan.launch_id,
                    status="booting",
                    lambda_instance_id=instance_id,
                    launched_type=candidate,
                    hourly_rate_cents=plan.prices[candidate],
                    launched_at=utcnow(),
                    error=None,
                )
                return instance_id, candidate
            if round_no < policy.max_attempts - 1:
                delay = backoff_delay(
                    round_no, policy.backoff_base_seconds, policy.backoff_max_seconds
                )
                await asyncio.sleep(delay)
        self.db.update_launch(
            plan.launch_id, status="failed",
            error=f"No capacity after {attempts} attempts across "
                  f"{', '.join(plan.types_to_try)} in {plan.region}. "
                  f"Try again later or add fallback types in config.yaml.",
        )
        return None

    async def _wait_until_active(
        self, plan: LaunchPlan, instance_id: str
    ) -> InstanceInfo | None:
        policy = self.settings.launch
        waited = 0.0
        while True:
            instance = await self.client.get_instance(instance_id)
            if instance.status == "active" and instance.ip:
                return instance
            if instance.status in ("terminated", "terminating", "preempted", "unhealthy"):
                self.db.update_launch(
                    plan.launch_id, status="failed",
                    error=f"instance entered status '{instance.status}' while booting",
                )
                return None
            if waited >= policy.boot_timeout_seconds:
                self.db.update_launch(
                    plan.launch_id, status="failed",
                    error=f"instance {instance_id} did not become active within "
                          f"{policy.boot_timeout_seconds:.0f}s — it may still be "
                          f"running and billing; check the dashboard and terminate "
                          f"it if unwanted.",
                )
                return None
            await asyncio.sleep(policy.boot_poll_seconds)
            waited += policy.boot_poll_seconds

    async def _establish_connection(
        self, plan: LaunchPlan, instance: InstanceInfo
    ) -> None:
        manager = self.managers[plan.connection_mode]
        host = manager.dial_target(instance)   # the only mode-specific step
        conn = ManagedConnection(
            host,
            self.settings.ssh,
            connect_fn=self._connect_fn(host) if self._connect_fn else None,
        )
        conn.start()
        self.connections[instance.id] = conn
        self.db.update_launch(plan.launch_id, status="active", active_at=utcnow())

    # -- test/introspection helpers ----------------------------------------------

    async def wait_for_launch(self, launch_id: str, timeout: float = 10.0) -> dict:
        """Wait until a launch reaches a settled state (active/failed).

        Used by tests and available to callers that want synchronous behavior.
        """
        task = self._launch_tasks.get(launch_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.db.get_launch(launch_id)

    def connection_state(self, instance_id: str) -> ConnectionState | None:
        conn = self.connections.get(instance_id)
        return conn.state if conn else None

"""Instance connectivity.

One managed SSH connection per instance carries everything: shell sessions,
port forwards, task push, rsync. The backend is always the SSH client.

Two layers, deliberately separated:

- ConnectionManager: decides WHERE to dial. This is the only thing that
  differs between direct-ssh (public IP) and tailscale (tailnet IP) modes.
  Everything above the dial is byte-identical.
- ManagedConnection: owns one asyncssh connection to one host, supervises it,
  and reconnects with exponential backoff when it drops. Exposes a state
  machine the dashboard renders on the instance card.

For tests, ManagedConnection accepts an injected `connect_fn` (anything that
returns an object with run/close/wait_closed) and an injected `sleep`, so
reconnect behavior is testable without a network or real clock.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import os
from typing import Awaitable, Callable

import asyncssh

from .config import SSHSettings
from .lambda_api import InstanceInfo

CONNECTION_MODES = ("direct-ssh", "tailscale")


class ConnectionState(str, enum.Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"   # deliberately closed
    FAILED = "failed"


class ConnectionManager(abc.ABC):
    """Chooses the address to dial for an instance. The mode swap point —
    no business logic lives here, and nothing else may branch on mode."""

    mode: str

    @abc.abstractmethod
    def dial_target(self, instance: InstanceInfo) -> str:
        """Return the host/IP the managed SSH connection should dial."""


class DirectSSHConnectionManager(ConnectionManager):
    mode = "direct-ssh"

    def dial_target(self, instance: InstanceInfo) -> str:
        if not instance.ip:
            raise ValueError(f"instance {instance.id} has no public IP yet")
        return instance.ip


class TailscaleConnectionManager(ConnectionManager):
    mode = "tailscale"

    def dial_target(self, instance: InstanceInfo) -> str:
        raise NotImplementedError("tailscale mode is implemented in Phase 3")


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff: base * 2^attempt, capped. attempt is 0-indexed."""
    return min(base * (2 ** attempt), cap)


class ManagedConnection:
    """One long-lived SSH connection to one instance, with auto-reconnect.

    Lifecycle: start() begins a supervisor task that connects, waits for the
    connection to drop, and reconnects with exponential backoff (forever,
    with a capped delay — if the instance is truly gone, terminating it
    closes this object). close() shuts everything down deliberately.
    """

    def __init__(
        self,
        host: str,
        ssh: SSHSettings,
        *,
        connect_fn: Callable[[], Awaitable] | None = None,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ):
        self.host = host
        self._ssh = ssh
        self._connect_fn = connect_fn or self._default_connect
        self._sleep = sleep
        self.state = ConnectionState.CONNECTING
        self.last_error: str = ""
        self._conn = None
        self._supervisor: asyncio.Task | None = None
        self._closing = False

    async def _default_connect(self):
        return await asyncssh.connect(
            self.host,
            username=self._ssh.username,
            client_keys=[os.path.expanduser(self._ssh.private_key_path)],
            # Fresh cloud instances have never-seen host keys; pinning is
            # recorded as a future hardening step in DECISIONS.md.
            known_hosts=None,
            connect_timeout=self._ssh.connect_timeout_seconds,
        )

    def start(self) -> None:
        self._supervisor = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        attempt = 0
        connected_before = False
        while not self._closing:
            try:
                self.state = (
                    ConnectionState.RECONNECTING if connected_before
                    else ConnectionState.CONNECTING
                )
                self._conn = await self._connect_fn()
                attempt = 0
                connected_before = True
                self.last_error = ""
                self.state = ConnectionState.CONNECTED
                await self._conn.wait_closed()
                if self._closing:
                    break
                self.last_error = "connection dropped"
                self.state = ConnectionState.RECONNECTING
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    break
                self.last_error = str(exc)
                self.state = ConnectionState.RECONNECTING
                delay = backoff_delay(
                    attempt,
                    self._ssh.reconnect_base_seconds,
                    self._ssh.reconnect_max_seconds,
                )
                attempt += 1
                await self._sleep(delay)
        self.state = ConnectionState.DISCONNECTED

    async def run(self, command: str) -> tuple[int, str, str]:
        """Run a command over the managed connection.

        Returns (exit_status, stdout, stderr). Raises ConnectionError when
        the connection is not currently up — callers decide how to react.
        """
        if self.state != ConnectionState.CONNECTED or self._conn is None:
            raise ConnectionError(
                f"no SSH connection to {self.host} (state: {self.state.value})"
            )
        result = await self._conn.run(command)
        return result.exit_status, result.stdout or "", result.stderr or ""

    async def close(self) -> None:
        self._closing = True
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        if self._conn is not None:
            self._conn.close()
        self.state = ConnectionState.DISCONNECTED


# -- Test double ---------------------------------------------------------------


class MockSSHConnection:
    """Stands in for an asyncssh connection in tests and mock mode."""

    def __init__(self):
        self._closed = asyncio.Event()
        self.commands: list[str] = []

    async def run(self, command: str):
        self.commands.append(command)

        class _Result:
            exit_status = 0
            stdout = f"mock output of: {command}"
            stderr = ""

        return _Result()

    def close(self) -> None:
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def simulate_drop(self) -> None:
        """Simulate the network dropping the connection."""
        self._closed.set()

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
import json
import os
from pathlib import Path
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
    """Dials the instance's tailnet MagicDNS name instead of its public IP.

    cloud-init joins the instance to the tailnet with hostname = the
    instance's Manifold name, so that name resolves on any tailnet machine
    (including this orchestrator host). Everything above the dial is
    byte-identical to direct-ssh.
    """

    mode = "tailscale"

    def dial_target(self, instance: InstanceInfo) -> str:
        if not instance.name:
            raise ValueError(
                f"instance {instance.id} has no name to resolve on the tailnet"
            )
        return instance.name


class HostKeyStore:
    """Trust-on-first-use SSH host key pins: one JSON file, host -> key line.

    A fresh cloud instance has a never-seen host key, so the first connect
    accepts whatever key the server presents and records it (TOFU). Every
    reconnect after that must present the same key or the connect fails —
    that closes the window where a rebooted/hijacked host could silently
    swap identities mid-lifecycle.

    Pins are forgotten when the instance is terminated (the orchestrator
    calls forget): Lambda recycles public IPs, so a stale pin would wrongly
    reject the next instance that gets the same address.
    """

    def __init__(self, path: str):
        self._path = Path(path)

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, pins: dict[str, str]) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(pins, indent=2, sort_keys=True))
        tmp.replace(self._path)

    def get(self, host: str) -> str | None:
        """The pinned public key line for a host, or None if never seen."""
        return self._load().get(host)

    def record(self, host: str, public_key: str) -> None:
        self._save({**self._load(), host: public_key.strip()})

    def forget(self, host: str) -> None:
        pins = self._load()
        if pins.pop(host, None) is not None:
            self._save(pins)


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
        host_keys: HostKeyStore | None = None,
    ):
        self.host = host
        self._ssh = ssh
        self._host_keys = host_keys
        self._connect_fn = connect_fn or self._default_connect
        self._sleep = sleep
        self.state = ConnectionState.CONNECTING
        self.last_error: str = ""
        self._conn = None
        self._supervisor: asyncio.Task | None = None
        self._closing = False

    async def _default_connect(self):
        # TOFU pinning: the first connect to a host trusts and records the
        # key it presents; every later connect must match that pin.
        pinned = self._host_keys.get(self.host) if self._host_keys else None
        try:
            conn = await asyncssh.connect(
                self.host,
                username=self._ssh.username,
                client_keys=[os.path.expanduser(self._ssh.private_key_path)],
                known_hosts=(
                    asyncssh.import_known_hosts(f"{self.host} {pinned}\n")
                    if pinned else None
                ),
                connect_timeout=self._ssh.connect_timeout_seconds,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            raise ConnectionError(
                f"host key for {self.host} does not match the key pinned on "
                f"first connect — possible MITM, or a stale pin if this IP "
                f"was recycled outside Manifold ({exc})"
            ) from exc
        if self._host_keys is not None and pinned is None:
            key = conn.get_server_host_key()
            if key is not None:
                self._host_keys.record(
                    self.host, key.export_public_key().decode()
                )
        return conn

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

    def ssh_connection(self):
        """The live asyncssh connection, or None when not connected.
        Used for port forwards; command execution should go through run()."""
        if self.state != ConnectionState.CONNECTED:
            return None
        return self._conn

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

    async def sftp_write(self, remote_path: str, chunks) -> int:
        """Stream chunks (an async iterator of bytes) to a remote file over
        SFTP, creating parent directories. Returns bytes written."""
        if self.state != ConnectionState.CONNECTED or self._conn is None:
            raise ConnectionError(
                f"no SSH connection to {self.host} (state: {self.state.value})"
            )
        import posixpath
        sftp = await self._conn.start_sftp_client()
        try:
            parent = posixpath.dirname(remote_path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            written = 0
            f = await sftp.open(remote_path, "wb")
            try:
                async for chunk in chunks:
                    await f.write(chunk)
                    written += len(chunk)
            finally:
                await f.close()
            return written
        finally:
            sftp.exit()

    async def sftp_read(self, remote_path: str, chunk_size: int = 65536):
        """Async iterator over a remote file's bytes."""
        if self.state != ConnectionState.CONNECTED or self._conn is None:
            raise ConnectionError(
                f"no SSH connection to {self.host} (state: {self.state.value})"
            )
        sftp = await self._conn.start_sftp_client()
        try:
            f = await sftp.open(remote_path, "rb")
            try:
                while True:
                    chunk = await f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await f.close()
        finally:
            sftp.exit()

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


# -- Test doubles ----------------------------------------------------------------


class _MockStream:
    """Reader side of a MockSSHProcess: read(n) and line iteration."""

    def __init__(self):
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._buffer = ""
        self._eof = False

    def _feed(self, data: str) -> None:
        self._queue.put_nowait(data)

    def _feed_eof(self) -> None:
        self._queue.put_nowait(None)

    async def read(self, n: int = -1) -> str:
        if self._buffer:
            data, self._buffer = self._buffer[:n], self._buffer[n:]
            return data
        if self._eof:
            return ""
        chunk = await self._queue.get()
        if chunk is None:
            self._eof = True
            return ""
        if n == -1 or len(chunk) <= n:
            return chunk
        self._buffer = chunk[n:]
        return chunk[:n]

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # Line iteration, used by the dispatcher's log streaming.
        while "\n" not in self._buffer:
            if self._eof:
                if self._buffer:
                    line, self._buffer = self._buffer, ""
                    return line
                raise StopAsyncIteration
            chunk = await self._queue.get()
            if chunk is None:
                self._eof = True
                continue
            self._buffer += chunk
        line, self._buffer = self._buffer.split("\n", 1)
        return line + "\n"


class MockSSHProcess:
    """Duck-types asyncssh's SSHClientProcess for two uses:

    - command mode (dispatcher): emits "mock output of: <command>", exits 0.
    - shell mode (terminal): a tiny fake shell with a prompt, echo, and
      canned responses for the commands the docs/demo exercise.
    """

    PROMPT = "ubuntu@mock-gpu:~$ "

    CANNED = {
        "nvidia-smi": (
            "+-----------------------------------------------------------+\n"
            "| NVIDIA-SMI 550.90       Driver: 550.90    CUDA: 12.4      |\n"
            "|  0  Mock A10   24564MiB / 24564MiB   34%   41C   P0  57W  |\n"
            "+-----------------------------------------------------------+"
        ),
        "nvcc --version": "Cuda compilation tools, release 12.4, V12.4.131 (mock)",
        "claude --version": "claude-code (mock install, launch with: claude)",
        "ls": "manifold-data  workspace",
        "pwd": "/home/ubuntu",
    }

    def __init__(self, command: str | None = None, term_size=(80, 24)):
        self.command = command
        self.term_size = term_size
        self.resizes: list[tuple[int, int]] = []
        self.stdin = self
        self.stdout = _MockStream()
        self.stderr = _MockStream()
        self.exit_status: int | None = None
        self._line = ""
        if command is not None:
            self.stdout._feed(f"mock output of: {command}\n")
            self.stdout._feed_eof()
            self.stderr._feed_eof()
            self.exit_status = 0
        else:
            self.stdout._feed(
                "Welcome to the Manifold mock shell (no GPU was billed).\r\n"
                + self.PROMPT
            )

    # stdin interface -----------------------------------------------------------
    def write(self, data: str) -> None:
        if self.command is not None:
            return
        # A real PTY echoes typed characters; do the same.
        for ch in data:
            if ch in ("\r", "\n"):
                self.stdout._feed("\r\n")
                self._run_line(self._line.strip())
                self._line = ""
            elif ch == "\x7f":  # backspace
                if self._line:
                    self._line = self._line[:-1]
                    self.stdout._feed("\b \b")
            else:
                self._line += ch
                self.stdout._feed(ch)

    def write_eof(self) -> None:
        pass

    def _run_line(self, line: str) -> None:
        if line:
            if line == "exit":
                self.stdout._feed("logout\r\n")
                self.exit_status = 0
                self.stdout._feed_eof()
                return
            output = self.CANNED.get(line, f"mock-shell: ran '{line}'")
            self.stdout._feed(output.replace("\n", "\r\n") + "\r\n")
        self.stdout._feed(self.PROMPT)

    # process interface -----------------------------------------------------------
    def change_terminal_size(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))
        self.term_size = (cols, rows)

    async def wait(self):
        # Command mode finishes instantly; shell mode waits for exit.
        while self.exit_status is None:
            await asyncio.sleep(0.01)
        return self

    def close(self) -> None:
        if self.exit_status is None:
            self.exit_status = -1
        self.stdout._feed_eof()
        self.stderr._feed_eof()


class _MockSFTPFile:
    """One open file against the MockSFTP in-memory store."""

    def __init__(self, store: dict, path: str, mode: str):
        self._store = store
        self._path = path
        self._mode = mode
        self._pos = 0
        if "w" in mode:
            store[path] = b""
        elif path not in store:
            raise FileNotFoundError(f"[mock sftp] no such file: {path}")

    async def write(self, data: bytes) -> None:
        self._store[self._path] += data

    async def read(self, n: int = -1) -> bytes:
        data = self._store[self._path]
        if n == -1:
            chunk, self._pos = data[self._pos:], len(data)
        else:
            chunk = data[self._pos:self._pos + n]
            self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        pass


class MockSFTP:
    """Duck-types the slice of asyncssh's SFTPClient that Manifold uses."""

    def __init__(self, store: dict):
        self.store = store
        self.makedirs_calls: list[str] = []

    async def makedirs(self, path: str, exist_ok: bool = False) -> None:
        self.makedirs_calls.append(path)

    async def open(self, path: str, mode: str) -> _MockSFTPFile:
        return _MockSFTPFile(self.store, path, mode)

    def exit(self) -> None:
        pass


class MockSSHConnection:
    """Stands in for an asyncssh connection in tests and mock mode."""

    def __init__(self):
        self._closed = asyncio.Event()
        self.commands: list[str] = []
        self.processes: list[MockSSHProcess] = []
        # In-memory remote filesystem shared by all SFTP sessions on this
        # connection: absolute remote path -> bytes.
        self.sftp_files: dict[str, bytes] = {}

    async def start_sftp_client(self) -> MockSFTP:
        return MockSFTP(self.sftp_files)

    async def run(self, command: str):
        self.commands.append(command)

        class _Result:
            exit_status = 0
            stdout = f"mock output of: {command}"
            stderr = ""

        return _Result()

    async def create_process(self, command: str | None = None, *,
                             term_type: str | None = None,
                             term_size=(80, 24), **kwargs):
        if command is not None:
            self.commands.append(command)
        process = MockSSHProcess(command, term_size=term_size)
        self.processes.append(process)
        return process

    def close(self) -> None:
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def simulate_drop(self) -> None:
        """Simulate the network dropping the connection."""
        self._closed.set()

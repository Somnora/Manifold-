"""Local pty lifecycle: no zombies, no orphaned children.

Every exited local shell used to sit as a zombie until the backend itself
exited (nothing ever waitpid()ed it), and closing a tab signalled only the
shell leader - children it left running lingered. _end_shell_group hangs
up the whole process group and reaps the child, escalating to SIGKILL.

These tests fork REAL processes (the suite already opens real local
shells through /local/terminal); POSIX only, like the endpoint itself.
"""

import asyncio
import os
import pty
import subprocess
import sys
import time

import pytest

from app.main import _end_shell_group

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX pty only")


def proc_state(pid: int) -> str:
    """ps STAT for pid: 'Z*' = zombie, '' = fully gone (reaped)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return out


def spawn_pty(argv: list[str]) -> int:
    pid, fd = pty.fork()
    if pid == 0:                        # child: never return into pytest
        try:
            os.execvp(argv[0], argv)
        except BaseException:
            pass
        os._exit(127)
    os.close(fd)
    return pid


async def wait_gone(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc_state(pid) == "":
            return True
        await asyncio.sleep(0.05)
    return False


async def test_live_shell_is_killed_and_reaped():
    pid = spawn_pty(["sleep", "60"])
    assert proc_state(pid) != ""            # alive
    _end_shell_group(pid)
    assert await wait_gone(pid), f"pid {pid} still {proc_state(pid)!r}"


async def test_naturally_exited_shell_is_not_left_a_zombie():
    pid = spawn_pty(["true"])               # exits immediately
    deadline = time.monotonic() + 5
    while "Z" not in proc_state(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert "Z" in proc_state(pid)           # the pre-fix state: a zombie
    _end_shell_group(pid)                   # reap-only path (already dead)
    assert await wait_gone(pid)


async def test_children_of_the_shell_die_with_the_group():
    # The "shell" backgrounds a child, then waits: killing only the leader
    # used to orphan that child. The group hangup must take both.
    pid = spawn_pty([
        "/bin/sh", "-c", "sleep 60 & echo $! > /dev/null; wait",
    ])
    await asyncio.sleep(0.3)                # let it fork the background child
    out = subprocess.run(
        ["pgrep", "-g", str(pid)], capture_output=True, text=True
    ).stdout.split()
    assert len(out) >= 2, "expected leader + backgrounded child in the group"
    _end_shell_group(pid)
    assert await wait_gone(pid)

    async def group_empty() -> bool:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            left = subprocess.run(
                ["pgrep", "-g", str(pid)], capture_output=True, text=True
            ).stdout.split()
            if not left:
                return True
            await asyncio.sleep(0.05)
        return False

    assert await group_empty(), "background child survived the group hangup"


# -- fd lifecycle: churn must not leak master descriptors -------------------------

ORIGIN = {"origin": "http://localhost:3000"}


def _fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def _settles_at(base: int, timeout: float = 8.0) -> int:
    """The lowest fd count observed while waiting to return to `base`
    (teardown is async: reap tasks and socket closes need a beat).
    Synchronous on purpose: an asyncio.run here would open its own
    kqueue+socketpair fds mid-measurement and read as a 3-fd leak."""
    deadline = time.monotonic() + timeout
    lowest = _fd_count()
    while time.monotonic() < deadline:
        lowest = min(lowest, _fd_count())
        if lowest <= base:
            return lowest
        time.sleep(0.1)
    return lowest


def test_abrupt_teardown_churn_leaks_no_descriptors(client, monkeypatch):
    """Chaos check: open shells and drop their sockets abruptly, repeatedly.
    Ephemeral sessions (no ?session=) are killed on drop; the process
    fd table must return to its baseline - a rise of even one descriptor
    is a leak in the master-fd lifecycle."""
    monkeypatch.setenv("SHELL", "/bin/sh")     # fast, hermetic shell
    # Warm-up: first session pays one-time import/allocation costs.
    with client.websocket_connect("/local/terminal", headers=ORIGIN) as ws:
        ws.receive_text()
    time.sleep(0.5)
    base = _fd_count()

    for _ in range(10):
        with client.websocket_connect("/local/terminal",
                                      headers=ORIGIN) as ws:
            ws.receive_text()                  # shell is up
        # context exit = abrupt socket drop, no clean close message

    lowest = _settles_at(base)
    assert lowest <= base, f"leaked {lowest - base} fd(s) over 10 sessions"


def test_natural_shell_exit_closes_the_master_fd(client, monkeypatch):
    """The pre-fix leak: a shell that exits on its own left its master fd
    open (and unregistered from nothing but the selector) until the
    backend itself exited - one descriptor per exited shell."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    with client.websocket_connect("/local/terminal", headers=ORIGIN) as ws:
        ws.receive_text()
    time.sleep(0.5)
    base = _fd_count()

    with client.websocket_connect("/local/terminal", headers=ORIGIN) as ws:
        ws.receive_text()
        ws.send_json({"type": "input", "data": "exit\n"})
        try:
            while True:
                ws.receive_text()              # drain until server closes
        except Exception:
            pass

    lowest = _settles_at(base)
    assert lowest <= base, "master fd survived a natural shell exit"


def test_shell_exiting_while_detached_still_closes_the_master_fd(
        client, monkeypatch):
    """THE pre-fix leak. With a socket attached, teardown funnels through
    kill() -> close_pty either way. But a shell that exited while DETACHED
    (refresh, never reattached) was only reaped: the registry dropped the
    exited session without ever closing its master fd, one leaked
    descriptor per such shell for the life of the backend."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    with client.websocket_connect("/local/terminal", headers=ORIGIN) as ws:
        ws.receive_text()
    time.sleep(0.5)
    base = _fd_count()

    with client.websocket_connect("/local/terminal?session=fd-detach",
                                  headers=ORIGIN) as ws:
        ws.receive_text()
        ws.send_json({"type": "input", "data": "sleep 1; exit\n"})
        # Context exit drops the socket NOW; with a session id that
        # DETACHES the shell, which then exits ~1s later on its own.

    lowest = _settles_at(base, timeout=10.0)
    assert lowest <= base, "master fd leaked when the shell exited detached"

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

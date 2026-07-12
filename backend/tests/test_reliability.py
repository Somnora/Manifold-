"""Connection-reliability pass: SSH keepalive so a dead link is noticed in
seconds not minutes, a per-command timeout so a stalled mount can't wedge a
request forever, and a short-TTL cache on list_instances so the 2s dashboard
poll does not hammer Lambda's rate-limited API. All driven by "backend errors
appearing periodically" during live testing."""

import asyncio

import asyncssh
import pytest

from app.config import SSHSettings
from app.connections import ConnectionState, ManagedConnection
from app.lambda_api import SwappableLambdaClient


# -- SSH keepalive ----------------------------------------------------------------


def test_connect_passes_keepalive(monkeypatch):
    """The managed connection asks asyncssh to ping the link and drop it
    after a few misses, so the supervisor can reconnect promptly."""
    captured = {}

    async def fake_connect(host, **kwargs):
        captured.update(kwargs)

        class _Conn:
            def get_server_host_key(self):
                return None
        return _Conn()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    mc = ManagedConnection(
        "203.0.113.9",
        SSHSettings(private_key_path="/dev/null",
                    keepalive_interval_seconds=15, keepalive_count_max=3),
    )
    asyncio.run(mc._default_connect())
    assert captured["keepalive_interval"] == 15
    assert captured["keepalive_count_max"] == 3


# -- per-command timeout ----------------------------------------------------------


class _SlowConn:
    def __init__(self, delay):
        self._delay = delay

    async def run(self, command):
        await asyncio.sleep(self._delay)

        class _R:
            exit_status = 0
            stdout = "done"
            stderr = ""
        return _R()


def _connected(mc, conn):
    mc.state = ConnectionState.CONNECTED
    mc._conn = conn
    return mc


def test_run_times_out_on_a_stalled_command():
    mc = _connected(
        ManagedConnection("h", SSHSettings(command_timeout_seconds=0.1)),
        _SlowConn(delay=5),
    )
    with pytest.raises(ConnectionError, match="timed out after"):
        asyncio.run(mc.run("sleep 5"))


def test_run_returns_before_timeout():
    mc = _connected(
        ManagedConnection("h", SSHSettings(command_timeout_seconds=5)),
        _SlowConn(delay=0),
    )
    exit_status, out, err = asyncio.run(mc.run("echo done"))
    assert (exit_status, out) == (0, "done")


def test_run_timeout_none_waits():
    """timeout=None disables the ceiling (job dispatch streams for hours)."""
    mc = _connected(
        ManagedConnection("h", SSHSettings(command_timeout_seconds=0.01)),
        _SlowConn(delay=0.05),
    )
    # Would raise if the 0.01s default applied; None lets it complete.
    exit_status, out, _ = asyncio.run(mc.run("x", timeout=None))
    assert exit_status == 0


# -- list_instances cache ---------------------------------------------------------


class _CountingInner:
    """Duck-typed LambdaClient that counts upstream calls and returns a fresh
    list object each time (so identity distinguishes cache hits from misses)."""

    def __init__(self):
        self.list_calls = 0
        self.launched = 0
        self.terminated = 0

    async def list_instances(self):
        self.list_calls += 1
        return [f"snapshot-{self.list_calls}"]

    async def launch_instance(self, **kwargs):
        self.launched += 1
        return {"id": "i-new"}

    async def terminate_instance(self, instance_id):
        self.terminated += 1
        return {"terminated": True}

    async def close(self):
        pass


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_list_instances_cached_within_ttl():
    clock = _Clock()
    inner = _CountingInner()
    client = SwappableLambdaClient(inner, cache_ttl_seconds=2.0, clock=clock)

    a = asyncio.run(client.list_instances())
    b = asyncio.run(client.list_instances())   # within TTL -> cached
    assert a is b
    assert inner.list_calls == 1

    clock.t = 2.5                               # past TTL -> refetch
    c = asyncio.run(client.list_instances())
    assert c is not a
    assert inner.list_calls == 2


def test_launch_and_terminate_invalidate_cache():
    clock = _Clock()
    inner = _CountingInner()
    client = SwappableLambdaClient(inner, cache_ttl_seconds=100.0, clock=clock)

    asyncio.run(client.list_instances())
    assert inner.list_calls == 1

    # A launch we initiate must show up immediately, not wait out the TTL.
    asyncio.run(client.launch_instance(instance_type="gpu_1x_a10"))
    asyncio.run(client.list_instances())
    assert inner.list_calls == 2

    asyncio.run(client.terminate_instance("i-1"))
    asyncio.run(client.list_instances())
    assert inner.list_calls == 3


def test_fresh_bypasses_cache_for_the_spend_guard():
    """fresh=True (used by the concurrency guard) must always hit the API,
    even within the TTL, so a guard never decides on a stale snapshot."""
    clock = _Clock()
    inner = _CountingInner()
    client = SwappableLambdaClient(inner, cache_ttl_seconds=100.0, clock=clock)

    asyncio.run(client.list_instances())          # populate cache
    assert inner.list_calls == 1
    asyncio.run(client.list_instances(fresh=True))  # bypass despite TTL
    assert inner.list_calls == 2
    # And the forced read refreshed the cache for ordinary readers.
    asyncio.run(client.list_instances())
    assert inner.list_calls == 2


def test_inner_swap_invalidates_cache():
    """New credentials must not serve the old account's cached instances."""
    clock = _Clock()
    inner1, inner2 = _CountingInner(), _CountingInner()
    client = SwappableLambdaClient(inner1, cache_ttl_seconds=100.0, clock=clock)

    asyncio.run(client.list_instances())
    client.inner = inner2                       # swap credentials
    asyncio.run(client.list_instances())
    assert inner2.list_calls == 1               # fetched fresh from the new one

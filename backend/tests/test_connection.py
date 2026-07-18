"""ManagedConnection reconnect behavior and the ConnectionManager swap point."""

import asyncio

import pytest

from app.config import SSHSettings
from app.connections import (
    CONNECTION_MODES,
    ConnectionManager,
    ConnectionState,
    DirectSSHConnectionManager,
    ManagedConnection,
    MockSSHConnection,
    TailscaleConnectionManager,
    backoff_delay,
)
from app.lambda_api import InstanceInfo


SSH = SSHSettings(key_name="k", reconnect_base_seconds=1, reconnect_max_seconds=30)


def make_instance(ip="203.0.113.7") -> InstanceInfo:
    return InstanceInfo(
        id="inst1", name="test", status="active", ip=ip,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=75,
    )


async def wait_state(conn: ManagedConnection, state: ConnectionState, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if conn.state == state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"never reached {state}, stuck at {conn.state}")


def test_backoff_is_exponential_and_capped():
    assert [backoff_delay(n, 1, 30) for n in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    assert backoff_delay(0, 5, 120) == 5
    assert backoff_delay(10, 5, 120) == 120


def test_backoff_survives_an_overnight_outage():
    # ~1000 retries in, base * 2**attempt overflowed float and the
    # OverflowError - raised inside the supervisor's retry handler -
    # killed the supervisor, so the connection never retried again.
    assert backoff_delay(5000, 1.0, 60.0) == 60.0


async def test_mock_stream_read_all_does_not_drop_the_last_char():
    from app.connections import _MockStream
    stream = _MockStream()
    stream._feed("abc")
    assert await stream.read(2) == "ab"      # leaves "c" buffered
    assert await stream.read() == "c"        # read-all sliced [:-1] before


async def test_connect_retries_with_exponential_backoff():
    """First two dials fail; backoff delays follow base * 2^n."""
    dials = 0
    slept: list[float] = []

    async def flaky_connect():
        nonlocal dials
        dials += 1
        if dials <= 2:
            raise OSError(f"connection refused (dial {dials})")
        return MockSSHConnection()

    async def fake_sleep(delay):
        slept.append(delay)

    conn = ManagedConnection("203.0.113.7", SSH,
                             connect_fn=flaky_connect, sleep=fake_sleep)
    conn.start()
    await wait_state(conn, ConnectionState.CONNECTED)
    assert dials == 3
    assert slept == [1, 2]           # exponential: 1s, then 2s
    assert conn.last_error == ""
    await conn.close()


async def test_reconnects_after_drop():
    connections: list[MockSSHConnection] = []

    async def connect():
        c = MockSSHConnection()
        connections.append(c)
        return c

    conn = ManagedConnection("203.0.113.7", SSH, connect_fn=connect)
    conn.start()
    await wait_state(conn, ConnectionState.CONNECTED)

    connections[0].simulate_drop()
    # Wait until a FRESH connection is up (state alone would race the drop).
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if len(connections) == 2 and conn.state == ConnectionState.CONNECTED:
            break
        await asyncio.sleep(0.005)
    assert len(connections) == 2     # a fresh connection replaced the dropped one
    assert conn.state == ConnectionState.CONNECTED
    await conn.close()
    assert conn.state == ConnectionState.DISCONNECTED


async def test_run_requires_connection():
    async def never_connect():
        raise OSError("host unreachable")

    async def fake_sleep(_):
        await asyncio.sleep(0)

    conn = ManagedConnection("203.0.113.7", SSH,
                             connect_fn=never_connect, sleep=fake_sleep)
    conn.start()
    await asyncio.sleep(0.02)
    assert conn.state == ConnectionState.RECONNECTING
    assert "unreachable" in conn.last_error
    with pytest.raises(ConnectionError):
        await conn.run("nvidia-smi")
    await conn.close()


async def test_run_executes_over_connection():
    mock = MockSSHConnection()

    async def connect():
        return mock

    conn = ManagedConnection("203.0.113.7", SSH, connect_fn=connect)
    conn.start()
    await wait_state(conn, ConnectionState.CONNECTED)
    exit_status, stdout, _ = await conn.run("nvidia-smi")
    assert exit_status == 0
    assert mock.commands == ["nvidia-smi"]
    await conn.close()


# -- ConnectionManager: the mode swap point ------------------------------------


def test_contract_both_managers_are_interchangeable():
    """The two modes must be drop-in replacements: same interface, same
    behavior shape — a valid instance yields a dialable host string, an
    incomplete one raises ValueError. Only the returned address differs."""
    instance = make_instance()
    managers = [DirectSSHConnectionManager(), TailscaleConnectionManager()]
    assert [m.mode for m in managers] == list(CONNECTION_MODES)
    for manager in managers:
        assert isinstance(manager, ConnectionManager)
        host = manager.dial_target(instance)
        assert isinstance(host, str) and host
        # ConnectionManager's whole public surface is `mode` and
        # `dial_target` — anything more would be a place for mode-specific
        # logic to hide.
        public = {n for n in dir(manager) if not n.startswith("_")}
        assert public == {"mode", "dial_target"}


def test_direct_ssh_dials_public_ip():
    assert DirectSSHConnectionManager().dial_target(make_instance()) == "203.0.113.7"


def test_direct_ssh_requires_ip():
    with pytest.raises(ValueError):
        DirectSSHConnectionManager().dial_target(make_instance(ip=None))


def test_tailscale_dials_tailnet_hostname():
    instance = make_instance()
    instance.name = "manifold-abc123"
    assert TailscaleConnectionManager().dial_target(instance) == "manifold-abc123"


def test_tailscale_requires_name():
    instance = make_instance()
    instance.name = ""
    with pytest.raises(ValueError):
        TailscaleConnectionManager().dial_target(instance)

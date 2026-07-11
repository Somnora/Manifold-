"""Host-key pinning: TOFU record on first connect, enforcement on
reconnect, forget on termination. No network — asyncssh.connect is
monkeypatched; the keys themselves are real asyncssh keys."""

import asyncio

import asyncssh
import pytest

from app.connections import HostKeyStore, ManagedConnection
from app.config import SSHSettings


# -- HostKeyStore ----------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = HostKeyStore(str(tmp_path / "host_keys.json"))
    assert store.get("1.2.3.4") is None
    store.record("1.2.3.4", "ssh-ed25519 AAAA-key-one\n")
    assert store.get("1.2.3.4") == "ssh-ed25519 AAAA-key-one"

    # Persists across instances (it is a file, not memory).
    again = HostKeyStore(str(tmp_path / "host_keys.json"))
    assert again.get("1.2.3.4") == "ssh-ed25519 AAAA-key-one"

    again.forget("1.2.3.4")
    assert store.get("1.2.3.4") is None
    again.forget("never-seen")   # forgetting the unknown is a no-op


def test_store_survives_corrupt_file(tmp_path):
    path = tmp_path / "host_keys.json"
    path.write_text("{not json")
    store = HostKeyStore(str(path))
    assert store.get("1.2.3.4") is None
    store.record("1.2.3.4", "k")
    assert store.get("1.2.3.4") == "k"


# -- ManagedConnection TOFU ------------------------------------------------------


class FakeSSHConn:
    """Just enough of an asyncssh connection for _default_connect."""

    def __init__(self, host_key):
        self._host_key = host_key

    def get_server_host_key(self):
        return self._host_key


def make_managed(tmp_path, monkeypatch, server_key):
    """A ManagedConnection whose asyncssh.connect is captured, presenting
    server_key as the host key. Returns (mc, calls) where calls collects
    the known_hosts argument of each connect."""
    store = HostKeyStore(str(tmp_path / "host_keys.json"))
    calls = []

    async def fake_connect(host, **kwargs):
        calls.append(kwargs["known_hosts"])
        # Mirror asyncssh: if a known_hosts is given, validate the presented
        # key against it and fail on mismatch.
        kh = kwargs["known_hosts"]
        if kh is not None:
            trusted = kh.match(host, host, 22)[0]
            presented = server_key.convert_to_public().export_public_key()
            if not any(k.export_public_key() == presented for k in trusted):
                raise asyncssh.HostKeyNotVerifiable("Host key is not trusted")
        return FakeSSHConn(server_key.convert_to_public())

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    mc = ManagedConnection(
        "203.0.113.7",
        SSHSettings(key_name="test", private_key_path="/dev/null"),
        host_keys=store,
    )
    return mc, store, calls


def test_first_connect_records_pin_and_reconnect_enforces_it(
        tmp_path, monkeypatch):
    key = asyncssh.generate_private_key("ssh-ed25519")
    mc, store, calls = make_managed(tmp_path, monkeypatch, key)

    # First connect: no pin yet -> TOFU (known_hosts=None), then recorded.
    asyncio.run(mc._default_connect())
    assert calls[0] is None
    pinned = store.get("203.0.113.7")
    assert pinned == key.export_public_key().decode().strip()

    # Reconnect: the pin is enforced (known_hosts set) and matches.
    asyncio.run(mc._default_connect())
    assert calls[1] is not None
    trusted = calls[1].match("203.0.113.7", "203.0.113.7", 22)[0]
    assert {k.export_public_key() for k in trusted} == {
        key.convert_to_public().export_public_key()
    }


def test_changed_host_key_is_rejected_with_clear_error(tmp_path, monkeypatch):
    key = asyncssh.generate_private_key("ssh-ed25519")
    mc, store, _ = make_managed(tmp_path, monkeypatch, key)
    asyncio.run(mc._default_connect())   # records the pin

    # Same host now presents a DIFFERENT key (MITM / silent replacement).
    imposter = asyncssh.generate_private_key("ssh-ed25519")
    mc2, _, _ = make_managed(tmp_path, monkeypatch, imposter)
    with pytest.raises(ConnectionError, match="does not match the key pinned"):
        asyncio.run(mc2._default_connect())
    # The imposter's key must NOT replace the pin.
    assert store.get("203.0.113.7") == key.export_public_key().decode().strip()


def test_no_store_means_previous_behavior(tmp_path, monkeypatch):
    """Without a store (e.g. bare ManagedConnection in scripts/tests),
    connects stay TOFU-less: known_hosts=None every time, nothing written."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    calls = []

    async def fake_connect(host, **kwargs):
        calls.append(kwargs["known_hosts"])
        return FakeSSHConn(key.convert_to_public())

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    mc = ManagedConnection(
        "203.0.113.7",
        SSHSettings(key_name="test", private_key_path="/dev/null"),
    )
    asyncio.run(mc._default_connect())
    asyncio.run(mc._default_connect())
    assert calls == [None, None]


# -- Orchestrator: pins are forgotten at termination -------------------------------


def test_terminate_forgets_pin(client):
    """Terminating through the API drops the host's pin, so a recycled IP
    with a fresh host key is not wrongly rejected."""
    from tests.test_reconcile import launch_connected

    orch = client.app.state.orchestrator
    launch_id, instance_id = launch_connected(client)
    conn = orch.connections[instance_id]
    orch.host_keys.record(conn.host, "ssh-ed25519 AAAA-pinned")

    resp = client.delete(f"/instances/{instance_id}?force=true")
    assert resp.status_code == 200
    assert orch.host_keys.get(conn.host) is None

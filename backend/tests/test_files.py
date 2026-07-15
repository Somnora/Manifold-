"""File bridge: upload/download over the managed SSH connection (SFTP)."""

import io

import httpx
import pytest

import app.mcp_server as mcp_server
from tests.test_reconcile import launch_connected


def sftp_store(client, instance_id) -> dict:
    """The mock SFTP in-memory filesystem behind the managed connection."""
    conn = client.app.state.orchestrator.connections[instance_id]
    return conn.ssh_connection().sftp_files


def test_upload_relative_lands_on_persistent(client):
    _, instance_id = launch_connected(client)
    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("sprite.png", io.BytesIO(b"PNG-bytes"), "image/png")},
        data={"dest": "inbox/"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"path": "/lambda/nfs/manifold-data/inbox/sprite.png",
                    "bytes": 9}
    assert sftp_store(client, instance_id)[body["path"]] == b"PNG-bytes"

    # Audited.
    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "file_upload" for e in audit)


def test_upload_explicit_filename_and_absolute_path(client):
    _, instance_id = launch_connected(client)
    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("x.bin", io.BytesIO(b"abc"))},
        data={"dest": "/workspace/ephemeral/scratch/renamed.bin"},
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "/workspace/ephemeral/scratch/renamed.bin"


def test_upload_rejects_traversal_and_foreign_roots(client):
    _, instance_id = launch_connected(client)
    for dest in ("../../etc/cron.d/evil", "/etc/passwd",
                 "/lambda/nfs/../../root/x", "/home/ubuntu/.ssh/keys"):
        resp = client.post(
            f"/instances/{instance_id}/files/upload",
            files={"file": ("evil", io.BytesIO(b"x"))},
            data={"dest": dest},
        )
        assert resp.status_code == 400, dest
        assert "must stay under" in resp.json()["detail"]
    assert all("evil" not in p and "passwd" not in p
               for p in sftp_store(client, instance_id))


def test_download_roundtrip(client):
    _, instance_id = launch_connected(client)
    store = sftp_store(client, instance_id)
    store["/lambda/nfs/manifold-data/outputs/model.glb"] = b"GLB" * 1000

    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/model.glb"},
    )
    assert resp.status_code == 200
    assert resp.content == b"GLB" * 1000
    assert 'filename="model.glb"' in resp.headers["content-disposition"]


def test_download_missing_file_is_404(client):
    _, instance_id = launch_connected(client)
    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/nope.glb"},
    )
    assert resp.status_code == 404


def test_transfer_requires_connected_instance(client):
    resp = client.post(
        "/instances/i-none/files/upload",
        files={"file": ("x", io.BytesIO(b"x"))},
        data={"dest": "inbox/"},
    )
    assert resp.status_code == 409
    resp = client.get("/instances/i-none/files/download",
                      params={"path": "outputs/a.txt"})
    assert resp.status_code == 409


# -- MCP tools --------------------------------------------------------------------


@pytest.fixture
async def mcp_wired_client(tmp_path, mock_client, mock_storage, mock_sidecar):
    """Real app + MCP module pointed at it in-process (same as test_mcp)."""
    from asgi_lifespan import LifespanManager
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn

    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    async with LifespanManager(app) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield app
        await mcp_server._client.aclose()
        mcp_server._client = old


async def _launch_connected_async(app, mock_client):
    import asyncio
    from app.connections import ConnectionState
    orch = app.state.orchestrator
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    instance_id = final["lambda_instance_id"]
    for _ in range(200):
        conn = orch.connections.get(instance_id)
        if conn and conn.state == ConnectionState.CONNECTED:
            return instance_id
        await asyncio.sleep(0.01)
    raise AssertionError("never connected")


async def test_mcp_upload_download_roundtrip(mcp_wired_client, mock_client,
                                             tmp_path):
    app = mcp_wired_client
    instance_id = await _launch_connected_async(app, mock_client)

    # Agent uploads a local sprite (instance auto-selected: only one).
    local = tmp_path / "sprite.png"
    local.write_bytes(b"sprite-data")
    result = await mcp_server.upload_file(str(local), "inbox/",
                                          note="asset for 3d gen")
    assert result == {"path": "/lambda/nfs/manifold-data/inbox/sprite.png",
                      "bytes": 11}

    # ...a job would produce an output; simulate it, then download it back.
    conn = app.state.orchestrator.connections[instance_id]
    conn.ssh_connection().sftp_files[
        "/lambda/nfs/manifold-data/outputs/sprite.glb"] = b"mesh-bytes"
    out = tmp_path / "downloads" / "sprite.glb"
    result = await mcp_server.download_file("outputs/sprite.glb", str(out),
                                            note="fetch the mesh")
    assert result == {"local_path": str(out), "bytes": 10}
    assert out.read_bytes() == b"mesh-bytes"


async def test_mcp_upload_missing_local_file(mcp_wired_client):
    result = await mcp_server.upload_file("/no/such/file.png")
    assert "local file not found" in result["error"]


def test_mcp_server_still_structurally_thin():
    """The new tools must not have widened the import allowlist."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(mcp_server.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    # asyncio: stdlib retry plumbing for wait_for_launch, not backend access.
    assert imported <= {"__future__", "asyncio", "os", "typing", "httpx",
                        "mcp.server.fastmcp"}
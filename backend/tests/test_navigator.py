"""File navigator: browse / usage / delete (sidecar-backed) + tar.gz archive."""

import hashlib

from tests.test_reconcile import launch_connected


def test_list_navigates_the_tree(client):
    _, instance_id = launch_connected(client)
    body = client.get(f"/instances/{instance_id}/files/list",
                      params={"root_name": "persistent", "path": ""}).json()
    assert [e["name"] for e in body["entries"]] == ["manifold-data"]

    body = client.get(f"/instances/{instance_id}/files/list", params={
        "root_name": "persistent", "path": "manifold-data",
    }).json()
    names = [e["name"] for e in body["entries"]]
    assert "research" in names and "models" in names
    assert all(e["is_dir"] for e in body["entries"])   # demo tree top level

    body = client.get(f"/instances/{instance_id}/files/list", params={
        "root_name": "persistent", "path": "manifold-data/research/scrapes",
    }).json()
    files = {e["name"]: e for e in body["entries"]}
    assert files["candidates-2026-raw.jsonl"]["size_bytes"] == 3_221_225_472
    assert files["candidates-2026-raw.jsonl"]["is_dir"] is False


def test_list_errors_map_to_http(client):
    _, instance_id = launch_connected(client)
    assert client.get(f"/instances/{instance_id}/files/list", params={
        "root_name": "persistent", "path": "no/such/dir",
    }).status_code == 404
    assert client.get(f"/instances/{instance_id}/files/list", params={
        "root_name": "persistent", "path": "../etc",
    }).status_code == 400
    assert client.get("/instances/i-none/files/list").status_code == 409


def test_usage_heaviest_first(client):
    _, instance_id = launch_connected(client)
    body = client.get(f"/instances/{instance_id}/files/usage", params={
        "root_name": "persistent", "path": "manifold-data",
    }).json()
    names = [c["name"] for c in body["children"]]
    # models (16GB) > research (~4.3GB) > cache (2GB) > datasets > outputs
    assert names[0] == "models"
    assert names[1] == "research"
    research = body["children"][1]
    assert research["total_bytes"] == 3_221_225_472 + 1_073_741_824 + 4_096
    assert research["file_count"] == 3


def test_delete_flow_with_directory_guard(client):
    _, instance_id = launch_connected(client)
    # Directory without recursive -> 409 with guidance.
    resp = client.delete(f"/instances/{instance_id}/files", params={
        "root_name": "persistent", "path": "manifold-data/research",
    })
    assert resp.status_code == 409
    assert "recursive" in resp.json()["detail"]

    # With recursive: the unsynthesized research is gone.
    resp = client.delete(f"/instances/{instance_id}/files", params={
        "root_name": "persistent", "path": "manifold-data/research",
        "recursive": "true",
    })
    assert resp.status_code == 200
    listing = client.get(f"/instances/{instance_id}/files/list", params={
        "root_name": "persistent", "path": "manifold-data",
    }).json()
    assert "research" not in [e["name"] for e in listing["entries"]]

    # Audited.
    audit = client.get("/audit").json()["entries"]
    entry = next(e for e in audit if e["action"] == "file_delete")
    assert "research" in entry["detail"] and "(recursive)" in entry["detail"]


def test_delete_refuses_root(client):
    _, instance_id = launch_connected(client)
    resp = client.delete(f"/instances/{instance_id}/files", params={
        "root_name": "persistent", "path": "", "recursive": "true",
    })
    assert resp.status_code == 400
    assert "root" in resp.json()["detail"]


def test_archive_streams_targz(client):
    _, instance_id = launch_connected(client)
    conn = client.app.state.orchestrator.connections[instance_id]
    ssh = conn.ssh_connection()

    # The mock run() records but doesn't execute tar, so seed the archive
    # bytes at the deterministic temp path the endpoint computes.
    remote = "/lambda/nfs/manifold-data/outputs"
    tmp = ("/workspace/ephemeral/.manifold-archives/"
           + hashlib.sha256(remote.encode()).hexdigest()[:16] + ".tar.gz")
    ssh.sftp_files[tmp] = b"\x1f\x8b-fake-targz-bytes"

    resp = client.get(f"/instances/{instance_id}/files/archive",
                      params={"path": "outputs"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert 'filename="outputs.tar.gz"' in resp.headers["content-disposition"]
    assert resp.content == b"\x1f\x8b-fake-targz-bytes"

    # tar ran on the instance with quoted paths, and the temp was cleaned.
    tar_cmd = next(c for c in ssh.commands if "tar czf" in c)
    assert "-C /lambda/nfs/manifold-data outputs" in tar_cmd
    assert any(c.startswith("rm -f ") and tmp in c for c in ssh.commands)


def test_archive_rejects_jail_escape(client):
    _, instance_id = launch_connected(client)
    resp = client.get(f"/instances/{instance_id}/files/archive",
                      params={"path": "/etc"})
    assert resp.status_code == 400

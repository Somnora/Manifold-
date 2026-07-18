"""Creating persistent filesystems from Manifold (POST /filesystems).

Creation is free (storage bills by GB-month used), so no spend guard
applies; what IS enforced: a sane name, a known region, and an audit row.
"""


def test_create_filesystem_appears_in_list(client):
    resp = client.post("/filesystems", json={
        "name": "texas-filebase", "region": "us-south-1",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "texas-filebase"
    assert body["region"] == "us-south-1"
    assert body["mount_point"] == "/lambda/nfs/texas-filebase"
    assert body["is_in_use"] is False

    names = [fs["name"] for fs in
             client.get("/filesystems").json()["filesystems"]]
    assert "texas-filebase" in names

    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "filesystem_created"
               and "texas-filebase" in e["detail"] for e in audit)


def test_create_filesystem_rejects_bad_name_and_region(client):
    bad_name = client.post("/filesystems", json={
        "name": "no spaces allowed", "region": "us-east-1",
    })
    assert bad_name.status_code == 422

    empty = client.post("/filesystems", json={
        "name": "   ", "region": "us-east-1",
    })
    assert empty.status_code == 422

    bad_region = client.post("/filesystems", json={
        "name": "fine-name", "region": "mars-north-1",
    })
    assert bad_region.status_code == 422
    assert "region" in bad_region.json()["detail"].lower()


def test_create_filesystem_duplicate_name_surfaces_api_error(client):
    first = client.post("/filesystems", json={
        "name": "dupe-check", "region": "us-east-1",
    })
    assert first.status_code == 201
    second = client.post("/filesystems", json={
        "name": "dupe-check", "region": "us-east-1",
    })
    assert second.status_code == 400
    assert "already exists" in second.json()["detail"]


# -- deletion: the data-safety dance for a whole volume ---------------------------


def test_delete_refuses_without_typed_confirmation(client):
    client.post("/filesystems", json={
        "name": "doomed", "region": "us-east-1"})
    resp = client.delete("/filesystems/doomed")
    assert resp.status_code == 428
    detail = resp.json()["detail"]
    assert "permanently destroys" in detail and "confirm_name" in detail
    # Still there.
    names = [f["name"] for f in client.get("/filesystems").json()["filesystems"]]
    assert "doomed" in names


def test_delete_with_confirmation_deletes_and_audits(client):
    client.post("/filesystems", json={
        "name": "doomed", "region": "us-east-1"})
    resp = client.delete("/filesystems/doomed?confirm_name=doomed")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "doomed"
    names = [f["name"] for f in client.get("/filesystems").json()["filesystems"]]
    assert "doomed" not in names
    actions = [e["action"] for e in client.get("/audit").json()["entries"]]
    assert "filesystem_deleted" in actions


def test_delete_wrong_confirmation_still_refuses(client):
    client.post("/filesystems", json={
        "name": "doomed", "region": "us-east-1"})
    resp = client.delete("/filesystems/doomed?confirm_name=Doomed")
    assert resp.status_code == 428


def test_delete_in_use_filesystem_is_409(client, mock_client):
    from dataclasses import replace
    client.post("/filesystems", json={
        "name": "attached", "region": "us-east-1"})
    mock_client.filesystems = [
        replace(fs, is_in_use=True) if fs.name == "attached" else fs
        for fs in mock_client.filesystems
    ]
    resp = client.delete("/filesystems/attached?confirm_name=attached")
    assert resp.status_code == 409
    assert "attached" in resp.json()["detail"]


def test_delete_unknown_filesystem_is_404(client):
    assert client.delete("/filesystems/nope?confirm_name=nope").status_code == 404

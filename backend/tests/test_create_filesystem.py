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

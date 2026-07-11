"""The /regions endpoint: full region universe with human names."""

from app.lambda_api import NA_REGIONS, REGION_NAMES


def test_region_name_map_covers_na_regions():
    # Every region code we advertise has a human label.
    for code in NA_REGIONS:
        assert REGION_NAMES[code].endswith("USA")
    assert REGION_NAMES["us-east-1"] == "Virginia, USA"
    assert REGION_NAMES["us-west-2"] == "Arizona, USA"
    # Both Washington DC regions from the console are present.
    assert REGION_NAMES["us-east-2"] == "Washington DC, USA"
    assert REGION_NAMES["us-east-3"] == "Washington DC, USA"


def test_regions_endpoint_returns_named_universe(client):
    body = client.get("/regions").json()
    regions = body["regions"]
    codes = [r["code"] for r in regions]
    # All twelve NA regions are present, east->west order preserved.
    assert codes[: len(NA_REGIONS)] == NA_REGIONS
    virginia = next(r for r in regions if r["code"] == "us-east-1")
    assert virginia["name"] == "Virginia, USA"


def test_a10_available_regions_match_console(client):
    """The mock A10 (James's box) is available in Virginia + Arizona only."""
    types = client.get("/instance-types").json()
    a10 = types["gpu_1x_a10"]
    assert set(a10["regions_with_capacity"]) == {"us-east-1", "us-west-2"}


def test_regions_endpoint_survives_unconfigured_backend(tmp_path):
    """With no Lambda key, /regions still returns the static NA set."""
    from fastapi.testclient import TestClient
    from app.lambda_api import SwappableLambdaClient, UnconfiguredLambdaClient
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn

    settings = make_settings(tmp_path, lambda_api_key="")
    app = create_app(
        settings,
        lambda_client=SwappableLambdaClient(UnconfiguredLambdaClient()),
        storage_factory=lambda fs: None,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: None,
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as c:
        codes = [r["code"] for r in c.get("/regions").json()["regions"]]
        assert codes == NA_REGIONS

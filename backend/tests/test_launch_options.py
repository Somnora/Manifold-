"""Launch-target discovery: cross-reference the live catalog with the user's
filesystems so a launch picks an AVAILABLE, co-located (type, region,
filesystem) instead of guessing a region blind.

Field motivation (2026-07-15): driving launch_gpu through MCP with no way to
see capacity, the first region guess had no A10 and failed after 5 retries,
and neither guess preferred a region where the user already had files. The
backend knew both facts (regions_with_capacity per type; region per
filesystem) but never exposed them together.
"""

import httpx
import pytest

import app.mcp_server as mcp_server
from app.lambda_api import (
    DEFAULT_MOCK_TYPES,
    FilesystemInfo,
    InstanceTypeInfo,
    default_mock_filesystems,
)
from app.orchestrator import launch_options


def _type(name, cents, regions):
    return InstanceTypeInfo(
        name=name, description=name, gpu_description=name,
        price_cents_per_hour=cents, specs={}, regions_with_capacity=regions,
    )


# -- pure ranking ----------------------------------------------------------------

def test_colocated_with_existing_data_ranks_first():
    r = launch_options(DEFAULT_MOCK_TYPES, default_mock_filesystems())
    top = r["targets"][0]
    # manifold-data lives in us-east-1 with bytes; the cheapest available type
    # there (the A10 at $1.29) is the single best pick.
    assert top["instance_type"] == "gpu_1x_a10"
    assert top["region"] == "us-east-1"
    assert top["filesystem"] == "manifold-data"
    assert top["colocated"] is True


def test_all_colocated_targets_precede_scratch():
    r = launch_options(DEFAULT_MOCK_TYPES, default_mock_filesystems())
    flags = [t["colocated"] for t in r["targets"]]
    assert flags == sorted(flags, reverse=True)   # every True before any False


def test_scratch_only_target_has_null_filesystem():
    r = launch_options(DEFAULT_MOCK_TYPES, default_mock_filesystems())
    scratch = [t for t in r["targets"] if not t["colocated"]]
    assert scratch, "mock has capacity in regions with no filesystem"
    assert all(t["filesystem"] is None for t in scratch)


def test_types_without_capacity_are_unavailable_not_targets():
    r = launch_options(DEFAULT_MOCK_TYPES, default_mock_filesystems())
    assert "gpu_1x_gh200" in r["unavailable"]         # empty regions in mock
    target_types = {t["instance_type"] for t in r["targets"]}
    assert "gpu_1x_gh200" not in target_types


def test_existing_data_beats_empty_filesystem_same_region():
    # Two filesystems in one region: the populated one is the offered target.
    types = {"gpu_1x_a10": _type("gpu_1x_a10", 129, ["us-west-1"])}
    filesystems = [
        FilesystemInfo(id="e", name="empty-fs", mount_point="/m/e",
                       region="us-west-1", is_in_use=False, bytes_used=0),
        FilesystemInfo(id="d", name="data-fs", mount_point="/m/d",
                       region="us-west-1", is_in_use=False, bytes_used=999),
    ]
    r = launch_options(types, filesystems)
    assert r["targets"][0]["filesystem"] == "data-fs"


def test_no_filesystems_yields_only_scratch_targets():
    types = {"gpu_1x_a10": _type("gpu_1x_a10", 129, ["us-west-1"])}
    r = launch_options(types, [])
    assert r["targets"] and all(not t["colocated"] for t in r["targets"])
    assert all(t["filesystem"] is None for t in r["targets"])


# -- HTTP endpoint ---------------------------------------------------------------

def test_launch_options_endpoint(client):
    resp = client.get("/launch-options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["targets"][0]["instance_type"] == "gpu_1x_a10"
    assert body["targets"][0]["filesystem"] == "manifold-data"
    assert "gpu_1x_gh200" in body["unavailable"]


# -- MCP tool --------------------------------------------------------------------

@pytest.fixture
async def mcp_wired(tmp_path, mock_client, mock_storage, mock_sidecar):
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


async def test_mcp_list_launch_options_returns_ranked_targets(mcp_wired):
    result = await mcp_server.list_launch_options(note="pick a box")
    assert "error" not in result
    top = result["targets"][0]
    # A guiding LLM copies this straight into launch_gpu.
    assert top["instance_type"] == "gpu_1x_a10"
    assert top["region"] == "us-east-1"
    assert top["filesystem"] == "manifold-data"

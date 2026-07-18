"""Lambda Cloud API client.

LambdaClient is the interface everything else depends on. RealLambdaClient
speaks to https://cloud.lambda.ai/api/v1 over httpx; MockLambdaClient serves
canned data (including scripted capacity failures) so the entire test suite
and dashboard development run with zero live spend.

API facts verified against the published OpenAPI spec (v1.10.0, July 2026):
- Auth: `Authorization: Bearer <api_key>`
- Errors: `{"error": {"code", "message", "suggestion"}}`
- Capacity failures: code "instance-operations/launch/insufficient-capacity"
- Prices are integer US cents per hour on the instance type.
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://cloud.lambda.ai/api/v1"

INSUFFICIENT_CAPACITY = "instance-operations/launch/insufficient-capacity"


class LambdaAPIError(Exception):
    """An error response from the Lambda Cloud API."""

    def __init__(self, code: str, message: str, suggestion: str = "",
                 status: int = 400):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.status = status

    @property
    def is_capacity_error(self) -> bool:
        return self.code == INSUFFICIENT_CAPACITY


# -- Data shapes (only the fields Manifold uses) ------------------------------


@dataclass
class InstanceTypeInfo:
    name: str
    description: str
    gpu_description: str
    price_cents_per_hour: int
    specs: dict
    regions_with_capacity: list[str] = field(default_factory=list)


@dataclass
class FilesystemInfo:
    id: str            # UUID; doubles as the S3 adapter bucket name
    name: str
    mount_point: str   # e.g. /lambda/nfs/<name>
    region: str
    is_in_use: bool
    bytes_used: int = 0


@dataclass
class SSHKeyInfo:
    id: str
    name: str


@dataclass
class InstanceInfo:
    id: str
    name: str
    status: str        # booting|active|unhealthy|terminated|terminating|preempted
    ip: str | None
    region: str
    instance_type: str
    hourly_rate_cents: int
    gpu_description: str = ""
    file_system_names: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        """Counts toward concurrency/budget guards (still billable)."""
        return self.status in ("booting", "active", "unhealthy")


# -- Interface -----------------------------------------------------------------


class LambdaClient(abc.ABC):
    @abc.abstractmethod
    async def list_instance_types(self) -> dict[str, InstanceTypeInfo]: ...

    @abc.abstractmethod
    async def list_filesystems(self) -> list[FilesystemInfo]: ...

    @abc.abstractmethod
    async def create_filesystem(self, *, name: str,
                                region: str) -> FilesystemInfo:
        """Create a persistent filesystem in `region`. Storage is billed by
        the GB-month actually used, so creation itself costs nothing."""
        ...

    @abc.abstractmethod
    async def delete_filesystem(self, fs_id: str) -> None:
        """Permanently delete a filesystem by id. The API refuses (400,
        filesystem-in-use) while any instance has it attached."""
        ...

    @abc.abstractmethod
    async def list_ssh_keys(self) -> list[SSHKeyInfo]: ...

    @abc.abstractmethod
    async def list_instances(self, *, fresh: bool = False) -> list[InstanceInfo]:
        """List instances. `fresh=True` bypasses any caching layer — used by
        the spend guards, which must never decide on stale state."""
        ...

    @abc.abstractmethod
    async def get_instance(self, instance_id: str) -> InstanceInfo: ...

    @abc.abstractmethod
    async def launch_instance(
        self,
        *,
        instance_type: str,
        region: str,
        ssh_key_names: list[str],
        filesystem_names: list[str],
        name: str = "",
        user_data: str = "",
    ) -> str:
        """Launch one instance; returns its Lambda instance id."""

    @abc.abstractmethod
    async def terminate_instance(self, instance_id: str) -> None: ...

    async def close(self) -> None:  # noqa: B027 (optional hook)
        pass


class UnconfiguredLambdaClient(LambdaClient):
    """Placeholder used when real mode starts without a Lambda API key.

    Every call fails with the same clear, actionable message — the backend
    stays up and the dashboard can point the user at Settings, instead of
    the old behavior (crash at startup, blank dropdowns, no explanation).
    """

    def _err(self) -> LambdaAPIError:
        return LambdaAPIError(
            code="manifold/not-configured",
            message="No Lambda API key configured. Open the dashboard's "
                    "Settings page to add one (or edit .env).",
            status=503,
        )

    async def list_instance_types(self):
        raise self._err()

    async def list_filesystems(self):
        raise self._err()

    async def create_filesystem(self, *, name: str, region: str):
        raise self._err()

    async def delete_filesystem(self, fs_id: str):
        raise self._err()

    async def list_ssh_keys(self):
        raise self._err()

    async def list_instances(self, *, fresh: bool = False):
        raise self._err()

    async def get_instance(self, instance_id: str):
        raise self._err()

    async def launch_instance(self, **kwargs):
        raise self._err()

    async def terminate_instance(self, instance_id: str):
        raise self._err()


class SwappableLambdaClient(LambdaClient):
    """Delegating wrapper so credentials can be applied at runtime.

    Everything (orchestrator, dispatcher, routes) holds this one object;
    the Settings flow replaces `inner` and every holder sees the new
    client immediately. No restart required.

    Also a short-TTL cache for `list_instances`: the dashboard polls it
    every ~2s (and a second browser tab, MCP, and capacity watches pile on),
    each poll otherwise hitting Lambda's rate-limited API. The cache is
    invalidated on any state change WE initiate (launch/terminate) and when
    `inner` is swapped, so it never masks an action taken through Manifold —
    only out-of-band changes wait out the TTL, which already matches the
    poll cadence. Safe for the reconcile in instances_with_state: any
    instance we hold a connection to was launched through us (cache-busting)
    minutes before, so a live connection can never be reaped on stale data.
    """

    def __init__(self, inner: LambdaClient, *, cache_ttl_seconds: float = 2.0,
                 clock=time.monotonic):
        self._inner = inner
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._instances_cache = None
        self._instances_at = 0.0

    @property
    def inner(self) -> LambdaClient:
        return self._inner

    @inner.setter
    def inner(self, value: LambdaClient) -> None:
        self._inner = value
        self._invalidate()   # new credentials -> fresh data, not cached

    def _invalidate(self) -> None:
        self._instances_cache = None

    async def list_instance_types(self):
        return await self._inner.list_instance_types()

    async def list_filesystems(self):
        return await self._inner.list_filesystems()

    async def create_filesystem(self, *, name: str, region: str):
        return await self._inner.create_filesystem(name=name, region=region)

    async def delete_filesystem(self, fs_id: str):
        return await self._inner.delete_filesystem(fs_id)

    async def list_ssh_keys(self):
        return await self._inner.list_ssh_keys()

    async def list_instances(self, *, fresh: bool = False):
        now = self._clock()
        if (not fresh and self._instances_cache is not None
                and now - self._instances_at < self._ttl):
            return self._instances_cache
        # fresh=True (spend guards) always hits the API and refreshes the
        # cache, so the guard decides on live state and later readers benefit.
        result = await self._inner.list_instances()
        self._instances_cache = result
        self._instances_at = now
        return result

    async def get_instance(self, instance_id: str):
        return await self._inner.get_instance(instance_id)

    async def launch_instance(self, **kwargs):
        result = await self._inner.launch_instance(**kwargs)
        self._invalidate()   # a new instance exists; reflect it now
        return result

    async def terminate_instance(self, instance_id: str):
        result = await self._inner.terminate_instance(instance_id)
        self._invalidate()   # it's gone; don't serve it from cache
        return result

    async def close(self):
        await self._inner.close()


# -- Real client ----------------------------------------------------------------


class RealLambdaClient(LambdaClient):
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        if not api_key:
            raise ValueError("LAMBDA_API_KEY is not set")
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        resp = await self._http.request(method, path, json=json)
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            err = body.get("error", {})
            raise LambdaAPIError(
                code=err.get("code", f"http/{resp.status_code}"),
                message=err.get("message", resp.text[:200]),
                suggestion=err.get("suggestion", ""),
                status=resp.status_code,
            )
        return body.get("data", {})

    async def list_instance_types(self) -> dict[str, InstanceTypeInfo]:
        data = await self._request("GET", "/instance-types")
        result = {}
        for name, item in data.items():
            it = item["instance_type"]
            result[name] = InstanceTypeInfo(
                name=it["name"],
                description=it["description"],
                gpu_description=it["gpu_description"],
                price_cents_per_hour=it["price_cents_per_hour"],
                specs=it.get("specs", {}),
                regions_with_capacity=[
                    r["name"] for r in item.get("regions_with_capacity_available", [])
                ],
            )
        return result

    async def list_filesystems(self) -> list[FilesystemInfo]:
        data = await self._request("GET", "/file-systems")
        return [
            FilesystemInfo(
                id=fs["id"],
                name=fs["name"],
                mount_point=fs["mount_point"],
                region=fs["region"]["name"],
                is_in_use=fs["is_in_use"],
                bytes_used=fs.get("bytes_used", 0),
            )
            for fs in data
        ]

    async def create_filesystem(self, *, name: str,
                                region: str) -> FilesystemInfo:
        # Lambda API quirk: the LIST route is /file-systems (hyphenated),
        # the CREATE route is /filesystems. Both verified against the docs.
        data = await self._request(
            "POST", "/filesystems", json={"name": name, "region": region})
        region_field = data.get("region", region)
        if isinstance(region_field, dict):
            region_field = region_field.get("name", region)
        return FilesystemInfo(
            id=data.get("id", ""),
            name=data.get("name", name),
            mount_point=data.get("mount_point", f"/lambda/nfs/{name}"),
            region=region_field,
            is_in_use=bool(data.get("is_in_use", False)),
            bytes_used=data.get("bytes_used", 0),
        )

    async def delete_filesystem(self, fs_id: str) -> None:
        # Same route-naming quirk as create: /filesystems, not /file-systems.
        await self._request("DELETE", f"/filesystems/{fs_id}")

    async def list_ssh_keys(self) -> list[SSHKeyInfo]:
        data = await self._request("GET", "/ssh-keys")
        return [SSHKeyInfo(id=k["id"], name=k["name"]) for k in data]

    @staticmethod
    def _instance_from_api(inst: dict) -> InstanceInfo:
        return InstanceInfo(
            id=inst["id"],
            name=inst.get("name") or "",
            status=inst["status"],
            ip=inst.get("ip"),
            region=inst["region"]["name"],
            instance_type=inst["instance_type"]["name"],
            hourly_rate_cents=inst["instance_type"]["price_cents_per_hour"],
            gpu_description=inst["instance_type"].get("gpu_description", ""),
            file_system_names=inst.get("file_system_names", []),
        )

    async def list_instances(self, *, fresh: bool = False) -> list[InstanceInfo]:
        data = await self._request("GET", "/instances")
        return [self._instance_from_api(i) for i in data]

    async def get_instance(self, instance_id: str) -> InstanceInfo:
        data = await self._request("GET", f"/instances/{instance_id}")
        return self._instance_from_api(data)

    async def launch_instance(
        self,
        *,
        instance_type: str,
        region: str,
        ssh_key_names: list[str],
        filesystem_names: list[str],
        name: str = "",
        user_data: str = "",
    ) -> str:
        body: dict = {
            "instance_type_name": instance_type,
            "region_name": region,
            "ssh_key_names": ssh_key_names,
            "file_system_names": filesystem_names,
        }
        if name:
            body["name"] = name
        if user_data:
            body["user_data"] = user_data
        data = await self._request("POST", "/instance-operations/launch", json=body)
        return data["instance_ids"][0]

    async def terminate_instance(self, instance_id: str) -> None:
        await self._request(
            "POST", "/instance-operations/terminate",
            json={"instance_ids": [instance_id]},
        )


# -- Mock client -----------------------------------------------------------------


def capacity_error() -> LambdaAPIError:
    return LambdaAPIError(
        code=INSUFFICIENT_CAPACITY,
        message="Not enough capacity to fulfill launch request.",
        suggestion="Try again later or use a different instance type/region.",
    )


def _mock_type(name: str, description: str, cents: int, vcpus: int,
               memory_gib: int, storage_gib: int, gpus: int,
               regions: list[str]) -> InstanceTypeInfo:
    # gpu_description is the description minus the leading "Nx " count.
    gpu_desc = description.split(" ", 1)[1] if gpus else "CPU only"
    return InstanceTypeInfo(
        name=name, description=description, gpu_description=gpu_desc,
        price_cents_per_hour=cents,
        specs={"vcpus": vcpus, "memory_gib": memory_gib,
               "storage_gib": storage_gib, "gpus": gpus},
        regions_with_capacity=regions,
    )


# Known Lambda regions (from the console, July 2026), US listed
# roughly east -> west. Real mode gets regions live from the API; the mock
# spreads capacity across these. REGION_NAMES maps each code to the human
# label the Lambda console shows, so the dashboard reads "Virginia, USA"
# instead of "us-east-1".
REGION_NAMES = {
    "us-east-1": "Virginia, USA",
    "us-east-2": "Washington DC, USA",
    "us-east-3": "Washington DC, USA",
    "us-southeast-1": "Georgia, USA",
    "us-midwest-1": "Illinois, USA",
    "us-midwest-2": "Ohio, USA",
    "us-south-1": "Texas, USA",
    "us-south-2": "North Texas, USA",
    "us-south-3": "Central Texas, USA",
    "us-west-1": "California, USA",
    "us-west-2": "Arizona, USA",
    "us-west-3": "Utah, USA",
    # International (from the console's region picker, July 2026).
    "europe-central-1": "Germany",
    "me-west-1": "Israel",
    "asia-south-1": "India",
    "asia-northeast-1": "Osaka, Japan",
    "asia-northeast-2": "Tokyo, Japan",
    "australia-east-1": "Sydney, Australia",
}
KNOWN_REGIONS = list(REGION_NAMES)

# Mirrors the real Lambda catalog (prices/specs from the console, July 2026)
# so mock mode looks and costs like production. Types with an empty region
# list model "out of capacity". Real mode ignores all of this and pulls the
# live catalog from the API.
DEFAULT_MOCK_TYPES = {
    t.name: t for t in [
        # With capacity in mock mode:
        _mock_type("gpu_8x_h100_sxm5", "8x H100 (80 GB SXM5)", 3192, 208, 1800, 22528, 8, ["us-east-2", "us-south-1"]),
        _mock_type("gpu_1x_h100_sxm5", "1x H100 (80 GB SXM5)", 429, 26, 225, 2867, 1, ["us-east-1", "us-east-2", "us-south-1"]),
        _mock_type("gpu_1x_h100_pcie", "1x H100 (80 GB PCIe)", 329, 26, 200, 1024, 1, ["us-west-1", "us-west-2"]),
        _mock_type("gpu_8x_a100_80gb_sxm4", "8x A100 (80 GB SXM4)", 2232, 240, 1800, 20480, 8, ["us-east-1", "us-midwest-1"]),
        # A10: available in Virginia + Arizona, matching the console.
        _mock_type("gpu_1x_a10", "1x A10 (24 GB PCIe)", 129, 30, 200, 1434, 1, ["us-east-1", "us-west-2"]),
        _mock_type("gpu_1x_a100_sxm4", "1x A100 (40 GB SXM4)", 199, 30, 200, 512, 1, ["us-east-1", "us-west-3"]),
        # Out of capacity (empty regions), matching the console screenshots:
        _mock_type("gpu_1x_gh200", "1x GH200 (96 GB)", 229, 64, 432, 4096, 1, []),
        _mock_type("gpu_8x_b200_sxm6", "8x B200 (180 GB SXM6)", 5352, 208, 2900, 22528, 8, []),
        _mock_type("gpu_2x_b200_sxm6", "2x B200 (180 GB SXM6)", 1378, 52, 720, 5632, 2, []),
        _mock_type("gpu_1x_b200_sxm6", "1x B200 (180 GB SXM6)", 699, 26, 360, 2867, 1, []),
        _mock_type("gpu_4x_h100_sxm5", "4x H100 (80 GB SXM5)", 1636, 104, 900, 11264, 4, []),
        _mock_type("gpu_2x_h100_sxm5", "2x H100 (80 GB SXM5)", 838, 52, 450, 5632, 2, []),
        _mock_type("gpu_1x_rtx6000", "1x RTX 6000 (24 GB)", 69, 14, 46, 512, 1, []),
        _mock_type("gpu_1x_a100_pcie", "1x A100 (40 GB PCIe)", 199, 30, 200, 512, 1, []),
        _mock_type("gpu_2x_a100_pcie", "2x A100 (40 GB PCIe)", 398, 60, 400, 1024, 2, []),
        _mock_type("gpu_4x_a100_pcie", "4x A100 (40 GB PCIe)", 796, 120, 800, 1024, 4, []),
        _mock_type("gpu_8x_a100", "8x A100 (40 GB SXM4)", 1592, 124, 1800, 6144, 8, []),
        _mock_type("gpu_1x_a6000", "1x A6000 (48 GB)", 109, 14, 100, 205, 1, []),
        _mock_type("gpu_2x_a6000", "2x A6000 (48 GB)", 218, 28, 200, 1024, 2, []),
        _mock_type("gpu_4x_a6000", "4x A6000 (48 GB)", 436, 56, 400, 1024, 4, []),
        _mock_type("gpu_8x_v100", "8x Tesla V100 (16 GB)", 632, 92, 448, 6042, 8, []),
        _mock_type("cpu_4x_general", "4x CPU General (16 GiB)", 20, 4, 16, 102, 0, []),
    ]
}


def default_mock_filesystems() -> list[FilesystemInfo]:
    return [
        FilesystemInfo(
            id="398578a2336b49079e74043f0bd2cfe8",
            name="manifold-data",
            mount_point="/lambda/nfs/manifold-data",
            region="us-east-1",
            is_in_use=False,
            bytes_used=52_428_800,
        )
    ]


class MockLambdaClient(LambdaClient):
    """Canned Lambda API for tests and mock-mode dashboard development.

    - `scripted_launch_errors`: errors raised by successive launch calls
      (front of the list first) before launches start succeeding. Lets tests
      exercise capacity-retry paths deterministically.
    - Launched instances report status "booting" until `get_instance` has
      been polled `polls_until_active` times, then flip to "active" with an IP.
    """

    def __init__(
        self,
        *,
        instance_types: dict[str, InstanceTypeInfo] | None = None,
        filesystems: list[FilesystemInfo] | None = None,
        ssh_keys: list[SSHKeyInfo] | None = None,
        scripted_launch_errors: list[LambdaAPIError] | None = None,
        polls_until_active: int = 2,
    ):
        self.instance_types = instance_types or dict(DEFAULT_MOCK_TYPES)
        self.filesystems = filesystems if filesystems is not None else default_mock_filesystems()
        self.ssh_keys = ssh_keys if ssh_keys is not None else [
            SSHKeyInfo(id="key1", name="mock-key"),
            SSHKeyInfo(id="key2", name="test-ssh-key"),
        ]
        self.scripted_launch_errors = list(scripted_launch_errors or [])
        self.polls_until_active = polls_until_active
        self.instances: dict[str, InstanceInfo] = {}
        self.launch_calls: list[dict] = []   # every attempted launch, for assertions
        self._poll_counts: dict[str, int] = {}

    async def list_instance_types(self) -> dict[str, InstanceTypeInfo]:
        return dict(self.instance_types)

    async def list_filesystems(self) -> list[FilesystemInfo]:
        return list(self.filesystems)

    async def create_filesystem(self, *, name: str,
                                region: str) -> FilesystemInfo:
        if any(fs.name == name for fs in self.filesystems):
            raise LambdaAPIError(
                code="global/duplicate",
                message=f"A filesystem named '{name}' already exists",
                status=400,
            )
        fs = FilesystemInfo(
            id=f"fs-{name}",
            name=name,
            mount_point=f"/lambda/nfs/{name}",
            region=region,
            is_in_use=False,
        )
        self.filesystems.append(fs)
        return fs

    async def delete_filesystem(self, fs_id: str) -> None:
        match = [fs for fs in self.filesystems if fs.id == fs_id]
        if not match:
            raise LambdaAPIError(
                code="global/object-does-not-exist",
                message=f"No filesystem with id '{fs_id}'", status=404)
        if match[0].is_in_use:
            raise LambdaAPIError(
                code="instance-operations/filesystem/filesystem-in-use",
                message=f"Filesystem '{match[0].name}' is attached to an "
                        f"instance and cannot be deleted", status=400)
        self.filesystems = [fs for fs in self.filesystems if fs.id != fs_id]

    async def list_ssh_keys(self) -> list[SSHKeyInfo]:
        return list(self.ssh_keys)

    async def list_instances(self, *, fresh: bool = False) -> list[InstanceInfo]:
        return [i for i in self.instances.values() if i.status != "terminated"]

    async def get_instance(self, instance_id: str) -> InstanceInfo:
        inst = self.instances.get(instance_id)
        if inst is None:
            raise LambdaAPIError(
                code="global/object-does-not-exist",
                message=f"Instance {instance_id} not found", status=404,
            )
        if inst.status == "booting":
            self._poll_counts[instance_id] = self._poll_counts.get(instance_id, 0) + 1
            if self._poll_counts[instance_id] >= self.polls_until_active:
                inst.status = "active"
                inst.ip = "192.0.2.10"
        return inst

    async def launch_instance(
        self,
        *,
        instance_type: str,
        region: str,
        ssh_key_names: list[str],
        filesystem_names: list[str],
        name: str = "",
        user_data: str = "",
    ) -> str:
        self.launch_calls.append({
            "instance_type": instance_type, "region": region,
            "ssh_key_names": ssh_key_names, "filesystem_names": filesystem_names,
            "name": name, "user_data": user_data,
        })
        if self.scripted_launch_errors:
            raise self.scripted_launch_errors.pop(0)
        type_info = self.instance_types[instance_type]
        instance_id = uuid.uuid4().hex
        self.instances[instance_id] = InstanceInfo(
            id=instance_id, name=name, status="booting", ip=None,
            region=region, instance_type=instance_type,
            hourly_rate_cents=type_info.price_cents_per_hour,
            gpu_description=type_info.gpu_description,
            file_system_names=list(filesystem_names),
        )
        return instance_id

    async def terminate_instance(self, instance_id: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            raise LambdaAPIError(
                code="global/object-does-not-exist",
                message=f"Instance {instance_id} not found", status=404,
            )
        inst.status = "terminated"

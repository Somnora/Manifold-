"""Pre-launch image preflight: does a template's container image actually
exist in its registry?

Checked from the backend BEFORE booting a GPU, so a typo'd or deleted image
(the whisper-batch ghcr failure that motivated this) fails the job
immediately instead of paying for an instance that then dies on `docker
pull`. It verifies ANONYMOUS pullability via the OCI/Docker Registry v2 API,
which is exactly what an instance does (no docker login is configured on
instances).

Policy: a definitive not-found / denied fails the job; anything we cannot
determine (network error, a gated registry we cannot read anonymously) is
fail-OPEN — a flaky check must never become a wall in front of every launch.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger("manifold.image_checker")

_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])


@dataclass
class ImageCheck:
    image: str
    exists: bool | None      # True=found, False=definitively missing, None=undetermined
    detail: str

    @property
    def definitely_missing(self) -> bool:
        return self.exists is False


def parse_image_ref(ref: str) -> tuple[str, str, str]:
    """Split a docker image reference into (registry_api_host, repository,
    reference). Handles the docker.io defaults (library/ prefix, the
    registry-1 API host) and bare tags vs digests."""
    if "@" in ref:                       # name@sha256:...
        name, reference = ref.split("@", 1)
    else:
        last = ref.rsplit("/", 1)[-1]
        if ":" in last:                  # a tag on the final path component
            name, reference = ref.rsplit(":", 1)
        else:
            name, reference = ref, "latest"
    parts = name.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]
                            or parts[0] == "localhost"):
        registry, repo = parts
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io":
        api_host = "registry-1.docker.io"
        if "/" not in repo:              # official images live under library/
            repo = "library/" + repo
    else:
        api_host = registry
    return api_host, repo, reference


class ImageChecker(abc.ABC):
    @abc.abstractmethod
    async def image_exists(self, image: str) -> ImageCheck:
        """Whether `image` (a docker ref) can be pulled anonymously."""


class RealImageChecker(ImageChecker):
    """Queries the Registry v2 API over HTTPS. Results are cached briefly so a
    burst of jobs on the same image does not re-hit the registry."""

    def __init__(self, *, timeout: float = 10.0, cache_ttl: float = 300.0):
        self._timeout = timeout
        self._ttl = cache_ttl
        self._cache: dict[str, tuple[float, ImageCheck]] = {}

    async def image_exists(self, image: str) -> ImageCheck:
        now = time.monotonic()
        cached = self._cache.get(image)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        result = await self._check(image)
        self._cache[image] = (now, result)
        return result

    async def _check(self, image: str) -> ImageCheck:
        try:
            api_host, repo, ref = parse_image_ref(image)
        except Exception as exc:  # noqa: BLE001 - never raise into the caller
            return ImageCheck(image, None, f"unparseable image ref: {exc}")
        url = f"https://{api_host}/v2/{repo}/manifests/{ref}"
        headers = {"Accept": _MANIFEST_ACCEPT}
        try:
            async with httpx.AsyncClient(timeout=self._timeout,
                                         follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 401:
                    token = await self._anon_token(client, resp)
                    if token is None:
                        return ImageCheck(
                            image, None,
                            "registry requires credentials we do not have")
                    resp = await client.get(
                        url, headers={**headers,
                                      "Authorization": f"Bearer {token}"})
                return self._classify(image, resp.status_code, api_host)
        except httpx.HTTPError as exc:
            return ImageCheck(image, None, f"registry check errored: {exc}")

    @staticmethod
    def _classify(image: str, status: int, api_host: str) -> ImageCheck:
        if status == 200:
            return ImageCheck(image, True, "manifest found")
        if status == 404:
            return ImageCheck(image, False,
                              f"tag/repository not found in {api_host}")
        if status == 403:
            # ghcr and others return 403/denied for a missing OR private image.
            # Instances pull anonymously, so either way it will not pull.
            return ImageCheck(
                image, False,
                f"not pullable anonymously from {api_host} (missing or private)")
        return ImageCheck(image, None,
                          f"unexpected registry status {status} from {api_host}")

    @staticmethod
    async def _anon_token(client: httpx.AsyncClient,
                          resp: httpx.Response) -> str | None:
        """Follow a Bearer WWW-Authenticate challenge to fetch an anonymous
        pull token (docker.io, ghcr.io, ...)."""
        header = resp.headers.get("WWW-Authenticate", "")
        if not header.lower().startswith("bearer "):
            return None
        parts: dict[str, str] = {}
        for kv in header[len("bearer "):].split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                parts[k.strip()] = v.strip().strip('"')
        realm = parts.get("realm")
        if not realm:
            return None
        params = {k: parts[k] for k in ("service", "scope") if parts.get(k)}
        try:
            tr = await client.get(realm, params=params)
            if tr.status_code != 200:
                return None
            data = tr.json()
            return data.get("token") or data.get("access_token")
        except (httpx.HTTPError, ValueError):
            return None


class MockImageChecker(ImageChecker):
    """Offline checker for tests and mock mode. Approves everything except a
    configured set of missing/undetermined images."""

    def __init__(self, *, missing: set[str] = frozenset(),
                 undetermined: set[str] = frozenset()):
        self.missing = set(missing)
        self.undetermined = set(undetermined)
        self.checked: list[str] = []

    async def image_exists(self, image: str) -> ImageCheck:
        self.checked.append(image)
        if image in self.undetermined:
            return ImageCheck(image, None, "undetermined (mock)")
        if image in self.missing:
            return ImageCheck(image, False, "not found (mock)")
        return ImageCheck(image, True, "found (mock)")

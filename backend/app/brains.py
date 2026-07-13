"""The brains registry: every model Manifold can put in the driver's seat.

Three kinds, one interface (OpenAI-compatible chat completions):

- instance:  a model served on a Manifold-launched GPU (vllm-serve /
             sglang-serve), reached over the managed SSH connection —
             the original Autopilot brain.
- local:     a model server on THIS machine (Ollama, LM Studio). The
             backend already runs on the user's machine, so these are
             plain loopback HTTP — auto-detected by probing /v1/models.
- api:       a frontier API (Anthropic, OpenAI, Gemini) via its
             OpenAI-compatible endpoint. Appears only once its key env
             var is set; keys live in .env (Settings page), never here.

Brain refs are strings so they fit the existing agent_runs schema:
    instance:<instance_id>        local:<endpoint>/<model>       api:<name>
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from .config import Settings
from .model_client import ModelClientError

logger = logging.getLogger("manifold.brains")

PROBE_TIMEOUT = 1.5          # local servers answer instantly or not at all
DETECT_CACHE_SECONDS = 10.0


@dataclass
class BrainInfo:
    ref: str                 # "instance:i-...", "local:ollama/llama3", "api:claude"
    kind: str                # instance | local | api
    label: str               # human name for the picker
    model: str               # model id sent in requests
    detail: str = ""         # where it lives / status note
    ready: bool = True


class ExternalBrainClient:
    """OpenAI-compatible chat against a plain HTTP endpoint (local or API).

    Presents the same chat_stream/chat_completion surface as ModelClient,
    so the agent loop and chat-tools executor are brain-agnostic. `port` is
    accepted and ignored (instance clients need it; HTTP brains do not).
    """

    def __init__(self, base_url: str, *, api_key: str = "",
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout

    async def chat_completion(self, port: int, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise ModelClientError(f"brain unreachable at {self.base_url}: {exc}")
        if resp.status_code != 200:
            raise ModelClientError(
                f"brain at {self.base_url} answered {resp.status_code}: "
                f"{resp.text[:200]}")
        return resp.json()

    async def chat_stream(self, port: int,
                          payload: dict) -> AsyncIterator[str]:
        payload = {**payload, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions",
                    json=payload, headers=self._headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode(errors="replace")
                        raise ModelClientError(
                            f"brain at {self.base_url} answered "
                            f"{resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n\n"
        except httpx.HTTPError as exc:
            raise ModelClientError(f"brain unreachable at {self.base_url}: {exc}")


class BrainRegistry:
    """Discovers brains and resolves a ref to a chat client."""

    def __init__(self, settings: Settings, orchestrator, queue, templates,
                 *, http_get=None):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        # Injectable prober for tests: async (url) -> list[model_id] | None.
        self._http_get = http_get or self._probe_models
        self._local_cache: tuple[float, list[BrainInfo]] | None = None

    # -- discovery ------------------------------------------------------------------

    @staticmethod
    async def _probe_models(url: str) -> list[str] | None:
        """GET <base>/models; None = server not there, [] = there, no models."""
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json().get("data", [])
            return [m.get("id", "") for m in data if m.get("id")]
        except (httpx.HTTPError, ValueError):
            return None

    async def _instance_brains(self) -> list[BrainInfo]:
        from .agent import find_serving_task
        out = []
        for instance_id in list(self.orchestrator.connections):
            task = find_serving_task(self.queue, self.templates, instance_id)
            if task is None:
                continue
            out.append(BrainInfo(
                ref=f"instance:{instance_id}", kind="instance",
                label=f"{task['model_id']} (GPU instance)",
                model=task["model_id"],
                detail=f"served on {instance_id}",
            ))
        return out

    async def _local_brains(self) -> list[BrainInfo]:
        now = asyncio.get_event_loop().time()
        if self._local_cache and now - self._local_cache[0] < DETECT_CACHE_SECONDS:
            return self._local_cache[1]
        out: list[BrainInfo] = []
        for ep in self.settings.hub.local_endpoints:
            models = await self._http_get(f"{ep.base_url}/models")
            if models is None:
                continue
            for model in models[:8]:     # keep the picker sane
                out.append(BrainInfo(
                    ref=f"local:{ep.name}/{model}", kind="local",
                    label=f"{model} ({ep.name}, this machine)",
                    model=model, detail=ep.base_url,
                ))
        self._local_cache = (now, out)
        return out

    def _api_brains(self) -> list[BrainInfo]:
        out = []
        for brain in self.settings.hub.api_brains:
            if not os.environ.get(brain.api_key_env, ""):
                continue     # no key -> not offered; never a broken option
            out.append(BrainInfo(
                ref=f"api:{brain.name}", kind="api",
                label=f"{brain.model} ({brain.name} API)",
                model=brain.model,
                detail=f"key from {brain.api_key_env}",
            ))
        return out

    async def list_brains(self) -> list[BrainInfo]:
        return (await self._instance_brains()
                + await self._local_brains()
                + self._api_brains())

    # -- resolution -----------------------------------------------------------------

    def resolve(self, ref: str) -> tuple[object, str, int]:
        """ref -> (chat client, model id, port). Raises ValueError with a
        human-readable reason when the ref cannot be served right now."""
        kind, _, rest = ref.partition(":")
        if kind == "instance":
            from .agent import find_serving_task
            task = find_serving_task(self.queue, self.templates, rest)
            if task is None:
                raise ValueError(f"no model is being served on {rest}")
            client = self.orchestrator.model_client_for(rest)
            if client is None:
                raise ValueError(f"no managed connection to {rest}")
            return client, task["model_id"], task["port"]
        if kind == "local":
            name, _, model = rest.partition("/")
            for ep in self.settings.hub.local_endpoints:
                if ep.name == name:
                    if not model:
                        raise ValueError(f"local brain ref missing model: {ref}")
                    return (ExternalBrainClient(ep.base_url), model, 0)
            raise ValueError(f"unknown local endpoint '{name}'")
        if kind == "api":
            for brain in self.settings.hub.api_brains:
                if brain.name == rest:
                    key = os.environ.get(brain.api_key_env, "")
                    if not key:
                        raise ValueError(
                            f"{brain.api_key_env} is not set - add it in "
                            f"Settings to use the {brain.name} brain")
                    return (ExternalBrainClient(brain.base_url, api_key=key),
                            brain.model, 0)
            raise ValueError(f"unknown api brain '{rest}'")
        raise ValueError(
            f"bad brain ref '{ref}' (expected instance:/local:/api: prefix)")

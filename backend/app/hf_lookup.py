"""Exact model size from the HuggingFace API, for the model-fit preflight.

The name-parse heuristic in estimates.py covers models that state their
size ("Qwen3-8B"), but misses renamed forks and gated repos. The HF model
API reports the exact parameter count and dtype breakdown for any repo the
caller can see - including gated ones when HF_TOKEN (in .env) has accepted
the license.

Strictly advisory and fail-open: any error returns None and the caller
falls back to the name parse. A preflight must never become a wall."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("manifold.hf")

# Bytes per parameter by HF dtype name. F32 intentionally included: some
# repos really do ship fp32 weights and the whole point is honesty.
_DTYPE_BYTES = {
    "F64": 8.0, "F32": 4.0, "BF16": 2.0, "F16": 2.0,
    "I8": 1.0, "U8": 1.0, "F8_E4M3": 1.0, "F8_E5M2": 1.0,
    "I4": 0.5, "U4": 0.5, "F4": 0.5,
}


async def lookup_weights_gb(model_id: str, token: str = "") -> dict | None:
    """Exact weight footprint for an HF repo, or None (unknown/unreachable).

    Returns {"params_b", "weights_gb", "gated"} on success. `gated` is True
    when the repo needs a license acceptance - useful for the note when the
    lookup succeeds via token but a later download on the instance would
    still need HF_TOKEN in the job environment."""
    if "/" not in (model_id or ""):
        return None          # local paths and bare names have no HF repo
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(
                f"https://huggingface.co/api/models/{model_id}",
                headers=headers, follow_redirects=True,
            )
        if resp.status_code >= 400:
            return None      # missing, private without token, rate-limited
        data = resp.json()
    except Exception:
        logger.debug("HF lookup for %s failed", model_id, exc_info=True)
        return None
    st = data.get("safetensors") or {}
    per_dtype = st.get("parameters") or {}
    total = st.get("total")
    if not total or not isinstance(per_dtype, dict):
        return None          # no safetensors metadata (e.g. GGUF-only repo)
    bytes_total = 0.0
    for dtype, count in per_dtype.items():
        try:
            bytes_total += _DTYPE_BYTES.get(str(dtype).upper(), 2.0) * int(count)
        except (TypeError, ValueError):
            return None
    return {
        "params_b": round(total / 1e9, 2),
        "weights_gb": round(bytes_total / 1e9, 1),
        "gated": bool(data.get("gated")),
    }

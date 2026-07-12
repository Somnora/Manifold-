"""Curated vLLM-serveable model presets, tiered by GPU VRAM.

Only ungated models that load in vllm/vllm-openai with no HuggingFace token
are listed, so clicking a preset and serving it "just works" on a first run.
Gated models (Llama, Gemma) need a token Manifold does not pass yet — noted
as a future step in DECISIONS.md rather than offered as a preset that fails.

vram_gib is a practical single-GPU floor (fp16 weights + KV cache at ~8k
context, vLLM's default 0.9 memory utilisation), not a theoretical minimum.
"""

MODEL_PRESETS = [
    {
        "label": "Qwen2.5 7B Instruct",
        "model_id": "Qwen/Qwen2.5-7B-Instruct",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "Solid general-purpose default; fits a single A10.",
    },
    {
        "label": "Qwen2.5 3B Instruct",
        "model_id": "Qwen/Qwen2.5-3B-Instruct",
        "vram_gib": 12,
        "tier": "A10 24GB",
        "note": "Small and fast, with plenty of headroom on an A10.",
    },
    {
        "label": "Phi-3.5 Mini Instruct",
        "model_id": "microsoft/Phi-3.5-mini-instruct",
        "vram_gib": 12,
        "tier": "A10 24GB",
        "note": "~3.8B; strong for its size, ungated.",
    },
    {
        "label": "Qwen2.5 14B Instruct",
        "model_id": "Qwen/Qwen2.5-14B-Instruct",
        "vram_gib": 40,
        "tier": "H100 80GB",
        "note": "More capable; needs more than an A10 — use an H100.",
    },
    {
        "label": "Qwen2.5 32B Instruct",
        "model_id": "Qwen/Qwen2.5-32B-Instruct",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "Large; comfortably fits an H100 80GB.",
    },
]

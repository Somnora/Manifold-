"""Curated vLLM-serveable model presets, tiered by GPU VRAM.

Only ungated models that load in vllm/vllm-openai with no HuggingFace token
are listed, so clicking a preset and serving it "just works" on a first run.
Every repo id below was verified against the HF API (exists, gated=False)
on 2026-07-12. Gated models (Llama, Gemma) need a token Manifold does not
pass yet — noted in DECISIONS.md rather than offered as a preset that fails.

vram_gib is a practical floor (weights + KV cache at ~8k context, vLLM's
default 0.9 memory utilisation), not a theoretical minimum. Multi-GPU
presets carry `parameters` (e.g. tensor_parallel) that the Jobs page seeds
into the vllm-serve form alongside the model id.
"""

MODEL_PRESETS = [
    # -- A10 24GB ------------------------------------------------------------
    {
        "label": "Qwen3 8B",
        "model_id": "Qwen/Qwen3-8B",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "Solid general-purpose default; fits a single A10.",
    },
    {
        "label": "Qwen3 4B Instruct",
        "model_id": "Qwen/Qwen3-4B-Instruct-2507",
        "vram_gib": 12,
        "tier": "A10 24GB",
        "note": "Small and fast, with plenty of headroom on an A10.",
    },
    {
        "label": "gpt-oss 20B",
        "model_id": "openai/gpt-oss-20b",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "OpenAI's open-weight reasoner (MoE, MXFP4); strong for the tier.",
    },
    # -- A100 40GB -----------------------------------------------------------
    {
        "label": "Qwen3 14B",
        "model_id": "Qwen/Qwen3-14B",
        "vram_gib": 40,
        "tier": "A100 40GB",
        "note": "More capable dense model; needs more than an A10.",
    },
    {
        "label": "Qwen3.6 27B (FP8)",
        "model_id": "Qwen/Qwen3.6-27B-FP8",
        "vram_gib": 40,
        "tier": "A100 40GB",
        "note": "Current Qwen flagship dense, FP8-quantized to fit a 40GB card.",
    },
    # -- H100 80GB -----------------------------------------------------------
    {
        "label": "Qwen3.6 27B",
        "model_id": "Qwen/Qwen3.6-27B",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "Full-precision Qwen flagship dense; comfortable on an H100.",
    },
    {
        "label": "Qwen3.6 35B-A3B (FP8)",
        "model_id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "MoE (3B active): fast tokens, 262K context, vision input.",
    },
    {
        "label": "gpt-oss 120B",
        "model_id": "openai/gpt-oss-120b",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "OpenAI's large open-weight reasoner, sized for a single H100.",
    },
    # -- multi-GPU clusters ----------------------------------------------------
    {
        "label": "Hunyuan Hy3 (FP8)",
        "model_id": "tencent/Hy3-FP8",
        "vram_gib": 640,
        "tier": "8x H100 640GB",
        "note": "Tencent's 295B MoE (21B active), 256K context. Needs the "
                "8x H100 cluster; served with tensor parallel across all 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "GLM-5.2 (FP8)",
        "model_id": "zai-org/GLM-5.2-FP8",
        "vram_gib": 1440,
        "tier": "8x B200 1.4TB",
        "note": "Z.ai's 744B coding MoE (~750GB weights): frontier-class, "
                "needs the 8x B200 cluster with tensor parallel across all 8.",
        "parameters": {"tensor_parallel": 8},
    },
]

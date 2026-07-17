"""Model-fit preflight: weights-vs-VRAM sanity check from the model name.
Advisory only. Born from a 27B Int4 checkpoint OOMing a 24 GB A10."""

from app.estimates import (
    model_fit,
    parse_model_params_b,
    parse_quant_bytes,
    parse_vram_gb,
)


def test_parse_params_from_common_names():
    assert parse_model_params_b("Qwen/Qwen3.5-27B-GPTQ-Int4") == 27
    assert parse_model_params_b("meta-llama/Llama-3.1-8B-Instruct") == 8
    assert parse_model_params_b("Qwen/Qwen2.5-0.5B-Instruct") == 0.5
    assert parse_model_params_b("mistralai/Mixtral-8x7B-v0.1") == 56
    assert parse_model_params_b("openai/whisper-large-v3") is None
    assert parse_model_params_b("gpt2") is None


def test_parse_quant_markers():
    assert parse_quant_bytes("Qwen3.5-27B-GPTQ-Int4")[1] == "4-bit"
    assert parse_quant_bytes("Llama-3.1-8B-AWQ")[1] == "4-bit"
    assert parse_quant_bytes("model-FP8-dynamic")[1] == "8-bit"
    assert parse_quant_bytes("Llama-3.1-8B-Instruct")[1] == "fp16/bf16"


def test_parse_vram_from_type_and_description():
    assert parse_vram_gb("gpu_1x_a10", "A10 (24 GB PCIe)") == 24
    assert parse_vram_gb("gpu_8x_a100_sxm4", "A100 (40 GB SXM4)") == 320
    assert parse_vram_gb("cpu_4x_general", "4x CPU General (16 GiB)") is None


def test_the_night_we_lost_27b_int4_does_not_fit_an_a10():
    fit = model_fit("Qwen/Qwen3.5-27B-GPTQ-Int4", "gpu_1x_a10",
                    "A10 (24 GB PCIe)")
    assert fit["verdict"] in ("tight", "no")
    assert fit["note"]
    assert "estimated from the model name" in fit["basis"]


def test_small_model_fits_and_says_nothing():
    fit = model_fit("Qwen/Qwen2.5-0.5B-Instruct", "gpu_1x_a10",
                    "A10 (24 GB PCIe)")
    assert fit["verdict"] == "fits"
    assert fit["note"] == ""


def test_27b_int4_fits_an_a100_40gb():
    fit = model_fit("Qwen/Qwen3.5-27B-GPTQ-Int4", "gpu_1x_a100_sxm4",
                    "A100 (40 GB SXM4)")
    assert fit["verdict"] == "fits"


def test_fp16_8b_on_a10_is_flagged_tight_or_fits():
    # 8B fp16 is ~17 GB in 24 GB: workable for batch, cramped for serving.
    fit = model_fit("meta-llama/Llama-3.1-8B-Instruct", "gpu_1x_a10",
                    "A10 (24 GB PCIe)")
    assert fit["verdict"] == "tight"


def test_unknown_size_or_gpu_gives_unknown_verdict():
    assert model_fit("openai/whisper-large-v3", "gpu_1x_a10",
                     "A10 (24 GB PCIe)")["verdict"] == "unknown"
    assert model_fit("Llama-3.1-8B", "gpu_1x_a10", "")["verdict"] == "unknown"


def test_route_resolves_gpu_description(client):
    resp = client.get("/estimate/model-fit", params={
        "model": "Qwen/Qwen3.5-27B-GPTQ-Int4",
        "instance_type": "gpu_1x_a10",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] in ("tight", "no")

    unknown = client.get("/estimate/model-fit", params={
        "model": "some-model", "instance_type": "gpu_1x_a10",
    }).json()
    assert unknown["verdict"] == "unknown"

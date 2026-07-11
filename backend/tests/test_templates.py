"""Template registry: schema validation and the volume-separation rule."""

from pathlib import Path

import pytest

from app.templates import TemplateError, load_templates, parse_template

REPO_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"


def test_reference_templates_load_cleanly():
    templates, errors = load_templates(REPO_TEMPLATES)
    assert errors == {}
    # The registry grows over time; the three reference templates must exist.
    assert {"vllm-serve", "whisper-batch", "axolotl-finetune"} <= set(templates)
    # Every model-downloading reference template maps the HF cache to
    # persistent storage (gpu-smoke pulls no models, so it is exempt).
    for name in ("vllm-serve", "whisper-batch", "axolotl-finetune"):
        t = templates[name]
        hf = [v for v in t.volumes if v.container == "/root/.cache/huggingface"]
        assert hf, f"{name} must map the HuggingFace cache to persistent"
        assert hf[0].host.startswith("{persistent}")


def test_parameter_schema_is_served():
    templates, _ = load_templates(REPO_TEMPLATES)
    vllm = templates["vllm-serve"].to_api()
    model = next(p for p in vllm["parameters"] if p["name"] == "model_id")
    assert model["required"] is True          # no default
    ctx = next(p for p in vllm["parameters"] if p["name"] == "max_context")
    assert ctx == {
        "name": "max_context", "type": "integer",
        "description": "Maximum context length in tokens",
        "default": 8192, "required": False,
    }


ILLEGAL_MOUNT = """
name: evil
description: tries to read the host
image: alpine
command: cat /host-etc/passwd
volumes:
  - host: /etc
    container: /host-etc
"""


def test_illegal_mount_rejected():
    with pytest.raises(TemplateError, match="illegal mount '/etc'"):
        parse_template(ILLEGAL_MOUNT)


def test_home_mount_rejected():
    text = ILLEGAL_MOUNT.replace("/etc", "/home/ubuntu/.ssh")
    with pytest.raises(TemplateError, match="illegal mount"):
        parse_template(text)


def test_sanctioned_roots_accepted():
    text = """
name: fine
description: uses only sanctioned roots
image: alpine
command: "true"
volumes:
  - host: /workspace/ephemeral/scratch
    container: /scratch
  - host: "{persistent}/outputs"
    container: /out
"""
    t = parse_template(text)
    assert [v.host for v in t.volumes] == [
        "/workspace/ephemeral/scratch", "{persistent}/outputs",
    ]


def test_undeclared_placeholder_rejected():
    text = """
name: typo
description: command references a parameter that is not declared
image: alpine
command: echo {{model_idd}}
parameters:
  - name: model_id
    type: string
    description: the model
"""
    with pytest.raises(TemplateError, match="model_idd"):
        parse_template(text)


def test_unknown_parameter_type_rejected():
    text = """
name: badtype
description: bad parameter type
image: alpine
command: "true"
parameters:
  - name: x
    type: float64
    description: nope
"""
    with pytest.raises(TemplateError, match="float64"):
        parse_template(text)


def test_broken_template_surfaces_in_errors(tmp_path):
    (tmp_path / "good.yaml").write_text("""
name: good
description: fine
image: alpine
command: "true"
""")
    (tmp_path / "bad.yaml").write_text(ILLEGAL_MOUNT)
    templates, errors = load_templates(tmp_path)
    assert "good" in templates
    assert "bad.yaml" in errors
    assert "illegal mount" in errors["bad.yaml"]


def test_templates_endpoint_serves_schemas(client):
    body = client.get("/templates").json()
    names = {t["name"] for t in body["templates"]}
    assert {"vllm-serve", "whisper-batch", "axolotl-finetune"} <= names
    assert body["errors"] == {}

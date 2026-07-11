"""Job template registry.

A job template is a YAML file in templates/ describing a Docker-based job:
image, command with {{parameter}} placeholders, a parameter schema, volume
mounts, env vars, and GPU requirements.

The volume rule is enforced HERE, at load time: every host mount must live
under one of the two sanctioned roots —

    /workspace/ephemeral        scratch, dies with the instance
    {persistent}                the persistent filesystem; the token is
                                replaced with /lambda/nfs/<name> at dispatch

A template mounting anything else (e.g. /etc, /home) is rejected and never
becomes launchable. Ports, when declared, are always bound to 127.0.0.1 by
the dispatcher — nothing a template says can open a public listener.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

EPHEMERAL_ROOT = "/workspace/ephemeral"
PERSISTENT_TOKEN = "{persistent}"

PARAMETER_TYPES = ("string", "integer", "number", "boolean")

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class TemplateError(Exception):
    """A template file that must not be served or dispatched."""


@dataclass
class TemplateParameter:
    name: str
    type: str
    description: str
    default: object | None = None

    @property
    def required(self) -> bool:
        return self.default is None


@dataclass
class VolumeMount:
    host: str
    container: str
    read_only: bool = False


@dataclass
class PortMapping:
    host: int          # always bound to 127.0.0.1 by the dispatcher
    container: int


@dataclass
class JobTemplate:
    name: str
    description: str
    image: str
    command: str
    parameters: list[TemplateParameter] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    ports: list[PortMapping] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    gpu: dict = field(default_factory=dict)   # min_vram_gib, recommended_types
    # "" (default) = docker bridge. "host" = share the instance's network
    # namespace — needed by jobs that CALL a server another job publishes on
    # the host's loopback (e.g. llm-synthesize -> vllm-serve on 127.0.0.1).
    # Safe under the hard rule: host networking lets a container dial
    # loopback, it does not create any new listener.
    network: str = ""

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "image": self.image,
            "command": self.command,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "default": p.default,
                    "required": p.required,
                }
                for p in self.parameters
            ],
            "volumes": [
                {"host": v.host, "container": v.container, "read_only": v.read_only}
                for v in self.volumes
            ],
            "env": self.env,
            "gpu": self.gpu,
        }


def _validate_mount(host: str, template_name: str) -> None:
    ok = host.startswith(EPHEMERAL_ROOT) or host.startswith(PERSISTENT_TOKEN)
    if not ok:
        raise TemplateError(
            f"template '{template_name}': illegal mount '{host}'. Host paths "
            f"must live under {EPHEMERAL_ROOT} (scratch) or {PERSISTENT_TOKEN} "
            f"(persistent filesystem); nothing else on the instance may be "
            f"mounted into a job container."
        )


def parse_template(text: str, source: str = "<inline>") -> JobTemplate:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise TemplateError(f"{source}: not a YAML mapping")

    for required_field in ("name", "description", "image", "command"):
        if not raw.get(required_field):
            raise TemplateError(f"{source}: missing required field '{required_field}'")
    name = str(raw["name"])

    parameters = []
    for p in raw.get("parameters") or []:
        if not p.get("name") or not p.get("description"):
            raise TemplateError(
                f"template '{name}': every parameter needs a name and a description"
            )
        ptype = p.get("type", "string")
        if ptype not in PARAMETER_TYPES:
            raise TemplateError(
                f"template '{name}': parameter '{p['name']}' has unknown type "
                f"'{ptype}' (valid: {', '.join(PARAMETER_TYPES)})"
            )
        parameters.append(TemplateParameter(
            name=str(p["name"]), type=ptype,
            description=str(p["description"]), default=p.get("default"),
        ))
    declared = {p.name for p in parameters}

    # Every {{placeholder}} in the command must be a declared parameter, so a
    # typo fails at load time instead of dispatching a broken docker command.
    command = str(raw["command"]).strip()
    used = set(PLACEHOLDER_RE.findall(command))
    undeclared = used - declared
    if undeclared:
        raise TemplateError(
            f"template '{name}': command uses undeclared parameter(s): "
            f"{', '.join(sorted(undeclared))}"
        )

    volumes = []
    for v in raw.get("volumes") or []:
        host, container = v.get("host"), v.get("container")
        if not host or not container:
            raise TemplateError(
                f"template '{name}': every volume needs 'host' and 'container'"
            )
        _validate_mount(str(host), name)
        volumes.append(VolumeMount(
            host=str(host), container=str(container),
            read_only=bool(v.get("read_only", False)),
        ))

    ports = []
    for pm in raw.get("ports") or []:
        ports.append(PortMapping(host=int(pm["host"]), container=int(pm["container"])))

    env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
    gpu = raw.get("gpu") or {}

    network = str(raw.get("network") or "")
    if network not in ("", "host"):
        raise TemplateError(
            f"template '{name}': network must be omitted or 'host', "
            f"got '{network}'"
        )
    if network == "host" and ports:
        raise TemplateError(
            f"template '{name}': 'ports' and 'network: host' are mutually "
            f"exclusive (host networking has no port mappings)"
        )

    return JobTemplate(
        name=name, description=str(raw["description"]), image=str(raw["image"]),
        command=command, parameters=parameters, volumes=volumes, ports=ports,
        env=env, gpu=gpu, network=network,
    )


def load_templates(directory: Path) -> tuple[dict[str, JobTemplate], dict[str, str]]:
    """Load every *.yaml in the directory.

    Returns (templates by name, errors by filename). A broken template never
    silently disappears: its error is surfaced through GET /templates.
    """
    templates: dict[str, JobTemplate] = {}
    errors: dict[str, str] = {}
    if not directory.exists():
        return templates, errors
    for path in sorted(directory.glob("*.yaml")):
        try:
            template = parse_template(path.read_text(), source=path.name)
            if template.name in templates:
                raise TemplateError(
                    f"duplicate template name '{template.name}' in {path.name}"
                )
            templates[template.name] = template
        except (TemplateError, yaml.YAMLError) as exc:
            errors[path.name] = str(exc)
    return templates, errors

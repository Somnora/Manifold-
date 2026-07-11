"""Configuration loading.

Two sources, strictly separated:
- .env (gitignored) holds secrets: API keys, S3 credentials, Tailscale key.
- config.yaml holds tunables: guardrails, retry policy, SSH settings.

Nothing in this module talks to the network or the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Repo root is one level above backend/.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Guardrails:
    max_concurrent_instances: int = 1
    max_hourly_spend_usd: float = 4.00


@dataclass(frozen=True)
class LaunchPolicy:
    max_attempts: int = 5
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    fallback_instance_types: tuple[str, ...] = ()
    boot_timeout_seconds: float = 900.0
    boot_poll_seconds: float = 10.0


@dataclass(frozen=True)
class SSHSettings:
    key_name: str = ""
    private_key_path: str = "~/.ssh/id_ed25519"
    username: str = "ubuntu"
    connect_timeout_seconds: float = 15.0
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0


@dataclass(frozen=True)
class TaskSettings:
    poll_seconds: float = 1.0


@dataclass(frozen=True)
class IdleSettings:
    timeout_seconds: float = 300.0
    poll_seconds: float = 15.0


@dataclass(frozen=True)
class WatchSettings:
    poll_seconds: float = 60.0
    auto_launch_enabled: bool = False


@dataclass(frozen=True)
class AutopilotSettings:
    max_steps_default: int = 20
    max_steps_cap: int = 50
    wait_cap_seconds: float = 120.0
    chat_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class Settings:
    # Secrets (from .env). Empty string means "not configured".
    lambda_api_key: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    tailscale_authkey: str = ""
    # Optional: if set, the OpenAI-compatible /v1 proxy requires this as a
    # bearer token. Empty = open (fine for localhost-only single-user use).
    proxy_api_key: str = ""

    guardrails: Guardrails = field(default_factory=Guardrails)
    launch: LaunchPolicy = field(default_factory=LaunchPolicy)
    ssh: SSHSettings = field(default_factory=SSHSettings)
    tasks: TaskSettings = field(default_factory=TaskSettings)
    idle: IdleSettings = field(default_factory=IdleSettings)
    watches: WatchSettings = field(default_factory=WatchSettings)
    autopilot: AutopilotSettings = field(default_factory=AutopilotSettings)
    default_connection_mode: str = "direct-ssh"
    db_path: str = str(REPO_ROOT / "manifold.db")


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, preserving comments and order.

    Existing keys are updated in place; missing keys are appended. Values
    are written verbatim and never logged.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped else None
        if key and not stripped.startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def load_settings(
    config_path: Path | None = None, env_path: Path | None = None
) -> Settings:
    """Build Settings from config.yaml + .env at the repo root."""
    load_dotenv(env_path or REPO_ROOT / ".env")

    raw: dict = {}
    path = config_path or REPO_ROOT / "config.yaml"
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    guard = raw.get("guardrails", {})
    launch = raw.get("launch", {})
    ssh = raw.get("ssh", {})
    conn = raw.get("connection", {})
    database = raw.get("database", {})
    tasks = raw.get("tasks", {})
    idle = raw.get("idle", {})
    watches = raw.get("watches", {})
    autopilot = raw.get("autopilot", {})

    db_path = database.get("path", "manifold.db")
    if not os.path.isabs(db_path):
        db_path = str(REPO_ROOT / db_path)

    return Settings(
        lambda_api_key=os.environ.get("LAMBDA_API_KEY", ""),
        s3_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", ""),
        s3_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
        tailscale_authkey=os.environ.get("TAILSCALE_AUTHKEY", ""),
        proxy_api_key=os.environ.get("MANIFOLD_PROXY_KEY", ""),
        guardrails=Guardrails(
            max_concurrent_instances=int(guard.get("max_concurrent_instances", 1)),
            max_hourly_spend_usd=float(guard.get("max_hourly_spend_usd", 4.00)),
        ),
        launch=LaunchPolicy(
            max_attempts=int(launch.get("max_attempts", 5)),
            backoff_base_seconds=float(launch.get("backoff_base_seconds", 5)),
            backoff_max_seconds=float(launch.get("backoff_max_seconds", 120)),
            fallback_instance_types=tuple(launch.get("fallback_instance_types") or ()),
            boot_timeout_seconds=float(launch.get("boot_timeout_seconds", 900)),
            boot_poll_seconds=float(launch.get("boot_poll_seconds", 10)),
        ),
        tasks=TaskSettings(
            poll_seconds=float(tasks.get("poll_seconds", 1.0)),
        ),
        idle=IdleSettings(
            timeout_seconds=float(idle.get("timeout_seconds", 300)),
            poll_seconds=float(idle.get("poll_seconds", 15)),
        ),
        watches=WatchSettings(
            poll_seconds=float(watches.get("poll_seconds", 60)),
            auto_launch_enabled=bool(watches.get("auto_launch_enabled", False)),
        ),
        autopilot=AutopilotSettings(
            max_steps_default=int(autopilot.get("max_steps_default", 20)),
            max_steps_cap=int(autopilot.get("max_steps_cap", 50)),
            wait_cap_seconds=float(autopilot.get("wait_cap_seconds", 120)),
            chat_timeout_seconds=float(autopilot.get("chat_timeout_seconds", 300)),
        ),
        ssh=SSHSettings(
            key_name=str(ssh.get("key_name", "")),
            private_key_path=str(ssh.get("private_key_path", "~/.ssh/id_ed25519")),
            username=str(ssh.get("username", "ubuntu")),
            connect_timeout_seconds=float(ssh.get("connect_timeout_seconds", 15)),
            reconnect_base_seconds=float(ssh.get("reconnect_base_seconds", 1)),
            reconnect_max_seconds=float(ssh.get("reconnect_max_seconds", 30)),
        ),
        default_connection_mode=str(conn.get("default_mode", "direct-ssh")),
        db_path=db_path,
    )

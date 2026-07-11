"""cloud-init user-data generation.

The script installs Docker + NVIDIA Container Toolkit, the sidecar
(loopback-only), and the Claude Code CLI. If and only if the launch carries
a Tailscale auth key, it also installs and joins Tailscale with SSH enabled.
In both modes sshd remains the only public listener: the sidecar binds to
127.0.0.1 and Docker jobs are published to 127.0.0.1 by the dispatcher.

The sidecar source is embedded verbatim into the user-data (it is a single
file well under Lambda's 1 MB limit), so instances need no fetch-from-
somewhere step and no extra credentials.
"""

from __future__ import annotations

from pathlib import Path

SIDECAR_PATH = Path(__file__).resolve().parent.parent.parent / "sidecar" / "manifold_sidecar.py"

_TEMPLATE = """#!/bin/bash
# Manifold cloud-init: Docker + NVIDIA toolkit, sidecar (127.0.0.1 only),
# Claude Code CLI{tailscale_note}. sshd stays the only public listener.
set -euxo pipefail
exec > /var/log/manifold-init.log 2>&1

export DEBIAN_FRONTEND=noninteractive

# --- Docker + NVIDIA Container Toolkit (Lambda images ship the driver) ----
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi

# --- Workspace layout ------------------------------------------------------
mkdir -p /workspace/ephemeral
chown ubuntu:ubuntu /workspace/ephemeral

# --- Sidecar: single file, loopback only, systemd-supervised ---------------
python3 -m pip install --quiet 'fastapi>=0.115' 'uvicorn>=0.30' pynvml
install -d /opt/manifold
cat > /opt/manifold/manifold_sidecar.py <<'MANIFOLD_SIDECAR_EOF'
{sidecar_source}
MANIFOLD_SIDECAR_EOF

cat > /etc/systemd/system/manifold-sidecar.service <<'UNIT'
[Unit]
Description=Manifold GPU sidecar (loopback only)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/manifold/manifold_sidecar.py
Restart=always
RestartSec=2
User=ubuntu

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now manifold-sidecar

# --- Claude Code CLI (auth is manual/interactive on first use) -------------
curl -fsSL https://claude.ai/install.sh | HOME=/home/ubuntu bash || true
chown -R ubuntu:ubuntu /home/ubuntu/.local || true
{tailscale_block}
touch /var/run/manifold-init-done
"""

_TAILSCALE_BLOCK = """
# --- Tailscale (this launch requested tailscale mode) -----------------------
# Joins the tailnet so the orchestrator AND other machines on the tailnet
# can SSH in. Adds no public listener; tailscaled talks outbound only.
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey='{authkey}' --ssh --hostname='{hostname}'
"""


def build_user_data(*, tailscale_authkey: str = "", hostname: str = "") -> str:
    """Render the cloud-init script; embeds the sidecar source."""
    sidecar_source = SIDECAR_PATH.read_text()
    if "MANIFOLD_SIDECAR_EOF" in sidecar_source:
        raise ValueError("sidecar source must not contain the heredoc marker")
    if tailscale_authkey:
        ts_block = _TAILSCALE_BLOCK.format(
            authkey=tailscale_authkey, hostname=hostname or "manifold-gpu"
        )
        note = ", Tailscale"
    else:
        ts_block = ""
        note = ""
    return _TEMPLATE.format(
        sidecar_source=sidecar_source,
        tailscale_block=ts_block,
        tailscale_note=note,
    )

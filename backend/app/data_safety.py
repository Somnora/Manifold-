"""Data safety: deciding what to save off an instance before it dies.

An instance's scratch disk (/workspace/ephemeral) is destroyed with the
instance. The persistent filesystem (/lambda/nfs/<name>) survives. The
sidecar reports which ephemeral files look valuable — model weights,
datasets, images, transcripts — and Manifold has always REFUSED to terminate
while any exist (the Phase 3 safety hook).

Refusing is the right answer when a human is watching. It is the wrong answer
at 3am: an autopilot run hits the block, the GPU keeps billing, and nobody
sees the error. So termination now RESCUES first and re-checks after.

This module is the pure half: given a file list and a policy, decide what to
save, where, and what will not fit. It does no I/O — the transport (rsync to
the persistent volume, SFTP down to this machine) lives in the orchestrator,
which owns the connections. Keeping the decisions here means they can be
tested without an instance, an SSH server, or a byte of network.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

# Where the sidecar's relative paths are rooted on the instance. Mirrors the
# sidecar's EPHEMERAL_ROOT; both would have to change together.
EPHEMERAL_ROOT = "/workspace/ephemeral"

# The "deliverable" convention: templates write what a job PRODUCED under
# outputs/. Checkpoints, caches, and intermediate scratch live elsewhere, so
# scope="outputs" is the cheap rescue (pull the results, leave the weights).
OUTPUTS_PREFIX = "outputs/"

GIB = 1024 ** 3


@dataclass
class RescuePlan:
    """What a rescue will do, decided before a single byte moves."""
    # Files to pull down to this machine, in the order they will be fetched.
    download: list[dict] = field(default_factory=list)
    # Files deliberately NOT downloaded, each with a reason. These are only
    # a data-loss risk if they are not ALSO going to the persistent volume —
    # the caller weighs that; this module just reports honestly.
    skipped: list[dict] = field(default_factory=list)
    total_download_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "download": self.download,
            "skipped": self.skipped,
            "total_download_bytes": self.total_download_bytes,
        }


def in_scope(file: dict, scope: str) -> bool:
    """Does this unpersisted file fall inside the rescue scope?

    "all"     everything the sidecar flagged as valuable.
    "outputs" only a job's deliverables (outputs/...), so a 40 GB checkpoint
              does not get dragged over a home internet connection just
              because a run finished.
    """
    if scope != "outputs":
        return True
    path = str(file.get("path", "")).lstrip("/")
    return path.startswith(OUTPUTS_PREFIX)


def plan_local_transfer(files: list[dict], *, scope: str,
                        max_bytes: int) -> RescuePlan:
    """Decide which files to pull to this machine, within a byte budget.

    Smallest first: with a limited budget, saving nine small datasets beats
    saving one checkpoint that eats the whole allowance. Anything that does
    not fit is skipped WITH A REASON and surfaced to the user — a rescue that
    quietly drops files is worse than no rescue, because it lies.
    """
    plan = RescuePlan()
    candidates = []
    for f in files:
        if not in_scope(f, scope):
            plan.skipped.append({
                **f, "reason": f"outside the '{scope}' rescue scope"})
            continue
        candidates.append(f)

    used = 0
    for f in sorted(candidates, key=lambda f: int(f.get("size_bytes", 0) or 0)):
        size = int(f.get("size_bytes", 0) or 0)
        if used + size > max_bytes:
            plan.skipped.append({
                **f,
                "reason": (
                    f"over the {max_bytes / GIB:.0f} GiB local transfer "
                    f"budget (this file is {size / GIB:.1f} GiB)"
                ),
            })
            continue
        used += size
        plan.download.append(f)
    plan.total_download_bytes = used
    return plan


def remote_path(rel_path: str) -> str:
    """Absolute path on the instance for a sidecar-reported file.

    The sidecar reports paths relative to its ephemeral root; a hostile or
    buggy one must not be able to make us read /etc/shadow, so the result is
    normalized and confined back under the root.
    """
    joined = posixpath.normpath(posixpath.join(EPHEMERAL_ROOT, rel_path))
    if joined != EPHEMERAL_ROOT and not joined.startswith(EPHEMERAL_ROOT + "/"):
        raise ValueError(f"path escapes {EPHEMERAL_ROOT}: {rel_path!r}")
    return joined


def local_path(local_dir: str, instance_id: str, rel_path: str) -> Path:
    """Where a rescued file lands on THIS machine.

    <local_dir>/<instance_id>/<the file's path on the instance>, so two
    instances rescuing the same filename never collide and the layout still
    reads like the box it came from. Same confinement rule as above: a path
    from the instance can never write outside the rescue directory.
    """
    root = Path(local_dir).expanduser().resolve() / instance_id
    target = (root / rel_path.lstrip("/")).resolve()
    if not str(target).startswith(str(root) + "/") and target != root:
        raise ValueError(f"path escapes the rescue directory: {rel_path!r}")
    return target


def summarize(report: dict) -> str:
    """One human line for the audit log and the notification body."""
    saved = len(report.get("downloaded", []))
    synced = report.get("synced_to")
    left = len(report.get("unsaved", []))
    parts = []
    if synced:
        parts.append(f"synced scratch to {synced}")
    if saved:
        got = report.get("downloaded_bytes", 0)
        parts.append(f"downloaded {saved} file(s), {got / GIB:.2f} GiB")
    if left:
        parts.append(f"{left} file(s) could NOT be saved")
    return "; ".join(parts) or "nothing to rescue"

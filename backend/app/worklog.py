"""Worklog: an append-only markdown record of what Manifold accomplished.

Every settled job and autopilot run becomes one human-readable entry. The
canonical file lives next to the database (worklog.md under the data dir),
and an optional user-chosen mirror directory gets the same entries appended
to manifold-worklog.md there. Point the mirror at an Obsidian vault and the
vault "integration" is done - vaults are just files; point it at a repo and
every agent session in that repo (Claude, Codex, a local model) can read
what the GPU side did without asking. The get_work_log MCP tool serves the
same entries to connected agents directly.

Writes must never break the work they describe: any I/O failure here is
logged and swallowed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("manifold.worklog")

ENTRY_MARK = "## "


class Worklog:
    def __init__(self, primary: Path, prefs=None):
        self._primary = Path(primary)
        self._prefs = prefs      # PreferenceStore; read per write, never cached

    def _targets(self) -> list[Path]:
        targets = [self._primary]
        if self._prefs is not None:
            try:
                mirror = self._prefs.get().worklog.mirror_dir.strip()
            except Exception:
                mirror = ""
            if mirror:
                targets.append(
                    Path(mirror).expanduser() / "manifold-worklog.md")
        return targets

    def record(self, title: str, lines: list[str]) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = (
            f"\n{ENTRY_MARK}{stamp} - {title}\n"
            + "\n".join(f"- {line}" for line in lines if line)
            + "\n"
        )
        for path in self._targets():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(entry)
            except Exception:
                logger.warning("worklog write to %s failed", path,
                               exc_info=True)

    def tail(self, limit: int = 20) -> list[str]:
        """The most recent `limit` entries, oldest first."""
        try:
            text = self._primary.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        # The first split piece may keep its own mark (a file that starts
        # at "## " with no leading newline, e.g. hand-trimmed); strip it
        # before re-adding so no entry ever gets a doubled "## ## " header.
        entries = [
            (ENTRY_MARK + chunk.removeprefix(ENTRY_MARK)).strip()
            for chunk in text.split("\n" + ENTRY_MARK)
            if chunk.strip()
        ]
        return entries[-limit:]

    @property
    def path(self) -> str:
        return str(self._primary)

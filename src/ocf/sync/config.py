"""Settings and local state locations for ``ocf sync``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_TOOLS: tuple[str, ...] = (
    "claude-code-cli",
    "claude-code-app",
    "claude-cowork-app",
    "codex",
    "cursor",
)
"""Tools synced by default. ChatGPT and claude.ai come as downloaded
ZIP exports and stay a manual ``ocf export`` step."""

SETTINGS_FILENAME = "sync-settings.json"


def state_root() -> Path:
    """Per-user directory for sync state, outside any synced folder."""
    override = os.environ.get("OCF_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ocf"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ocf"


def machine_name() -> str:
    """Short, filesystem-safe name of this machine (``OCF_MACHINE`` overrides)."""
    raw = os.environ.get("OCF_MACHINE") or os.environ.get("COMPUTERNAME") or socket.gethostname()
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.strip()).strip("-")
    return name or "machine"


def settings_path() -> Path:
    return state_root() / SETTINGS_FILENAME


@dataclass
class SyncConfig:
    """What to sync where, and how eagerly.

    Timing:

    - ``interval``: seconds between scans of the session storage.
    - ``quiet``: a changed session is written once it has not changed
      for this many seconds (one write per pause, not per message).
    - ``max_delay``: a session that keeps changing is written at least
      this often anyway.
    - ``slow_scan``: minimum seconds between full scans of tools with an
      expensive scan (Cursor's SQLite database).
    """

    archive: Path | None = None
    archive_md: Path | None = None
    meili_url: str | None = None
    meili_key: str | None = None
    index_name: str = "ocf-sessions"
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    interval: float = 15.0
    quiet: float = 90.0
    max_delay: float = 600.0
    slow_scan: float = 120.0
    codex_imports: bool = False
    """Also archive threads Codex Desktop imported from other agents
    (copies of Claude Code sessions); see CodexAdapter.include_imports."""
    machine: str = field(default_factory=machine_name)

    # -- derived -------------------------------------------------------

    def resolved_key(self) -> str:
        """API key: given for this run, else the credential store, else
        ``MEILI_MASTER_KEY``. Empty if none is set."""
        if self.meili_key:
            return self.meili_key
        from ocf.sync import secrets

        return secrets.get_key(self.meili_url) or os.environ.get("MEILI_MASTER_KEY") or ""

    def index_configured(self) -> bool:
        """A Meilisearch URL is set (indexing wanted)."""
        return bool(self.meili_url)

    def state_dir(self) -> Path:
        """Local state for this archive: manifests, activity log, backlog.

        Derived from the path as written, not resolved, so it stays the
        same while a cloud drive is not mounted yet.
        """
        tag = os.path.normcase(os.path.abspath(str(self.archive))) if self.archive else "none"
        digest = hashlib.sha1(tag.encode("utf-8")).hexdigest()[:10]
        return state_root() / "sync" / digest

    # -- persistence ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("archive", "archive_md"):
            if data[key] is not None:
                data[key] = str(data[key])
        data.pop("machine", None)  # always derived on the machine itself
        data.pop("meili_key", None)  # lives in the credential store
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncConfig:
        known = {f.name for f in fields(cls)} - {"machine"}
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("archive", "archive_md"):
            if kwargs.get(key):
                kwargs[key] = Path(kwargs[key])
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path | None = None) -> SyncConfig:
        path = path or settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(data if isinstance(data, dict) else {})

    def save(self, path: Path | None = None) -> Path:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path


__all__ = [
    "DEFAULT_TOOLS",
    "SyncConfig",
    "machine_name",
    "settings_path",
    "state_root",
]

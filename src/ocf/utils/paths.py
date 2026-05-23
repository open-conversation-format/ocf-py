"""Default source paths for AI-tool conversation stores.

Each AI tool stores its sessions under a platform-specific default
location. The exporters use these as the default ``source_dir``;
callers can override with a custom path.

Cross-platform handling:
    - Linux/macOS: ``$HOME/.<tool>/...``
    - Windows: ``%APPDATA%\\...`` for IDE installs, ``%USERPROFILE%\\.<tool>\\...`` for CLI tools.

If a default cannot be determined for the current platform, the
function returns the most likely path even if it does not exist;
callers are expected to verify with :meth:`Path.exists` before use.

MSIX packaging note (Windows)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Claude Desktop App ships as an MSIX package.  Windows virtualises
``%APPDATA%\\Claude\\`` for processes inside the container — they see
it at the normal Roaming path.  External processes (user shells,
standalone Python) see the real location under::

    %LOCALAPPDATA%\\Packages\\Claude_<hash>\\LocalCache\\Roaming\\Claude\\

:func:`_claude_desktop_data_dir` transparently resolves to whichever
location actually exists.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def home() -> Path:
    """User home directory (cross-platform)."""
    return Path.home()


def appdata() -> Path:
    """Windows ``%APPDATA%`` (Roaming) — falls back to ``~/.config`` elsewhere."""
    if sys.platform.startswith("win"):
        env = os.environ.get("APPDATA")
        if env:
            return Path(env)
        return home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return home() / "Library" / "Application Support"
    # Linux / other Unix
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else home() / ".config"


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def codex_sessions_dir() -> Path:
    """Default Codex sessions directory: ``~/.codex/sessions/``."""
    return home() / ".codex" / "sessions"


# ---------------------------------------------------------------------------
# Claude Desktop App — MSIX-aware data directory
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _claude_desktop_data_dir() -> Path:
    """Resolve the Claude Desktop App's data directory.

    1. ``%APPDATA%/Claude/`` — works inside the MSIX container
       (i.e. when Claude Code runs as a child of the Desktop App).
    2. ``%LOCALAPPDATA%/Packages/Claude_*/LocalCache/Roaming/Claude/``
       — the real path that external processes (user shells) must use.
    3. Falls back to ``%APPDATA%/Claude/`` even if it doesn't exist
       (best-effort for non-Windows or unusual setups).
    """
    candidate = appdata() / "Claude"
    if candidate.exists():
        return candidate
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            packages = Path(local) / "Packages"
            try:
                for pkg in packages.iterdir():
                    if pkg.name.lower().startswith("claude_") and pkg.is_dir():
                        msix = pkg / "LocalCache" / "Roaming" / "Claude"
                        if msix.exists():
                            return msix
            except OSError:
                pass
    return candidate  # fallback: original path even if missing


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def claude_code_projects_dir() -> Path:
    """Default Claude Code projects directory: ``~/.claude/projects/``.

    Used by Claude Code CLI, IDE extensions, and the Desktop App's
    main session writer (Desktop also writes a metadata index alongside
    these jsonl files - see :func:`claude_code_desktop_sessions_metadata_dir`).
    """
    return home() / ".claude" / "projects"


def claude_code_global_history() -> Path:
    """Default Claude Code global history file: ``~/.claude/history.jsonl``."""
    return home() / ".claude" / "history.jsonl"


def claude_code_desktop_sessions_metadata_dir() -> Path:
    """Claude Code Desktop App's session-metadata directory.

    Per session: ``<acc>/<org>/local_<cliSessionId>.json`` carrying
    ``{title, cwd, model, cliSessionId, completedTurns, ...}``.

    The actual jsonl content for these sessions still lives in
    :func:`claude_code_projects_dir`; the metadata file maps via
    ``cliSessionId``.

    Uses :func:`_claude_desktop_data_dir` to resolve MSIX virtualisation
    on Windows (see module docstring).
    """
    return _claude_desktop_data_dir() / "claude-code-sessions"


def claude_code_desktop_agent_mode_sessions_dir() -> Path:
    """Claude Code Desktop App's "agent mode" sandboxed sessions root.

    Each spawned background agent runs inside a virtualized worktree
    with its own ``.claude/projects/`` containing its session jsonl.

    Layout::

        <root>/<acc>/<org>/<agent-uuid>/.claude/projects/<encoded>/*.jsonl

    Uses :func:`_claude_desktop_data_dir` to resolve MSIX virtualisation
    on Windows (see module docstring).
    """
    return _claude_desktop_data_dir() / "local-agent-mode-sessions"


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def cursor_user_dir() -> Path:
    """Default Cursor User directory.

    Probes the platform-specific candidates in order and returns the
    first one that actually exists. Falls back to the primary
    platform default if nothing is found (so callers can still get a
    deterministic path for error messages).

    Probed locations:

    - **Windows**: ``%APPDATA%/Cursor/User/``
    - **macOS**: ``~/Library/Application Support/Cursor/User/``
    - **Linux native**: ``~/.config/Cursor/User/`` (or ``$XDG_CONFIG_HOME``)
    - **Linux Remote / SSH** (``cursor-server``): ``~/.cursor-server/data/User/``
    - **WSL2 reading Windows-native Cursor**:
      ``/mnt/c/Users/<u>/AppData/Roaming/Cursor/User/`` — discovered by
      scanning ``/mnt/c/Users/*`` so the Windows username doesn't
      need to be known.

    Matches the locations listed by ``cursor-chat-browser`` so users
    on Remote-SSH / WSL setups get sessions discovered automatically
    without passing ``--source-dir``.
    """
    candidates: list[Path] = []

    if sys.platform.startswith("win"):
        candidates.append(appdata() / "Cursor" / "User")
    elif sys.platform == "darwin":
        candidates.append(
            home() / "Library" / "Application Support" / "Cursor" / "User"
        )
    else:
        # Linux family: native config, cursor-server (Remote/SSH), WSL2
        candidates.append(appdata() / "Cursor" / "User")
        candidates.append(home() / ".cursor-server" / "data" / "User")
        mnt_c_users = Path("/mnt/c/Users")
        if mnt_c_users.is_dir():
            try:
                for user_dir in mnt_c_users.iterdir():
                    p = user_dir / "AppData" / "Roaming" / "Cursor" / "User"
                    if p.is_dir():
                        candidates.append(p)
            except OSError:
                pass

    for c in candidates:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return candidates[0]


def cursor_global_state_db() -> Path:
    """Cursor's global state DB."""
    return cursor_user_dir() / "globalStorage" / "state.vscdb"


def cursor_workspace_storage_dir() -> Path:
    """Cursor's per-workspace storage dir."""
    return cursor_user_dir() / "workspaceStorage"


__all__ = [
    "home",
    "appdata",
    "codex_sessions_dir",
    "claude_code_projects_dir",
    "claude_code_global_history",
    "claude_code_desktop_sessions_metadata_dir",
    "claude_code_desktop_agent_mode_sessions_dir",
    "_claude_desktop_data_dir",
    "cursor_user_dir",
    "cursor_global_state_db",
    "cursor_workspace_storage_dir",
]

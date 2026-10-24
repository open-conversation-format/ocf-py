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

Since 2026-10 the app moves its user data out of the container into::

    %LOCALAPPDATA%\\Claude-Data\\claude_<hash>\\

leaving only an empty ``.user-data-left.claude_<hash>`` marker in both
old locations.

:func:`_claude_desktop_data_dir` transparently resolves to whichever
location actually holds the session data.
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


def codex_archived_sessions_dir() -> Path:
    """Where Codex moves archived threads: ``~/.codex/archived_sessions/``."""
    return home() / ".codex" / "archived_sessions"


# ---------------------------------------------------------------------------
# Claude Desktop App — MSIX-aware data directory
# ---------------------------------------------------------------------------

_CLAUDE_DESKTOP_SESSION_SUBDIRS = ("claude-code-sessions", "local-agent-mode-sessions")


def _claude_desktop_candidates() -> list[Path]:
    """Possible Claude Desktop data dirs, most current layout first."""
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") if sys.platform.startswith("win") else None
    if local:
        try:
            candidates.extend(
                sorted(
                    p for p in (Path(local) / "Claude-Data").iterdir()
                    if p.is_dir() and p.name.lower().startswith("claude_")
                )
            )
        except OSError:
            pass
    candidates.append(appdata() / "Claude")
    if local:
        try:
            for pkg in sorted((Path(local) / "Packages").iterdir()):
                if pkg.name.lower().startswith("claude_") and pkg.is_dir():
                    candidates.append(pkg / "LocalCache" / "Roaming" / "Claude")
        except OSError:
            pass
    return candidates


@lru_cache(maxsize=1)
def _claude_desktop_data_dir() -> Path:
    """Resolve the Claude Desktop App's data directory.

    Returns the first candidate that actually contains session data:

    1. ``%LOCALAPPDATA%/Claude-Data/claude_*/`` - current layout (the
       app migrated its data here; old dirs only keep a marker file).
    2. ``%APPDATA%/Claude/`` - pre-migration; also what processes inside
       the MSIX container see.
    3. ``%LOCALAPPDATA%/Packages/Claude_*/LocalCache/Roaming/Claude/``
       - the real pre-migration path for external processes.

    Existence alone is not enough: after the migration ``%APPDATA%/Claude``
    still exists, holding only the ``.user-data-left.*`` marker. Falls
    back to ``%APPDATA%/Claude/`` when no candidate has session data.
    """
    for candidate in _claude_desktop_candidates():
        if any((candidate / sub).is_dir() for sub in _CLAUDE_DESKTOP_SESSION_SUBDIRS):
            return candidate
    return appdata() / "Claude"


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

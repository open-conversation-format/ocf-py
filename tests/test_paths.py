"""Tests for ocf.utils.paths - Claude Desktop data dir resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ocf.utils import paths


@pytest.fixture()
def win_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    roaming.mkdir()
    local.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    paths._claude_desktop_data_dir.cache_clear()
    yield {"roaming": roaming, "local": local}
    paths._claude_desktop_data_dir.cache_clear()


def test_desktop_dir_prefers_migrated_claude_data(win_env: dict[str, Path]) -> None:
    """After the 2026-10 migration, %APPDATA%/Claude only holds a marker."""
    old = win_env["roaming"] / "Claude"
    old.mkdir()
    (old / ".user-data-left.claude_abc123").touch()
    new = win_env["local"] / "Claude-Data" / "claude_abc123"
    (new / "claude-code-sessions").mkdir(parents=True)

    assert paths._claude_desktop_data_dir() == new
    assert paths.claude_code_desktop_sessions_metadata_dir() == new / "claude-code-sessions"
    assert paths.claude_code_desktop_agent_mode_sessions_dir() == new / "local-agent-mode-sessions"


def test_desktop_dir_pre_migration_appdata(win_env: dict[str, Path]) -> None:
    old = win_env["roaming"] / "Claude"
    (old / "local-agent-mode-sessions").mkdir(parents=True)

    assert paths._claude_desktop_data_dir() == old


def test_desktop_dir_pre_migration_msix_container(win_env: dict[str, Path]) -> None:
    msix = win_env["local"] / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    (msix / "claude-code-sessions").mkdir(parents=True)

    assert paths._claude_desktop_data_dir() == msix


def test_desktop_dir_fallback_without_session_data(win_env: dict[str, Path]) -> None:
    (win_env["roaming"] / "Claude").mkdir()

    assert paths._claude_desktop_data_dir() == win_env["roaming"] / "Claude"

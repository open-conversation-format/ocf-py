"""Test fixtures for exporters.

Synthetic fixtures mirror the **real** Codex Responses-API event
shape verified against an actual ``rollout-*.jsonl`` from
``~/.codex/sessions/``. The earlier ChatSyncer-style synthetic fixture
used Chat-Completions-flavored events that don't match what Codex CLI
actually writes; that fixture is gone now.

There is also an integration fixture pointing at the real Codex
sessions directory if it exists on the test machine; tests using it
are skipped when not available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Codex Responses-API events (synthetic, format-accurate)
# ---------------------------------------------------------------------------

CODEX_SESSION_ID = "019dc9c5-4467-72b2-b4d5-62de12f45004"
CODEX_CWD = "C:\\Development\\Projekte\\OpenChatFormat"


def _codex_events() -> list[dict[str, Any]]:
    """Minimal session covering the 4 top-level event types and the main
    response_item subtypes (message, reasoning, function_call,
    function_call_output). Mirrors real Codex CLI v0.124+ output.
    """
    return [
        # session_meta — first event
        {
            "timestamp": "2026-04-26T12:30:46.147Z",
            "type": "session_meta",
            "payload": {
                "id": CODEX_SESSION_ID,
                "timestamp": "2026-04-26T12:30:41.796Z",
                "cwd": CODEX_CWD,
                "originator": "Codex Desktop",
                "cli_version": "0.125.0-alpha.3",
                "source": "vscode",
                "model_provider": "openai",
                "base_instructions": {"text": "You are Codex, a coding agent."},
            },
        },
        # turn_context — sets model for upcoming turn
        {
            "timestamp": "2026-04-26T12:30:46.150Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "019dc9c5-54e6-74a1-a689-9f1e99bc1e4e",
                "cwd": CODEX_CWD,
                "model": "gpt-5.5",
                "personality": "friendly",
            },
        },
        # event_msg.task_started — telemetry, dropped by exporter
        {
            "timestamp": "2026-04-26T12:30:46.149Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "019dc9c5-54e6-74a1-a689-9f1e99bc1e4e",
            },
        },
        # response_item.message — developer (initial permissions setup)
        {
            "timestamp": "2026-04-26T12:30:46.151Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": "<permissions instructions>..."}
                ],
            },
        },
        # response_item.message — user prompt
        {
            "timestamp": "2026-04-26T12:30:50.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "List files in this folder."}
                ],
            },
        },
        # response_item.reasoning — model thinks
        {
            "timestamp": "2026-04-26T12:30:54.534Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"text": "User wants directory listing."}],
                "content": None,
                "encrypted_content": "gAAAAAB...",
            },
        },
        # response_item.function_call — assistant calls a tool
        {
            "timestamp": "2026-04-26T12:30:55.898Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": "{\"command\":\"Get-ChildItem -Force\","
                "\"workdir\":\"C:\\\\Development\\\\Projekte\\\\OpenChatFormat\","
                "\"timeout_ms\":120000}",
                "call_id": "call_Y5YZhS0LthKy1jec4a3iHo1S",
            },
        },
        # response_item.function_call_output — tool result
        {
            "timestamp": "2026-04-26T12:30:56.378Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_Y5YZhS0LthKy1jec4a3iHo1S",
                "output": "Exit code: 0\nWall time: 0.2 seconds\nOutput:\nfile1.md\nfile2.py\n",
            },
        },
        # response_item.message — assistant final text
        {
            "timestamp": "2026-04-26T12:30:58.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Found 2 files: file1.md, file2.py."}
                ],
            },
        },
        # event_msg.thread_name_updated — title set late in session
        {
            "timestamp": "2026-04-26T12:31:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_name_updated",
                "thread_name": "List files in OpenChatFormat",
            },
        },
        # event_msg.task_complete — telemetry, dropped
        {
            "timestamp": "2026-04-26T12:31:00.500Z",
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]


@pytest.fixture()
def codex_rollout_file(tmp_path: Path) -> Path:
    """A single Codex rollout-*.jsonl file with format-accurate events."""
    rollout = (
        tmp_path
        / f"rollout-2026-04-26T12-30-41-{CODEX_SESSION_ID}.jsonl"
    )
    with rollout.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in _codex_events():
            fh.write(json.dumps(ev) + "\n")
    return rollout


@pytest.fixture()
def codex_sessions_dir(tmp_path: Path) -> Path:
    """A sessions directory containing one rollout file in the
    real ``year/month/day`` layout Codex uses.
    """
    sessions = tmp_path / "codex" / "sessions" / "2026" / "04" / "26"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-2026-04-26T12-30-41-{CODEX_SESSION_ID}.jsonl"
    with rollout.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in _codex_events():
            fh.write(json.dumps(ev) + "\n")
    # Also write a sibling session_index.jsonl two levels up
    index = tmp_path / "codex" / "session_index.jsonl"
    with index.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            json.dumps(
                {
                    "id": CODEX_SESSION_ID,
                    "thread_name": "List files in OpenChatFormat",
                    "updated_at": "2026-04-26T12:31:00.000Z",
                }
            )
            + "\n"
        )
    return tmp_path / "codex" / "sessions"


# ---------------------------------------------------------------------------
# Multi-session fixture for find_by_name
# ---------------------------------------------------------------------------

def _write_codex_session(
    path: Path,
    *,
    session_id: str,
    cwd: str,
    thread_name: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-04-26T12:30:46.147Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-04-26T12:30:41.796Z",
                "cwd": cwd,
                "originator": "Codex Desktop",
                "cli_version": "0.125.0-alpha.3",
                "source": "vscode",
            },
        },
        {
            "timestamp": "2026-04-26T12:30:50.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hi"}],
            },
        },
    ]
    if thread_name:
        events.append(
            {
                "timestamp": "2026-04-26T12:31:00.000Z",
                "type": "event_msg",
                "payload": {"type": "thread_name_updated", "thread_name": thread_name},
            }
        )
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


@pytest.fixture()
def codex_multi_sessions_dir(tmp_path: Path) -> Path:
    """Three Codex sessions with different cwds and thread_names."""
    base = tmp_path / "codex"
    sessions = base / "sessions"
    _write_codex_session(
        sessions / "2026" / "04" / "24"
        / "rollout-2026-04-24T10-00-00-019aaa00-0000-7000-8000-000000000001.jsonl",
        session_id="019aaa00-0000-7000-8000-000000000001",
        cwd="/home/user/openchatformat",
        thread_name="Spec design discussion",
    )
    _write_codex_session(
        sessions / "2026" / "04" / "25"
        / "rollout-2026-04-25T10-00-00-019bbb00-0000-7000-8000-000000000002.jsonl",
        session_id="019bbb00-0000-7000-8000-000000000002",
        cwd="/home/user/ocf-py",
        thread_name="Python exporter implementation",
    )
    _write_codex_session(
        sessions / "2026" / "04" / "26"
        / "rollout-2026-04-26T10-00-00-019ccc00-0000-7000-8000-000000000003.jsonl",
        session_id="019ccc00-0000-7000-8000-000000000003",
        cwd="/home/user/openchatformat",
        thread_name="Roundtrip analysis",
    )
    # session_index.jsonl alongside sessions/
    index = base / "session_index.jsonl"
    with index.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            json.dumps(
                {
                    "id": "019aaa00-0000-7000-8000-000000000001",
                    "thread_name": "Spec design discussion",
                    "updated_at": "2026-04-24T11:00:00Z",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "id": "019bbb00-0000-7000-8000-000000000002",
                    "thread_name": "Python exporter implementation",
                    "updated_at": "2026-04-25T11:00:00Z",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "id": "019ccc00-0000-7000-8000-000000000003",
                    "thread_name": "Roundtrip analysis",
                    "updated_at": "2026-04-26T11:00:00Z",
                }
            )
            + "\n"
        )
    return sessions


# ---------------------------------------------------------------------------
# Real-machine integration fixture (skipped when Codex not present)
# ---------------------------------------------------------------------------

REAL_CODEX_SESSIONS_DIR = (Path.home() / ".codex" / "sessions").resolve()


@pytest.fixture()
def real_codex_session() -> Path:
    """Yield a real Codex rollout file from this machine, or skip.

    Used for sanity smoke-testing the exporter against actual data.
    Tests using this fixture should not assert content - only that
    export_one returns a schema-valid OCF document without raising.
    """
    if not REAL_CODEX_SESSIONS_DIR.exists():
        pytest.skip("No real Codex sessions on this machine")
    candidates = sorted(REAL_CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    if not candidates:
        pytest.skip("Codex sessions directory exists but contains no rollouts")
    return candidates[-1]  # most recent

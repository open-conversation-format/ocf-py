"""Pre-flight format census: list raw types the adapters do not know yet.

The converters ignore unknown event and content-block types silently, so a
plain export cannot reveal that a tool update started writing something new.
This scans this machine's Claude Code, Codex and Cursor storage and reports
only the types outside the known lists below. Run it before the first export
after a tool update:

    uv run python scripts/format_census.py [--days 30]

Exit code 0: nothing unknown. Exit code 1: unknown types found; decide per
type whether an adapter must convert it or it joins the ignored list (keep
both lists in sync with src/ocf/exporters/README.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocf.utils.jsonl import read_maybe_compressed
from ocf.utils.paths import (
    claude_code_desktop_agent_mode_sessions_dir,
    claude_code_projects_dir,
    codex_archived_sessions_dir,
    codex_sessions_dir,
    cursor_user_dir,
)
from ocf.utils.sqlite_ro import open_ro

# Converted or deliberately ignored, per src/ocf/exporters/README.md.
CLAUDE_CODE_EVENTS = {
    "user", "assistant", "tool_result", "tool_use", "ai-title", "custom-title", "system",
    "attachment", "queue-operation", "last-prompt", "mode", "permission-mode", "atis-latch",
    "agent-name", "cost-state", "file-history-snapshot", "file-history-delta", "bridge-session",
    "relocated", "frame-link", "teleported-from", "summary", "tools-used",
    "artifact-comment-monitor", "artifact-autoreact-ledger", "session-owner",
    "api-request", "api-request-shape", "api-request-blob",
}
CLAUDE_CODE_BLOCKS = {
    "assistant": {"text", "thinking", "tool_use", "fallback"},
    "user": {"text", "image", "tool_result", "document"},
}
CODEX_TOP_LEVEL = {
    "session_meta", "turn_context", "response_item", "event_msg",
    "world_state", "token_usage_record", "compacted",
}
CODEX_RESPONSE_ITEMS = {
    "message", "reasoning", "function_call", "function_call_output", "web_search_call",
    "custom_tool_call", "custom_tool_call_output", "additional_tools", "compaction",
}
CURSOR_BUBBLE_FIELDS = ("text", "rawText", "thinking", "codeBlocks", "toolFormerData")


def _shape(value: Any) -> Any:
    """Keys and short scalars only - never dump content."""
    if isinstance(value, dict):
        return {k: (v if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) < 60)
                    else f"<{type(v).__name__}>") for k, v in value.items()}
    return type(value).__name__


def claude_code(examples: dict[str, Any]) -> tuple[Counter, Counter, int]:
    events, blocks, files = Counter(), Counter(), 0
    for root in (claude_code_projects_dir(), claude_code_desktop_agent_mode_sessions_dir()):
        try:
            paths = list(root.rglob("*.jsonl"))
        except OSError:
            continue
        for f in paths:
            if f.name in ("audit.jsonl", "journal.jsonl"):
                continue
            files += 1
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype not in CLAUDE_CODE_EVENTS:
                        events[etype] += 1
                        examples.setdefault(f"claude-code event {etype}", _shape(ev))
                    msg = ev.get("message") if etype in ("user", "assistant") else None
                    if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                        continue
                    known = CLAUDE_CODE_BLOCKS.get(msg.get("role"), set())
                    for b in msg["content"]:
                        if isinstance(b, dict) and b.get("type") not in known:
                            key = f"{msg.get('role')}:{b.get('type')}"
                            blocks[key] += 1
                            examples.setdefault(f"claude-code block {key}", sorted(b))
    return events, blocks, files


def codex(examples: dict[str, Any]) -> tuple[Counter, Counter, Counter, int]:
    top, items, versions = Counter(), Counter(), Counter()
    files = [p for d in (codex_sessions_dir(), codex_archived_sessions_dir()) if d.exists()
             for pattern in ("rollout-*.jsonl", "rollout-*.jsonl.zst") for p in d.rglob(pattern)]
    for f in files:
        for line in read_maybe_compressed(f).decode("utf-8", errors="replace").split("\n"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype, payload = ev.get("type"), ev.get("payload") or {}
            if etype == "session_meta":
                versions[payload.get("cli_version")] += 1
            if etype not in CODEX_TOP_LEVEL:
                top[etype] += 1
                examples.setdefault(f"codex {etype}", _shape(payload))
            if etype == "response_item" and payload.get("type") not in CODEX_RESPONSE_ITEMS:
                items[payload.get("type")] += 1
                examples.setdefault(f"codex response_item {payload.get('type')}", _shape(payload))
    return top, items, versions, len(files)


def cursor(days: int) -> dict[str, int]:
    db = cursor_user_dir() / "globalStorage" / "state.vscdb"
    stats = {"composers": 0, "bubbles": 0, "unreadable": 0, "headerless": 0}
    if not db.exists():
        return stats
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    with open_ro(db) as conn:
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            try:
                composer = json.loads(value or "")
            except json.JSONDecodeError:
                continue
            if (composer.get("lastUpdatedAt") or composer.get("createdAt") or 0) < cutoff:
                continue
            cid = key.split(":", 1)[1]
            rows = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key LIKE ?", (f"bubbleId:{cid}:%",)
            ).fetchall()
            if not rows:
                continue
            stats["composers"] += 1
            headers = composer.get("fullConversationHeadersOnly") or conn.execute(
                "SELECT 1 FROM cursorDiskKV WHERE key LIKE ? LIMIT 1",
                (f"composerConvHeaders:{cid}:%",),
            ).fetchone()
            stats["headerless"] += not headers
            for (raw,) in rows:
                try:
                    bubble = json.loads(raw or "")
                except json.JSONDecodeError:
                    continue
                stats["bubbles"] += 1
                stats["unreadable"] += not any(bubble.get(f) for f in CURSOR_BUBBLE_FIELDS)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30,
                        help="Cursor: only composers updated in this many days (default 30)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    examples: dict[str, Any] = {}

    events, blocks, files = claude_code(examples)
    print(f"Claude Code: {files} session files")
    print(f"  unknown event types:    {dict(events) or 'none'}")
    print(f"  unknown content blocks: {dict(blocks) or 'none'}")

    top, items, versions, cx_files = codex(examples)
    print(f"Codex: {cx_files} rollout files, cli versions {dict(sorted(versions.items(), key=str))}")
    print(f"  unknown top-level types:     {dict(top) or 'none'}")
    print(f"  unknown response_item types: {dict(items) or 'none'}")

    cs = cursor(args.days)
    share = 100 * cs["unreadable"] / max(cs["bubbles"], 1)
    print(f"Cursor: {cs['composers']} composers with bubbles updated in {args.days} days, "
          f"{cs['bubbles']} bubbles")
    print(f"  bubbles without any field the adapter reads: {cs['unreadable']} ({share:.1f}%), "
          f"composers without any headers: {cs['headerless']}")

    if examples:
        print("examples (keys only):")
        for name, shape in examples.items():
            print(f"  {name}: {json.dumps(shape, ensure_ascii=False)[:240]}")
    return 1 if (events or blocks or top or items) else 0


if __name__ == "__main__":
    sys.exit(main())

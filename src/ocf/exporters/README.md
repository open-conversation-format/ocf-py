# Source adapters — reference projects

Each ocf-py source adapter (`codex`, `claude_code`, `cursor`) is built
against a specific tool's on-disk storage. When the tool ships a new
version that changes the storage format, the adapter has to follow.

To stay on top of that — and to learn from what other people doing the
same job have already figured out — this file tracks the most useful
external implementations and their reading of the same data, per tool.
Re-read this list before any significant adapter change; check the
linked repos' recent commits / issues for newly discovered fields or
format breaks.

The numbers below (stars, last reviewed) are point-in-time. Refresh
them whenever you revisit the list — they're a coarse signal of "is
this still maintained" and "did anything land since I last looked".

## cursor

Adapter: [`cursor.py`](cursor.py). Storage map: [`docs/cursor-storage.md`](../../../docs/cursor-storage.md).

| Project | URL | Stars | Last reviewed | What it reads | Notable |
| --- | --- | ---:| --- | --- | --- |
| **thomas-pedersen / cursor-chat-browser** | https://github.com/thomas-pedersen/cursor-chat-browser | 514 | 2026-05-23 | `ItemTable.composer.composerData`, global `composerData:*`, `bubbleId:*`, `messageRequestContext:*` | Best-in-class workspace mapping via `projectLayouts[].rootPath`. No model detection. Multi-root `.code-workspace` unhandled. Issue [#18](https://github.com/thomas-pedersen/cursor-chat-browser/issues/18) — Cursor v44.9 globalStorage shift; [#27](https://github.com/thomas-pedersen/cursor-chat-browser/issues/27) — 0.48.6 workspaceStorage break. |
| **saharmor / cursor-view** | https://github.com/saharmor/cursor-view | 126 | 2026-05-23 | `bubbleId:*`, per-workspace `history.entries[*].editor.resource` | Naive common-prefix of attached file paths for workspace. Drops model. Drops `toolFormerData`. |
| **S2thend / cursor-history** + **cursor-history-mcp** | https://github.com/S2thend/cursor-history | 73 + 31 | 2026-05-23 | `composer.composerData`, `workbench.panel.aichat.view.aichat.chatdata`, **`workbench.panel.chat.view.chat.chatdata`** (post v44.9 key — others miss), `aiService.prompts`, `aiService.generations`, `bubbleId:{composerId}:%` | Probes `sqlite_master` for table existence first — copy this pattern when handling drift. Doesn't parse model, doesn't parse tools. |
| **Cedriccmh / cursor-trace-exporter** | https://github.com/Cedriccmh/cursor-trace-exporter | 7 | 2026-05-23 | Everything in 83 columns: `toolFormerData`, `toolResults`, `supportedTools`, `capabilities*`, `thinking`, `allThinkingBlocks`, `thinkingDurationMs`, `unifiedMode`, `modelInfo` (raw blob) | Most exhaustive field dump in the field, but no semantic parsing. Useful as a "is this field still there?" reference dump. |
| **lucifer1004 / cursor-helper** | https://github.com/lucifer1004/cursor-helper | 19 | 2026-05-23 | Rewrites `composer.composerData` + `folderUri` mappings | Reference for project-move / rename-detection, not for export. |
| **somogyijanos / cursor-chat-export** | (archived 2025-06-17) | — | 2026-05-23 | Legacy `tabs/bubbles` only | Pre-`cursorDiskKV` format. Not useful for current Cursor. |

**Gaps where ocf-py is unique:**
- Model detection via `modelConfig.modelName` (nobody else does this).
- `viewPane / selectedComposerIds` workspace heuristic (commit `3eb84df`).
- Structured `toolFormerData` parsing into tool-call / tool-result pairs.
- Explicit empty-composer skip (`SkipExport`), keeping the archive clean.

## claude_code

Adapter: [`claude_code.py`](claude_code.py).

No third-party reference exports identified — Claude Code's session
storage is well-documented by Anthropic and the format is more stable
than Cursor's. The internal "Local Agent Mode" sub-agent dirs under
`%APPDATA%/Claude/local-agent-mode-sessions/` are less documented;
worth re-checking on each Claude Code update whether new sub-agent
flavors appear with new filename patterns.

## codex

Adapter: [`codex.py`](codex.py).

Reads OpenAI Responses-API event JSONL. Format is documented by the
Codex CLI repo:

| Project | URL | Notes |
| --- | --- | --- |
| **openai / codex** | https://github.com/openai/codex | Source of truth for the event schema. Check release notes when a new originator or event type appears. |

## How to update this file

When you re-survey:

1. Re-run `gh search repos` for "cursor chat export", "cursor history",
   etc. — see if anything new with traction appeared.
2. For each row above, glance at the repo: any commit on the storage
   layer in the last ~3 months? Any open issue describing a Cursor
   schema break we haven't seen yet?
3. Update the **Last reviewed** column with today's date.
4. If a row's tool went stale (no commit in 12 months, no
   issue activity), mark it `(stale 2026-XX)` so future-you knows.

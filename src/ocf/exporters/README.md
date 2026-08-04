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

## Upstream projects to watch

The tools themselves — check these first when a format change is
suspected. Release notes / commit log will name the format-affecting
changes before we notice them in the wild.

| Tool | Upstream | Releases / Changelog | Storage docs |
| --- | --- | --- | --- |
| **Claude Code** | https://github.com/anthropics/claude-code | [Releases](https://github.com/anthropics/claude-code/releases), [CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) | [code.claude.com/docs/en/data-usage](https://code.claude.com/docs/en/data-usage) |
| **Codex CLI** | https://github.com/openai/codex | [Releases](https://github.com/openai/codex/releases) | Read the repo's `codex-rs/core/src/rollout/` for the current event schema |
| **Cursor IDE** | closed-source | [Changelog](https://cursor.com/changelog) — announces UI features but rarely storage-schema changes; expect to learn about DB changes from the reverse-engineering tools below |
| **Anthropic SDK** (usage schema source) | https://github.com/anthropics/anthropic-sdk-python | [Releases](https://github.com/anthropics/anthropic-sdk-python/releases) — usage-block field additions land here first (e.g. `cache_read_input_tokens`) |

## cursor

Adapter: [`cursor.py`](cursor.py). Storage map: [`docs/cursor-storage.md`](../../../docs/cursor-storage.md).

| Project | URL | Stars | Last reviewed | What it reads | Notable |
| --- | --- | ---:| --- | --- | --- |
| **thomas-pedersen / cursor-chat-browser** | https://github.com/thomas-pedersen/cursor-chat-browser ([issues](https://github.com/thomas-pedersen/cursor-chat-browser/issues), [commits](https://github.com/thomas-pedersen/cursor-chat-browser/commits/main)) | 514 | 2026-05-23 | `ItemTable.composer.composerData`, global `composerData:*`, `bubbleId:*`, `messageRequestContext:*` | Best-in-class workspace mapping via `projectLayouts[].rootPath`. No model detection. Multi-root `.code-workspace` unhandled. Issue [#18](https://github.com/thomas-pedersen/cursor-chat-browser/issues/18) — Cursor v44.9 globalStorage shift; [#27](https://github.com/thomas-pedersen/cursor-chat-browser/issues/27) — 0.48.6 workspaceStorage break. |
| **saharmor / cursor-view** | https://github.com/saharmor/cursor-view ([issues](https://github.com/saharmor/cursor-view/issues), [commits](https://github.com/saharmor/cursor-view/commits/main)) | 126 | 2026-05-23 | `bubbleId:*`, per-workspace `history.entries[*].editor.resource` | Naive common-prefix of attached file paths for workspace. Drops model. Drops `toolFormerData`. |
| **S2thend / cursor-history** + **cursor-history-mcp** | https://github.com/S2thend/cursor-history ([issues](https://github.com/S2thend/cursor-history/issues), [commits](https://github.com/S2thend/cursor-history/commits/main)) | 73 + 31 | 2026-05-23 | `composer.composerData`, `workbench.panel.aichat.view.aichat.chatdata`, **`workbench.panel.chat.view.chat.chatdata`** (post v44.9 key — others miss), `aiService.prompts`, `aiService.generations`, `bubbleId:{composerId}:%` | Probes `sqlite_master` for table existence first — copy this pattern when handling drift. Doesn't parse model, doesn't parse tools. |
| **Cedriccmh / cursor-trace-exporter** | https://github.com/Cedriccmh/cursor-trace-exporter ([issues](https://github.com/Cedriccmh/cursor-trace-exporter/issues), [commits](https://github.com/Cedriccmh/cursor-trace-exporter/commits/main)) | 7 | 2026-05-23 | Everything in 83 columns: `toolFormerData`, `toolResults`, `supportedTools`, `capabilities*`, `thinking`, `allThinkingBlocks`, `thinkingDurationMs`, `unifiedMode`, `modelInfo` (raw blob) | Most exhaustive field dump in the field, but no semantic parsing. Useful as a "is this field still there?" reference dump. |
| **lucifer1004 / cursor-helper** | https://github.com/lucifer1004/cursor-helper ([issues](https://github.com/lucifer1004/cursor-helper/issues), [commits](https://github.com/lucifer1004/cursor-helper/commits/main)) | 19 | 2026-05-23 | Rewrites `composer.composerData` + `folderUri` mappings | Reference for project-move / rename-detection, not for export. |
| **somogyijanos / cursor-chat-export** | https://github.com/somogyijanos/cursor-chat-export (archived 2025-06-17) | — | 2026-05-23 | Legacy `tabs/bubbles` only | Pre-`cursorDiskKV` format. Not useful for current Cursor. |

**Gaps where ocf-py is unique:**
- Model detection via `modelConfig.modelName` (nobody else does this).
- `viewPane / selectedComposerIds` workspace heuristic (commit `3eb84df`).
- Structured `toolFormerData` parsing into tool-call / tool-result pairs.
- Explicit empty-composer skip (`SkipExport`), keeping the archive clean.
- Per-bubble `modelInfo.modelName` fallback + `messageRequestContext.projectLayouts.absPath` fallback (commit `af797d2`).

## claude_code

Adapter: [`claude_code.py`](claude_code.py).

No mature third-party reference exports identified — the JSONL format
under `~/.claude/projects/` is well-documented by Anthropic in
[code.claude.com/docs/en/data-usage](https://code.claude.com/docs/en/data-usage)
and the format is more stable than Cursor's.

The **Desktop App** paths are less documented:

- `%APPDATA%/Claude/claude-code-sessions/<acc>/<org>/local_<sid>.json`
  — sidecar metadata for regular sessions
- `%APPDATA%/Claude/local-agent-mode-sessions/<acc>/<org>/<workTreeId>/`
  — Cowork background-agent worktrees, each with its own `audit.jsonl`
  (HMAC-signed duplicate — see `39d7622` commit for handling)
- `%APPDATA%/Claude/local-agent-mode-sessions/<acc>/<org>/local_<workTreeId>.json`
  — Cowork sidecar metadata

Known upstream bugs affecting export completeness:
- [anthropics/claude-code#53717](https://github.com/anthropics/claude-code/issues/53717)
  — Windows: Sessions in sidebar but message content missing (Electron
  cache never flushed to disk). Handled by the `lost::<uuid>` synthetic
  source pattern (commit `152fdf4`).
- [#41591](https://github.com/anthropics/claude-code/issues/41591)
  — Auto-update deletes `.jsonl` files.
- [#59736](https://github.com/anthropics/claude-code/issues/59736)
  — Desktop UI loses sessions while JSONLs remain on disk.

Worth re-checking on each Claude Code release whether new sub-agent
filename patterns appear (currently: UUID-stem, `agent-*.jsonl`,
`agent-acompact-*.jsonl`, and the excluded `audit.jsonl`).

## codex

Adapter: [`codex.py`](codex.py).

Reads OpenAI Responses-API event JSONL. Format is documented by the
Codex CLI repo itself.

| Project | URL | Notes |
| --- | --- | --- |
| **openai / codex** | https://github.com/openai/codex ([releases](https://github.com/openai/codex/releases), [commits](https://github.com/openai/codex/commits/main)) | Source of truth for the event schema. Check release notes when a new originator or event type appears. Rollout code lives under `codex-rs/core/src/rollout/`. |

## How to update this file

Recurring format-check (do this maybe once a month, or after a Cursor /
Claude Code update lands on your machine):

1. **Upstream releases first.** Skim the release notes for the three
   upstream repos in the top table — anthropics/claude-code,
   openai/codex, cursor.com/changelog. That's where format-affecting
   changes are usually announced (or omitted, in which case the
   reverse-engineering tools below flag them).
2. **Reverse-engineering tools.** For each row in the cursor table:
   glance at the linked commits / issues pages. Any commit on the
   storage layer in the last ~3 months? Any open issue describing a
   Cursor schema break we haven't seen yet?
3. **Re-run repo search** for `cursor chat export`, `cursor history`,
   `cursor-mcp` on GitHub to see if anything new with traction appeared.
4. **Update timestamps.** Set the **Last reviewed** column to today's
   date on any row you actually re-read.
5. **Prune stale entries.** If a row's tool went stale (no commit in
   12 months, no issue activity), mark it `(stale 2026-XX)` so
   future-you knows.

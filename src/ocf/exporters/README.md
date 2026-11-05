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
| **Codex CLI** | https://github.com/openai/codex | [Releases](https://github.com/openai/codex/releases) | Read the repo's `codex-rs/rollout/src/` (incl. `compression.rs`, `rollout_file_name.rs`) and `codex-rs/history/src/` (RolloutItem wire types) |
| **Cursor IDE** | closed-source | [Changelog](https://cursor.com/changelog) — announces UI features but rarely storage-schema changes; expect to learn about DB changes from the reverse-engineering tools below |
| **ChatGPT** | closed-source (the 2026 desktop app is the renamed Codex app) | [Release notes](https://help.openai.com/en/articles/6825453-chatgpt-release-notes) | No local history; data only via the official export or the web client's `/backend-api` |
| **Anthropic SDK** (usage schema source) | https://github.com/anthropics/anthropic-sdk-python | [Releases](https://github.com/anthropics/anthropic-sdk-python/releases) — usage-block field additions land here first (e.g. `cache_read_input_tokens`) |

## cursor

Adapter: [`cursor.py`](cursor.py). Storage map: [`docs/cursor-storage.md`](../../../docs/cursor-storage.md).

| Project | URL | Stars | Last reviewed | What it reads | Notable |
| --- | --- | ---:| --- | --- | --- |
| **thomas-pedersen / cursor-chat-browser** | https://github.com/thomas-pedersen/cursor-chat-browser ([issues](https://github.com/thomas-pedersen/cursor-chat-browser/issues), [commits](https://github.com/thomas-pedersen/cursor-chat-browser/commits/main)) | 516 | 2026-10-23 | `ItemTable.composer.composerData`, global `composerData:*`, `bubbleId:*`, `messageRequestContext:*` | Best-in-class workspace mapping via `projectLayouts[].rootPath`. No model detection. Multi-root `.code-workspace` unhandled. Issue [#18](https://github.com/thomas-pedersen/cursor-chat-browser/issues/18) - Cursor v44.9 globalStorage shift; [#27](https://github.com/thomas-pedersen/cursor-chat-browser/issues/27) - 0.48.6 workspaceStorage break. Since 2026-05 only [PR #40](https://github.com/thomas-pedersen/cursor-chat-browser/pull/40) (null check); last commit 2026-08-23. |
| **saharmor / cursor-view** | https://github.com/saharmor/cursor-view ([issues](https://github.com/saharmor/cursor-view/issues), [commits](https://github.com/saharmor/cursor-view/commits/main)) | 134 | 2026-10-23 | `bubbleId:*`, per-workspace `history.entries[*].editor.resource` | Naive common-prefix of attached file paths for workspace. Drops model. Drops `toolFormerData`. (stale 2026-10: last commit 2025-09-12) |
| **S2thend / cursor-history** + **cursor-history-mcp** | https://github.com/S2thend/cursor-history ([issues](https://github.com/S2thend/cursor-history/issues), [commits](https://github.com/S2thend/cursor-history/commits/main)) | 152 + 33 | 2026-10-23 | `composer.composerData`, `workbench.panel.aichat.view.aichat.chatdata`, **`workbench.panel.chat.view.chat.chatdata`** (post v44.9 key - others miss), `aiService.prompts`, `aiService.generations`, `bubbleId:{composerId}:%` | Probes `sqlite_master` for table existence first - copy this pattern when handling drift. Doesn't parse model, doesn't parse tools. Very active (138 commits since 2026-05): CLI/WSL/ACP "store stack" (`~/.cursor/chats/<md5(cwd)>/<uuid>/store.db`, `~/.cursor/projects/<slug>/agent-transcripts/`, issue [#31](https://github.com/S2thend/cursor-history/issues/31)), recovery of sessions without workspace stamp (v0.16), bounded Composer SQLite readers. |
| **Cedriccmh / cursor-trace-exporter** | https://github.com/Cedriccmh/cursor-trace-exporter ([issues](https://github.com/Cedriccmh/cursor-trace-exporter/issues), [commits](https://github.com/Cedriccmh/cursor-trace-exporter/commits/main)) | 7 | 2026-10-23 | Everything in 83 columns: `toolFormerData`, `toolResults`, `supportedTools`, `capabilities*`, `thinking`, `allThinkingBlocks`, `thinkingDurationMs`, `unifiedMode`, `modelInfo` (raw blob) | Most exhaustive field dump in the field, but no semantic parsing. Useful as a "is this field still there?" reference dump. |
| **lucifer1004 / cursor-helper** | https://github.com/lucifer1004/cursor-helper ([issues](https://github.com/lucifer1004/cursor-helper/issues), [commits](https://github.com/lucifer1004/cursor-helper/commits/main)) | 27 | 2026-10-23 | Rewrites `composer.composerData` + `folderUri` mappings | Reference for project-move / rename-detection, not for export. Issue [#9](https://github.com/lucifer1004/cursor-helper/issues/9) documents `hasMigratedComposerData` (workspace `composer.composerData` now only holds `selectedComposerIds`). |
| **somogyijanos / cursor-chat-export** | https://github.com/somogyijanos/cursor-chat-export (archived 2025-06-17) | 247 | 2026-10-23 | Legacy `tabs/bubbles` only | Pre-`cursorDiskKV` format. Not useful for current Cursor. |

**Multi-provider tools with traction** (found 2026-10-23, not yet
reviewed in depth - check them first when a format change is suspected):
[jazzyalex/agent-sessions](https://github.com/jazzyalex/agent-sessions)
(909 stars, reads Codex `.jsonl.zst`, see commit `3a3bd27`),
[Dicklesworthstone/coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search)
(1190 stars, 11+ providers, tests for compressed Codex rollouts),
[jhlee0409/claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer)
(2277 stars),
[sybil-solutions/ai-data-extraction](https://github.com/sybil-solutions/ai-data-extraction)
(1351 stars),
[mikhailsal/cursor-chronicle](https://github.com/mikhailsal/cursor-chronicle)
(24 stars).

**Cursor 3.x storage notes (census 2026-10-23, Cursor 3.27.6):**
- Composer `_v: 18` no longer stores `fullConversationHeadersOnly`
  inline (empty list). The headers (same shape) moved to chunked keys
  `composerConvHeaders:<cid>:<000000..>`, which the adapter reads -
  they decide order and selection (Cursor leaves empty capability
  bubbles out of them). Bubbles are still under `bubbleId:<cid>:<bid>`.
  With no headers at all, bubbles are ordered by `createdAt` (the SQL
  scan returns insertion order, which drifts on bubble rewrites).
- New SQLite table `composerHeaders` (`composerId, workspaceId,
  createdAt, lastUpdatedAt, isArchived, isSubagent, recency,
  checkpointAt, value, subagentTypeName`) - sidebar list headers,
  flagged by `cursorDiskKV['composer.composerHeaders.migratedToTable']
  = 1`. Not read yet; `workspaceId` / `isSubagent` could improve
  workspace mapping and sub-agent detection.
- `agentKv:blob:<sha256>` (~142k keys) holds content-addressed agent
  messages (e.g. `{"role":"tool","content":[{"type":"tool-result"}]}`);
  composerData carries `conversationState` (encoded) and
  `blobEncryptionKey`. This becomes the content source if Cursor ever
  stops writing `bubbleId:*`. Also new: `composer.content.<sha>`.
- CLI / WSL / ACP sessions use a separate "store stack"
  (`~/.cursor/chats/<md5(cwd)>/<uuid>/store.db`, lossy
  `~/.cursor/projects/<slug>/agent-transcripts/*.jsonl`). On the
  maintainer's machine all agent transcripts also exist as composers,
  so nothing is missed today.

**Gaps where ocf-py is unique:**
- Model detection via `modelConfig.modelName` (nobody else does this).
- `viewPane / selectedComposerIds` workspace heuristic (commit `3eb84df`).
- Structured `toolFormerData` parsing into tool-call / tool-result pairs.
- Explicit empty-composer skip (`SkipExport`), keeping the archive clean.
- Per-bubble `modelInfo.modelName` fallback + `messageRequestContext.projectLayouts.absPath` fallback (commit `af797d2`).

## chatgpt

Adapter: [`chatgpt.py`](chatgpt.py). Input: one raw `/backend-api/conversation/<id>` object per file (what pionxzh/chatgpt-exporter writes as "JSON"), loose or in ZIPs.

| Project | URL | Stars | Last reviewed | What it reads | Notable |
| --- | --- | ---:| --- | --- | --- |
| **pionxzh / chatgpt-exporter** | https://github.com/pionxzh/chatgpt-exporter ([issues](https://github.com/pionxzh/chatgpt-exporter/issues), [commits](https://github.com/pionxzh/chatgpt-exporter/commits/master)) | 2807 | 2026-11-02 | Web client `/backend-api` from the logged-in session (userscript) | Produces our input files. Only exports the visible branch (`current_node` walk). Citation handling in `utils/citations.ts` (`content_references`, longest `matched_text` first) was the model for ours. Single exports are wrapped in a one-element list. |
| **mohamed-chs / convoviz** | https://github.com/mohamed-chs/convoviz ([issues](https://github.com/mohamed-chs/convoviz/issues), [commits](https://github.com/mohamed-chs/convoviz/commits/main)) | 888 | 2026-11-02 | Official export ZIP (`conversations.json` / `conversations-NNN.json`) | Hide rules in `message_logic.py:is_message_hidden`. Issue [#70](https://github.com/mohamed-chs/convoviz/issues/70): the Oct 2026 official export drops `children` and adds stray `{}` nodes; we build the tree from `parent` for that reason. |
| **hoya98 / chatgpt-export** | https://github.com/hoya98/chatgpt-export | 21 | 2026-11-02 | `/backend-api/conversations` paging + per-conversation fetch | Minimal bulk fetcher with backoff; reference for the list/fetch endpoints. |
| **daugaard47 / ChatGPT_Conversations_To_Markdown** | https://github.com/daugaard47/ChatGPT_Conversations_To_Markdown | 180 | 2026-11-02 | Official export | Do not copy: guesses the branch from leaf timestamps and strips private-use characters only, leaving `citeturn0search1` behind. |

Format notes from 1451 real conversations (2023-2026):
- `web.run` tool results have empty `parts`; the hits are in `metadata.search_result_groups`.
- About 700 tool messages have no explicit call (parent is a user, tool or plain assistant message); the adapter synthesizes the call.
- `reasoning_recap` ("Thought for 6s") is a separate message and is dropped.
- Images are `file-service://` / `sediment://` pointers that are not part of the export; only tool-generated images arrive inline as `data:` URIs.

## claude_code

Adapter: [`claude_code.py`](claude_code.py).

The JSONL format under `~/.claude/projects/` is documented by Anthropic in
[code.claude.com/docs/en/data-usage](https://code.claude.com/docs/en/data-usage)
and [claude-directory](https://code.claude.com/docs/en/claude-directory),
and is more stable than Cursor's. Reference implementation worth
watching: [daaain/claude-code-log](https://github.com/daaain/claude-code-log)
(1249 stars, Python, reviewed 2026-10-23) - handles Workflow invocation
shapes, teammate sub-agents, the forks/workflows DAG, and has a Codex
provider with fork-prefix fan-out.

The **Desktop App** paths are less documented. `<data>` is
`%LOCALAPPDATA%/Claude-Data/claude_<hash>/` since the app's data
migration on 2026-10-20 (both old locations, `%APPDATA%/Claude/` and
the MSIX container's `LocalCache/Roaming/Claude/`, keep only an empty
`.user-data-left.claude_<hash>` marker). `utils/paths.py` resolves to
whichever candidate holds session data.

- `<data>/claude-code-sessions/<acc>/<org>/local_<sid>.json`
  - sidecar metadata for regular sessions
- `<data>/local-agent-mode-sessions/<acc>/<org>/<workTreeId>/`
  - Cowork background-agent worktrees, each with its own `audit.jsonl`
  (HMAC-signed duplicate - see `39d7622` commit for handling)
- `<data>/local-agent-mode-sessions/<acc>/<org>/local_<workTreeId>.json`
  - Cowork sidecar metadata

JSONL event types seen as of 2.1.30x (census 2026-10-23). Converted:
`user`, `assistant`, `tool_result`, `ai-title`, `custom-title` (user
rename - wins over every other title source), `system` (metadata).
User `document` blocks (attached PDFs / text files) become OCF `file`
blocks. Deliberately ignored as telemetry / UI state: `attachment`,
`queue-operation`, `last-prompt`, `mode`, `permission-mode`,
`atis-latch`, `agent-name`, `cost-state`, `file-history-snapshot`,
`file-history-delta`, `bridge-session`, `relocated`, `frame-link`,
`teleported-from`, `summary` (legacy), `tools-used` (tool name list),
`artifact-comment-monitor` and `artifact-autoreact-ledger` (artifact
UI state; all three new in 2.1.31x, census 2026-11-05), and the
request-logging trio
`api-request`, `api-request-shape`, `api-request-blob` (these carry
`message` payloads with system prompts - never treat them as turns).
Sessions that yield no messages at all (e.g. summary-only stub files:
a single `{"type": "summary"}` line) raise `SkipExport`, as do empty
Codex rollouts; 1310 such stubs from April/May 2026 still sit in the
G: archive with an export-time `created_at`.

Open points (2026-10-23):
- Desktop sidecars gained `priorCliSessionIds` (earlier JSONLs of the
  same Desktop session; the newer JSONL repeats their messages with the
  same uuids). The metadata index keys only on `cliSessionId`, so those
  earlier files are classified as CLI and overlap in content. Also new:
  `forkedFromSessionId`, `bridgeSessionIds`, `titleTurn`; some sidecars
  have no `cliSessionId`.
- `relocated {relocatedCwd}` (`/cd`) is ignored; the project stays the
  first cwd.
- Set-aside transcripts `<session>.orphaned-<ts>-<suffix>.jsonl` match
  `*.jsonl` and would export as separate sessions (none seen yet).
- 2.1.299 stopped keeping PDF bytes in transcripts; 2.1.287 spills large
  MCP results into `<session>/tool-results/` (preview only inline).

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
`agent-acompact-*.jsonl`, and the excluded `audit.jsonl` and
`journal.jsonl` - the latter is the Workflow tool's event journal
under `<session-uuid>/subagents/workflows/wf_*/`, seen 2026-09).
The same workflow dirs hold real `agent-*.jsonl` transcripts plus
`.meta.json` sidecars (not yet read).

## codex

Adapter: [`codex.py`](codex.py).

Reads OpenAI Responses-API event JSONL. Format is documented by the
Codex CLI repo itself.

Storage changes seen with Codex Desktop's bundled 0.163 (2026-10; the
`codex` CLI on PATH can lag far behind the Desktop app's version):

- **zstd compression.** Finished rollouts are rewritten in place as
  `rollout-*.jsonl.zst`; the active one stays plain until it is done.
  The adapter discovers both (plain wins if both exist) and maps them
  to the same OCF filename / `original_id`.
- **Paginated history** (`session_meta.history_mode: "paginated"`).
  Past ~50 MB a thread continues in a new file
  `rollout-<ts>-<thread>_<page>.jsonl` whose `session_meta` repeats the
  thread id and carries `history_base: {thread_id,
  end_ordinal_exclusive, end_byte_offset}`. Each page is exported as its
  own OCF document; `history_base` is kept in `meta.codex` so pages can
  be linked. Merging pages into one conversation is open.
- Archived threads move to `~/.codex/archived_sessions/` (also scanned).
- Paginated mode no longer writes the old `event_msg` content types
  (`user_message`, `agent_message`, ...) or `thread_name_updated`;
  `response_item` lines still carry the conversation. Not converted
  yet: `response_item:agent_message` (inter-agent messages with
  `author`/`recipient`; none on the maintainer's machine so far).
- New ignored event types: `world_state`, `token_usage_record`,
  `compacted` (its `replacement_history` duplicates earlier items),
  `response_item:additional_tools`, and since 0.165 also
  `response_item:compaction` (only `encrypted_content`, unreadable).
  Titles still come from
  `~/.codex/session_index.jsonl`; `state_5.sqlite` (`threads` table)
  holds the same titles plus `rollout_path`.

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

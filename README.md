# ocf-py

Reference Python implementation of the [Open Conversation Format (OCF)](https://github.com/open-conversation-format/spec).

**Status: pre-alpha (0.1.0.dev0).** Not on PyPI yet. Built locally and validated against real-world session data from Codex CLI, Claude Code (CLI + Desktop), Cursor IDE, ChatGPT exports and claude.ai exports before going public.

## What it does

`ocf-py` ships four things you can run today:

1. **Source adapters** that read each tool's session storage and convert it to a wire-strict OCF document - `chatgpt`, `claude-ai`, `codex`, `claude-code-cli`, `claude-code-app`, `claude-cowork-app`, `cursor`.
2. **A renderer layer** that turns OCF documents into human-readable Markdown (with YAML front matter for Obsidian / Meilisearch indexing).
3. **A Meilisearch indexer** that makes the archive full-text searchable.
4. **An `ocf` CLI** that wires everything into a cron-friendly pipeline. All tools export into one flat archive directory:

   ```bash
   ocf export claude-code-cli   --out ./ocf-archive
   ocf export claude-code-app   --out ./ocf-archive
   ocf export claude-cowork-app --out ./ocf-archive
   ocf export codex             --out ./ocf-archive
   ocf export cursor            --out ./ocf-archive
   ocf export chatgpt           --source-dir ~/Downloads --out ./ocf-archive

   ocf render ./ocf-archive --out ./ocf-archive-md
   ocf index --url http://localhost:7700 --from-archive ./ocf-archive
   ```

Export and render keep a manifest in the output directory, so re-running only re-processes inputs whose content actually changed (mtime + size, then sha256).

## Why each adapter exists

| Tool | Source | Notes |
|---|---|---|
| Codex | `~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl` (zstd-compressed `*.jsonl.zst` after 7 idle days) plus `~/.codex/archived_sessions/` | OpenAI Responses-API events. Originator covers Codex CLI, Desktop, and IDE extensions. Long threads are paginated across several rollout files. |
| Claude Code | `~/.claude/projects/` plus the Desktop app's `local-agent-mode-sessions/` (Cowork agents; under `%LOCALAPPDATA%/Claude-Data/claude_<hash>/` since 2026-10) | Anthropic Messages format with `parentUuid` branching, plus a metadata index for Desktop App titles. |
| Cursor | `%APPDATA%/Cursor/User/globalStorage/state.vscdb` (SQLite, ~3 GB) | Read-only via `mode=ro` URI, per-composer fingerprinting, `toolFormerData` extraction (~71% of bubbles carry tool calls). |
| Claude.ai | The official data export (Settings > Privacy > Export data): `conversations-NNN.zip` with `conversations.json`, read in place; one folder per export | Chats of claude.ai and the Desktop app's "Claude" mode are not stored locally. Full message tree with branches, server-side tool calls and results (web search, MCP), thinking summaries, attachments. |
| ChatGPT | Export files you downloaded: one `ChatGPT-<title>.json` per conversation, loose or inside ZIPs (pionxzh/chatgpt-exporter "JSON" format, the raw `/backend-api/conversation` response) | ChatGPT keeps no local history, so there is no default location. Full message tree with branches, `recipient`-based tool calls, thinking, citations resolved to links. |

Each adapter handles its format's edge cases — NULL bubble values, zombie composer rows, filename collisions across Desktop sub-agent dirs, heartbeat / health-check sessions — and skips them via `SkipExport` so the archive stays clean.

### ChatGPT exports

ChatGPT only stores history on OpenAI's servers, so the `chatgpt` adapter reads export files instead of a local store. Point it at the folder with your downloads, either per call or once via environment variable (then `ocf list`, `ocf export` and `ocf index` pick it up without `--source-dir`):

```bash
ocf export chatgpt --source-dir ~/Downloads --out ./ocf-archive
export OCF_CHATGPT_EXPORT_DIR=~/Downloads   # several folders: separate with os.pathsep
```

ZIPs are read in place. Do not extract several export ZIPs into one folder: different conversations share filenames (`ChatGPT-New_chat.json`) and would overwrite each other. Output files are named after the conversation id (`chatgpt-<id>.ocf.json`). OpenAI's official multi-conversation `conversations.json` is not supported yet.

### Claude.ai exports

Chats on claude.ai and in the Desktop app's "Claude" mode live on Anthropic's servers only, so the `claude-ai` adapter reads the official data export. Request it in Settings > Privacy > Export data; the email links to a manifest with one ZIP per category, of which only `conversations-NNN.zip` matters (the others hold projects, memories, artifacts and account metadata). The links expire after 24 hours.

Keep every export in its own dated folder under one root and point the adapter at the root:

```bash
ocf export claude-ai --source-dir ~/claude-exports --out ./ocf-archive
export OCF_CLAUDE_AI_EXPORT_DIR=~/claude-exports   # then --source-dir can be omitted
```

Each export is a full snapshot, so the same conversation appears in every one; the adapter exports each conversation once, from the export with its newest `updated_at`. Keep old exports: a chat deleted on claude.ai is missing from later snapshots but stays exportable from the earlier ones. Images and documents are not part of the export and become placeholders; attachment text (`extracted_content`) is kept as OCF `file` blocks. Output files are named `claude-ai-<uuid>.ocf.json`. Requesting the export is a manual click: Anthropic's Consumer Terms do not allow automated access to claude.ai.

## CLI reference

```
ocf export <tool> [--out DIR] [--source LOCATOR] [--source-dir DIR]
                  [--force] [--dry-run] [--quiet]

ocf list <tool> [--source-dir DIR] [--query Q]

ocf render <input>... [--out DIR] [--format md]
                      [--query Q] [--platform P] [--project P] [--since DATE]
                      [--force] [--dry-run] [--quiet]

ocf index --url URL [--key KEY] [--index-name NAME]
          --from-archive DIR [--recovered DIR]        # recommended
ocf index --url URL [--tool T] [--watch] [--interval S]   # live sessions

ocf sync [--archive DIR] [--md DIR] [--url URL] [--key KEY] [--index-name NAME]
         [--tools T...] [--interval S] [--quiet-period S] [--max-delay S]
         [--save] [--once | --status]
ocf app  [same settings] [--show] [--autostart on|off]
```

`<tool>` is one of `chatgpt` (downloaded ChatGPT exports), `claude-ai` (claude.ai data exports), `claude-code-cli`, `claude-code-app` (Desktop app sessions), `claude-cowork-app` (Desktop background agents), `codex`, `cursor`.

`--source` is polymorphic: pass a UUID, a path to a file/directory, or a fuzzy title query.

`ocf render` filters operate on OCF fields, so they work uniformly across all adapters — `--platform cursor`, `--project DerJarl`, `--since 2026-01-01` — and find sessions even after the source storage rotated away.

`ocf index` pushes rendered sessions into Meilisearch (`pip install ocf-py[search]`). With `--from-archive` it reads the OCF archive instead of this machine's live session storage, so the search index mirrors the archive - complete even when several machines feed one shared archive and the tools have long rotated their own session files away. Empty documents are skipped; `--recovered DIR` additionally pushes the sessions in `*.meili.json` documents saved from an earlier index (unless the archive holds them again).

Each session is split into chunks of at most 4,000 words / 24,000 characters, cut at message boundaries, one index document per chunk: Meilisearch only indexes the first 65,535 word positions of a field, which would hide the late part of long sessions. Every chunk carries `doc` (the archive filename stem), `chunk` / `chunks` (position), `msg_from` / `msg_to` (message range in the OCF document), `chunk_from` / `chunk_to` (time range), `updated_at` (last activity of the session) and the size of the whole session (`session_messages`, `session_words`, `session_chars`, `archive_bytes`), so a reader can choose between paging through chunks and loading the whole archive file. Search with `"distinct": "doc"` for one hit per session, filter `doc = "<stem>"` to search inside one session, and read neighbours by `chunk`. Chunk ids are `<stem>__c<n>` in both modes, so re-indexing is an idempotent upsert; a session that shrinks leaves its trailing chunks behind until the next rebuild. To rebuild cleanly, index into a fresh `--index-name` and swap it in with Meilisearch's atomic `POST /swap-indexes`.

`ocf sync` does export, render and index continuously, see "Continuous sync" below. `ocf app` runs the same with a tray icon and an activity window (`pip install ocf-py[app]`).

Exit codes are cron-friendly: `0` success, `1` partial failure or no matches, `2` user/environment error (missing source dir, invalid date), `130` SIGINT.

## Running it across several machines

The archive is the source of truth, not the tools' own storage: Claude Code deletes session files after about 30 days, Codex compresses and moves them, and app updates have relocated whole data directories before. Everything that reached the archive stays there.

**One shared archive.** Point every machine at the same archive directory (e.g. a folder on a synced cloud drive) and keep it flat: one `ocf-archive/` for all tools, no per-tool subfolders, one `ocf-archive-md/` next to it. OCF filenames are unique per session, so documents from different machines and tools coexist; the renderer writes all Markdown into one directory, so a session filed twice in different subfolders would overwrite its own Markdown.

**Continuous sync** (recommended). Install the app extra and start the app; on first start it opens its settings (archive, Markdown folder, Meilisearch URL, index and API key):

```bash
uv sync --extra app
```

```bash
ocf app --show
```

"Start at login" in the window (or `ocf app --autostart on`) adds it to the Windows Startup folder. The settings are stored locally (`%LOCALAPPDATA%\ocf\sync-settings.json`, `~/.local/state/ocf/` elsewhere); the API key goes into the system credential store (Windows Credential Manager), never into a file. Without a key the sync still archives and renders, warns, and skips indexing; the skipped sessions are remembered and indexed as soon as a key is set. Without the app, the same setup on the command line:

```bash
ocf sync --archive ARCHIVE --md ARCHIVE-md --url URL --key KEY --save
```

The key is looked up in this order: `--key` for the current run, the credential store, `MEILI_MASTER_KEY`. The app's "Test connection" checks URL and key before saving.

Or headless, in a terminal or as a service: `ocf sync`. For a single catch-up round that exports, renders and indexes everything that changed and then exits: `ocf sync --once`.

How it behaves:

- It scans this machine's session storage every 15 s (`--interval`). Cursor's database is only scanned when its files changed, and at most every 2 minutes.
- A changed session is written once it has been quiet for 90 s (`--quiet-period`), so one write per pause instead of one per message; a session that never pauses is written at least every 10 minutes (`--max-delay`). "Sync now" in the app, or a new start, writes everything at once.
- Each write exports the session, renders its Markdown and replaces its chunks in the index. Results identical to the archived file are not written again, so the synced folder does not upload them.
- Export and render manifests live in the local state directory, one per machine and archive; the first start copies the shared `.ocf-manifest.json` / `.ocf-render-manifest.json` there, so sessions archived before stay skipped. Several machines can therefore sync into one archive at the same time.
- Each machine keeps `ARCHIVE/.ocf-machines/<machine>.json` up to date: state, last write, the last sessions written, its ocf-py commit and how many commits it is behind. The app shows these under "Machines"; agents reading the archive can see from it how current each machine is.
- If the index is unreachable, written sessions wait in a backlog and are indexed on the next successful attempt (retried every minute). If the archive is unreachable (cloud drive not mounted), nothing is written until it is back.
- Once an hour it fetches the ocf-py remote and reports new commits; after a `git pull` it asks for a restart. It never updates itself.
- Large sessions are uploaded whole by most sync clients on every write; raise `--max-delay` and `--quiet-period` if that is too much traffic.

The activity log and the app's log file are in the local state directory ("Log folder" in the app).

**Manual run**, on any machine without `ocf sync`:

```bash
for t in claude-code-cli claude-code-app claude-cowork-app codex cursor; do ocf export $t --out ARCHIVE; done
```

On the machine that holds the downloaded ChatGPT or claude.ai exports (see "ChatGPT exports" and "Claude.ai exports" above), add:

```bash
ocf export chatgpt --out ARCHIVE
```

```bash
ocf export claude-ai --out ARCHIVE
```

Then render and index:

```bash
ocf render ARCHIVE --out ARCHIVE-md
```

```bash
ocf index --url URL --from-archive ARCHIVE --recovered ARCHIVE-md/recovered
```

Rules that keep the archive and the index consistent:

- **Same version everywhere.** Pull before exporting or indexing. An older ocf-py misses newer storage formats (e.g. compressed Codex rollouts, the Claude Desktop data move of 2026-10) and labels documents differently.
- **Index only from the archive.** The live mode (`ocf index` without `--from-archive`) only sees the sessions currently on that one machine; with several machines it lets the index drift from the archive.
- **One manual exporter at a time.** `ocf export` and `ocf render` rewrite the shared manifests in the archive as a whole, so two machines running them at once can lose each other's manifest entries (the cost is only repeated work). `ocf sync` keeps its manifests locally and is not affected; it runs alongside manual runs and other machines' syncs.
- **Re-export with `--force` after converter fixes.** The manifest only notices changed sources; a fixed adapter does not re-convert unchanged sessions on its own. Render and index pick up the changed documents automatically.
- **Check formats after tool updates.** Unknown event or block types are ignored silently, so a plain export cannot reveal them. Before the first run after a tool update, run `uv run python scripts/format_census.py`: it lists every raw type in this machine's session files that the adapters neither convert nor deliberately ignore (exit code 1 if there is one). See [`src/ocf/exporters/README.md`](src/ocf/exporters/README.md) for the current inventory and upstream sources.

**Exports record their adapter.** `conversation.meta.ocf_py.source_adapter` names the adapter that wrote a document, which becomes the index's `tool` facet. The OCF platform alone cannot tell Claude Code CLI, Desktop and Cowork sessions apart, so documents exported before this field existed get `claude-cowork-app` when the manifest's source path shows a Cowork worktree and the collective `claude-code` otherwise.

**Rebuilding the search index.** Build a fresh index from the archive, compare it with the live one, then swap the two atomically and keep the old one under a backup name:

```bash
ocf index --url URL --index-name ocf-sessions-new --from-archive ARCHIVE --recovered ARCHIVE-md/recovered
```

```bash
curl -X POST "URL/swap-indexes" -H "Authorization: Bearer $MEILI_MASTER_KEY" -H "Content-Type: application/json" -d '[{"indexes": ["ocf-sessions", "ocf-sessions-new"]}]'
```

```bash
curl -X POST "URL/swap-indexes" -H "Authorization: Bearer $MEILI_MASTER_KEY" -H "Content-Type: application/json" -d '[{"indexes": ["ocf-sessions-new", "ocf-sessions-backup-YYYY-MM-DD"], "rename": true}]'
```

Clients keep querying `ocf-sessions` throughout. Delete the backup index once the new one has proven itself.

**Recovered sessions.** `ARCHIVE-md/recovered/` holds sessions that only an older search index still had (source and OCF document gone everywhere): `<session>.md` with front matter for reading, and `<session>.meili.json`, the verbatim index document that `--recovered` pushes back.

**Cleaning up.** Move files out of the archive instead of deleting them: into a sibling directory outside `ARCHIVE` / `ARCHIVE-md` (so render and index ignore it), together with a log of every move, and delete it only once everything checks out.

## Component status

| Component | State |
|---|---|
| `core.canonical` (canonical JSON, sha256_hex) | Done |
| `core.schema` (jsonschema strict + soft validation) | Done |
| `utils.hashing` / `utils.jsonl` / `utils.sqlite_ro` / `utils.paths` | Done |
| `exporters._base` (Strategy ABC, generic `export_all`, `SkipExport`) | Done |
| `exporters.codex` | Done |
| `exporters.claude_code` | Done |
| `exporters.cursor` | Done |
| `exporters.chatgpt` | Done |
| `exporters.claude_ai` | Done |
| `renderers._base` (Strategy ABC, generic `render_all`) | Done |
| `renderers.markdown` | Done |
| `indexers.meilisearch` (live sessions or OCF archive -> Meilisearch) | Done |
| `sync` (continuous export/render/index, per-machine manifests, machine status files) | Done |
| `sync.app` (tray icon + activity window, tkinter/pystray) | Done |
| `cli` (`export`, `list`, `render`, `index`, `watch`, `sync`, `app`) | Done |
| `core.strip_to_wire` (OCF -> Chat Completions wire form) | Planned |
| `core.redaction` | Planned |
| `core.bundle` (multi-doc archives with shared resources) | Planned |
| `importers.*` (OCF -> source format) | Planned |
| `conformance` (round-trip test suite) | Planned |
| `renderers.html` | Planned |

## Local development

```bash
uv sync --extra dev --extra search
uv run pytest
uv run ocf --version
```

The test suite covers all adapters and the renderer end-to-end with synthetic fixtures, and includes opt-in real-machine smoke tests that skip cleanly when the relevant source isn't installed.

## Real-world validation

Used daily on the maintainer's machines to archive ~2300 conversations across the three tools into one shared archive (3.0 GB OCF / 2.8 GB rendered Markdown, as of 2026-10). The skip mechanism keeps incremental runs cheap once the initial sweep is done; a fresh re-render of just the changed sessions completes in seconds.

## License

Apache 2.0 — same as the [OCF specification](https://github.com/open-conversation-format/spec).

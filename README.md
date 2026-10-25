# ocf-py

Reference Python implementation of the [Open Conversation Format (OCF)](https://github.com/open-conversation-format/spec).

**Status: pre-alpha (0.1.0.dev0).** Not on PyPI yet. Built locally and validated against real-world session data from Codex CLI, Claude Code (CLI + Desktop), and Cursor IDE before going public.

## What it does

`ocf-py` ships four things you can run today:

1. **Source adapters** that read each tool's session storage and convert it to a wire-strict OCF document - `codex`, `claude-code-cli`, `claude-code-app`, `claude-cowork-app`, `cursor`.
2. **A renderer layer** that turns OCF documents into human-readable Markdown (with YAML front matter for Obsidian / Meilisearch indexing).
3. **A Meilisearch indexer** that makes the archive full-text searchable.
4. **An `ocf` CLI** that wires everything into a cron-friendly pipeline. All tools export into one flat archive directory:

   ```bash
   ocf export claude-code-cli   --out ./ocf-archive
   ocf export claude-code-app   --out ./ocf-archive
   ocf export claude-cowork-app --out ./ocf-archive
   ocf export codex             --out ./ocf-archive
   ocf export cursor            --out ./ocf-archive

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

Each adapter handles its format's edge cases — NULL bubble values, zombie composer rows, filename collisions across Desktop sub-agent dirs, heartbeat / health-check sessions — and skips them via `SkipExport` so the archive stays clean.

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
```

`<tool>` is one of `claude-code-cli`, `claude-code-app` (Desktop app sessions), `claude-cowork-app` (Desktop background agents), `codex`, `cursor`.

`--source` is polymorphic: pass a UUID, a path to a file/directory, or a fuzzy title query.

`ocf render` filters operate on OCF fields, so they work uniformly across all adapters — `--platform cursor`, `--project DerJarl`, `--since 2026-01-01` — and find sessions even after the source storage rotated away.

`ocf index` pushes rendered sessions into Meilisearch (`pip install ocf-py[search]`). With `--from-archive` it reads the OCF archive instead of this machine's live session storage, so the search index mirrors the archive - complete even when several machines feed one shared archive and the tools have long rotated their own session files away. Empty documents are skipped; `--recovered DIR` additionally pushes `*.meili.json` documents saved from an earlier index verbatim. Document IDs derive from the archive filename in both modes, so re-indexing is an idempotent upsert. To rebuild cleanly, index into a fresh `--index-name` and swap it in with Meilisearch's atomic `POST /swap-indexes`.

Exit codes are cron-friendly: `0` success, `1` partial failure or no matches, `2` user/environment error (missing source dir, invalid date), `130` SIGINT.

## Running it across several machines

The archive is the source of truth, not the tools' own storage: Claude Code deletes session files after about 30 days, Codex compresses and moves them, and app updates have relocated whole data directories before. Everything that reached the archive stays there.

**One shared archive.** Point every machine at the same archive directory (e.g. a folder on a synced cloud drive) and keep it flat: one `ocf-archive/` for all tools, no per-tool subfolders, one `ocf-archive-md/` next to it. OCF filenames are unique per session, so documents from different machines and tools coexist; the renderer writes all Markdown into one directory, so a session filed twice in different subfolders would overwrite its own Markdown.

**Recurring run**, on any machine:

```bash
for t in claude-code-cli claude-code-app claude-cowork-app codex cursor; do ocf export $t --out ARCHIVE; done
```

```bash
ocf render ARCHIVE --out ARCHIVE-md
```

```bash
ocf index --url URL --from-archive ARCHIVE --recovered ARCHIVE-md/recovered
```

Rules that keep the archive and the index consistent:

- **Same version everywhere.** Pull before exporting or indexing. An older ocf-py misses newer storage formats (e.g. compressed Codex rollouts, the Claude Desktop data move of 2026-10) and labels documents differently.
- **Index only from the archive.** The live mode (`ocf index` without `--from-archive`) only sees the sessions currently on that one machine; with several machines it lets the index drift from the archive.
- **One exporter at a time.** The export manifest is rewritten as a whole, so two machines exporting into the same archive at once can lose each other's manifest entries.
- **Re-export with `--force` after converter fixes.** The manifest only notices changed sources; a fixed adapter does not re-convert unchanged sessions on its own. Render and index pick up the changed documents automatically.
- **Check formats after tool updates.** Unknown event or block types are ignored silently, so a plain export cannot reveal them. Before the first run after a tool update, compare the raw types in the session files with what the adapters handle; see [`src/ocf/exporters/README.md`](src/ocf/exporters/README.md) for the current inventory and upstream sources.

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
| `renderers._base` (Strategy ABC, generic `render_all`) | Done |
| `renderers.markdown` | Done |
| `indexers.meilisearch` (live sessions or OCF archive -> Meilisearch) | Done |
| `cli` (`export`, `list`, `render`, `index`, `watch`) | Done |
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

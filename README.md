# ocf-py

Reference Python implementation of the [Open Conversation Format (OCF)](https://github.com/open-conversation-format/spec).

**Status: pre-alpha (0.1.0.dev0).** Not on PyPI yet. Built locally and validated against real-world session data from Codex CLI, Claude Code (CLI + Desktop), and Cursor IDE before going public.

## What it does

`ocf-py` ships three things you can run today:

1. **Source adapters** that read each tool's session storage and convert it to a wire-strict OCF document — `codex`, `claude_code`, `cursor`.
2. **A renderer layer** that turns OCF documents into human-readable Markdown (with YAML front matter for Obsidian / Meilisearch indexing).
3. **An `ocf` CLI** that wires both ends into a cron-friendly pipeline:

   ```bash
   ocf export codex       --out ./ocf-archive/codex
   ocf export claude_code --out ./ocf-archive/claude_code
   ocf export cursor      --out ./ocf-archive/cursor

   ocf render ./ocf-archive --out ./ocf-archive-md
   ```

Both stages keep a manifest in the output directory, so re-running only re-processes inputs whose content actually changed (mtime + size, then sha256).

## Why each adapter exists

| Tool | Source | Notes |
|---|---|---|
| Codex | `~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl` | OpenAI Responses-API events. Originator covers Codex CLI, Desktop, and IDE extensions. |
| Claude Code | `~/.claude/projects/` plus `%APPDATA%/Claude/local-agent-mode-sessions/` (Desktop sub-agents) | Anthropic Messages format with `parentUuid` branching, plus a metadata index for Desktop App titles. |
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
```

`<tool>` is one of `codex`, `claude_code` (alias `claude-code`), `cursor`.

`--source` is polymorphic: pass a UUID, a path to a file/directory, or a fuzzy title query.

`ocf render` filters operate on OCF fields, so they work uniformly across all adapters — `--platform cursor`, `--project DerJarl`, `--since 2026-01-01` — and find sessions even after the source storage rotated away.

Exit codes are cron-friendly: `0` success, `1` partial failure or no matches, `2` user/environment error (missing source dir, invalid date), `130` SIGINT.

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
| `cli` (`export`, `list`, `render`) | Done |
| `core.strip_to_wire` (OCF -> Chat Completions wire form) | Planned |
| `core.redaction` | Planned |
| `core.bundle` (multi-doc archives with shared resources) | Planned |
| `importers.*` (OCF -> source format) | Planned |
| `conformance` (round-trip test suite) | Planned |
| `renderers.html` | Planned |

## Local development

```bash
uv sync
uv run pytest
uv run ocf --version
```

The test suite covers all adapters and the renderer end-to-end with synthetic fixtures, and includes opt-in real-machine smoke tests that skip cleanly when the relevant source isn't installed.

## Real-world validation

Used daily on the maintainer's machine to archive ~1400 conversations across the three tools (193 MB OCF / 150 MB rendered Markdown). The skip mechanism keeps incremental runs cheap once the initial sweep is done; a fresh re-render of just the changed sessions completes in seconds.

## License

Apache 2.0 — same as the [OCF specification](https://github.com/open-conversation-format/spec).

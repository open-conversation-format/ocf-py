"""``ocf`` — command-line interface.

The ``ocf`` console script (declared in ``pyproject.toml``) is a thin
wrapper over the source adapters in :mod:`ocf.exporters`. It exists
so you can drop a one-line cron job somewhere and stop thinking about
incremental archival of your AI sessions::

    ocf export codex        --out ~/ocf-archive
    ocf export claude_code  --out ~/ocf-archive
    ocf export cursor       --out ~/ocf-archive

All three exporters share the manifest, so re-running is fast — only
new or modified sessions get re-converted. Pair it with the ``--source``
locator to grab a single session by UUID, by file/directory path, or by
fuzzy title query::

    ocf export claude_code --out ./out --source "HGF Migration"
    ocf export cursor      --out ./out --source 06984dd8-3c87-44b8-92e5-90237e74eb94

The ``list`` subcommand mirrors discovery without touching the output
side, useful for sanity-checking what a tool can see on this machine
before you commit to a sweep.

Design notes
------------

- **Argparse, no third-party dep.** Adding ``click``/``typer`` for two
  subcommands isn't worth the dependency-tree weight.
- **Exit codes:** 0 on full success, 1 if any source failed conversion
  or no source matched ``--source``, 2 on user/environment errors
  (missing source dir), 130 on Ctrl-C.
- **Exporter modules are interchangeable** because they all expose the
  same module-level shim API (``export_all``, ``resolve_sources``,
  ``discover``). A tool registry maps tool name -> module.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ocf import __version__
from ocf.exporters import claude_code as _cc
from ocf.exporters import codex as _codex
from ocf.exporters import cursor as _cursor
from ocf.exporters._base import AmbiguousMatchError
from ocf.renderers import RENDERERS, render_all, select_ocf_files

# Tool dispatch table. Aliases (``claude-code``) point at the same
# module object so argparse accepts either spelling. Sorted display in
# ``--help`` is achieved by passing ``choices`` to argparse separately.
_TOOLS: dict[str, ModuleType] = {
    "codex": _codex,
    "claude_code": _cc,
    "claude-code": _cc,
    "cursor": _cursor,
}


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "export":
            return _cmd_export(args)
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "render":
            return _cmd_render(args)
    except FileNotFoundError as exc:
        # Missing source directory is a user/environment problem, not a bug.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AmbiguousMatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for cand in exc.candidates:
            print(f"  {cand}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    parser.print_help()
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

_TOOL_CHOICES = sorted(_TOOLS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocf",
        description=(
            "Open Conversation Format toolkit. Convert AI session storage "
            "(Codex, Claude Code, Cursor) into portable OCF documents."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ocf-py {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---- export -------------------------------------------------------
    p_export = sub.add_parser(
        "export",
        help="Convert sessions to OCF documents.",
        description=(
            "Sweep a tool's session storage and write OCF documents to "
            "OUT. Re-runs are incremental: unchanged sessions are skipped "
            "via the manifest. Pass --source to scope to one session by "
            "UUID, path, or fuzzy title."
        ),
    )
    p_export.add_argument(
        "tool",
        choices=_TOOL_CHOICES,
        metavar="TOOL",
        help=f"One of: {', '.join(_TOOL_CHOICES)}",
    )
    p_export.add_argument(
        "--out",
        type=Path,
        default=Path("./ocf-archive"),
        help="Output directory for OCF files (default: ./ocf-archive).",
    )
    p_export.add_argument(
        "--source",
        default=None,
        help=(
            "Optional source locator: file path, directory, UUID, or "
            "fuzzy title query. Without this flag, all sessions are "
            "swept."
        ),
    )
    p_export.add_argument(
        "--source-dir",
        dest="source_dir",
        default=None,
        type=Path,
        help=(
            "Override the tool's default source directory "
            "(advanced; usually unnecessary)."
        ),
    )
    p_export.add_argument(
        "--force",
        action="store_true",
        help="Re-export every session even if the manifest says skip.",
    )
    p_export.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what would happen; do not write any files.",
    )
    p_export.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-file lines; print only the summary.",
    )

    # ---- list ---------------------------------------------------------
    p_list = sub.add_parser(
        "list",
        help="List discovered sessions for a tool.",
        description=(
            "Print the sessions a tool can see in its default source "
            "directory (or the path passed to --source-dir). Useful for "
            "verifying a fresh install before running export."
        ),
    )
    p_list.add_argument(
        "tool",
        choices=_TOOL_CHOICES,
        metavar="TOOL",
        help=f"One of: {', '.join(_TOOL_CHOICES)}",
    )
    p_list.add_argument(
        "--source-dir",
        dest="source_dir",
        default=None,
        type=Path,
        help="Override the tool's default source directory.",
    )
    p_list.add_argument(
        "--query",
        default=None,
        help="Filter to sessions matching this UUID or fuzzy title.",
    )

    # ---- render -------------------------------------------------------
    render_format_choices = sorted(set(RENDERERS))
    p_render = sub.add_parser(
        "render",
        help="Render OCF documents to a human-readable format (Markdown).",
        description=(
            "Convert OCF documents into Markdown (default) or any other "
            "registered renderer format. Selection happens against OCF "
            "fields (title, platform, project, id) — sources are not "
            "touched. The render manifest tracks per-file hashes so "
            "re-runs only re-render changed inputs."
        ),
    )
    p_render.add_argument(
        "input",
        nargs="+",
        type=Path,
        help=(
            "One or more OCF files or directories containing *.ocf.json. "
            "Directories are recursed."
        ),
    )
    p_render.add_argument(
        "--out",
        type=Path,
        default=Path("./ocf-rendered"),
        help="Output directory (default: ./ocf-rendered).",
    )
    p_render.add_argument(
        "--format",
        dest="format",
        choices=render_format_choices,
        default="md",
        help=f"Output format. One of: {', '.join(render_format_choices)}.",
    )
    p_render.add_argument(
        "--query",
        default=None,
        help=(
            "Filter to OCF docs matching this fuzzy title query, UUID, "
            "or source.original_id."
        ),
    )
    p_render.add_argument(
        "--platform",
        default=None,
        help="Filter to docs whose conversation.source.platform == VALUE.",
    )
    p_render.add_argument(
        "--project",
        default=None,
        help="Filter to docs whose conversation.project matches VALUE.",
    )
    p_render.add_argument(
        "--since",
        default=None,
        help=(
            "Filter to docs created on/after this ISO date "
            "(e.g. 2026-01-01 or 2026-04-26T00:00:00Z)."
        ),
    )
    p_render.add_argument(
        "--force",
        action="store_true",
        help="Re-render every input even if the manifest says skip.",
    )
    p_render.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what would happen; do not write any files.",
    )
    p_render.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-file lines; print only the summary.",
    )

    return parser


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_export(args: argparse.Namespace) -> int:
    module = _TOOLS[args.tool]
    source_dir = args.source_dir

    if args.source is None:
        result = module.export_all(
            args.out,
            source_dir=source_dir,
            force=args.force,
            dry_run=args.dry_run,
        )
    else:
        sources = module.resolve_sources(args.source, source_dir=source_dir)
        if not sources:
            print(
                f"no sessions matched: {args.source!r}",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            print(f"resolved {len(sources)} source(s) for {args.source!r}")
        result = module.export_all(
            args.out,
            sources=sources,
            force=args.force,
            dry_run=args.dry_run,
        )

    if not args.quiet:
        for src in result.new:
            print(f"new      {src}")
        for src in result.updated:
            print(f"updated  {src}")
        for src in result.skipped:
            print(f"skipped  {src}")

    if args.dry_run:
        print(f"[dry-run] {result.summary()}")
    else:
        print(result.summary())

    if result.failed:
        for src, msg in result.failed:
            print(f"FAILED   {src}: {msg}", file=sys.stderr)
        return 1
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    module = _TOOLS[args.tool]
    source_dir = args.source_dir

    if args.query is not None:
        sources = module.resolve_sources(args.query, source_dir=source_dir)
    else:
        sources = module.discover(source_dir=source_dir)

    for s in sources:
        print(s)
    print(f"total: {len(sources)}", file=sys.stderr)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from datetime import datetime

    renderer_cls = RENDERERS[args.format]
    renderer = renderer_cls()

    since: datetime | None = None
    if args.since is not None:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print(
                f"error: --since value {args.since!r} is not a valid ISO date",
                file=sys.stderr,
            )
            return 2

    selected = select_ocf_files(
        args.input,
        query=args.query,
        platform=args.platform,
        project=args.project,
        since=since,
    )
    if not selected:
        print("no OCF documents matched the selection", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"selected {len(selected)} OCF document(s) for rendering")

    result = render_all(
        renderer,
        args.out,
        sources=selected,
        force=args.force,
        dry_run=args.dry_run,
    )

    if not args.quiet:
        for src in result.new:
            print(f"new      {src}")
        for src in result.updated:
            print(f"updated  {src}")
        for src in result.skipped:
            print(f"skipped  {src}")

    if args.dry_run:
        print(f"[dry-run] {result.summary()}")
    else:
        print(result.summary())

    if result.failed:
        for src, msg in result.failed:
            print(f"FAILED   {src}: {msg}", file=sys.stderr)
        return 1
    return 0


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

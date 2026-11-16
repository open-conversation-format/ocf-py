"""``ocf sync`` and ``ocf app`` subcommands."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from ocf.sync.config import DEFAULT_TOOLS, SyncConfig, settings_path, state_root


def add_parsers(sub: argparse._SubParsersAction, tool_choices: list[str]) -> None:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("settings (stored with --save)")
    g.add_argument("--archive", type=Path, help="Shared OCF archive directory.")
    g.add_argument("--md", dest="archive_md", type=Path,
                   help="Markdown directory next to the archive (omit: no rendering).")
    g.add_argument("--url", dest="meili_url",
                   help="Meilisearch URL (omit: no indexing). Use --url '' to turn it off.")
    g.add_argument("--key", dest="meili_key",
                   help="Meilisearch API key. With --save it goes into the system credential "
                        "store (Windows Credential Manager), never into the settings file. "
                        "Default: the stored key, else MEILI_MASTER_KEY. Without a key "
                        "indexing is skipped with a warning.")
    g.add_argument("--index-name", dest="index_name", help="Index name (default: ocf-sessions).")
    g.add_argument("--tools", nargs="+", choices=tool_choices, metavar="TOOL",
                   help=f"Tools to sync (default: {' '.join(DEFAULT_TOOLS)}).")
    g.add_argument("--interval", type=float, help="Seconds between scans (default: 15).")
    g.add_argument("--quiet-period", dest="quiet", type=float,
                   help="Write a session after it was unchanged this long (default: 90).")
    g.add_argument("--max-delay", dest="max_delay", type=float,
                   help="Write a busy session at least this often (default: 600).")
    g.add_argument("--save", action="store_true",
                   help=f"Store the given settings in {settings_path()} for later runs.")

    p_sync = sub.add_parser(
        "sync",
        parents=[common],
        help="Keep the archive, Markdown and search index up to date continuously.",
        description=(
            "Watch this machine's session storage and write changed sessions "
            "into the shared archive: export, render Markdown, index - only "
            "what changed, after the session paused for --quiet-period "
            "seconds. Each machine keeps its own manifests locally, so several "
            "machines can sync into one archive at the same time.\n\n"
            "First run:  ocf sync --archive ARCHIVE --md ARCHIVE-md --url URL --save\n"
            "Then:       ocf sync            (or 'ocf app' for the tray app)\n"
            "One round:  ocf sync --once     (catch up and exit)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sync.add_argument("--once", action="store_true",
                        help="Run one catch-up round (write every changed session) and exit.")
    p_sync.add_argument("--status", action="store_true",
                        help="Show settings, machines and recent activity, then exit.")

    p_app = sub.add_parser(
        "app",
        parents=[common],
        help="Run the sync with a tray icon and an activity window.",
        description=(
            "Same engine as 'ocf sync', with a tray icon (status at a glance) "
            "and a window listing every write. Needs the 'app' extra: "
            "uv sync --extra app"
        ),
    )
    p_app.add_argument("--show", action="store_true", help="Open the window at start.")
    p_app.add_argument("--autostart", choices=["on", "off"],
                       help="Start the app at login (Windows Startup folder) and exit.")


def resolve_config(args: argparse.Namespace) -> SyncConfig:
    config = SyncConfig.load()
    for name in ("archive", "archive_md", "meili_url", "meili_key", "index_name",
                 "tools", "interval", "quiet", "max_delay"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config, name, value if value != "" else None)
    if args.save:
        path = config.save()
        print(f"settings saved to {path}")
        if config.meili_key:
            if not config.meili_url:
                print("warning: --key needs --url; key not stored", file=sys.stderr)
            else:
                from ocf.sync import secrets

                try:
                    secrets.set_key(config.meili_url, config.meili_key)
                    print("API key stored in the system credential store")
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: cannot store the API key: {exc}", file=sys.stderr)
    return config


def _check(config: SyncConfig, *, need_archive: bool) -> str | None:
    if config.archive is None:
        return ("no archive configured; run once with "
                "--archive ARCHIVE [--md ARCHIVE-md] [--url URL] --save")
    if need_archive and not config.archive.is_dir():
        return f"archive directory not found: {config.archive}"
    return None


def cmd_sync(args: argparse.Namespace) -> int:
    from ocf.sync.engine import SyncEngine, run_loop
    from ocf.sync.system import InstanceLock

    config = resolve_config(args)
    if args.status:
        return _print_status(config)
    # A continuous run waits for an unreachable archive (cloud drive not
    # mounted yet); a single round needs it now.
    problem = _check(config, need_archive=args.once)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    lock = InstanceLock(state_root() / "sync.lock")
    if not lock.acquire():
        print("error: OCF Sync is already running on this machine (ocf sync or ocf app)",
              file=sys.stderr)
        return 2
    try:
        engine = SyncEngine(config, emit=lambda a: print(a.line(), flush=True))
        if args.once:
            engine.start()
            written = engine.tick(force=True)
            engine.stop()
            print(f"{len(written)} session(s) written")
            return 1 if engine.status.state == "error" else 0

        print(f"syncing into {config.archive} every {config.interval:g}s "
              f"(write after {config.quiet:g}s quiet, at most every {config.max_delay:g}s). "
              "Ctrl+C to stop.", flush=True)
        stop, wake = threading.Event(), threading.Event()

        def _stop(*_: object) -> None:
            stop.set()
            wake.set()

        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _stop)
        run_loop(engine, stop, wake)
        return 0
    finally:
        lock.release()


def cmd_app(args: argparse.Namespace) -> int:
    from ocf.sync import system

    if args.autostart:
        try:
            link = system.set_autostart(args.autostart == "on")
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"autostart {'enabled' if args.autostart == 'on' else 'removed'}: {link}")
        return 0
    # No checks here: without settings the app opens its settings dialog,
    # an unreachable archive is shown in the window and retried.
    config = resolve_config(args)
    try:
        from ocf.sync.app import run_app
    except ImportError as exc:
        print(f"error: the app needs extra packages ({exc}); run: uv sync --extra app",
              file=sys.stderr)
        return 2
    return run_app(config, show=args.show)


def _print_status(config: SyncConfig) -> int:
    from ocf.sync.activity import ActivityLog
    from ocf.sync.system import autostart_enabled, parse_iso_utc, read_machine_statuses

    print(f"settings     {settings_path()}")
    print(f"machine      {config.machine}")
    print(f"archive      {config.archive}")
    print(f"markdown     {config.archive_md or '-'}")
    if config.meili_url:
        key = "API key set" if config.resolved_key() else "NO API KEY - indexing skipped"
        print(f"index        {config.meili_url} {config.index_name} ({key})")
    else:
        print("index        -")
    print(f"tools        {' '.join(config.tools)}")
    print(f"timing       scan every {config.interval:g}s, write after {config.quiet:g}s quiet, "
          f"at most every {config.max_delay:g}s")
    print(f"autostart    {'on' if autostart_enabled() else 'off'}")
    if config.archive is None:
        return 0
    print(f"state dir    {config.state_dir()}")
    print()
    print("machines")
    for m in read_machine_statuses(config.archive):
        seen = parse_iso_utc(m.get("updated_at"))
        age = f"{(time.time() - seen) / 60:.0f} min ago" if seen else "?"
        print(f"  {m.get('machine', '?'):<16} {m.get('state', '?'):<8} status {age}, "
              f"last write {m.get('last_write_at') or '-'}, code {m.get('code') or '?'}")
    print()
    print("recent activity")
    for a in ActivityLog(config.state_dir() / "activity.jsonl").tail(20):
        print("  " + a.line())
    return 0


__all__ = ["add_parsers", "cmd_app", "cmd_sync", "resolve_config"]

"""Move Codex Desktop's copies of other agents' sessions out of the archive.

Codex Desktop can import Claude Code sessions as Codex rollouts (turns
named ``external-import-turn-<n>``). Exports before 2026-11-17 archived
these copies as ``codex`` documents next to the originals. This script
finds them among THIS machine's Codex rollouts (pure imports only; a
thread continued in Codex stays), then:

- without ``--apply``: lists them,
- with ``--apply``: moves the OCF and Markdown files into an attic folder
  outside the archive (with ``MOVES.tsv``), and with ``--url`` also deletes
  their chunks from the search index.

Run it on every machine that runs Codex Desktop; each machine only sees
its own rollouts.

    uv run python scripts/prune_codex_imports.py --archive ARCHIVE --md ARCHIVE-md
    uv run python scripts/prune_codex_imports.py --archive ARCHIVE --md ARCHIVE-md --apply --url URL
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from ocf.exporters.codex import CodexAdapter, import_turns
from ocf.utils.jsonl import iter_jsonl_tailsafe

OCF_SUFFIX = ".ocf.json"


def find_pure_imports(archive: Path) -> list[tuple[str, Path]]:
    """(archive file name, source rollout) for pure imports in the archive."""
    adapter = CodexAdapter()
    found = []
    for src in adapter.discover():
        imported, native = import_turns(list(iter_jsonl_tailsafe(src)))
        if imported and not native:
            name = adapter.ocf_filename_for(src)
            if (archive / name).exists():
                found.append((name, src))
    return sorted(found)


def remove_from_index(url: str, key: str, index_name: str, docs: list[str]) -> int:
    import meilisearch

    from ocf.indexers.meilisearch import wait_for_tasks

    client = meilisearch.Client(url, key or None, timeout=60)
    index = client.index(index_name)
    removed = 0
    for i in range(0, len(docs), 50):
        part = docs[i : i + 50]
        flt = f"doc IN [{', '.join(json.dumps(d) for d in part)}]"
        # get_documents counts exactly; search hit estimates stop at 1000.
        removed += index.get_documents({"filter": flt, "limit": 1, "fields": ["id"]}).total
        task = index.delete_documents(filter=flt)
        failed = wait_for_tasks(client, [task.task_uid])
        if failed:
            raise RuntimeError(failed[task.task_uid])
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--md", type=Path, help="Markdown directory next to the archive.")
    ap.add_argument("--attic", type=Path,
                    help="Where to move the files (default: <archive parent>/ocf-archive-attic/"
                         "<today>-codex-imports).")
    ap.add_argument("--apply", action="store_true", help="Move the files (default: list only).")
    ap.add_argument("--url", help="Meilisearch URL: also delete the documents' chunks.")
    ap.add_argument("--key", help="API key (default: MEILI_MASTER_KEY, else OCF Sync's stored key).")
    ap.add_argument("--index-name", default="ocf-sessions")
    args = ap.parse_args(argv)

    found = find_pure_imports(args.archive)
    print(f"{len(found)} Codex import copies in {args.archive}")
    for name, src in found:
        print(f"  {name}  <-  {src.name}")
    if not found or not args.apply:
        if found:
            print("dry run; add --apply to move them")
        return 0

    today = dt.date.today().isoformat()
    attic = args.attic or args.archive.parent / "ocf-archive-attic" / f"{today}-codex-imports"
    (attic / args.archive.name).mkdir(parents=True, exist_ok=True)
    if args.md:
        (attic / args.md.name).mkdir(parents=True, exist_ok=True)
    log = attic / "MOVES.tsv"
    new_log = not log.exists()
    moved_docs = []
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        if new_log:
            fh.write("source\tdestination\treason\n")
        for name, _src in found:
            stem = name[: -len(OCF_SUFFIX)]
            pairs = [(args.archive / name, attic / args.archive.name / name)]
            if args.md:
                pairs.append((args.md / f"{stem}.md", attic / args.md.name / f"{stem}.md"))
            for a, b in pairs:
                if a.exists():
                    shutil.move(str(a), str(b))
                    fh.write(f"{a}\t{b}\tCodex external agent import (copy of another agent's session)\n")
            moved_docs.append(stem)
    print(f"moved {len(moved_docs)} documents to {attic} (see MOVES.tsv)")

    if args.url:
        key = args.key or os.environ.get("MEILI_MASTER_KEY") or ""
        if not key:
            from ocf.sync import secrets

            key = secrets.get_key(args.url) or ""
        removed = remove_from_index(args.url, key, args.index_name, moved_docs)
        print(f"removed {removed} chunks from {args.index_name}")
    else:
        print("index unchanged; to remove their chunks run:")
        print(f"  uv run ocf index --url URL --remove {' '.join(moved_docs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

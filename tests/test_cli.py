"""Tests for the ``ocf`` console-script CLI.

These tests drive :func:`ocf.cli.main` directly with synthetic argv
lists. We exercise the export/list dispatch end-to-end against the
Codex fixture so the manifest, validation, and atomic-write code paths
all run for real — but no real-machine state is touched.

Each test asserts on **exit code, stdout, stderr, and side effects on
disk** in that order — the CLI's contract for cron consumers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.cli import main

# Conftest discovery: pytest only looks in this file's directory chain
# for conftest.py. The codex fixtures live under ``tests/test_exporters/``
# (a sibling directory), so we import them by name to register them in
# this module's namespace — pytest then sees them as fixtures available
# to tests in this file.
from tests.test_exporters.conftest import (  # noqa: F401
    CODEX_SESSION_ID,
    codex_multi_sessions_dir,
    codex_sessions_dir,
)


# ---------------------------------------------------------------------------
# Top-level / parser plumbing
# ---------------------------------------------------------------------------

def test_version_flag_prints_version_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "ocf-py" in out


def test_no_args_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    # Both subcommands advertised
    assert "export" in out
    assert "list" in out


def test_unknown_tool_rejected_by_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "bogus_tool", "--out", "x"])
    # argparse exits with code 2 on bad choice
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "bogus_tool" in err or "invalid choice" in err.lower()


def test_dash_alias_for_claude_code_accepted() -> None:
    """``claude-code`` and ``claude_code`` should both dispatch."""
    # We don't run a real export — just confirm the parser accepts the
    # alias without raising. The discover/export side will be empty
    # because we point at an empty dir.
    rc = main(
        [
            "list",
            "claude-code",
            "--source-dir",
            str(Path("/nonexistent-claude-dir-xxxx")),
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# export — happy path against the codex fixture
# ---------------------------------------------------------------------------

def test_export_codex_sweep_writes_ocf_file(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
        ]
    )
    assert rc == 0
    # Exactly one OCF document plus the manifest sidecar
    ocf_files = sorted(out.glob("*.ocf.json"))
    assert len(ocf_files) == 1
    # OCF file is well-formed JSON
    doc = json.loads(ocf_files[0].read_text(encoding="utf-8"))
    assert "ocf_version" in doc
    out_text = capsys.readouterr().out
    assert "1 new" in out_text


def test_export_codex_quiet_suppresses_per_file_lines(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
            "--quiet",
        ]
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    # Summary line still printed, but no "new      <path>" prefix
    assert "1 new" in out_text
    assert "new      " not in out_text


def test_export_codex_dry_run_writes_nothing(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
            "--dry-run",
        ]
    )
    assert rc == 0
    # Output dir was created (mkdir is unconditional) but no .ocf.json file
    assert not list(out.glob("*.ocf.json"))
    # Manifest also not persisted on dry-run
    assert not (out / "manifest.json").exists()
    out_text = capsys.readouterr().out
    assert "[dry-run]" in out_text


def test_export_codex_second_run_skips(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
        ]
    )
    capsys.readouterr()  # discard
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
        ]
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "0 new" in out_text
    assert "1 skipped" in out_text


def test_export_codex_force_re_exports(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
        ]
    )
    capsys.readouterr()  # discard
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
            "--force",
        ]
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    # On --force, the second run reports "updated" not "skipped"
    assert "1 updated" in out_text
    assert "0 skipped" in out_text


# ---------------------------------------------------------------------------
# export — --source resolves polymorphically
# ---------------------------------------------------------------------------

def test_export_with_uuid_source_resolves(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
            "--source",
            CODEX_SESSION_ID,
        ]
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "resolved 1 source" in out_text
    assert len(list(out.glob("*.ocf.json"))) == 1


def test_export_with_no_match_returns_one(
    codex_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_sessions_dir),
            "--source",
            "definitely-not-a-real-query-xyzzy",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no sessions matched" in err


def test_export_with_query_string_resolves(
    codex_multi_sessions_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fuzzy title query through resolve_sources -> find_by_name."""
    out = tmp_path / "ocf-out"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(codex_multi_sessions_dir),
            "--source",
            "Roundtrip analysis",
        ]
    )
    assert rc == 0
    assert len(list(out.glob("*.ocf.json"))) == 1


# ---------------------------------------------------------------------------
# export — error paths
# ---------------------------------------------------------------------------

def test_export_missing_source_dir_returns_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "ocf-out"
    missing = tmp_path / "does-not-exist"
    rc = main(
        [
            "export",
            "codex",
            "--out",
            str(out),
            "--source-dir",
            str(missing),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_codex_prints_discovered_files(
    codex_sessions_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "list",
            "codex",
            "--source-dir",
            str(codex_sessions_dir),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    # File path goes to stdout, count to stderr
    assert "rollout-" in captured.out
    assert "total: 1" in captured.err


def test_list_codex_with_query_filters(
    codex_multi_sessions_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "list",
            "codex",
            "--source-dir",
            str(codex_multi_sessions_dir),
            "--query",
            "Roundtrip analysis",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "total: 1" in captured.err


def test_list_codex_empty_dir_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "list",
            "codex",
            "--source-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "total: 0" in captured.err


# ---------------------------------------------------------------------------
# render — CLI plumbing for the renderer layer
# ---------------------------------------------------------------------------

def _write_ocf(path: Path, **conv_overrides) -> None:
    """Helper: write a minimal valid OCF doc with optional overrides."""
    conv = {
        "id": conv_overrides.pop("id", "conv_render_test"),
        "title": conv_overrides.pop("title", "Render Test"),
        "created_at": "2026-04-26T10:00:00Z",
        "default_model": "claude-sonnet-4-5",
        "source": {
            "platform": conv_overrides.pop("platform", "cursor"),
            "original_id": "x:1",
        },
    }
    if "project_name" in conv_overrides:
        conv["project"] = {
            "id": "p1",
            "name": conv_overrides.pop("project_name"),
        }
    doc = {
        "ocf_version": "0.1.0",
        "conversation": conv,
        "messages": [
            {
                "id": "msg_0001",
                "created_at": "2026-04-26T10:00:01Z",
                "message": {"role": "user", "content": "Hi"},
            },
            {
                "id": "msg_0002",
                "created_at": "2026-04-26T10:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello back"}],
                },
            },
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_render_directory_writes_md_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json", id="conv_a", title="Alpha")
    _write_ocf(src_dir / "b.ocf.json", id="conv_b", title="Beta")
    out = tmp_path / "rendered"
    rc = main(["render", str(src_dir), "--out", str(out)])
    assert rc == 0
    md_files = sorted(out.glob("*.md"))
    assert [p.name for p in md_files] == ["a.md", "b.md"]
    text = (out / "a.md").read_text(encoding="utf-8")
    assert "# Alpha" in text


def test_render_filter_platform_narrows_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json", id="conv_a", platform="cursor")
    _write_ocf(src_dir / "b.ocf.json", id="conv_b", platform="claude_code")
    out = tmp_path / "rendered"
    rc = main(
        ["render", str(src_dir), "--out", str(out), "--platform", "cursor"]
    )
    assert rc == 0
    md_files = sorted(out.glob("*.md"))
    assert [p.name for p in md_files] == ["a.md"]


def test_render_filter_query_matches_title(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(
        src_dir / "a.ocf.json",
        id="conv_a",
        title="Workspace rename and session migration",
    )
    _write_ocf(src_dir / "b.ocf.json", id="conv_b", title="Other thing")
    out = tmp_path / "rendered"
    rc = main(
        [
            "render",
            str(src_dir),
            "--out",
            str(out),
            "--query",
            "workspace rename",
        ]
    )
    assert rc == 0
    md_files = sorted(out.glob("*.md"))
    assert [p.name for p in md_files] == ["a.md"]


def test_render_no_match_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json")
    out = tmp_path / "rendered"
    rc = main(
        [
            "render",
            str(src_dir),
            "--out",
            str(out),
            "--query",
            "absolutely-no-match-query-xyzzy",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no OCF documents matched" in err


def test_render_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json")
    out = tmp_path / "rendered"
    rc = main(["render", str(src_dir), "--out", str(out), "--dry-run"])
    assert rc == 0
    assert not list(out.glob("*.md"))
    assert "[dry-run]" in capsys.readouterr().out


def test_render_invalid_format_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json")
    with pytest.raises(SystemExit):
        main(
            [
                "render",
                str(src_dir),
                "--out",
                str(tmp_path / "rendered"),
                "--format",
                "pdf",
            ]
        )


def test_render_invalid_since_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "ocf"
    src_dir.mkdir()
    _write_ocf(src_dir / "a.ocf.json")
    rc = main(
        [
            "render",
            str(src_dir),
            "--out",
            str(tmp_path / "rendered"),
            "--since",
            "not-a-date",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a valid ISO date" in err

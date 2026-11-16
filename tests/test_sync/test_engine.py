"""Tests for ocf.sync - the continuous sync engine (no server, fake clock)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.exporters.claude_code import ClaudeCodeCliAdapter
from ocf.sync.activity import Activity, ActivityLog
from ocf.sync.config import SyncConfig
from ocf.sync.engine import MeiliPusher, SyncEngine
from ocf.sync.system import InstanceLock, read_machine_statuses

SID_A = "aaaaaaaa-2222-3333-4444-555555555555"
SID_B = "bbbbbbbb-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OCF_MACHINE", "testbox")


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Adapter(ClaudeCodeCliAdapter):
    def __init__(self, root: Path, meta: Path) -> None:
        super().__init__(metadata_dir_override=meta)
        self.root = root

    def default_source_dirs(self) -> list[Path]:
        return [self.root]


def _event(role: str, text: str, ts: str) -> dict:
    msg = {"role": role, "content": [{"type": "text", "text": text}]}
    if role == "assistant":
        msg["model"] = "claude-opus-4-7"
    return {"type": role, "timestamp": ts, "cwd": "/home/u/demo", "message": msg}


def _write_session(path: Path, turns: int) -> Path:
    events = []
    for i in range(turns):
        events.append(_event("user", f"Question {i}", f"2026-04-26T10:{i:02d}:05Z"))
        events.append(_event("assistant", f"Answer {i}", f"2026-04-26T10:{i:02d}:10Z"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


class _Setup:
    def __init__(self, tmp_path: Path, *, pusher=None, md: bool = True, **timing) -> None:
        self.src = tmp_path / "projects" / "home-u-demo"
        self.src.mkdir(parents=True)
        (tmp_path / "meta").mkdir()
        self.archive = tmp_path / "archive"
        self.archive.mkdir()
        self.md = tmp_path / "archive-md" if md else None
        self.clock = _Clock()
        self.seen: list[Activity] = []
        self.config = SyncConfig(archive=self.archive, archive_md=self.md, **timing)
        self.engine = SyncEngine(
            self.config,
            adapters={"claude-code-cli": _Adapter(tmp_path / "projects", tmp_path / "meta")},
            pusher=pusher,
            emit=self.seen.append,
            clock=self.clock,
            check_updates=False,
        )

    def session(self, sid: str, turns: int) -> Path:
        return _write_session(self.src / f"{sid}.jsonl", turns)

    def writes(self) -> list[tuple[str, str]]:
        return [(a.kind, a.doc) for a in self.seen if a.kind in ("new", "updated")]

    def tick(self, advance: float = 0.0, **kw) -> list[Activity]:
        self.clock.now += advance
        return self.engine.tick(**kw)


def test_first_round_catches_up_then_waits_for_a_pause(tmp_path: Path) -> None:
    s = _Setup(tmp_path, quiet=90, max_delay=600)
    s.session(SID_A, 1)

    written = s.tick()
    assert [(a.kind, a.doc) for a in written] == [("new", SID_A)]
    assert (s.archive / f"{SID_A}.ocf.json").exists()
    assert (s.md / f"{SID_A}.md").exists()
    assert "2 messages" in written[0].detail and "md " in written[0].detail
    # Manifests stay local; nothing is written into the shared folders.
    assert not (s.archive / ".ocf-manifest.json").exists()
    assert not (s.md / ".ocf-render-manifest.json").exists()
    assert (s.config.state_dir() / ".ocf-manifest.json").exists()

    s.session(SID_A, 2)  # the session goes on
    assert s.tick(10) == []  # change seen here
    assert [p.source.stem for p in s.engine.status.pending] == [SID_A]
    assert s.tick(60) == []  # 60 s quiet - not yet
    written = s.tick(31)  # 91 s quiet
    assert [(a.kind, a.doc) for a in written] == [("updated", SID_A)]
    assert "4 messages" in written[0].detail
    assert s.engine.status.pending == []


def test_busy_session_is_written_after_max_delay(tmp_path: Path) -> None:
    s = _Setup(tmp_path, quiet=90, max_delay=200)
    s.session(SID_A, 1)
    s.tick()
    for turns in range(2, 6):  # changes every 60 s, never quiet for 90 s
        s.session(SID_A, turns)
        s.tick(60)
    assert s.writes() == [("new", SID_A)]  # first change seen at 60 s, now 240 s
    s.session(SID_A, 6)
    s.tick(60)  # 300 s: 240 s after the first change
    assert s.writes() == [("new", SID_A), ("updated", SID_A)]


def test_sync_now_writes_waiting_sessions_at_once(tmp_path: Path) -> None:
    s = _Setup(tmp_path, quiet=90)
    s.session(SID_A, 1)
    s.tick()
    s.session(SID_A, 2)
    s.session(SID_B, 1)
    s.tick(5)
    assert len(s.engine.status.pending) == 2
    written = s.tick(1, force=True)
    assert sorted((a.kind, a.doc) for a in written) == [("new", SID_B), ("updated", SID_A)]


def test_unchanged_content_is_not_rewritten(tmp_path: Path) -> None:
    s = _Setup(tmp_path, quiet=30)
    src = s.session(SID_A, 1)
    s.tick()
    ocf = s.archive / f"{SID_A}.ocf.json"
    before = ocf.stat().st_mtime_ns

    # Same bytes, new mtime: the manifest's hash check skips it.
    content = src.read_bytes()
    src.write_bytes(content)
    s.tick(5)
    assert s.tick(60) == []

    # Lost manifest entry, identical result: the file is left alone.
    (s.config.state_dir() / ".ocf-manifest.json").unlink()
    src.write_bytes(content)
    s.tick(5)
    assert s.tick(60) == []
    assert ocf.stat().st_mtime_ns == before


def test_local_manifest_is_seeded_from_the_archive(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.session(SID_A, 1)
    s.tick()
    local = s.config.state_dir() / ".ocf-manifest.json"
    # Another machine (or an earlier manual run) left the shared manifest.
    (s.archive / ".ocf-manifest.json").write_bytes(local.read_bytes())
    local.unlink()

    fresh = SyncEngine(s.config, adapters=s.engine.adapters, emit=s.seen.append,
                       clock=s.clock, check_updates=False)
    assert fresh.tick() == []
    assert local.exists()
    assert any("seeded" in a.message for a in s.seen)


class _FlakyPusher:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.pushed: list[list[str]] = []

    def push(self, items):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("down")
        self.pushed.append([p.name for p, _, _ in items])
        return {p: 1 for p, _, _ in items}

    def reset(self) -> None:
        pass


def test_index_backlog_is_retried(tmp_path: Path) -> None:
    pusher = _FlakyPusher(fail_times=1)
    s = _Setup(tmp_path, pusher=pusher, quiet=30)
    s.session(SID_A, 1)
    written = s.tick()
    assert "index pending" in written[0].detail
    assert s.engine.status.state == "warning"
    assert s.engine.status.index_backlog == 1

    s.tick(10)  # retry interval not reached
    assert pusher.pushed == []
    s.tick(60)
    assert pusher.pushed == [[f"{SID_A}.ocf.json"]]
    assert s.engine.status.state == "idle"
    assert s.engine.status.index == "ok"
    assert any("1 waiting session(s) indexed" in a.message for a in s.seen)


def test_without_key_index_is_skipped_and_caught_up_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ocf.sync.engine as engine_mod

    monkeypatch.delenv("MEILI_MASTER_KEY", raising=False)
    monkeypatch.setattr("ocf.sync.secrets.get_key", lambda url: None)
    s = _Setup(tmp_path, meili_url="http://meili.test", quiet=30)
    s.session(SID_A, 1)
    written = s.tick()
    assert "not indexed (no API key)" in written[0].detail
    assert s.engine.status.index == "no-key"
    assert s.engine.status.state == "warning"
    assert any("no API key" in a.message for a in s.seen if a.kind == "warning")

    # The backlog survives a restart ...
    again = SyncEngine(s.config, adapters=s.engine.adapters, emit=s.seen.append,
                       clock=s.clock, check_updates=False)
    assert again.status.index == "no-key"
    assert [p.name for p in again._index_backlog] == [f"{SID_A}.ocf.json"]  # noqa: SLF001

    # ... and is indexed once a key is entered.
    pusher = _FlakyPusher(fail_times=0)
    monkeypatch.setattr(engine_mod, "MeiliPusher", lambda url, key, name: pusher)
    again.set_index("http://meili.test", "secret", "ocf-sessions")
    assert again.status.index == "pending"
    s.clock.now += 5
    again.tick()
    assert pusher.pushed == [[f"{SID_A}.ocf.json"]]
    assert again.status.index == "ok"
    assert not (again.state_dir / "index-backlog.json").exists()


def test_machine_status_file(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.session(SID_A, 1)
    s.tick()
    (status,) = read_machine_statuses(s.archive)
    assert status["machine"] == "testbox"
    assert status["state"] == "running"
    assert status["last_write_at"]
    assert status["recent"][0]["doc"] == SID_A
    s.engine.stop()
    assert read_machine_statuses(s.archive)[0]["state"] == "stopped"


def test_unreachable_archive_is_an_error_not_a_crash(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.session(SID_A, 1)
    s.archive.rename(tmp_path / "gone")
    assert s.tick() == []
    assert s.engine.status.state == "error"
    (tmp_path / "gone").rename(s.archive)
    assert [a.doc for a in s.tick(20)] == [SID_A]
    assert s.engine.status.state == "idle"


def test_activity_log_persists_and_trims(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path / "a.jsonl", keep=10)
    for i in range(30):
        log.append(Activity("info", f"line {i}"))
    assert [a.message for a in log.tail(2)] == ["line 28", "line 29"]
    ActivityLog(tmp_path / "a.jsonl", keep=10)  # reopening trims
    assert len((tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()) == 10


def test_settings_roundtrip(tmp_path: Path) -> None:
    config = SyncConfig(archive=tmp_path / "a", archive_md=tmp_path / "m",
                        meili_url="http://x", tools=["codex"], quiet=45)
    path = config.save()
    loaded = SyncConfig.load(path)
    assert (loaded.archive, loaded.archive_md, loaded.meili_url) == (
        tmp_path / "a", tmp_path / "m", "http://x")
    assert (loaded.tools, loaded.quiet, loaded.machine) == (["codex"], 45, "testbox")
    assert "machine" not in json.loads(path.read_text(encoding="utf-8"))


def test_single_instance_lock(tmp_path: Path) -> None:
    first, second = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


class _Task:
    def __init__(self, uid: int) -> None:
        self.task_uid = uid
        self.status = "succeeded"


class _Index:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.filters: list[str] = []

    def add_documents(self, docs):
        self.added.extend(docs)
        return _Task(1)

    def delete_documents(self, *, filter):  # noqa: A002
        self.filters.append(filter)
        return _Task(2)


class _Client:
    def wait_for_task(self, uid, timeout_in_ms):
        return _Task(uid)


def test_pusher_upserts_chunks_and_trims_the_rest(tmp_path: Path) -> None:
    s = _Setup(tmp_path, md=False)
    s.session(SID_A, 2)
    s.tick()
    path = s.archive / f"{SID_A}.ocf.json"
    raw = path.read_bytes()

    pusher = MeiliPusher("http://x", "", "ocf-sessions")
    index = _Index()
    pusher._index, pusher._client = index, _Client()  # noqa: SLF001
    counts = pusher.push([(path, raw, json.loads(raw))])
    assert counts == {path: 1}
    assert index.added[0]["id"] == f"{SID_A}__c1"
    assert index.added[0]["tool"] == "claude-code-cli"
    assert index.filters == [f'(doc = "{SID_A}" AND chunk > 1)']

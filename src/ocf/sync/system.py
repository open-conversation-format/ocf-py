"""Machine-level helpers for ``ocf sync``: single-instance lock, code
version checks, per-machine status files in the archive, autostart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, IO

MACHINES_DIRNAME = ".ocf-machines"

# Keep git (and PowerShell) from flashing a console window when the
# app runs under pythonw.exe.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

class InstanceLock:
    """Exclusive lock on a file, held for the life of the process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# ---------------------------------------------------------------------------
# Code version (all machines should run the same ocf-py)
# ---------------------------------------------------------------------------

def repo_root() -> Path | None:
    """The ocf-py checkout this code runs from, if it is a git checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None


def _git(repo: Path, *args: str, timeout: float = 30.0) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def code_fingerprint() -> str:
    """Hash over the ``ocf`` package's Python sources on disk.

    Changes when a pull or a local edit changed the code, not when only
    the commit changed (committing the running code needs no restart).
    """
    import hashlib

    import ocf

    root = Path(ocf.__file__).resolve().parent
    h = hashlib.sha1()
    for path in sorted(root.rglob("*.py")):
        try:
            h.update(path.relative_to(root).as_posix().encode())
            h.update(path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()


def git_head(repo: Path | None) -> str | None:
    return _git(repo, "rev-parse", "--short", "HEAD") if repo else None


def git_commits_behind(repo: Path | None, *, fetch: bool = True) -> int | None:
    """Commits on the upstream branch that this checkout does not have."""
    if repo is None:
        return None
    if fetch and _git(repo, "fetch", "--quiet", timeout=60.0) is None:
        return None
    count = _git(repo, "rev-list", "--count", "HEAD..@{u}")
    return int(count) if count and count.isdigit() else None


# ---------------------------------------------------------------------------
# Per-machine status files in the archive
# ---------------------------------------------------------------------------

def machines_dir(archive: Path) -> Path:
    return Path(archive) / MACHINES_DIRNAME


def write_machine_status(archive: Path, machine: str, data: dict[str, Any]) -> None:
    """Atomically write ``<archive>/.ocf-machines/<machine>.json``."""
    target_dir = machines_dir(archive)
    target_dir.mkdir(exist_ok=True)
    path = target_dir / f"{machine}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_machine_statuses(archive: Path | None) -> list[dict[str, Any]]:
    if archive is None:
        return []
    out = []
    try:
        files = sorted(machines_dir(archive).glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("machine", path.stem)
            out.append(data)
    return out


def iso_utc(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso_utc(value: str | None) -> float | None:
    if not value:
        return None
    try:
        import calendar

        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Opening files and folders
# ---------------------------------------------------------------------------

def open_path(path: Path | str) -> None:
    """Open a file or folder with the system's default handler."""
    path = str(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Autostart (Windows: shortcut in the user's Startup folder)
# ---------------------------------------------------------------------------

AUTOSTART_NAME = "OCF Sync.lnk"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def gui_python() -> Path:
    """``pythonw.exe`` next to the running interpreter (no console window)."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def set_autostart(enabled: bool) -> Path:
    """Create or remove the Startup-folder shortcut that runs ``ocf app``."""
    if sys.platform != "win32":
        raise OSError("autostart setup is only implemented for Windows; "
                      "add 'ocf app' to your desktop's autostart instead")
    link = startup_dir() / AUTOSTART_NAME
    if not enabled:
        link.unlink(missing_ok=True)
        return link
    workdir = repo_root() or Path.cwd()
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:OCF_LNK);"
        "$s.TargetPath = $env:OCF_TARGET;"
        "$s.Arguments = '-m ocf app';"
        "$s.WorkingDirectory = $env:OCF_WORKDIR;"
        "$s.Description = 'OCF Sync - keeps AI sessions in the shared archive';"
        "$s.Save()"
    )
    env = dict(os.environ, OCF_LNK=str(link), OCF_TARGET=str(gui_python()),
               OCF_WORKDIR=str(workdir))
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, env=env, capture_output=True, creationflags=_NO_WINDOW,
    )
    return link


def autostart_enabled() -> bool:
    return sys.platform == "win32" and (startup_dir() / AUTOSTART_NAME).exists()


__all__ = [
    "InstanceLock",
    "MACHINES_DIRNAME",
    "autostart_enabled",
    "git_commits_behind",
    "git_head",
    "gui_python",
    "iso_utc",
    "machines_dir",
    "open_path",
    "parse_iso_utc",
    "read_machine_statuses",
    "repo_root",
    "set_autostart",
    "write_machine_status",
]

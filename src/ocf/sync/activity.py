"""Activity feed of ``ocf sync``: what was written, when, and what failed."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

KINDS = ("new", "updated", "error", "warning", "info")


@dataclass
class Activity:
    """One line in the activity feed.

    ``kind`` is ``new`` / ``updated`` for a written session, otherwise
    ``error``, ``warning`` or ``info``. For sessions, ``doc`` is the
    archive file stem and ``md_path`` the rendered Markdown, if any.
    """

    kind: str
    message: str
    ts: float = 0.0
    tool: str = ""
    title: str = ""
    doc: str = ""
    detail: str = ""
    md_path: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()

    def to_json(self) -> str:
        data = {k: v for k, v in asdict(self).items() if v not in ("", None)}
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Activity:
        allowed = {"kind", "message", "ts", "tool", "title", "doc", "detail", "md_path"}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def line(self) -> str:
        """Plain-text form for the console."""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        parts = [stamp, f"{self.kind:<8}"]
        if self.tool:
            parts.append(f"{self.tool:<17}")
        parts.append(self.message)
        if self.detail:
            parts.append(f"({self.detail})")
        return "  ".join(parts)


class ActivityLog:
    """Append-only JSON-lines file, trimmed to the newest ``keep`` entries."""

    def __init__(self, path: Path, keep: int = 5000) -> None:
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trim()

    def append(self, activity: Activity) -> None:
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(activity.to_json() + "\n")

    def tail(self, n: int = 500) -> list[Activity]:
        out: list[Activity] = []
        for raw in self._read_lines()[-n:]:
            try:
                out.append(Activity.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def _read_lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _trim(self) -> None:
        lines = self._read_lines()
        if len(lines) <= self.keep * 1.2:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-self.keep:]) + "\n", encoding="utf-8")
        tmp.replace(self.path)


__all__ = ["Activity", "ActivityLog", "KINDS"]

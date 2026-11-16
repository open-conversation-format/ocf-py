"""Continuous sync: export, render and index sessions as they change.

``ocf sync`` keeps this machine's sessions in the shared archive up to
date without manual runs. It polls the tools' session storage, waits
until a session has been quiet for a while (or has been busy for too
long), then exports it, renders its Markdown and pushes its chunks to
the search index - only the sessions that changed.

Every machine keeps its own export and render manifests in a local
state directory, so several machines can sync into one shared archive
at the same time without overwriting each other's bookkeeping. Each
machine also leaves a small status file in ``<archive>/.ocf-machines/``
so the others (and agents reading the archive) can see how current it
is.

``ocf app`` runs the same engine with a tray icon and an activity
window.
"""

from ocf.sync.activity import Activity, ActivityLog
from ocf.sync.config import DEFAULT_TOOLS, SyncConfig
from ocf.sync.engine import EngineStatus, Pending, SyncEngine, run_loop

__all__ = [
    "Activity",
    "ActivityLog",
    "DEFAULT_TOOLS",
    "EngineStatus",
    "Pending",
    "SyncConfig",
    "SyncEngine",
    "run_loop",
]

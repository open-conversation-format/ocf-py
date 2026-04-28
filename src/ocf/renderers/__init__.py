"""Rendering layer: OCF document -> human-readable target format.

Currently provides :class:`MarkdownRenderer` (the format the project
owner uses for ad-hoc inspection and downstream Meilisearch
indexing). HTML and other targets can drop in via the
:class:`Renderer` ABC without touching CLI or runner.
"""

from ocf.renderers._base import Renderer, render_all, select_ocf_files
from ocf.renderers.markdown import MarkdownRenderer

# Format-name -> renderer-class registry. New renderers register here
# so the CLI's ``--format`` flag picks them up without further wiring.
RENDERERS: dict[str, type[Renderer]] = {
    "md": MarkdownRenderer,
    "markdown": MarkdownRenderer,
}

__all__ = [
    "MarkdownRenderer",
    "RENDERERS",
    "Renderer",
    "render_all",
    "select_ocf_files",
]

"""Status icons for the tray and the window, drawn with Pillow."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

COLORS = {
    "idle": (46, 157, 91),
    "busy": (47, 111, 219),
    "starting": (47, 111, 219),
    "paused": (125, 133, 144),
    "warning": (212, 144, 15),
    "error": (214, 69, 69),
}

_SS = 4  # supersampling for smooth edges


@lru_cache(maxsize=32)
def status_icon(state: str, size: int = 64) -> Image.Image:
    big = size * _SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, big - 1, big - 1), fill=COLORS.get(state, COLORS["busy"]) + (255,))
    w = round(big * 0.1)
    white = (255, 255, 255, 255)

    def p(x: float, y: float) -> tuple[int, int]:
        return round(x * big), round(y * big)

    if state == "idle":
        d.line([p(0.28, 0.52), p(0.44, 0.68), p(0.73, 0.35)], fill=white, width=w, joint="curve")
    elif state == "paused":
        d.rectangle((*p(0.33, 0.28), *p(0.44, 0.72)), fill=white)
        d.rectangle((*p(0.56, 0.28), *p(0.67, 0.72)), fill=white)
    elif state in ("warning", "error"):
        d.rectangle((*p(0.445, 0.2), *p(0.555, 0.58)), fill=white)
        d.ellipse((*p(0.43, 0.67), *p(0.57, 0.81)), fill=white)
    else:  # busy / starting: circular arrow
        d.arc((*p(0.24, 0.24), *p(0.76, 0.76)), start=40, end=330, fill=white, width=w)
        d.polygon([p(0.60, 0.20), p(0.83, 0.30), p(0.66, 0.46)], fill=white)
    return img.resize((size, size), Image.LANCZOS)


__all__ = ["COLORS", "status_icon"]

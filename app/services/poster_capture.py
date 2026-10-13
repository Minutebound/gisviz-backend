"""
app/services/poster_capture.py
==============================
The post's image (feed cards, thumbnails, share previews) can be a screenshot the publisher drops or pastes in
the editor (frontend ThumbnailDrop), sent to PUT /posts/<id>/image. Here it is only framed: the picture is
placed in the centre 80% of a 1080 x 1080 PNG, on a background a shade darker than the picture's own edge.

A chosen image stays until the publisher drops another one. Posts without one get the image the server draws
(visual_image.render_poster), redrawn after edits, live refreshes and admin re-renders.
"""
from __future__ import annotations

import io
import os
import uuid

SIZE = 1080                         # the PNG is SIZE x SIZE
POSTER_FRAC = 0.80                  # the poster fits in the centre 80%
MAX_UPLOAD = 15 * 1024 * 1024       # bytes
UPLOADED_PREFIX = "post_up_"        # file name of an image the publisher chose: the server never replaces it


def is_uploaded(path) -> bool:
    """The post's image is one the publisher chose (kept by edits, live refreshes and admin re-renders)."""
    return bool(path) and f"/{UPLOADED_PREFIX}" in str(path)


class BadImage(ValueError):
    pass


def _hex(c: str, default=(244, 242, 236)) -> tuple:
    c = (c or "").strip()
    if len(c) == 7 and c.startswith("#"):
        try:
            return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return default


def compose(poster_png: bytes, bg: str | None, out_dir: str) -> str:
    """The poster picture scaled into the centre 80% of a SIZE x SIZE PNG. Returns the DB path (/uploads/...)."""
    from PIL import Image, UnidentifiedImageError
    if not poster_png or len(poster_png) > MAX_UPLOAD:
        raise BadImage("The image is empty or too large")
    try:
        poster = Image.open(io.BytesIO(poster_png))
        poster.load()
    except (UnidentifiedImageError, OSError) as e:
        raise BadImage("Not an image") from e
    if poster.width < 100 or poster.height < 100 or poster.width * poster.height > 40_000_000:
        raise BadImage("Unexpected image size")
    poster = poster.convert("RGBA")
    if not (bg or "").startswith("#"):                        # the background follows the picture's own corner
        bg = "#%02x%02x%02x" % poster.getpixel((min(4, poster.width - 1), min(4, poster.height - 1)))[:3]
    box = int(SIZE * POSTER_FRAC)
    k = min(box / poster.width, box / poster.height)
    poster = poster.resize((max(1, round(poster.width * k)), max(1, round(poster.height * k))), Image.LANCZOS)
    r, g, b = _hex(bg)
    dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.35
    f = 0.65 if dark else 0.93
    canvas = Image.new("RGBA", (SIZE, SIZE), (round(r * f), round(g * f), round(b * f), 255))
    canvas.alpha_composite(poster, ((SIZE - poster.width) // 2, (SIZE - poster.height) // 2))
    os.makedirs(out_dir, exist_ok=True)
    name = f"{UPLOADED_PREFIX}{uuid.uuid4().hex}.png"
    canvas.convert("RGB").save(os.path.join(out_dir, name), format="PNG", optimize=True)
    return f"/{out_dir.strip('/')}/{name}".replace("//", "/")
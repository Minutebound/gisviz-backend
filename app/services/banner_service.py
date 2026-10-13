"""
app/services/banner_service.py
==============================
The animated banner on top of the feed for each category (categories.banner).

The scene is picked by the same topic engine that styles posters on upload (backdrop_service.topic: the words
of the category's name, slug and description), so a climate category gets warming stripes and a rising
temperature curve, transport gets routes with moving vehicles, demographics a population pyramid, ...
The frontend draws and animates the scene (app/components/feed/CategoryBanner.tsx) in the category's colour;
nothing heavy is stored, just the choice:

    {"version": 1, "topic": "heat", "scene": "warming", "label": "Warming stripes and a rising curve",
     "color": "#e8590c", "source": "<fingerprint of the words it was made from>", "generated_at": "..."}

ensure(category)   (re)builds the banner when it is missing, outdated, or the category's words / colour changed;
                   a category without a theme colour gets the topic's colour
ensure_all(db)     every category (start-up, admin "refresh banners")
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.services import backdrop_service

VERSION = 1

# topic (backdrop_service.MOTIFS) -> the banner scene, what it shows, and its default colour
SCENES: dict[str, dict] = {
    "heat":       {"scene": "warming",   "color": "#e8590c", "label": "Warming stripes and a rising temperature curve"},
    "contours":   {"scene": "landscape", "color": "#2f9e44", "label": "Contour lines and a growing forest"},
    "waves":      {"scene": "water",     "color": "#1c7ed6", "label": "Waves, rain and a rising water gauge"},
    "ledger":     {"scene": "markets",   "color": "#b7791f", "label": "Candlesticks, a trend line and stacking coins"},
    "network":    {"scene": "transport", "color": "#7048e8", "label": "Routes between hubs with moving vehicles"},
    "blueprint":  {"scene": "energy",    "color": "#1971c2", "label": "A power grid with turbines and flowing current"},
    "blocks":     {"scene": "city",      "color": "#c2255c", "label": "A skyline rising on a street grid"},
    "people":     {"scene": "population", "color": "#d6336c", "label": "A population pyramid and a crowd"},
    "chalkboard": {"scene": "learning",  "color": "#0ca678", "label": "Scores scatter and a learning curve"},
    "graticule":  {"scene": "globe",     "color": "#06ba24", "label": "A turning globe with linked places"},
}


def _fingerprint(cat) -> str:
    raw = "|".join([cat.slug or "", cat.label or "", cat.description or "", cat.theme_color or "", str(VERSION)])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build(cat) -> dict:
    words = (cat.slug or "").replace("-", " ")
    topic = backdrop_service.topic(cat.label or "", cat.description or "", [cat.label or "", words])
    s = SCENES.get(topic, SCENES["graticule"])
    return {"version": VERSION, "topic": topic, "scene": s["scene"], "label": s["label"],
            "color": (cat.theme_color or s["color"]).lower(), "source": _fingerprint(cat),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def ensure(cat, force: bool = False) -> bool:
    """Make cat.banner current. Returns True when it changed (the caller commits)."""
    b = cat.banner if isinstance(cat.banner, dict) else None
    if not force and b and b.get("version") == VERSION and b.get("source") == _fingerprint(cat):
        return False
    if not cat.theme_color:                      # the topic's colour becomes the category's theme colour
        cat.theme_color = SCENES.get(backdrop_service.topic(cat.label or "", cat.description or "",
                                                            [cat.label or "", (cat.slug or "").replace("-", " ")]),
                                     SCENES["graticule"])["color"]
    cat.banner = build(cat)
    return True


def ensure_all(db, force: bool = False) -> int:
    from app.db.models import CategoryRecord
    n = 0
    for cat in db.query(CategoryRecord).all():
        n += ensure(cat, force)
    if n:
        db.commit()
    return n
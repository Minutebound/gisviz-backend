"""
app/services/backdrop_service.py
================================
The poster a post's visual sits on (posts.backdrop). Nothing is drawn on the server: the frontend paints the
background colour + a texture pattern, the title in the chosen font, the chart / map, the legend, the filter chips,
the sources and the GISViz mark, each block placed where the publisher dragged it in the poster editor.

What posts.backdrop keeps (validated here):
    {"mode": "auto" | "custom" | "none",
     "bg": "#rrggbb",                 background colour (the text colour follows it: dark bg -> light text)
     "texture": "paper" | ...,        see TEXTURES (patterns drawn by the frontend, no gradients)
     "font": "playfair" | ...,        title font, see FONTS
     "title_size": 0.7 .. 1.6,        title scale
     "title_align": "left" | "center",
     "map_base": "auto" | "streets" | "none",   map tiles under the data (none = shapes straight on the poster)
     "layout": {block: {"x": dx, "y": dy}}       offsets from the block's default place, in % of the poster width
     "topic": "<topic code>", "dark": bool}

  for_post(...)  "auto" -> colour / texture / font picked from the post's topic (keyword rules);
                 a dict -> the publisher's design, cleaned;  "none" -> a plain poster.
  clean(...)     validation of a design sent by the editor.
"""
from __future__ import annotations

import re
from typing import Optional, Union

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

TEXTURES = ("none", "paper", "grain", "dots", "grid", "ruled", "diagonal", "crosshatch", "topo", "waves")
FONTS = ("playfair", "dm_serif", "abril", "bodoni", "fraunces", "oswald", "bebas", "anton")
BLOCKS = ("title", "visual", "legend", "chips", "sources", "logo")
MAP_BASES = ("auto", "streets", "none")
CHOICES = ("auto", "light", "dark", "none")          # string choices (light / dark = auto with a plain colour)
OFFSET_MAX = 120.0                                   # % of the poster width
DEFAULT_FONT = "playfair"

# topic -> default background colour, texture, title font, and the words that point to it
MOTIFS: dict[str, dict] = {
    "chalkboard": {"bg": "#22312a", "texture": "grain", "font": "dm_serif",
                   "words": r"educat|school|student|pupil|teach|learn|math|pisa|exam|test score|literacy|universit|science|research|skill"},
    "heat":       {"bg": "#fbf1ea", "texture": "diagonal", "font": "playfair",
                   "words": r"climat|temperat|warming|heat|emission|co2|carbon|greenhouse|drought|wildfire|fire|weather"},
    "contours":   {"bg": "#eef3ea", "texture": "topo", "font": "fraunces",
                   "words": r"environment|forest|land|terrain|elevation|mountain|biodivers|species|park|soil|agricult|crop|farm|nature|deforest"},
    "waves":      {"bg": "#e8f2f7", "texture": "waves", "font": "playfair",
                   "words": r"ocean|sea|water|river|lake|rain|flood|coast|marine|fish|hydro|wave|glacier|ice"},
    "ledger":     {"bg": "#f6f3ea", "texture": "ruled", "font": "bodoni",
                   "words": r"econom|gdp|income|wage|salary|price|inflation|trade|export|import|financ|market|bank|tax|debt|money|cost|budget|poverty|wealth"},
    "network":    {"bg": "#f1f2f4", "texture": "dots", "font": "oswald",
                   "words": r"transport|traffic|road|rail|train|metro|bus|flight|airport|airline|mobility|commut|route|shipping|port|cycl|car|vehicle"},
    "blueprint":  {"bg": "#123a63", "texture": "grid", "font": "oswald",
                   "words": r"infrastruct|energy|power|electric|solar|wind|nuclear|grid|build|construct|bridge|internet|broadband|telecom|pipeline|engineer"},
    "blocks":     {"bg": "#f3f0ec", "texture": "grid", "font": "abril",
                   "words": r"urban|city|cities|housing|rent|house|home|real estate|metropol|zoning|street|neighbo|density|building"},
    "people":     {"bg": "#f5eff3", "texture": "dots", "font": "playfair",
                   "words": r"demograph|population|people|census|age|birth|death|life expectan|migra|health|hospital|disease|covid|vaccin|mortal|gender|women|men|child|survey|vote|election"},
    "graticule":  {"bg": "#f4f2ec", "texture": "paper", "font": "playfair",
                   "words": r"map|geograph|country|countries|world|global|region|border"},
}
DEFAULT_MOTIF = "graticule"
PLAIN = {"light": "#f4f1ea", "dark": "#171d26"}


def _luma(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def is_dark(bg: str) -> bool:
    return _luma(bg) < 0.18


def topic(title: str, description: str = "", categories: Optional[list[str]] = None,
          keywords: Optional[list[str]] = None) -> str:
    categories, keywords = categories or [], keywords or []
    text = " ".join([title or "", description or "", " ".join(categories), " ".join(keywords)]).lower()
    best, best_hits = DEFAULT_MOTIF, 0
    for code, m in MOTIFS.items():
        hits = len(re.findall(m["words"], text))
        hits += 2 * sum(1 for c in categories if re.search(m["words"], c.lower()))   # the category weighs more
        if hits > best_hits:
            best, best_hits = code, hits
    return best


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return default if n != n else max(lo, min(hi, n))      # NaN -> default


def clean(design: dict, topic_code: Optional[str] = None) -> dict:
    """A design from the poster editor, validated: unknown keys dropped, values clamped / defaulted."""
    if not isinstance(design, dict):
        raise ValueError("The poster design must be an object")
    m = MOTIFS.get(topic_code or "", MOTIFS[DEFAULT_MOTIF])
    bg = design.get("bg") if isinstance(design.get("bg"), str) and HEX_RE.match(design["bg"]) else m["bg"]
    out = {
        "mode": "custom",
        "bg": bg.lower(),
        "texture": design.get("texture") if design.get("texture") in TEXTURES else m["texture"],
        "font": design.get("font") if design.get("font") in FONTS else m["font"],
        "title_size": round(_num(design.get("title_size"), 0.7, 1.6, 1.0), 2),
        "title_align": "center" if design.get("title_align") == "center" else "left",
        "map_base": design.get("map_base") if design.get("map_base") in MAP_BASES else "auto",
        "dark": is_dark(bg),
    }
    layout = design.get("layout") or {}
    if not isinstance(layout, dict):
        raise ValueError("layout must map blocks to {x, y}")
    clean_layout = {}
    for block, pos in layout.items():
        if block in BLOCKS and isinstance(pos, dict):
            x = round(_num(pos.get("x"), -OFFSET_MAX, OFFSET_MAX, 0.0), 2)
            y = round(_num(pos.get("y"), -OFFSET_MAX, OFFSET_MAX, 0.0), 2)
            if x or y:
                clean_layout[block] = {"x": x, "y": y}
    out["layout"] = clean_layout
    if topic_code:
        out["topic"] = topic_code
    return out


def for_post(title: str, description: str = "", categories: Optional[list[str]] = None,
             keywords: Optional[list[str]] = None, choice: Union[str, dict, None] = "auto") -> dict:
    """What posts.backdrop keeps for a post (see the module docstring)."""
    code = topic(title, description, categories, keywords)
    if isinstance(choice, dict):
        return clean(choice, code)
    mode = (choice or "auto").strip().lower()
    if mode not in CHOICES:
        raise ValueError(f"Unknown background '{mode}'")
    if mode == "none":
        return {"mode": "none"}
    m = MOTIFS[code]
    base = {"bg": PLAIN[mode]} if mode in PLAIN else {}
    out = clean({"bg": m["bg"], "texture": m["texture"], "font": m["font"], **base}, code)
    out["mode"] = "auto"
    return out


def options() -> dict:
    return {"textures": list(TEXTURES), "fonts": list(FONTS), "blocks": list(BLOCKS), "map_bases": list(MAP_BASES)}
"""
app/services/visual_image.py
============================
Renders the static preview image (1200x630 JPEG) that the feed card and share
previews use. Runs on the server at publish time, so it needs no browser and
the image always matches the interactive visual stored in the post.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import matplotlib
matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch
from matplotlib.path import Path

VISUAL_DIR = os.path.join("uploads", "visuals")
ACCENT = os.getenv("GISVIZ_ACCENT", "#f65821")
INK, SOFT, GRID, BG = "#1c1c1c", "#6b6b6b", "#e3dfd6", "#faf8f3"
W_IN, H_IN, DPI = 12.0, 6.3, 100


def _num(v: Any):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _fmt(v: float) -> str:
    a = abs(v)
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= lim:
            return f"{v / lim:.3g}{suf}"
    return f"{v:.3g}"


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a towards b by t (0..1)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def _frame(title: str, subtitle: str, accent: str):
    fig = Figure(figsize=(W_IN, H_IN), dpi=DPI, facecolor=BG)
    FigureCanvasAgg(fig)
    fig.text(0.04, 0.93, title[:90], fontsize=20, fontweight="bold", color=INK, va="center")
    fig.text(0.04, 0.875, subtitle[:120], fontsize=11, color=SOFT, va="center")
    fig.text(0.96, 0.03, "gisviz", fontsize=11, color=accent, ha="right", fontweight="bold")
    return fig


def _style(ax):
    ax.set_facecolor(BG)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=SOFT, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _draw_chart(fig, spec: dict, rows: list[dict], accent: str):
    x, y, kind = spec["x"], spec["y"], spec["chart_type"]
    pts = [(r.get(x), _num(r.get(y))) for r in rows if _num(r.get(y)) is not None]
    ax = fig.add_axes([0.08, 0.14, 0.88, 0.66])
    _style(ax)
    if not pts:
        ax.text(0.5, 0.5, "No data", ha="center", color=SOFT, transform=ax.transAxes)
        return
    if kind == "scatter":
        xs = [_num(p[0]) for p in pts]
        ax.scatter(xs, [p[1] for p in pts], s=28, color=accent, alpha=0.75, edgecolors="none")
        ax.set_xlabel(x, color=SOFT, fontsize=10)
    else:
        labels = [str(p[0]) for p in pts]
        idx = list(range(len(pts)))
        vals = [p[1] for p in pts]
        if kind == "bar":
            ax.bar(idx, vals, color=accent, width=0.7)
        else:
            ax.plot(idx, vals, color=accent, linewidth=2.6)
            if len(pts) <= 40:
                ax.scatter(idx, vals, s=22, color=accent, zorder=3)
        step = max(1, len(labels) // 12)
        ax.set_xticks(idx[::step])
        ax.set_xticklabels([l[:12] for l in labels[::step]], rotation=30 if kind == "bar" else 0, ha="right" if kind == "bar" else "center")
    ax.set_ylabel(y, color=SOFT, fontsize=10)
    ax.yaxis.set_major_formatter(lambda v, _p: _fmt(v))


def _rings(geom: dict):
    """Yield (list_of_rings, kind) where each ring is a list of (lon, lat)."""
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon":
        yield c, "poly"
    elif t == "MultiPolygon":
        for poly in c:
            yield poly, "poly"
    elif t == "LineString":
        yield [c], "line"
    elif t == "MultiLineString":
        yield c, "line"
    elif t == "Point":
        yield [[c]], "pt"
    elif t == "MultiPoint":
        yield [c], "pt"


def _poly_path(rings):
    verts, codes = [], []
    for ring in rings:
        if len(ring) < 3:
            continue
        verts += [tuple(p[:2]) for p in ring] + [tuple(ring[0][:2])]
        codes += [Path.MOVETO] + [Path.LINETO] * (len(ring) - 1) + [Path.CLOSEPOLY]
    return Path(verts, codes) if verts else None


def _draw_map(fig, spec: dict, fc: dict, accent: str):
    feats = fc.get("features", [])
    field = spec["value_field"]
    vals = [_num((f.get("properties") or {}).get(field)) for f in feats]
    known = [v for v in vals if v is not None]
    cmap = LinearSegmentedColormap.from_list("gv", [_mix(accent, "#ffffff", 0.88), _mix(accent, "#ffffff", 0.55), accent, _mix(accent, "#000000", 0.45)])
    norm = Normalize(min(known), max(known)) if known and min(known) != max(known) else Normalize(0, 1)
    ax = fig.add_axes([0.04, 0.06, 0.92, 0.78])
    ax.set_facecolor(BG)
    ax.axis("off")
    xs, ys, pts = [], [], []
    for f, v in zip(feats, vals):
        g = f.get("geometry")
        if not g:
            continue
        color = cmap(norm(v)) if v is not None else "#c9c4b8"
        for rings, kind in _rings(g):
            if kind == "poly":
                path = _poly_path(rings)
                if path is not None:
                    ax.add_patch(PathPatch(path, facecolor=color, edgecolor="#ffffff", linewidth=0.5))
                    xs += [p[0] for r in rings for p in r]; ys += [p[1] for r in rings for p in r]
            elif kind == "line":
                for r in rings:
                    ax.plot([p[0] for p in r], [p[1] for p in r], color=color, linewidth=1.4)
                    xs += [p[0] for p in r]; ys += [p[1] for p in r]
            else:
                for p in rings[0]:
                    pts.append((p[0], p[1], v, color))
                    xs.append(p[0]); ys.append(p[1])
    if pts:
        pv = [p[2] for p in pts if p[2] is not None]
        lo, hi = (min(pv), max(pv)) if pv else (0, 1)
        sizes = [40 + 260 * ((p[2] - lo) / ((hi - lo) or 1)) if p[2] is not None else 40 for p in pts]
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=sizes, c=[p[3] for p in pts],
                   alpha=0.85, edgecolors="white", linewidths=0.8, zorder=3)
    if xs and ys:
        pad_x = (max(xs) - min(xs)) * 0.04 or 1
        pad_y = (max(ys) - min(ys)) * 0.04 or 1
        ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
        ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
        mid = (max(ys) + min(ys)) / 2
        import math
        ax.set_aspect(1 / max(0.2, math.cos(math.radians(mid))))
    if known:
        cax = fig.add_axes([0.70, 0.075, 0.24, 0.018])
        cb = fig.colorbar(matplotlib.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, orientation="horizontal")
        cb.ax.tick_params(labelsize=8, colors=SOFT)
        cb.ax.xaxis.set_major_formatter(lambda v, _p: _fmt(v))
        cb.outline.set_visible(False)
        fig.text(0.70, 0.115, field, fontsize=9, color=SOFT)


def render_thumbnail(title: str, subtitle: str, spec: dict, payload: Any, out_dir: str = VISUAL_DIR,
                     accent: str | None = None) -> str:
    """Render and save the preview. Returns the DB path, e.g. /uploads/visuals/post_<hex>.jpg"""
    os.makedirs(out_dir, exist_ok=True)
    accent = accent or ACCENT
    fig = _frame(title, subtitle, accent)
    if spec["kind"] == "map":
        _draw_map(fig, spec, payload, accent)
    else:
        _draw_chart(fig, spec, payload, accent)
    name = f"post_{uuid.uuid4().hex}.jpg"
    fig.savefig(os.path.join(out_dir, name), format="jpg", dpi=DPI, facecolor=BG,
                pil_kwargs={"quality": 88, "optimize": True})
    return f"/uploads/visuals/{name}"
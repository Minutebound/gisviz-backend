"""
app/services/visual_image.py
============================
Renders the static preview image (1200x630 PNG) that the feed card and share
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


PALETTE = ["#3b7ea1", "#d4a017", "#5b8c5a", "#8e5ea2", "#c0504d", "#2a9d8f", "#e76f51", "#6d6875"]   # keep in sync with D3Chart.tsx


def _series_colors(accent: str, n: int) -> list[str]:
    return ([accent] + PALETTE)[:max(1, n)] if n <= len(PALETTE) + 1 else [_mix(accent, "#ffffff", i / n * 0.7) for i in range(n)]


def _squarify(values: list[float], x: float, y: float, w: float, h: float):
    """Squarified treemap: returns one (x, y, w, h) rectangle per value (values sorted high to low)."""
    total = sum(values) or 1.0
    areas = [v / total * w * h for v in values]
    rects, rest = [], list(zip(range(len(values)), areas))
    out = {}

    def worst(row, side):
        s_ = sum(a for _, a in row)
        return max(max(side * side * a / (s_ * s_), (s_ * s_) / (side * side * a)) for _, a in row)

    while rest:
        side = min(w, h)
        row = [rest[0]]
        i = 1
        while i < len(rest) and worst(row + [rest[i]], side) <= worst(row, side):
            row.append(rest[i]); i += 1
        rest = rest[i:]
        s_ = sum(a for _, a in row)
        if w >= h:
            cw = s_ / h
            yy = y
            for idx, a in row:
                out[idx] = (x, yy, cw, a / cw); yy += a / cw
            x += cw; w -= cw
        else:
            ch = s_ / w
            xx = x
            for idx, a in row:
                out[idx] = (xx, y, a / ch, ch); xx += a / ch
            y += ch; h -= ch
    return [out[i] for i in range(len(values))]


def _draw_chart(fig, spec: dict, rows: list[dict], accent: str):
    x, y, kind = spec["x"], spec["y"], spec["chart_type"]
    ax = fig.add_axes([0.08, 0.14, 0.88, 0.66])
    _style(ax)
    if not rows:
        ax.text(0.5, 0.5, "No data", ha="center", color=SOFT, transform=ax.transAxes)
        return
    ylab = lambda: (ax.set_ylabel(y, color=SOFT, fontsize=10), ax.yaxis.set_major_formatter(lambda v, _p: _fmt(v)))

    if kind in ("stacked", "heatmap"):
        z = spec["z"]
        xs = list(dict.fromkeys(str(r.get(x)) for r in rows))
        zs = list(dict.fromkeys(str(r.get(z)) for r in rows))
        cell = {(str(r.get(x)), str(r.get(z))): _num(r.get(y)) or 0 for r in rows}
        if kind == "stacked":
            cols = _series_colors(accent, len(zs))
            bottoms = [0.0] * len(xs)
            for zi, zv in enumerate(zs):
                vals = [cell.get((xv, zv), 0) for xv in xs]
                ax.bar(range(len(xs)), vals, bottom=bottoms, color=cols[zi], width=0.7, label=zv[:16])
                bottoms = [b + v for b, v in zip(bottoms, vals)]
            ax.legend(frameon=False, fontsize=8, ncol=min(len(zs), 4), loc="upper left", labelcolor=SOFT)
            ylab()
        else:
            ax.grid(False)
            mat = [[cell.get((xv, zv)) for xv in xs] for zv in zs]
            cmap = LinearSegmentedColormap.from_list("gv", [_mix(accent, "#ffffff", 0.9), accent, _mix(accent, "#000000", 0.45)])
            ax.imshow([[v if v is not None else float("nan") for v in r] for r in mat], cmap=cmap, aspect="auto")
            ax.set_yticks(range(len(zs))); ax.set_yticklabels([z_[:14] for z_ in zs])
            for s_ in ("left", "bottom"):
                ax.spines[s_].set_visible(False)
        step = max(1, len(xs) // 12)
        ax.set_xticks(range(0, len(xs), step))
        ax.set_xticklabels([l[:12] for l in xs[::step]], rotation=30, ha="right")
        return

    if kind == "histogram":
        vals = [_num(r.get("count")) or 0 for r in rows]
        lefts = [_num(r.get("x0")) or 0 for r in rows]
        widths = [(_num(r.get("x1")) or 0) - (_num(r.get("x0")) or 0) for r in rows]
        ax.bar(lefts, vals, width=widths, align="edge", color=accent, edgecolor=BG, linewidth=1)
        ax.set_xlabel(y, color=SOFT, fontsize=10); ax.set_ylabel("count", color=SOFT, fontsize=10)
        return

    if kind == "bubble":
        s_key = spec["size"]
        pts = [(_num(r.get(x)), _num(r.get(y)), _num(r.get(s_key))) for r in rows]
        pts = [p for p in pts if None not in p]
        if pts:
            lo, hi = min(p[2] for p in pts), max(p[2] for p in pts)
            sizes = [40 + 900 * ((p[2] - lo) / ((hi - lo) or 1)) for p in pts]
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=sizes, color=accent, alpha=0.55, edgecolors=accent)
        ax.set_xlabel(f"{x}  (bubble size = {s_key})", color=SOFT, fontsize=10); ylab()
        return

    pts = [(r.get(x), _num(r.get(y))) for r in rows if _num(r.get(y)) is not None]
    if not pts:
        ax.text(0.5, 0.5, "No data", ha="center", color=SOFT, transform=ax.transAxes)
        return

    if kind == "scatter":
        ax.scatter([_num(p[0]) for p in pts], [p[1] for p in pts], s=28, color=accent, alpha=0.75, edgecolors="none")
        ax.set_xlabel(x, color=SOFT, fontsize=10); ylab()
        return

    labels = [str(p[0]) for p in pts]
    vals = [p[1] for p in pts]
    idx = list(range(len(pts)))

    if kind == "donut":
        ax.set_visible(False)
        pax = fig.add_axes([0.06, 0.08, 0.5, 0.74])
        cols = _series_colors(accent, len(vals))
        pax.pie(vals, colors=cols, startangle=90, counterclock=False, wedgeprops={"width": 0.38, "edgecolor": BG, "linewidth": 2})
        total = sum(vals) or 1
        fig.legend(pax.patches, [f"{l[:22]}  {v / total:.0%}" for l, v in zip(labels, vals)],
                   loc="center left", bbox_to_anchor=(0.6, 0.45), frameon=False, fontsize=11, labelcolor=INK)
        return

    if kind == "treemap":
        ax.set_visible(False)
        tax = fig.add_axes([0.04, 0.08, 0.92, 0.74]); tax.axis("off")
        order = sorted(zip(labels, vals), key=lambda t: -t[1])[:40]
        order = [o for o in order if o[1] > 0]
        if order:
            rects = _squarify([o[1] for o in order], 0, 0, 100, 60)
            hi = max(o[1] for o in order)
            for (lab, v), (rx, ry, rw, rh) in zip(order, rects):
                tax.add_patch(PathPatch(Path([(rx, ry), (rx + rw, ry), (rx + rw, ry + rh), (rx, ry + rh), (rx, ry)],
                                             [Path.MOVETO, Path.LINETO, Path.LINETO, Path.LINETO, Path.CLOSEPOLY]),
                                        facecolor=_mix(accent, "#ffffff", 0.75 - 0.6 * (v / hi)), edgecolor=BG, linewidth=2))
                if rw > 6 and rh > 4:
                    tax.text(rx + 0.8, ry + rh - 1.0, lab[:int(rw * 0.9)], fontsize=9, color=INK, va="top", fontweight="bold")
            tax.set_xlim(0, 100); tax.set_ylim(0, 60); tax.invert_yaxis()
        return

    step = max(1, len(labels) // 12)
    if kind == "hbar":
        ax.grid(axis="y", visible=False); ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.barh(range(len(vals)), list(reversed(vals)), color=accent, height=0.7)
        ax.set_yticks(range(len(vals))); ax.set_yticklabels([l[:22] for l in reversed(labels)])
        ax.xaxis.set_major_formatter(lambda v, _p: _fmt(v)); ax.set_xlabel(y, color=SOFT, fontsize=10)
        return
    if kind == "bar":
        ax.bar(idx, vals, color=accent, width=0.7)
    elif kind == "area":
        ax.fill_between(idx, vals, color=accent, alpha=0.22); ax.plot(idx, vals, color=accent, linewidth=2.4)
    else:
        ax.plot(idx, vals, color=accent, linewidth=2.6)
        if len(pts) <= 40:
            ax.scatter(idx, vals, s=22, color=accent, zorder=3)
    ax.set_xticks(idx[::step])
    rot = 30 if kind == "bar" else 0
    ax.set_xticklabels([l[:12] for l in labels[::step]], rotation=rot, ha="right" if rot else "center")
    ylab()


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
    if spec.get("map_style") == "heat":
        px = [f["geometry"]["coordinates"][0] for f in feats if f.get("geometry") and f["geometry"].get("type") == "Point"]
        py = [f["geometry"]["coordinates"][1] for f in feats if f.get("geometry") and f["geometry"].get("type") == "Point"]
        if px:
            ax.hexbin(px, py, gridsize=40, cmap=cmap, mincnt=1, linewidths=0.2, edgecolors=BG)
            import math
            ax.set_aspect(1 / max(0.2, math.cos(math.radians((max(py) + min(py)) / 2))))
            fig.text(0.70, 0.115, "point density", fontsize=9, color=SOFT)
        return
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
    """Render and save the preview. Returns the DB path, e.g. /uploads/visuals/post_<hex>.png"""
    os.makedirs(out_dir, exist_ok=True)
    accent = accent or ACCENT
    fig = _frame(title, subtitle, accent)
    if spec["kind"] == "map":
        _draw_map(fig, spec, payload, accent)
    else:
        _draw_chart(fig, spec, payload, accent)
    name = f"post_{uuid.uuid4().hex}.png"
    fig.savefig(os.path.join(out_dir, name), format="png", dpi=DPI, facecolor=BG,
                pil_kwargs={"optimize": True})
    return f"/uploads/visuals/{name}"
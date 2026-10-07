"""
app/services/visual_image.py
============================
Renders the post's poster as one square PNG (1200x1200) that the feed card, thumbnails and share
previews use. Runs on the server at publish time, so it needs no browser and
the image always matches the interactive visual stored in the post.
"""
from __future__ import annotations

import os
import threading
import uuid
from typing import Any

import matplotlib
matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.patches import PathPatch
from matplotlib.path import Path
import matplotlib.patches  # noqa: F401

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


def _style(ax):
    ax.set_facecolor("none")                     # the poster (colour + texture) shows through
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


def _fill_top(fig, target: float) -> None:
    """Every chart leaves a blank band above its plot (room for a legend, 14-20% of its area). On the square image
    that band is the gap under the title: stretch the axes up until the highest one reaches `target` (a fraction
    of the area's height). Text sizes do not change."""
    axes = list(fig.axes)
    if not axes:
        return
    top = max(a.get_position().y1 for a in axes)
    if not (0.4 < top < target):
        return
    k = target / top
    for a in axes:
        p = a.get_position()
        a.set_position([p.x0, p.y0 * k, p.width, p.height * k])


def _draw_chart(fig, spec: dict, rows: list[dict], accent: str):
    x, y, kind = spec["x"], spec["y"], spec["chart_type"]
    ax = fig.add_axes([0.08, 0.14, 0.88, 0.66])
    _style(ax)
    if not rows:
        ax.text(0.5, 0.5, "No data", ha="center", color=SOFT, transform=ax.transAxes)
        return
    ylab = lambda: (ax.set_ylabel(y, color=SOFT, fontsize=10), ax.yaxis.set_major_formatter(lambda v, _p: _fmt(v)))
    if _draw_extra(fig, ax, spec, rows, accent):
        return

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
    bar_cols, groups = _bar_colors(spec, rows, x, y, accent, len(vals))
    refs = _ref_values(spec, vals)
    if kind == "hbar":
        ax.grid(axis="y", visible=False); ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.barh(range(len(vals)), list(reversed(vals)), color=list(reversed(bar_cols)), height=0.7)
        ax.set_yticks(range(len(vals))); ax.set_yticklabels([l[:22] for l in reversed(labels)])
        ax.xaxis.set_major_formatter(lambda v, _p: _fmt(v)); ax.set_xlabel(y, color=SOFT, fontsize=10)
        for v, lab, mode in refs:
            if mode == "divider":   # the line sits between the last bar above the value and the first below it
                below = sum(1 for val in vals if val < v)
                ax.axhline(below - 0.5, color=INK, linestyle=(0, (4, 3)), linewidth=1)
                ax.text(0.995, below - 0.4, lab or _fmt(v), transform=ax.get_yaxis_transform(), ha="right", va="bottom", fontsize=9,
                        color=INK, bbox={"facecolor": BG, "edgecolor": "none", "pad": 1})
            else:
                ax.axvline(v, color=INK, linestyle=(0, (4, 3)), linewidth=1)
                ax.text(v, 1.0, lab or _fmt(v), transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9, color=INK)
        _legend(ax, groups)
        return
    if kind == "bar":
        ax.bar(idx, vals, color=bar_cols, width=0.7)
        for v, lab, _mode in refs:
            ax.axhline(v, color=INK, linestyle=(0, (4, 3)), linewidth=1)
            ax.text(0.995, v, lab or _fmt(v), transform=ax.get_yaxis_transform(), ha="right", va="bottom", fontsize=9, color=INK)
        _legend(ax, groups)
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


# Same palette and assignment order as the web chart (D3Chart.groupColors), so the thumbnail matches the post.
PALETTE = ["#3b7ea1", "#d4a017", "#5b8c5a", "#8e5ea2", "#c0504d", "#2a9d8f", "#e76f51", "#6d6875"]


def _bar_colors(spec: dict, rows: list, x: str, y: str, accent: str, n: int):
    """Per-bar colours when the visual is coloured by a group column; otherwise all accent."""
    by = spec.get("color_by")
    if not by:
        return [accent] * n, []
    chosen = spec.get("colors") or {}
    free = [c for c in [accent, *PALETTE] if c not in chosen.values()]
    seen, cols = {}, []
    for r in rows:
        if _num(r.get(y)) is None:
            continue
        g = str(r.get(by))
        if g not in seen:
            seen[g] = chosen.get(g) or free[len([k for k in seen if k not in chosen]) % len(free)]
        cols.append(seen[g])
    return cols[:n] or [accent] * n, list(seen.items())


def _ref_values(spec: dict, vals: list):
    out = []
    for r in spec.get("reference_lines") or []:
        v = r.get("value")
        if r.get("stat") == "mean" and vals:
            v = sum(vals) / len(vals)
        elif r.get("stat") == "median" and vals:
            sv = sorted(vals); m = len(sv) // 2
            v = sv[m] if len(sv) % 2 else (sv[m - 1] + sv[m]) / 2
        if isinstance(v, (int, float)):
            out.append((float(v), r.get("label") or "", r.get("mode") or "line"))
    return out


def _legend(ax, groups):
    if not groups:
        return
    from matplotlib.patches import Patch
    ax.legend([Patch(color=c) for _g, c in groups], [g[:16] for g, _c in groups], frameon=False, fontsize=9,
              loc="lower right", bbox_to_anchor=(1.0, 1.01), ncol=min(4, len(groups)), labelcolor=INK)   # top right


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
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.84])
    ax.set_anchor("N")                       # a map narrower than its box sits right under the header, not centred
    ax.set_facecolor("none")
    ax.axis("off")
    legend = {"field": field, "lo": min(known), "hi": max(known)} if known else None
    if spec.get("map_style") in ("bubble", "cartogram") and feats:
        _draw_bubbles(ax, feats, vals, cmap, norm, dorling=spec.get("map_style") == "cartogram")
        return legend
    if spec.get("map_style") in ("heat", "hexbin"):
        px = [f["geometry"]["coordinates"][0] for f in feats if f.get("geometry") and f["geometry"].get("type") == "Point"]
        py = [f["geometry"]["coordinates"][1] for f in feats if f.get("geometry") and f["geometry"].get("type") == "Point"]
        if px:
            ax.hexbin(px, py, gridsize=40, cmap=cmap, mincnt=1, linewidths=0.2, edgecolors=BG)
            import math
            ax.set_aspect(1 / max(0.2, math.cos(math.radians((max(py) + min(py)) / 2))))
        return {"field": "point density", "lo": None, "hi": None}
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
    return legend      # the poster draws the colour key top right of the map


def _centroid(g: dict):
    pts = []
    for rings, _k in _rings(g):
        for r in rings:
            pts += r
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _draw_bubbles(ax, feats, vals, cmap, norm, dorling=False):
    """Bubble map / cartogram preview: pale shapes, a circle per place sized by its value (area ~ value)."""
    import math
    cs, xs, ys = [], [], []
    for f, v in zip(feats, vals):
        g = f.get("geometry")
        if not g:
            continue
        for rings, kind in _rings(g):
            if kind == "poly" and not dorling:
                path = _poly_path(rings)
                if path is not None:
                    ax.add_patch(PathPatch(path, facecolor="#ece8df", edgecolor="#ffffff", linewidth=0.5))
        c = _centroid(g)
        if c:
            cs.append((c[0], c[1], v)); xs.append(c[0]); ys.append(c[1])
    if not cs:
        return
    hi = max((abs(v) for _x, _y, v in cs if v is not None), default=1) or 1
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1)
    for x_, y_, v in sorted(cs, key=lambda t: -(t[2] or 0)):
        r = (math.sqrt(abs(v) / hi) if v is not None else 0.1) * span * (0.06 if dorling else 0.045)
        ax.add_patch(matplotlib.patches.Circle((x_, y_), r, facecolor=cmap(norm(v)) if v is not None else "#c9c4b8",
                                               edgecolor="white", linewidth=0.8, alpha=0.9, zorder=3))
    pad = span * 0.08
    ax.set_xlim(min(xs) - pad, max(xs) + pad); ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_aspect(1 / max(0.2, math.cos(math.radians((max(ys) + min(ys)) / 2))))


def _kde(vals: list[float], grid):
    import numpy as np
    v = np.asarray(vals, dtype=float)
    if v.size < 2:
        return np.zeros_like(grid)
    bw = 1.06 * (v.std() or 1) * v.size ** (-1 / 5)
    return np.exp(-0.5 * ((grid[:, None] - v[None, :]) / bw) ** 2).sum(1) / (v.size * bw * 2.5066)


def _draw_extra(fig, ax, spec: dict, rows: list[dict], accent: str) -> bool:
    """Previews for the data-to-viz types beyond the classic charts. False = not one of them."""
    import math
    import numpy as np
    kind, x, y, z = spec["chart_type"], spec.get("x"), spec["y"], spec.get("z")
    cols = _series_colors(accent, 9)

    def groups():
        out: dict[str, list[float]] = {}
        for r in rows:
            v = _num(r.get(y))
            if v is not None:
                out.setdefault(str(r.get(x)) if x else y, []).append(v)
        return out

    if kind in ("boxplot", "violin"):
        g = groups()
        names = list(g)[:24]
        data = [g[n] for n in names]
        if kind == "boxplot":
            bp = ax.boxplot(data, patch_artist=True, widths=0.55, showfliers=True)
            for b in bp["boxes"]:
                b.set(facecolor=_mix(accent, "#ffffff", 0.55), edgecolor=accent)
            for k in ("medians", "whiskers", "caps"):
                for l_ in bp[k]:
                    l_.set(color=INK if k == "medians" else accent)
        else:
            vp = ax.violinplot([d if len(d) > 1 else d * 2 for d in data], showmedians=True)
            for b in vp["bodies"]:
                b.set(facecolor=accent, edgecolor=accent, alpha=0.45)
        ax.set_xticks(range(1, len(names) + 1)); ax.set_xticklabels([n[:12] for n in names], rotation=30 if len(names) > 6 else 0, ha="right" if len(names) > 6 else "center")
        ax.set_ylabel(y, color=SOFT, fontsize=10)
        return True

    if kind in ("density", "ridgeline"):
        g = groups()
        allv = [v for vs in g.values() for v in vs]
        if not allv:
            return True
        lo, hi = min(allv), max(allv)
        grid = np.linspace(lo - (hi - lo) * 0.1, hi + (hi - lo) * 0.1 or lo + 1, 200)
        for i, (n, vs) in enumerate(list(g.items())[:24]):
            d = _kde(vs, grid)
            if kind == "ridgeline":
                d = d / (d.max() or 1) * 1.6
                ax.fill_between(grid, i, i + d, color=cols[i % len(cols)], alpha=0.75, lw=0)
                ax.plot(grid, i + d, color=BG, lw=1)
            else:
                ax.fill_between(grid, 0, d, color=cols[i % len(cols)], alpha=0.35, lw=0)
                ax.plot(grid, d, color=cols[i % len(cols)], lw=2, label=n[:16])
        if kind == "ridgeline":
            ax.set_yticks(range(len(g))[:24]); ax.set_yticklabels([n[:14] for n in list(g)[:24]])
        elif len(g) > 1:
            ax.legend(frameon=False, fontsize=8)
        ax.set_xlabel(y, color=SOFT, fontsize=10)
        return True

    if kind == "density_2d":
        px = [_num(r.get(x)) for r in rows]; py = [_num(r.get(y)) for r in rows]
        pts = [(a, b) for a, b in zip(px, py) if a is not None and b is not None]
        if pts:
            ax.hexbin([p[0] for p in pts], [p[1] for p in pts], gridsize=30, mincnt=1,
                      cmap=LinearSegmentedColormap.from_list("gv", [_mix(accent, "#ffffff", 0.85), accent, _mix(accent, "#000000", 0.4)]))
        ax.set_xlabel(x, color=SOFT); ax.set_ylabel(y, color=SOFT)
        return True

    if kind == "correlogram":
        names = list(dict.fromkeys(str(r["a"]) for r in rows))
        m = np.zeros((len(names), len(names)))
        for r in rows:
            m[names.index(str(r["a"])), names.index(str(r["b"]))] = _num(r.get("r")) or 0
        ax.grid(False)
        ax.imshow(m, cmap=LinearSegmentedColormap.from_list("rb", ["#2b6cb0", "#f7f7f7", "#c0504d"]), vmin=-1, vmax=1)
        ax.set_xticks(range(len(names))); ax.set_xticklabels([n[:10] for n in names], rotation=35, ha="right")
        ax.set_yticks(range(len(names))); ax.set_yticklabels([n[:12] for n in names])
        return True

    if kind == "connected_scatter":
        px = [_num(r.get(x)) for r in rows]; py = [_num(r.get(y)) for r in rows]
        pts = [(a, b) for a, b in zip(px, py) if a is not None and b is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=accent, lw=1.8, marker="o", ms=4)
        ax.set_xlabel(x, color=SOFT); ax.set_ylabel(y, color=SOFT)
        return True

    if kind == "ranked_columns":
        cols_ = (spec.get("fields") or [y]) if not z else list(dict.fromkeys(str(r.get(z)) for r in rows))[:5]
        def val(r, c): return _num(r.get(c)) if not z else (_num(r.get(y)) if str(r.get(z)) == c else None)
        ranks = []
        for c in cols_:
            items = {}
            for r in rows:
                v = val(r, c)
                if v is not None:
                    items[str(r.get(x))] = v
            ranks.append(sorted(items.items(), key=lambda t: -t[1])[:18])
        ax.set_visible(False)
        rax = fig.add_axes([0.04, 0.04, 0.92, 0.78]); rax.axis("off")
        n = len(cols_)
        for i, (c, rk) in enumerate(zip(cols_, ranks)):
            col = ([accent] + PALETTE)[i % 9]
            rax.text(i / max(1, n - 1) if n > 1 else 0.5, 1.02, c[:18], ha="center", fontsize=11, fontweight="bold", color=col)
            for j, (lab, v) in enumerate(rk):
                cx, cy = (i / max(1, n - 1) if n > 1 else 0.5), 1 - (j + 0.5) / 18
                rax.add_patch(matplotlib.patches.FancyBboxPatch((cx - 0.07, cy - 0.022), 0.14, 0.044, boxstyle="round,pad=0.002,rounding_size=0.01",
                                                               color=_mix(col, "#ffffff", 0.5 * j / 18), transform=rax.transAxes))
                rax.text(cx, cy, f"{lab[:10]}  {_fmt(v)}", ha="center", va="center", fontsize=7.5, color="white", fontweight="bold", transform=rax.transAxes)
                if i + 1 < n:
                    nxt = dict((k, jj) for jj, (k, _v) in enumerate(ranks[i + 1]))
                    if lab in nxt:
                        x2 = (i + 1) / max(1, n - 1); y2 = 1 - (nxt[lab] + 0.5) / 18
                        rax.plot([cx + 0.07, (cx + x2) / 2, (cx + x2) / 2, x2 - 0.07], [cy, cy, y2, y2], color=col, alpha=0.35, lw=1, transform=rax.transAxes)
        rax.set_xlim(-0.1, 1.1)
        return True

    if kind == "parallel_coordinates":
        f = spec.get("fields") or [y]
        rng = {k: (min(v for v in (_num(r.get(k)) for r in rows) if v is not None), max(v for v in (_num(r.get(k)) for r in rows) if v is not None)) for k in f}
        for r in rows[:400]:
            vs = [_num(r.get(k)) for k in f]
            if None in vs:
                continue
            ax.plot(range(len(f)), [(v - rng[k][0]) / ((rng[k][1] - rng[k][0]) or 1) for v, k in zip(vs, f)], color=accent, alpha=0.18, lw=0.8)
        ax.set_xticks(range(len(f))); ax.set_xticklabels([k[:12] for k in f]); ax.set_yticks([])
        return True

    if kind in ("lollipop", "circular_bar", "radar", "word_cloud", "pie"):
        pts = [(str(r.get(x)), _num(r.get(y))) for r in rows if _num(r.get(y)) is not None]
        if not pts:
            return True
        if kind == "pie":
            ax.set_visible(False)
            pax = fig.add_axes([0.08, 0.08, 0.5, 0.74])
            pax.pie([max(0, p[1]) for p in pts], colors=_series_colors(accent, len(pts)), startangle=90, counterclock=False,
                    wedgeprops={"edgecolor": BG, "linewidth": 2})
            fig.legend(pax.patches, [p[0][:22] for p in pts], loc="center left", bbox_to_anchor=(0.6, 0.45), frameon=False, fontsize=11)
            return True
        if kind == "lollipop":
            pts = pts[:40][::-1]
            ax.hlines(range(len(pts)), 0, [p[1] for p in pts], color=_mix(accent, "#ffffff", 0.4), lw=1.5)
            ax.scatter([p[1] for p in pts], range(len(pts)), color=accent, s=40, zorder=3)
            ax.set_yticks(range(len(pts))); ax.set_yticklabels([p[0][:22] for p in pts]); ax.grid(axis="x", color=GRID)
            return True
        if kind == "word_cloud":
            ax.axis("off")
            hi = max(p[1] for p in pts) or 1
            rnd = __import__("random").Random(3)
            for i, (w, v) in enumerate(sorted(pts, key=lambda t: -t[1])[:60]):
                ax.text(rnd.uniform(0.05, 0.95), rnd.uniform(0.05, 0.95), w[:20], fontsize=8 + 30 * math.sqrt(max(0, v) / hi),
                        color=cols[i % len(cols)], ha="center", va="center", transform=ax.transAxes, fontweight="bold")
            return True
        ax.set_visible(False)
        pax = fig.add_axes([0.25, 0.06, 0.5, 0.76], projection="polar")
        pax.set_facecolor("none")
        if kind == "circular_bar":
            th = np.linspace(0, 2 * np.pi, len(pts), endpoint=False)
            pax.bar(th, [p[1] for p in pts], width=2 * np.pi / len(pts) * 0.85, bottom=max(p[1] for p in pts) * 0.25, color=accent)
            pax.set_xticks([]); pax.set_yticks([])
        else:
            series: dict[str, dict[str, float]] = {}
            for r in rows:
                series.setdefault(str(r.get(z)) if z else y, {})[str(r.get(x))] = _num(r.get(y)) or 0
            axes_ = list(dict.fromkeys(str(r.get(x)) for r in rows))
            th = np.linspace(0, 2 * np.pi, len(axes_), endpoint=False).tolist()
            for i, (n, d) in enumerate(series.items()):
                vals = [d.get(a, 0) for a in axes_]
                pax.plot(th + th[:1], vals + vals[:1], color=cols[i % len(cols)], lw=2)
                pax.fill(th + th[:1], vals + vals[:1], color=cols[i % len(cols)], alpha=0.18)
            pax.set_xticks(th); pax.set_xticklabels([a[:12] for a in axes_], fontsize=8); pax.set_yticklabels([])
        return True

    if kind in ("stacked_area", "streamgraph"):
        xs = list(dict.fromkeys(str(r.get(x)) for r in rows))
        zs = list(dict.fromkeys(str(r.get(z)) for r in rows))
        cell = {(str(r.get(x)), str(r.get(z))): _num(r.get(y)) or 0 for r in rows}
        ys = [[cell.get((a, b), 0) for a in xs] for b in zs]
        ax.stackplot(range(len(xs)), ys, colors=_series_colors(accent, len(zs)), baseline="wiggle" if kind == "streamgraph" else "zero",
                     labels=[b[:14] for b in zs], alpha=0.9)
        step = max(1, len(xs) // 8)
        ax.set_xticks(range(0, len(xs), step)); ax.set_xticklabels([a[:10] for a in xs[::step]])
        ax.legend(frameon=False, fontsize=8, loc="upper left", ncol=min(4, len(zs)))
        if kind == "streamgraph":
            ax.set_yticks([])
        return True

    if kind in ("sunburst", "dendrogram", "circle_packing") or (kind == "treemap" and z):
        tot: dict[str, float] = {}
        for r in rows:
            tot[str(r.get(x))] = tot.get(str(r.get(x)), 0) + abs(_num(r.get(y)) or (1 if kind == "dendrogram" else 0))
        order = sorted(tot.items(), key=lambda t: -t[1])[:30]
        ax.set_visible(False)
        tax = fig.add_axes([0.04, 0.08, 0.92, 0.74]); tax.axis("off")
        if order and kind in ("treemap", "circle_packing"):
            for (lab, v), (rx, ry, rw, rh), c in zip(order, _squarify([o[1] for o in order], 0, 0, 100, 60), _series_colors(accent, 30) * 4):
                tax.add_patch(PathPatch(Path([(rx, ry), (rx + rw, ry), (rx + rw, ry + rh), (rx, ry + rh), (rx, ry)],
                                             [Path.MOVETO, Path.LINETO, Path.LINETO, Path.LINETO, Path.CLOSEPOLY]),
                                        facecolor=c, edgecolor=BG, linewidth=2, alpha=0.85))
                if rw > 6 and rh > 4:
                    tax.text(rx + 0.8, ry + rh - 1, lab[:int(rw * 0.9)], fontsize=9, color="white", va="top", fontweight="bold")
            tax.set_xlim(0, 100); tax.set_ylim(0, 60); tax.invert_yaxis()
        else:                                  # rings (sunburst) / radial tree (dendrogram)
            pax = fig.add_axes([0.25, 0.06, 0.5, 0.76], projection="polar"); pax.axis("off")
            total = sum(v for _l, v in order) or 1
            a0 = 0.0
            for i, (lab, v) in enumerate(order):
                w = 2 * math.pi * v / total
                pax.bar(a0 + w / 2, 1, width=w, bottom=0.6, color=cols[i % len(cols)], edgecolor=BG)
                kids = [r for r in rows if str(r.get(x)) == lab]
                b0 = a0
                for k in kids:
                    kw = w * abs(_num(k.get(y)) or 1) / (sum(abs(_num(q.get(y)) or 1) for q in kids) or 1)
                    if kind == "sunburst":
                        pax.bar(b0 + kw / 2, 0.8, width=kw, bottom=1.65, color=_mix(cols[i % len(cols)], "#ffffff", 0.35), edgecolor=BG)
                    else:
                        pax.plot([a0 + w / 2, b0 + kw / 2], [1.0, 2.2], color=cols[i % len(cols)], lw=0.8)
                    b0 += kw
                a0 += w
        return True

    if kind in ("sankey", "chord", "network", "arc_diagram", "edge_bundling"):
        links = [(str(r.get(x)), str(r.get(z)), abs(_num(r.get(y)) or 1)) for r in rows]
        nodes = list(dict.fromkeys([l_[0] for l_ in links] + [l_[1] for l_ in links]))[:60]
        hi = max((l_[2] for l_ in links), default=1) or 1
        ax.set_visible(False)
        fax = fig.add_axes([0.04, 0.06, 0.92, 0.76]); fax.axis("off")
        if kind == "sankey":
            src = list(dict.fromkeys(l_[0] for l_ in links)); dst = [n for n in dict.fromkeys(l_[1] for l_ in links)]
            ps = {n: (0.1, 1 - (i + 0.5) / len(src)) for i, n in enumerate(src)}
            pd = {n: (0.9, 1 - (i + 0.5) / len(dst)) for i, n in enumerate(dst)}
            for a, b, w in links:
                if a in ps and b in pd:
                    xs_ = np.linspace(0.1, 0.9, 40); t = (1 - np.cos(np.linspace(0, np.pi, 40))) / 2
                    fax.plot(xs_, ps[a][1] + (pd[b][1] - ps[a][1]) * t, color=accent, alpha=0.35, lw=1 + 14 * w / hi)
            for n, (px_, py_) in list(ps.items()) + list(pd.items()):
                fax.text(px_ + (-0.01 if px_ < 0.5 else 0.01), py_, n[:18], ha="right" if px_ < 0.5 else "left", va="center", fontsize=8, color=INK)
            fax.set_xlim(0, 1); fax.set_ylim(0, 1)
        elif kind == "arc_diagram":
            pos = {n: i for i, n in enumerate(nodes)}
            for a, b, w in links:
                if a in pos and b in pos:
                    c_, r_ = (pos[a] + pos[b]) / 2, abs(pos[a] - pos[b]) / 2
                    t = np.linspace(0, np.pi, 40)
                    fax.plot(c_ + r_ * np.cos(t), r_ * np.sin(t), color=accent, alpha=0.4, lw=0.6 + 4 * w / hi)
            fax.scatter(range(len(nodes)), [0] * len(nodes), color=INK, s=12, zorder=3)
            fax.set_xlim(-1, len(nodes)); fax.set_ylim(-0.5, len(nodes) / 2 + 0.5)
        else:
            ang = {n: 2 * math.pi * i / len(nodes) for i, n in enumerate(nodes)}
            for a, b, w in links:
                if a in ang and b in ang:
                    p0 = np.array([math.cos(ang[a]), math.sin(ang[a])]); p1 = np.array([math.cos(ang[b]), math.sin(ang[b])])
                    t = np.linspace(0, 1, 30)[:, None]
                    ctrl = np.zeros(2) if kind != "network" else (p0 + p1) / 2
                    curve = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * ctrl * (0.2 if kind == "edge_bundling" else 1) + t ** 2 * p1
                    fax.plot(curve[:, 0], curve[:, 1], color=accent, alpha=0.35, lw=0.6 + 5 * w / hi)
            for i, n in enumerate(nodes):
                fax.scatter(math.cos(ang[n]), math.sin(ang[n]), color=cols[i % len(cols)], s=30, zorder=3)
            fax.set_aspect("equal"); fax.set_xlim(-1.3, 1.3); fax.set_ylim(-1.15, 1.15)
        return True
    return False


# ═════════════════════════════════ the poster image ═════════════════════════════════
# One square PNG per post (feed card, thumbnails, link previews): the whole poster as readers see it — background
# colour + texture, title in the poster font, subtitle, the chart / map with its legend, the GISViz mark and
# the "(Source: …)" line — drawn at 80% of the image with a margin around it. Block offsets from the poster editor
# (posts.backdrop.layout, % of the poster width) are applied as on the page. Maps are drawn without street tiles.

IMG_PX = 1200                       # square image
POSTER_FRAC = 0.80                  # the poster covers 80% of it
FONT_DIR = os.getenv("POSTER_FONT_DIR", "/app/fonts")          # scripts/fetch_fonts.py (Docker build)
ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
UPPER_FONTS = {"oswald", "bebas", "anton"}
_LOCK = threading.Lock()            # the chart helpers read the module colours (INK, SOFT, GRID, BG)


def _luma(hex_: str) -> float:
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(int(hex_[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _title_font(code: str) -> FontProperties:
    path = os.path.join(FONT_DIR, f"{code}.ttf")
    if os.path.exists(path):
        return FontProperties(fname=path)
    return FontProperties(family="DejaVu Serif", weight="bold")


def _wrap(renderer, text: str, prop: FontProperties, size_pt: float, max_w: float, max_lines: int) -> list[str]:
    """Greedy word wrap measured with the real font; the last line gets an ellipsis when cut."""
    p = prop.copy(); p.set_size(size_pt)
    width = lambda t: renderer.get_text_width_height_descent(t, p, ismath=False)[0]
    lines, cur = [], ""
    words = text.split()
    for i, w in enumerate(words):
        trial = f"{cur} {w}".strip()
        if width(trial) <= max_w or not cur:
            cur = trial
            continue
        lines.append(cur); cur = w
        if len(lines) == max_lines:
            break
    else:
        if cur:
            lines.append(cur)
        return lines
    last = lines[-1]
    while last and width(last + "…") > max_w:
        last = last[:-1]
    lines[-1] = last.rstrip() + "…"
    return lines


def _texture(ax, code: str, w: float, h: float, dark: bool, clip) -> None:
    """The same patterns as the website (lib/poster.ts), in poster pixels."""
    import numpy as np
    from matplotlib.collections import LineCollection
    c = "#ffffff" if dark else "#000000"
    a = (lambda n: n * 1.25) if dark else (lambda n: n)
    lines = []
    def add(segs, alpha, lw=1.0):
        if segs:
            lc = LineCollection(segs, colors=c, alpha=a(alpha), linewidths=lw * 0.72)
            lc.set_clip_path(clip); ax.add_collection(lc)
    if code in ("paper", "grain"):
        rng = np.random.default_rng(7)
        noise = rng.random((int(h / 3), int(w / 3)))
        rgba = np.zeros(noise.shape + (4,))
        rgba[..., :3] = 1.0 if dark else 0.0
        rgba[..., 3] = noise * a(0.05 if code == "paper" else 0.10)
        im = ax.imshow(rgba, extent=(0, w, h, 0), interpolation="bilinear", zorder=0)
        im.set_clip_path(clip)
    elif code == "dots":
        xs, ys = np.meshgrid(np.arange(9, w, 18), np.arange(9, h, 18))
        sc = ax.scatter(xs.ravel(), ys.ravel(), s=3.4, color=c, alpha=a(0.16), linewidths=0)
        sc.set_clip_path(clip)
    elif code == "grid":
        add([[(x, 0), (x, h)] for x in np.arange(0, w, 28)] + [[(0, y), (w, y)] for y in np.arange(0, h, 28)], 0.08)
    elif code == "ruled":
        add([[(0, y), (w, y)] for y in np.arange(29.5, h, 30)], 0.1)
    elif code == "diagonal":
        add([[(x, 0), (x - h, h)] for x in np.arange(0, w + h, 14)], 0.08, 1.2)
    elif code == "crosshatch":
        add([[(x, 0), (x - h, h)] for x in np.arange(0, w + h, 16)] + [[(x - h, 0), (x, h)] for x in np.arange(0, w + h, 16)], 0.07)
    elif code == "waves":
        t = np.linspace(0, w, int(w / 2))
        add([list(zip(t, y + 6 * np.sin(t / 40 * 2 * np.pi))) for y in np.arange(11, h, 22)], 0.1, 1.2)
    elif code == "topo":
        th = np.linspace(0, 2 * np.pi, 90)
        for cx in np.arange(130, w + 130, 260):
            for cy in np.arange(130, h + 130, 260):
                add([list(zip(cx + r * 1.05 * np.cos(th), cy + r * np.sin(th))) for r in (95, 68, 40, 15)], 0.05, 1.0)


def render_poster(*, title: str, spec: dict, payload: Any, design: dict | None = None, accent: str | None = None,
                  subtitle: str = "", eyebrow: str = "", sources: list[str] | None = None, note: str = "",
                  out_dir: str = VISUAL_DIR) -> str:
    """Render the post's poster as a square PNG. Returns the DB path, e.g. /uploads/visuals/post_<hex>.png"""
    os.makedirs(out_dir, exist_ok=True)
    accent = accent if accent and len(accent) == 7 else ACCENT
    d = design if design and design.get("mode") != "none" else {}
    bg = d.get("bg") if isinstance(d.get("bg"), str) and len(d.get("bg", "")) == 7 else "#f4f2ec"
    dark = _luma(bg) < 0.18
    ink, soft, line = ("#f4f2ee", "#b9b7b2", "#5a5a58") if dark else ("#141414", "#5e5e5e", "#dcd8cf")
    font_code = d.get("font") or "playfair"
    layout = d.get("layout") or {}

    S = IMG_PX
    P = S * POSTER_FRAC                              # poster size (px)
    X0 = Y0 = (S - P) / 2                            # poster corner (top-left, px from the image's top-left)
    pad = P * 0.05
    k = P / 900                                      # web poster ~900 px wide: same proportions

    def off(block):                                  # the editor's offset of a block, in px
        o = layout.get(block) or {}
        return (float(o.get("x", 0)) * P / 100, float(o.get("y", 0)) * P / 100)

    fx = lambda x: x / S                             # px -> figure fraction (y measured from the top)
    fy = lambda y: 1 - y / S
    pt = lambda px: px * 72 / DPI                    # px -> points

    global INK, SOFT, GRID, BG
    with _LOCK:
        saved = (INK, SOFT, GRID, BG)
        INK, SOFT, GRID, BG = ink, soft, line, bg
        try:
            outer = _mix(bg, "#000000", 0.07) if not dark else _mix(bg, "#000000", 0.35)
            fig = Figure(figsize=(S / DPI, S / DPI), dpi=DPI, facecolor=outer)
            FigureCanvasAgg(fig)
            renderer = fig.canvas.get_renderer()

            # poster card (soft shadow, rounded) + texture
            from matplotlib.patches import FancyBboxPatch
            r = 18 * k / S
            fig.add_artist(FancyBboxPatch((fx(X0) + 0.004, fy(Y0 + P) - 0.006), P / S, P / S, transform=fig.transFigure,
                                          boxstyle=f"round,pad=0,rounding_size={r}", facecolor="#000000", alpha=0.10, edgecolor="none",
                                          zorder=-20))
            card = FancyBboxPatch((fx(X0), fy(Y0 + P)), P / S, P / S, transform=fig.transFigure,
                                  boxstyle=f"round,pad=0,rounding_size={r}", facecolor=bg, edgecolor="none", zorder=-10)
            fig.add_artist(card)              # under everything else (figure artists draw by zorder)
            tex = fig.add_axes([fx(X0), fy(Y0 + P), P / S, P / S], zorder=-5)
            tex.set_xlim(0, P); tex.set_ylim(P, 0); tex.axis("off"); tex.set_facecolor("none")
            _texture(tex, d.get("texture") or ("paper" if d else "none"), P, P, dark, card)

            center = d.get("title_align") == "center"
            inner_w = P - 2 * pad

            # header: eyebrow, title, subtitle
            ox, oy = off("title")
            y = Y0 + pad * 1.1 + oy
            xh = (X0 + P / 2 if center else X0 + pad) + ox
            ha = "center" if center else "left"
            mono = FontProperties(family="DejaVu Sans Mono", weight="bold")
            # (no category / region line above the title on the image: the title starts the poster; `eyebrow` is
            # still accepted so existing callers keep working)
            tprop = _title_font(font_code)
            size = 62 * k * float(d.get("title_size") or 1.0)
            text = (title or "Untitled").strip()
            text = text.upper() if font_code in UPPER_FONTS else text
            lines = _wrap(renderer, text, tprop, pt(size), inner_w, 3)
            while len(lines) == 3 and lines[-1].endswith("…") and size > 34 * k:
                size *= 0.9
                lines = _wrap(renderer, text, tprop, pt(size), inner_w, 3)
            for ln in lines:
                fig.text(fx(xh), fy(y), ln, fontproperties=tprop, fontsize=pt(size), color=ink, ha=ha, va="top")
                y += size * 1.12
            y += size * 0.12
            if subtitle:
                y += 8 * k
                sprop = FontProperties(family="DejaVu Sans")
                for ln in _wrap(renderer, subtitle, sprop, pt(18 * k), min(inner_w, 620 * k), 2):
                    fig.text(fx(xh), fy(y), ln, fontproperties=sprop, fontsize=pt(18 * k), color=soft, ha=ha, va="top")
                    y += 25 * k
            header_bottom = Y0 + pad * 1.1 + (y - (Y0 + pad * 1.1 + oy))    # where the header ends without its offset

            # footer: the mark bottom left, note + sources bottom right
            foot_lines = []
            src = "; ".join(s for s in (sources or []) if s)
            text_src = " ".join(t for t in (note or "", f"(Source{'s' if len(sources or []) > 1 else ''}: {src})" if src else "") if t)
            iprop = FontProperties(family="DejaVu Sans", style="italic")
            if text_src:
                foot_lines = _wrap(renderer, text_src, iprop, pt(12.5 * k), inner_w * 0.68, 4)
            foot_h = max(30 * k, len(foot_lines) * 18 * k)
            fb = Y0 + P - pad                                        # footer bottom
            sx, sy = off("sources")
            for i, ln in enumerate(foot_lines):
                fig.text(fx(X0 + P - pad + sx), fy(fb - (len(foot_lines) - 1 - i) * 18 * k + sy), ln,
                         fontproperties=iprop, fontsize=pt(12.5 * k), color=soft, ha="right", va="bottom")
            lx, ly = off("logo")
            logo = os.path.join(ASSET_DIR, "gisviz_logo.png")
            lsize = 30 * k
            if os.path.exists(logo):
                import matplotlib.image as mpimg
                img = mpimg.imread(logo).copy()
                rgb = [int(ink[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                img[..., 0], img[..., 1], img[..., 2] = rgb
                lax = fig.add_axes([fx(X0 + pad + lx), fy(fb + ly), lsize / S, lsize / S])
                lax.imshow(img); lax.axis("off")
            fig.text(fx(X0 + pad + lsize + 6 * k + lx), fy(fb - lsize / 2 + ly), "GISVIZ", fontproperties=mono,
                     fontsize=pt(14 * k), color=ink, ha="left", va="center")

            # the chart / map: 90% of the poster width, between header and footer
            vx, vy = off("visual")
            top = header_bottom + 6 * k
            bottom = fb - foot_h - 20 * k
            cw, ch = P * 0.90, max(120.0, bottom - top)
            cx, cy = X0 + P * 0.05 + vx, top + vy
            gs = fig.add_gridspec(3, 3, left=0, right=1, bottom=0, top=1, wspace=0, hspace=0,
                                  width_ratios=[max(cx, 1), cw, max(S - cx - cw, 1)],
                                  height_ratios=[max(cy, 1), ch, max(S - cy - ch, 1)])
            sub = fig.add_subfigure(gs[1, 1], facecolor="none")
            legend = None
            if spec.get("kind") == "map":
                legend = _draw_map(sub, spec, payload, accent)
                _fill_top(sub, 0.97)       # up to the title block; the colour key sits over the map's top right, as on the page
            else:
                _draw_chart(sub, spec, payload, accent)
                _fill_top(sub, 0.97)

            # map colour key, top right of the map (five steps of the post colour, as on the page)
            if legend:
                gx, gy = off("legend")
                kw, kx, ky = 200 * k, cx + cw - 200 * k + gx, cy + gy
                fig.text(fx(kx), fy(ky), str(legend["field"])[:28], fontproperties=mono, fontsize=pt(11 * k), color=ink, va="top")
                if legend["lo"] is not None:
                    steps = [_mix(accent, "#ffffff", 0.85), _mix(accent, "#ffffff", 0.6), _mix(accent, "#ffffff", 0.3),
                             accent, _mix(accent, "#000000", 0.42)]
                    from matplotlib.patches import Rectangle
                    for i, col in enumerate(steps):
                        fig.add_artist(Rectangle((fx(kx + i * kw / 5), fy(ky + 26 * k)), kw / 5 / S, 8 * k / S,
                                                 transform=fig.transFigure, facecolor=col, edgecolor="none"))
                    lo, hi = legend["lo"], legend["hi"]
                    for frac, v, al in ((0, lo, "left"), (0.5, (lo + hi) / 2, "center"), (1, hi, "right")):
                        fig.text(fx(kx + frac * kw), fy(ky + 32 * k), _fmt(v), fontsize=pt(10.5 * k), color=soft, ha=al, va="top")

            for t in fig.texts:                       # like the page: what is dragged off the poster is cut off
                t.set_clip_path(card)
            name = f"post_{uuid.uuid4().hex}.png"
            fig.savefig(os.path.join(out_dir, name), format="png", dpi=DPI, facecolor=outer, pil_kwargs={"optimize": True})
        finally:
            INK, SOFT, GRID, BG = saved
    return f"/uploads/visuals/{name}"


def render_thumbnail(title: str, subtitle: str, spec: dict, payload: Any, out_dir: str = VISUAL_DIR,
                     accent: str | None = None) -> str:
    """Older call: a plain poster (no design) with this title and subtitle."""
    return render_poster(title=title, subtitle=subtitle, spec=spec, payload=payload, accent=accent or spec.get("accent"),
                         out_dir=out_dir)
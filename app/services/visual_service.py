"""
app/services/visual_service.py
==============================
Dataset search (Postgres catalog), column profiles, visual specs and the chart/map
payloads behind those specs (queried from the dataset's own DuckDB file, table `data`).

Payloads are cached in Redis as ready-to-send JSON (key = dataset + request params), filled
when a post is published and cleared when the dataset's data is re-uploaded.

Safety: dataset ids must match ^[a-z0-9_]{1,64}$ and exist in the catalog, and
every column name from a request is checked against that dataset's profile
before it is placed (quoted) into SQL. Values are always bound parameters.
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import os
import re
from typing import Any, Optional

from app.services import catalog_service as catalog
from app.services import duckdb_service as store
from app.services.cache_service import cache_service
from app.services.viz_recommender import (
    COUNT_PSEUDO, FLOW_VIZ, GRID_VIZ, RAW_VIZ, TIME_MAPS, VIZ_REGISTRY, coord_pairs, default_agg, numeric_candidates, recommend,
)

API_PREFIX = os.getenv("API_VERSION_STR", "/api/v0")
GEOM = "_gv_geom"
ROW_ID = "_gv_id"
MAX_CHART_ROWS = 2000
BAR_TOP_N = 25
TREEMAP_TOP_N = 60
DONUT_TOP_N = 12
STACK_SERIES_N = 8
GRID_N = 30
HIST_BINS = 20
BUBBLE_MAX = 1500
MAX_FEATURES = 20000
PAYLOAD_TTL = int(os.getenv("VISUAL_CACHE_TTL", str(7 * 24 * 3600)))   # seconds; uploads clear it anyway
PARAM_KEYS = ("viz", "x", "y", "z", "size", "label_field", "color", "part")   # part=series: a time map's values per period
COLOR_VIZ = ("hbar", "bar", "lollipop", "parallel_coordinates", "edge_bundling", "ranked_columns")   # can colour marks by a category column
RAW_N = 4000                           # sampled rows for distributions / 2D density
PARALLEL_N = 600                       # lines in parallel coordinates
CORR_MAX = 10                          # columns in a correlogram
FLOW_LINKS = {"sankey": 120, "chord": 150, "network": 300, "arc_diagram": 200, "edge_bundling": 300}
HIER_N = 400                           # parent/child pairs (sunburst, treemap, packing, dendrogram)
GROUPS_N = {"boxplot": 24, "violin": 16, "ridgeline": 24, "density": 6}
SERIES_TIME_N = 600                    # time points in stacked area / streamgraph
CONNECTIONS_N = 3000
COLOR_MAX_GROUPS = 24
RANKING_TOP_N = 80                     # horizontal bars: long rankings (the reader filters labels)
_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class VisualError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_safe(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


# ── catalog ──────────────────────────────────────────────────────────

def _check_id(dataset_id: str) -> str:
    dataset_id = (dataset_id or "").strip().lower()
    if not _ID_RE.match(dataset_id):
        raise VisualError("Invalid dataset id", 400)
    return dataset_id


def search(term: str = "", limit: int = 12) -> list[dict]:
    """Find active datasets (Postgres catalog) by name, id, category or column name."""
    return catalog.search(term, max(1, min(limit, 50)))


def get_card(dataset_id: str, allow_inactive: bool = False) -> dict:
    dataset_id = _check_id(dataset_id)
    card = catalog.get(dataset_id, include_inactive=allow_inactive)
    if not card:
        raise VisualError("Dataset not found", 404)
    return {k: _json_safe(v) for k, v in card.items()}


def get_columns(dataset_id: str) -> list[dict]:
    """Column profile saved on the catalog row at upload time (no DuckDB access)."""
    return catalog.get_columns(dataset_id)


def suggest(dataset_id: str, allow_inactive: bool = False) -> dict:
    card = get_card(dataset_id, allow_inactive)
    cols = get_columns(card["dataset_id"])
    if not cols:      # catalog row exists but the data file has not been uploaded yet
        raise VisualError("This dataset's data is not available on the server yet", 404)
    options = recommend(card, cols)
    if not options:
        raise VisualError("This dataset has no plottable columns", 422)
    return {"dataset": card, "columns": cols, "options": options, "recommended": options[0]["viz"]}


# ── params → validated plan ──────────────────────────────────────────

def plan(dataset_id: str, viz: Optional[str] = None, x: Optional[str] = None,
         y: Optional[str] = None, label_field: Optional[str] = None,
         z: Optional[str] = None, size: Optional[str] = None, allow_inactive: bool = False,
         info: Optional[dict] = None, color: Optional[str] = None) -> dict:
    """Validate a visual request against the dataset and fill missing params from the recommender.
    `info` (card + columns + options, as from suggest()) checks against other data, e.g. a staged upload."""
    info = info or suggest(dataset_id, allow_inactive)
    options = {o["viz"]: o for o in info["options"]}
    explicit = bool(viz)
    viz = viz or info["recommended"]
    if viz not in VIZ_REGISTRY:
        raise VisualError(f"Unknown visual type '{viz}'", 400)
    # The recommender's options only RANK and fill defaults. A visual asked for explicitly (a saved
    # post, a publisher's pick) is valid whenever its columns exist and fit the chart type, checked
    # below; otherwise replacing a file with, say, fewer categories would break posts whose columns
    # are all still there, just because the recommender would no longer suggest that chart first.
    if viz in options:
        params = dict(options[viz]["params"])
    elif explicit:
        params = {}
        if VIZ_REGISTRY[viz]["kind"] == "map" and viz != "connection_map" and not info["dataset"].get("geometry_type"):
            raise VisualError("A map needs data with geometry", 400)
    else:
        raise VisualError(f"'{viz}' is not available for this dataset", 400)

    cols = {c["column_name"]: c for c in info["columns"]}
    x = x or params.get("x")
    y = y or params.get("y")
    z = z or params.get("z")
    size = size or params.get("size")
    label_field = label_field or params.get("label_field")

    for name, val in (("x", x), ("y", y), ("z", z), ("size", size), ("label_field", label_field), ("color", color)):
        if val is not None and val != COUNT_PSEUDO and val not in cols:
            raise VisualError(f"Unknown column '{val}' for {name}", 400)
    if color:
        if viz not in COLOR_VIZ:
            raise VisualError("Colouring by a column is available for bar, lollipop, parallel-coordinates and edge-bundling charts", 400)
        if cols[color]["n_distinct"] > COLOR_MAX_GROUPS and viz != "edge_bundling":
            raise VisualError(f"'{color}' has too many values to colour by (max {COLOR_MAX_GROUPS})", 400)

    meta = VIZ_REGISTRY[viz]
    kind = meta["kind"]
    given = {"x": x, "y": y, "z": z, "size": size}
    for need in meta["needs"]:
        if not given[need]:
            raise VisualError(f"{meta['label']} needs a '{need}' column", 400)

    def is_num(c): return c != COUNT_PSEUDO and cols[c]["dtype"] == "numeric"
    coords = None
    if y == COUNT_PSEUDO and not meta.get("count"):
        raise VisualError(f"{meta['label']} needs a numeric value column, not a row count", 400)
    if kind == "map":
        if viz == "connection_map":
            coords = coord_pairs(info["columns"])
            if not coords:
                raise VisualError("A connection map needs origin and destination coordinate columns "
                                  "(e.g. from_lon, from_lat, to_lon, to_lat)", 400)
        elif y not in (None, COUNT_PSEUDO) and viz in ("bubble_map", "cartogram") and not is_num(y):
            raise VisualError(f"{meta['label']} needs a numeric value", 400)
        if z and viz in TIME_MAPS:
            if not label_field:
                raise VisualError("A map that plays over time needs the place-name column (to join each period)", 400)
            if z == label_field:
                raise VisualError("The time column must differ from the place names", 400)
    else:
        if y != COUNT_PSEUDO and not is_num(y):
            raise VisualError("y must be a numeric column", 400)
        if meta["x"] == "num" and not is_num(x):
            raise VisualError(f"{meta['label']} needs a numeric x column", 400)
        if viz == "bubble" and not is_num(size):
            raise VisualError("Bubble size must be a numeric column", 400)
        if z and z == x and (viz in GRID_VIZ or viz in ("treemap", "circle_packing", "radar", "connected_scatter")):
            raise VisualError("The two columns must differ", 400)
        if viz in RAW_VIZ and viz != "density_2d" and x and cols[x]["dtype"] == "numeric" and cols[x]["n_distinct"] > 60:
            raise VisualError(f"{meta['label']} groups must be a category column", 400)
        if viz == "radar" and x and not 3 <= cols[x]["n_distinct"] <= 30:
            raise VisualError("A radar chart needs 3–30 axes (values of the axes column)", 400)
        if viz == "ranked_columns" and not z and len(numeric_candidates(info["columns"])) < 2:
            raise VisualError("Connected ranking columns need two or more numeric columns (or a column for the groups)", 400)
        if viz in ("correlogram", "parallel_coordinates") and len(numeric_candidates(info["columns"])) < 3:
            raise VisualError(f"{meta['label']} needs at least 3 numeric columns", 400)

    # numeric columns the viewer can flip between (only for grouped-by-x charts and maps)
    if viz in ("correlogram", "parallel_coordinates") or (viz == "ranked_columns" and not z):   # every measure is an axis
        nums = [c["column_name"] for c in numeric_candidates(info["columns"])]
        fields = ([y] if y in nums else []) + [n for n in nums if n != y]
        fields = fields[:CORR_MAX if viz == "correlogram" else 5 if viz == "ranked_columns" else 8]
    elif kind == "map" or meta.get("grouped"):
        fields = [c["column_name"] for c in numeric_candidates(info["columns"]) if c["column_name"] != x]
        if y != COUNT_PSEUDO and y in fields:
            fields.remove(y)
        fields = ([y] if y else []) + fields[:11]
    else:
        fields = [y]

    # chart types that can share this chart's rows (same x), so the post page can flip between them
    if meta.get("grouped"):
        allowed = [v for v, o in options.items()
                   if VIZ_REGISTRY[v].get("grouped") and o["params"].get("x") == x]
        if viz not in allowed:
            allowed.insert(0, viz)
    else:
        allowed = [viz] if kind == "chart" else []

    return {"dataset": info["dataset"], "columns": cols, "viz": viz, "kind": kind,
            "x": x, "y": y, "z": z, "size": size, "label_field": label_field, "color": color if color != x else None,
            "fields": fields, "allowed_types": allowed, "coords": coords}


def data_url(dataset_id: str, p: dict) -> str:
    from urllib.parse import urlencode
    qs = {"viz": p["viz"], "y": p["y"]}
    for k in ("x", "z", "size", "label_field", "color"):
        if p.get(k):
            qs[k] = p[k]
    return f"{API_PREFIX}/visuals/datasets/{dataset_id}/data?{urlencode(qs)}"


def clean_accent(accent: Optional[str]) -> Optional[str]:
    if not accent:
        return None
    if not _HEX_RE.match(accent):
        raise VisualError("Theme colour must look like #1a2b3c", 400)
    return accent.lower()


def build_spec(dataset_id: str, viz=None, x=None, y=None, label_field=None, accent=None,
               z=None, size=None, allow_inactive: bool = False, color=None, story: Optional[dict] = None) -> dict:
    """The `visual_spec` JSON that the frontend's <InteractiveVisual> renders. One visual per post:
    the chart type is fixed when publishing (no switching on the post page). `story` holds the
    presentation choices: title, subtitle, y_label, colors, reference_lines, filter_default,
    visual_type (catalog code)."""
    spec = _build_spec(dataset_id, viz, x, y, label_field, z, size, allow_inactive, color, story or {})
    accent = clean_accent(accent)
    if accent:
        spec["accent"] = accent          # post theme colour
    return spec


# charts whose readers get the label dropdown (filtering the x labels, or the z series for SERIES_FILTER)
LABEL_FILTER_VIZ = ("ranked_columns", "bar", "hbar", "line", "area", "donut", "pie", "treemap", "stacked", "heatmap", "lollipop",
                    "circular_bar", "word_cloud", "radar", "boxplot", "violin", "ridgeline", "density", "sunburst",
                    "circle_packing", "dendrogram", "stacked_area", "streamgraph", "connected_scatter",
                    "sankey", "chord", "network", "arc_diagram", "edge_bundling")
SERIES_FILTER = ("stacked_area", "streamgraph", "radar", "connected_scatter")
REF_VIZ = ("bar", "hbar", "lollipop")
MAP_STYLE = {"map_heat": "heat", "bubble_map": "bubble", "hexbin_map": "hexbin", "cartogram": "cartogram",
             "connection_map": "connection"}


def _text(v, n: int) -> Optional[str]:
    v = (str(v) if v is not None else "").strip()
    return v[:n] or None


def _story(story: dict, p: dict) -> dict:
    """Validated presentation options (all optional)."""
    out = {}
    for k, n in (("title", 160), ("subtitle", 300), ("y_label", 60)):
        t = _text(story.get(k), n)
        if t:
            out[k] = t
    if p.get("color"):
        out["color_by"] = p["color"]
        colors = story.get("colors") or {}
        if not isinstance(colors, dict) or len(colors) > COLOR_MAX_GROUPS:
            raise VisualError("colors must map up to 24 groups to #rrggbb colours", 400)
        clean = {}
        for g, c in colors.items():
            if isinstance(c, str) and _HEX_RE.match(c):          # anything else is ignored
                clean[str(g)[:80]] = c.lower()
        if clean:
            out["colors"] = clean
    lines = story.get("reference_lines") or []
    if not isinstance(lines, list) or len(lines) > 3:
        raise VisualError("Up to 3 reference lines", 400)
    refs = []
    for r in lines:
        if not isinstance(r, dict):
            continue
        ref = {"label": _text(r.get("label"), 60) or "", "mode": "divider" if r.get("mode") == "divider" else "line"}
        if r.get("stat") in ("mean", "median"):
            ref["stat"] = r["stat"]
        elif isinstance(r.get("value"), (int, float)):
            ref["value"] = float(r["value"])
        else:
            raise VisualError("A reference line needs a value or a statistic (mean / median)", 400)
        refs.append(ref)
    if refs:
        if p["viz"] not in REF_VIZ:
            raise VisualError("Reference lines are available for bar and lollipop charts", 400)
        out["reference_lines"] = refs
    # labels the reader can filter by (dropdown on the post page); the publisher may pre-select some
    field = p["x"] if p["kind"] == "chart" else (p["label_field"] or None)
    if p["kind"] == "chart" and p["viz"] in SERIES_FILTER and p.get("z"):
        field = p["z"]                         # time on x: readers pick the series instead
    if field and field != COUNT_PSEUDO and p["kind"] == "chart" and p["viz"] in LABEL_FILTER_VIZ \
            or (p["kind"] == "map" and field):
        default = story.get("filter_default")
        if default is not None and (not isinstance(default, list) or len(default) > 200):
            raise VisualError("filter_default must be a list of labels", 400)
        out["label_filter"] = {"field": field, "default": [str(v)[:120] for v in default] if default else None}
    return out


def resolve_visual_type(viz: str, card: dict, y: Optional[str], requested: Optional[str] = None) -> str:
    """The catalog code (misc DB visual_types) behind a renderer code, e.g. map + polygons -> choropleth.
    Falls back to the renderer code when the catalog has not been seeded."""
    try:
        from app.db.database import MiscSessionLocal
        from app.db.models import VisualTypeRecord
        with MiscSessionLocal() as db:
            types = [(t.code, t.legacy_code or t.code) for t in db.query(VisualTypeRecord)
                     .filter(VisualTypeRecord.enabled == 1).all()]
    except Exception:
        types = []
    if not types:
        return requested or viz
    by_code = dict(types)
    if requested and by_code.get(requested) == viz:
        return requested
    # a catalog code that now has its own renderer (e.g. bubble_map used to be drawn by "map"): pick again
    candidates = [c for c, r in types if r == viz]
    if len(candidates) <= 1:
        return candidates[0] if candidates else viz
    g = (card.get("geometry_type") or "").lower()
    prefer = ("choropleth", "bubble_map") if "polygon" in g else ("line_map",) if "line" in g \
        else (("bubble_map", "point_map") if y and y != COUNT_PSEUDO else ("point_map", "bubble_map"))
    return next((c for c in prefer if c in candidates), candidates[0])


def _build_spec(dataset_id: str, viz=None, x=None, y=None, label_field=None, z=None, size=None,
                allow_inactive: bool = False, color=None, story: Optional[dict] = None) -> dict:
    story = story or {}
    p = plan(dataset_id, viz, x, y, label_field, z, size, allow_inactive, color=color)
    url = data_url(p["dataset"]["dataset_id"], p)
    vtype = resolve_visual_type(p["viz"], p["dataset"], p["y"], story.get("visual_type"))
    if p["kind"] == "map":
        label = p["label_field"] or (p["x"] if p["viz"] == "connection_map" else None) or p["y"]
        spec = {"kind": "map", "visual_type": vtype, "map_style": MAP_STYLE.get(p["viz"], "auto"),
                "data": url, "value_field": p["y"] if p["viz"] != "connection_map" else "weight",
                "label_field": label, "fields": [p["y"] if p["viz"] != "connection_map" else "weight"]}
        if p["viz"] == "connection_map" and p.get("z"):
            spec["target_field"] = p["z"]
        if p.get("z") and p["viz"] in TIME_MAPS:          # video map: geometry once + values per period, play button
            spec["time"] = {"field": p["z"], "join": p["label_field"], "series": url + "&part=series",
                            "start": "first", "interval_ms": 900}
            spec["fields"] = [f for f in p["fields"] if f != p["z"]][:12] or [p["y"]]
    else:
        spec = {"kind": "chart", "visual_type": vtype, "chart_type": p["viz"], "data": url, "x": p["x"], "y": p["y"],
                "fields": [p["y"]], "allowed_types": [p["viz"]]}        # one visual: no switching on the post page
        if p.get("z"):
            spec["z"] = p["z"]
        if p.get("size"):
            spec["size"] = p["size"]
        if p["viz"] in ("correlogram", "parallel_coordinates", "ranked_columns"):
            spec["fields"] = p["fields"]       # every measure is an axis / a column
        if p.get("label_field") and p["viz"] in ("scatter", "bubble"):
            spec["label_field"] = p["label_field"]
    spec.update(_story(story, p))
    return spec


# ── payloads ─────────────────────────────────────────────────────────

def _agg(col: str) -> str:
    return "count(*)" if col == COUNT_PSEUDO else f"{default_agg(col)}({q(col)})"


def chart_rows(p: dict) -> list[dict]:
    """Rows for a chart. Shape depends on the type (see each branch)."""
    ds, cols, viz = p["dataset"]["dataset_id"], p["columns"], p["viz"]
    x, y, z, fields = p["x"], p["y"], p.get("z"), p["fields"]
    table = "data"

    def run(sql, params=()):
        return store.query(ds, sql, params)

    extra = _extra_rows(p, run)
    if extra is not None:
        return [{k: _json_safe(v) for k, v in r.items()} for r in extra]

    if viz == "scatter":                       # raw rows: x, y
        sel = ", ".join(q(c) for c in [x] + [f for f in fields if f != x])
        rows = run(f"SELECT {sel} FROM {table} WHERE {q(x)} IS NOT NULL AND {q(y)} IS NOT NULL LIMIT {MAX_CHART_ROWS}")

    elif viz == "bubble":                      # raw rows: x, y, size (+ a label when there is one)
        keep = [x, y, p["size"]] + ([p["label_field"]] if p.get("label_field") else [])
        keep = list(dict.fromkeys(keep))
        rows = run(
            f"SELECT {', '.join(q(c) for c in keep)} FROM {table} "
            f"WHERE {q(x)} IS NOT NULL AND {q(y)} IS NOT NULL AND {q(p['size'])} IS NOT NULL LIMIT {BUBBLE_MAX}")

    elif viz == "histogram":                   # rows: bin, x0, x1, count
        lo, hi, n = run(
            f"SELECT min({q(y)})::DOUBLE AS lo, max({q(y)})::DOUBLE AS hi, count({q(y)}) AS n FROM {table}")[0].values()
        if not n or lo is None:
            return []
        bins = HIST_BINS if hi != lo else 1
        width = (hi - lo) / bins or 1.0
        counts = {int(r["b"]): int(r["c"]) for r in run(
            f"SELECT least(floor(({q(y)} - ?) / ?), ?)::INTEGER AS b, count(*) AS c FROM {table} "
            f"WHERE {q(y)} IS NOT NULL GROUP BY 1", [lo, width, bins - 1])}
        rows = []
        for i in range(bins):
            a, b = lo + i * width, lo + (i + 1) * width
            rows.append({"bin": f"{a:.3g}–{b:.3g}", "x0": a, "x1": b, "count": counts.get(i, 0)})

    elif viz in ("stacked", "heatmap"):        # rows: x, z, y (long format)
        nx = GRID_N if viz == "heatmap" else BAR_TOP_N
        nz = GRID_N if viz == "heatmap" else STACK_SERIES_N
        top = lambda col, n: [r["k"] for r in run(
            f"SELECT {q(col)} AS k FROM {table} WHERE {q(col)} IS NOT NULL "
            f"GROUP BY 1 ORDER BY {_agg(y)} DESC NULLS LAST LIMIT {n}")]
        xs, zs = top(x, nx), top(z, nz)
        if not xs or not zs:
            return []
        ph_x, ph_z = ", ".join("?" * len(xs)), ", ".join("?" * len(zs))
        rows = run(
            f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({_agg(y)}, 4) AS {q(y if y != COUNT_PSEUDO else COUNT_PSEUDO)} "
            f"FROM {table} WHERE {q(x)} IN ({ph_x}) AND {q(z)} IN ({ph_z}) GROUP BY 1, 2", xs + zs)

    elif y == COUNT_PSEUDO:                    # grouped: count rows per x
        limit = GROUPED_LIMITS.get(viz, BAR_TOP_N)
        col = f", mode({q(p['color'])}) AS {q(p['color'])}" if p.get("color") else ""
        rows = run(
            f"SELECT {q(x)} AS {q(x)}, count(*)::BIGINT AS {q(COUNT_PSEUDO)}{col} FROM {table} "
            f"WHERE {q(x)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}")

    else:                                      # grouped: one row per x, every switchable field aggregated
        aggs = ", ".join(f"round({default_agg(f)}({q(f)}), 4) AS {q(f)}" for f in fields)
        if p.get("color"):                     # the group of each bar (e.g. continent of a country)
            aggs += f", mode({q(p['color'])}) AS {q(p['color'])}"
        xinfo = cols[x]
        ordered_by_value = xinfo["dtype"] == "temporal" and xinfo["sql_type"] not in ("VARCHAR",)
        if viz in GROUPED_LIMITS or viz == "bar":
            order = f"{default_agg(y)}({q(y)}) DESC NULLS LAST"
            limit = GROUPED_LIMITS.get(viz, BAR_TOP_N)
        else:                                  # line / area keep the natural order
            order = q(x) if ordered_by_value else f"min({ROW_ID})"
            limit = MAX_CHART_ROWS
        rows = run(
            f"SELECT {q(x)} AS {q(x)}, {aggs} FROM {table} WHERE {q(x)} IS NOT NULL "
            f"GROUP BY {q(x)} ORDER BY {order} LIMIT {limit}")
    return [{k: _json_safe(v) for k, v in r.items()} for r in rows]


GROUPED_LIMITS = {"donut": DONUT_TOP_N, "pie": 8, "treemap": TREEMAP_TOP_N, "hbar": RANKING_TOP_N, "lollipop": 40,
                  "circular_bar": 60, "word_cloud": 120, "circle_packing": 80, "radar": 12}


def _ordered_x(p: dict) -> str:
    """ORDER BY for an x that is a time / order axis: by value when it is a real date or number, else file order."""
    xi = p["columns"][p["x"]]
    by_value = xi["dtype"] in ("temporal", "numeric") and xi.get("sql_type") not in ("VARCHAR",)
    return q(p["x"]) if by_value else f"min({ROW_ID})"


def _extra_rows(p: dict, run) -> Optional[list[dict]]:
    """Rows for the data-to-viz types beyond the classic charts (None = not one of them). Shapes:
      density / boxplot / violin / ridgeline   raw sample  {[x]: group?, [y]: number}
      density_2d                               raw sample  {[x]: number, [y]: number}
      correlogram                              {a, b, r}   Pearson r for every pair of measures
      connected_scatter                        {[z]: order, [x]: avg, [y]: avg} in order of z
      parallel_coordinates                     raw sample  {[field]: number..., [color]?}
      radar / treemap / circle_packing + z,
      sunburst / dendrogram, stacked_area / streamgraph, flows (sankey, chord, network, arc, bundling)
                                               long format {[x], [z], [y]} (+ [color] for bundling)"""
    viz, x, y, z = p["viz"], p["x"], p["y"], p.get("z")
    agg = _agg(y)
    yname = y if y != COUNT_PSEUDO else COUNT_PSEUDO
    sample = f"USING SAMPLE reservoir({RAW_N} ROWS) REPEATABLE (7)"

    if viz in ("density", "boxplot", "violin", "ridgeline"):
        if not x:
            return run(f"SELECT {q(y)} FROM (SELECT {q(y)} FROM data WHERE {q(y)} IS NOT NULL) {sample}")
        groups = [r["k"] for r in run(f"SELECT {q(x)} AS k FROM data WHERE {q(x)} IS NOT NULL AND {q(y)} IS NOT NULL "
                                       f"GROUP BY 1 ORDER BY count(*) DESC LIMIT {GROUPS_N[viz]}")]
        if not groups:
            return []
        ph = ", ".join("?" * len(groups))
        return run(f"SELECT {q(x)}, {q(y)} FROM (SELECT {q(x)}, {q(y)} FROM data WHERE {q(y)} IS NOT NULL "
                   f"AND {q(x)} IN ({ph})) {sample}", groups)

    if viz == "density_2d":
        return run(f"SELECT {q(x)}, {q(y)} FROM (SELECT {q(x)}, {q(y)} FROM data WHERE {q(x)} IS NOT NULL "
                   f"AND {q(y)} IS NOT NULL) {sample}")

    if viz == "correlogram":
        f = p["fields"]
        pairs = [(a, b) for i, a in enumerate(f) for b in f[i + 1:]]
        if not pairs:
            return []
        sel = ", ".join(f"corr({q(a)}::DOUBLE, {q(b)}::DOUBLE) AS c{i}" for i, (a, b) in enumerate(pairs))
        got = run(f"SELECT {sel} FROM data")[0]
        out = [{"a": a, "b": a, "r": 1.0} for a in f]
        for i, (a, b) in enumerate(pairs):
            r = got[f"c{i}"]
            out += [{"a": a, "b": b, "r": r}, {"a": b, "b": a, "r": r}]
        return out

    if viz == "connected_scatter":
        return run(f"SELECT {q(z)} AS {q(z)}, round(avg({q(x)}), 4) AS {q(x)}, round(avg({q(y)}), 4) AS {q(y)} "
                   f"FROM data WHERE {q(z)} IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT {MAX_CHART_ROWS}")

    if viz == "ranked_columns":
        if z:                                  # long: item, column, value
            return run(f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({agg}, 4) AS {q(yname)} FROM data "
                       f"WHERE {q(x)} IS NOT NULL AND {q(z)} IS NOT NULL GROUP BY 1, 2 LIMIT 2000")
        aggs = ", ".join(f"round({default_agg(f)}({q(f)}), 4) AS {q(f)}" for f in p["fields"])
        grp = f", mode({q(p['color'])}) AS {q(p['color'])}" if p.get("color") else ""
        return run(f"SELECT {q(x)} AS {q(x)}, {aggs}{grp} FROM data WHERE {q(x)} IS NOT NULL GROUP BY 1 LIMIT 400")

    if viz == "parallel_coordinates":
        keep = list(dict.fromkeys(p["fields"] + ([p["color"]] if p.get("color") else [])))
        where = " AND ".join(f"{q(c)} IS NOT NULL" for c in p["fields"])
        return run(f"SELECT {', '.join(q(c) for c in keep)} FROM (SELECT {', '.join(q(c) for c in keep)} FROM data "
                   f"WHERE {where}) USING SAMPLE reservoir({PARALLEL_N} ROWS) REPEATABLE (7)")

    two_level = viz in ("sunburst", "dendrogram") or (viz in ("treemap", "circle_packing", "radar") and z)
    if two_level:
        if viz == "radar":                     # axes x (top 12), series z (top 5)
            xs = [r["k"] for r in run(f"SELECT {q(x)} AS k FROM data WHERE {q(x)} IS NOT NULL GROUP BY 1 ORDER BY {agg} DESC NULLS LAST LIMIT 12")]
            zs = [r["k"] for r in run(f"SELECT {q(z)} AS k FROM data WHERE {q(z)} IS NOT NULL GROUP BY 1 ORDER BY {agg} DESC NULLS LAST LIMIT 5")]
            if not xs or not zs:
                return []
            return run(f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({agg}, 4) AS {q(yname)} FROM data "
                       f"WHERE {q(x)} IN ({', '.join('?' * len(xs))}) AND {q(z)} IN ({', '.join('?' * len(zs))}) GROUP BY 1, 2", xs + zs)
        return run(f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({agg}, 4) AS {q(yname)} FROM data "
                   f"WHERE {q(x)} IS NOT NULL AND {q(z)} IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC NULLS LAST LIMIT {HIER_N}")

    if viz in ("stacked_area", "streamgraph"):
        zs = [r["k"] for r in run(f"SELECT {q(z)} AS k FROM data WHERE {q(z)} IS NOT NULL GROUP BY 1 "
                                  f"ORDER BY {agg} DESC NULLS LAST LIMIT {STACK_SERIES_N}")]
        if not zs:
            return []
        ph = ", ".join("?" * len(zs))
        xs = [r["k"] for r in run(f"SELECT {q(x)} AS k FROM data WHERE {q(x)} IS NOT NULL GROUP BY 1 "
                                  f"ORDER BY {_ordered_x(p)} LIMIT {SERIES_TIME_N}")]
        if not xs:
            return []
        rows = run(f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({agg}, 4) AS {q(yname)} FROM data "
                   f"WHERE {q(z)} IN ({ph}) AND {q(x)} IN ({', '.join('?' * len(xs))}) GROUP BY 1, 2", zs + xs)
        order = {str(v): i for i, v in enumerate(xs)}
        return sorted(rows, key=lambda r: order.get(str(r[x]), 0))

    if viz in FLOW_VIZ:
        grp = f", mode({q(p['color'])}) AS {q(p['color'])}" if p.get("color") else ""
        return run(f"SELECT {q(x)} AS {q(x)}, {q(z)} AS {q(z)}, round({agg}, 4) AS {q(yname)}{grp} FROM data "
                   f"WHERE {q(x)} IS NOT NULL AND {q(z)} IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC NULLS LAST "
                   f"LIMIT {FLOW_LINKS[viz]}")
    return None


def _great_circle(lon1, lat1, lon2, lat2, n: int = 32) -> list[list[float]]:
    """Points along the shortest path on the globe; longitudes unwrapped so a line across 180° stays continuous."""
    import math
    r = math.radians
    p1 = (math.cos(r(lat1)) * math.cos(r(lon1)), math.cos(r(lat1)) * math.sin(r(lon1)), math.sin(r(lat1)))
    p2 = (math.cos(r(lat2)) * math.cos(r(lon2)), math.cos(r(lat2)) * math.sin(r(lon2)), math.sin(r(lat2)))
    d = math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(p1, p2)))))
    if d < 1e-9:
        return [[lon1, lat1], [lon2, lat2]]
    out, prev = [], None
    for i in range(n + 1):
        t = i / n
        a, b = math.sin((1 - t) * d) / math.sin(d), math.sin(t * d) / math.sin(d)
        xx, yy, zz = (a * p1[k] + b * p2[k] for k in range(3))
        lon, lat = math.degrees(math.atan2(yy, xx)), math.degrees(math.atan2(zz, math.hypot(xx, yy)))
        if prev is not None:
            while lon - prev > 180: lon -= 360
            while lon - prev < -180: lon += 360
        out.append([round(lon, 4), round(lat, 4)]); prev = lon
    return out


def connection_geojson(p: dict) -> dict:
    """Connection map: one curved LineString per origin -> destination row (great circle), heaviest first.
    Properties: the origin / destination names (x / z when given) and the weight (y)."""
    ds, c, x, z, y = p["dataset"]["dataset_id"], p["coords"], p.get("x"), p.get("z"), p["y"]
    keys = [c["from_lon"], c["from_lat"], c["to_lon"], c["to_lat"]]
    group = [k for k in (x, z) if k] + keys
    w = "count(*)" if y == COUNT_PSEUDO else f"sum({q(y)})"
    rows = store.query(ds, f"SELECT {', '.join(q(k) for k in group)}, {w} AS __w FROM data "
                           f"WHERE {' AND '.join(f'{q(k)} IS NOT NULL' for k in keys)} "
                           f"GROUP BY {', '.join(q(k) for k in group)} ORDER BY __w DESC NULLS LAST LIMIT {CONNECTIONS_N}")
    feats = []
    for i, r in enumerate(rows):
        props = {"weight": _json_safe(r["__w"]), y if y != COUNT_PSEUDO else "count": _json_safe(r["__w"])}
        if x: props[x] = _json_safe(r[x])
        if z: props[z] = _json_safe(r[z])
        line = _great_circle(*(float(r[k]) for k in keys))
        feats.append({"type": "Feature", "id": i, "geometry": {"type": "LineString", "coordinates": line}, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def map_series(p: dict) -> list[dict]:
    """A time map's values: one row per place per period, {[label_field], [z], [field]...}, in time order."""
    ds, lab, t = p["dataset"]["dataset_id"], p["label_field"], p["z"]
    keep = [f for f in dict.fromkeys(p["fields"] + [p["y"]]) if f and f in p["columns"] and f not in (lab, t)]
    aggs = "".join(f", round({default_agg(f)}({q(f)}), 4) AS {q(f)}" for f in keep)
    rows = store.query(ds, f"SELECT {q(lab)} AS {q(lab)}, {q(t)} AS {q(t)}{aggs} FROM data "
                           f"WHERE {q(lab)} IS NOT NULL AND {q(t)} IS NOT NULL GROUP BY 1, 2 ORDER BY 2, 1 LIMIT {MAX_FEATURES * 4}")
    return [{k: _json_safe(v) for k, v in r.items()} for r in rows]


def map_geojson(p: dict) -> dict:
    """GeoJSON FeatureCollection with the label and every plottable numeric column as properties.
    A time map (z = time column) gets each place once (its latest row); the values per period come from map_series."""
    ds = p["dataset"]["dataset_id"]
    if p.get("z") and p["viz"] in TIME_MAPS and p.get("label_field"):
        lab, t = p["label_field"], p["z"]
        keep = [c for c in dict.fromkeys([lab] + p["fields"] + [p["y"]]) if c and c in p["columns"] and c != t]
        sel = ", ".join(q(c) for c in keep + [GEOM])
        rows = store.query(ds, f"SELECT {sel} FROM (SELECT *, row_number() OVER (PARTITION BY {q(lab)} ORDER BY {q(t)} DESC) AS __n "
                               f"FROM data WHERE {q(GEOM)} IS NOT NULL) WHERE __n = 1 LIMIT {MAX_FEATURES}")
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": i, "geometry": json.loads(r.pop(GEOM)), "properties": {k: _json_safe(v) for k, v in r.items()}}
            for i, r in enumerate(rows)]}
    keep = []
    for c in ([p["label_field"]] if p["label_field"] else []) + p["fields"] + [p["y"]]:
        if c and c in p["columns"] and c not in keep:
            keep.append(c)
    sel = ", ".join(q(c) for c in keep + [GEOM])
    rows = store.query(ds, f"SELECT {sel} FROM data WHERE {q(GEOM)} IS NOT NULL LIMIT {MAX_FEATURES}")
    feats = []
    for i, r in enumerate(rows):
        feats.append({"type": "Feature", "id": i, "geometry": json.loads(r.pop(GEOM)),
                      "properties": {k: _json_safe(v) for k, v in r.items()}})
    return {"type": "FeatureCollection", "features": feats}


def payload(dataset_id: str, viz=None, x=None, y=None, label_field=None, z=None, size=None,
            allow_inactive: bool = False, color=None, part=None) -> Any:
    p = plan(dataset_id, viz, x, y, label_field, z, size, allow_inactive, color=color)
    if part == "series":
        if not (p["kind"] == "map" and p.get("z") and p["viz"] in TIME_MAPS):
            raise VisualError("Only a map with a time column has a series", 400)
        return map_series(p)
    if p["viz"] == "connection_map":
        return connection_geojson(p)
    return map_geojson(p) if p["kind"] == "map" else chart_rows(p)


# ── Redis-cached payloads ────────────────────────────────────────────

def _norm(params: dict) -> dict:
    return {k: str(params[k]) for k in PARAM_KEYS if params.get(k) not in (None, "")}


def cache_key(dataset_id: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps(_norm(params), sort_keys=True).encode()).hexdigest()[:20]
    return f"gisviz:viz:{dataset_id}:{h}"


def payload_json(dataset_id: str, params: dict, allow_inactive: bool = False) -> tuple[str, bool]:
    """JSON text for a chart/map request and whether it came from Redis.

    Hit: one Redis GET, no Postgres and no DuckDB. Miss: validate, query, serialise, store."""
    dataset_id = _check_id(dataset_id)
    key = cache_key(dataset_id, params)
    hit = cache_service.get_raw(key)
    if hit is not None:
        return hit, True
    p = _norm(params)
    body = json.dumps(payload(dataset_id, **{k: p.get(k) for k in PARAM_KEYS}, allow_inactive=allow_inactive),
                      separators=(",", ":"))
    cache_service.set_raw(key, body, PAYLOAD_TTL)
    return body, False


def info_for(card: dict, columns: list[dict]) -> dict:
    """suggest()-shaped info for a card + column profile that is not live yet (a staged upload)."""
    options = recommend(card, columns)
    return {"dataset": card, "columns": columns, "options": options,
            "recommended": options[0]["viz"] if options else None}


def spec_problem(dataset_id: str, spec: Optional[dict], info: dict) -> Optional[str]:
    """Why a saved visual_spec would not render against `info`'s data (None when it still works)."""
    if not spec or not spec.get("data"):
        return None
    if not info["options"]:
        return "the new data has no plottable columns"
    p = params_from_spec(spec)
    try:
        plan(dataset_id, p.get("viz"), p.get("x"), p.get("y"), p.get("label_field"), p.get("z"), p.get("size"),
             allow_inactive=True, info=info, color=p.get("color"))
    except VisualError as e:
        return e.message
    return None


def params_from_spec(spec: dict) -> dict:
    """The request params the frontend will send for this spec (parsed from its data URL)."""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(spec["data"]).query)
    return {k: v[0] for k, v in qs.items() if k in PARAM_KEYS}


def warm(dataset_id: str, spec: dict, allow_inactive: bool = False) -> Any:
    """Fill the cache for a post's visual (called on publish/edit) and return the payload."""
    body, _ = payload_json(dataset_id, params_from_spec(spec), allow_inactive)
    return json.loads(body)


def invalidate(dataset_id: str) -> int:
    """Drop every cached payload of a dataset (called after its data is re-uploaded)."""
    return cache_service.delete_pattern(f"gisviz:viz:{_check_id(dataset_id)}:*")
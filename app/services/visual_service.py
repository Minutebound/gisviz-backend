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
    COUNT_PSEUDO, VIZ_REGISTRY, default_agg, numeric_candidates, recommend,
)

API_PREFIX = os.getenv("API_V0_STR", "/api/v0")
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
PARAM_KEYS = ("viz", "x", "y", "z", "size", "label_field")
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
         z: Optional[str] = None, size: Optional[str] = None, allow_inactive: bool = False) -> dict:
    """Validate a visual request against the dataset and fill missing params from the recommender."""
    info = suggest(dataset_id, allow_inactive)
    options = {o["viz"]: o for o in info["options"]}
    viz = viz or info["recommended"]
    if viz not in VIZ_REGISTRY:
        raise VisualError(f"Unknown visual type '{viz}'", 400)
    if viz not in options:
        raise VisualError(f"'{viz}' is not available for this dataset", 400)

    cols = {c["column_name"]: c for c in info["columns"]}
    params = dict(options[viz]["params"])
    x = x or params.get("x")
    y = y or params.get("y")
    z = z or params.get("z")
    size = size or params.get("size")
    label_field = label_field or params.get("label_field")

    for name, val in (("x", x), ("y", y), ("z", z), ("size", size), ("label_field", label_field)):
        if val is not None and val != COUNT_PSEUDO and val not in cols:
            raise VisualError(f"Unknown column '{val}' for {name}", 400)

    meta = VIZ_REGISTRY[viz]
    kind = meta["kind"]
    given = {"x": x, "y": y, "z": z, "size": size}
    for need in meta["needs"]:
        if not given[need]:
            raise VisualError(f"{meta['label']} needs a '{need}' column", 400)

    def is_num(c): return c != COUNT_PSEUDO and cols[c]["dtype"] == "numeric"
    if kind == "map":
        pass
    else:
        if y != COUNT_PSEUDO and not is_num(y):
            raise VisualError("y must be a numeric column", 400)
        if viz in ("scatter", "bubble") and not is_num(x):
            raise VisualError(f"{meta['label']} needs a numeric x column", 400)
        if viz == "bubble" and not is_num(size):
            raise VisualError("Bubble size must be a numeric column", 400)
        if viz == "stacked" and z == x:
            raise VisualError("The series column must differ from x", 400)
        if viz == "heatmap" and z == x:
            raise VisualError("A heatmap needs two different columns", 400)

    # numeric columns the viewer can flip between (only for grouped-by-x charts and maps)
    if kind == "map" or meta.get("grouped"):
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
            "x": x, "y": y, "z": z, "size": size, "label_field": label_field,
            "fields": fields, "allowed_types": allowed}


def data_url(dataset_id: str, p: dict) -> str:
    from urllib.parse import urlencode
    qs = {"viz": p["viz"], "y": p["y"]}
    for k in ("x", "z", "size", "label_field"):
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
               z=None, size=None, allow_inactive: bool = False) -> dict:
    """The `visual_spec` JSON that the frontend's <InteractiveVisual> renders."""
    spec = _build_spec(dataset_id, viz, x, y, label_field, z, size, allow_inactive)
    accent = clean_accent(accent)
    if accent:
        spec["accent"] = accent          # post theme colour
    return spec


def _build_spec(dataset_id: str, viz=None, x=None, y=None, label_field=None, z=None, size=None,
                allow_inactive: bool = False) -> dict:
    p = plan(dataset_id, viz, x, y, label_field, z, size, allow_inactive)
    url = data_url(p["dataset"]["dataset_id"], p)
    if p["kind"] == "map":
        return {"kind": "map", "map_style": "heat" if p["viz"] == "map_heat" else "auto", "data": url,
                "value_field": p["y"], "label_field": p["label_field"] or p["y"],
                "fields": p["fields"] or [p["y"]]}
    spec = {"kind": "chart", "chart_type": p["viz"], "data": url, "x": p["x"], "y": p["y"],
            "fields": p["fields"], "allowed_types": p["allowed_types"]}
    if p.get("z"):
        spec["z"] = p["z"]
    if p.get("size"):
        spec["size"] = p["size"]
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
        limit = {"donut": DONUT_TOP_N, "treemap": TREEMAP_TOP_N}.get(viz, BAR_TOP_N)
        rows = run(
            f"SELECT {q(x)} AS {q(x)}, count(*)::BIGINT AS {q(COUNT_PSEUDO)} FROM {table} "
            f"WHERE {q(x)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}")

    else:                                      # grouped: one row per x, every switchable field aggregated
        aggs = ", ".join(f"round({default_agg(f)}({q(f)}), 4) AS {q(f)}" for f in fields)
        xinfo = cols[x]
        ordered_by_value = xinfo["dtype"] == "temporal" and xinfo["sql_type"] not in ("VARCHAR",)
        if viz in ("bar", "hbar", "donut", "treemap"):
            order = f"{default_agg(y)}({q(y)}) DESC NULLS LAST"
            limit = {"donut": DONUT_TOP_N, "treemap": TREEMAP_TOP_N}.get(viz, BAR_TOP_N)
        else:                                  # line / area keep the natural order
            order = q(x) if ordered_by_value else f"min({ROW_ID})"
            limit = MAX_CHART_ROWS
        rows = run(
            f"SELECT {q(x)} AS {q(x)}, {aggs} FROM {table} WHERE {q(x)} IS NOT NULL "
            f"GROUP BY {q(x)} ORDER BY {order} LIMIT {limit}")
    return [{k: _json_safe(v) for k, v in r.items()} for r in rows]


def map_geojson(p: dict) -> dict:
    """GeoJSON FeatureCollection with the label and every plottable numeric column as properties."""
    ds = p["dataset"]["dataset_id"]
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
            allow_inactive: bool = False) -> Any:
    p = plan(dataset_id, viz, x, y, label_field, z, size, allow_inactive)
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
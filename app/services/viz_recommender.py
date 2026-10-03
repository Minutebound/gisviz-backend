"""
app/services/viz_recommender.py
===============================
Decides which visual suits a dataset. Pure functions, no database access:
it works from the column profile computed when the dataset was loaded
(see app/services/dataset_ingest.py), so a suggestion costs microseconds.

Add a new visual type by adding one entry to VIZ_REGISTRY, one rule in
`recommend()`, and a renderer on the frontend.
"""
from __future__ import annotations

import re
from typing import Optional

# kind: chart -> drawn with D3, map -> drawn with MapLibre.
# needs: params the visual cannot render without. grouped: rows are one-per-x, so the viewer
# can flip between these types on the post page without refetching.
VIZ_REGISTRY: dict[str, dict] = {
    "map":       {"label": "Map (choropleth / points)", "kind": "map",   "needs": ["y"]},
    "map_heat":  {"label": "Heat map (point density)",  "kind": "map",   "needs": ["y"]},
    "line":      {"label": "Line chart",                "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "area":      {"label": "Area chart",                "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "bar":       {"label": "Bar chart",                 "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "hbar":      {"label": "Horizontal bar chart",      "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "donut":     {"label": "Donut chart",               "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "treemap":   {"label": "Treemap",                   "kind": "chart", "needs": ["x", "y"], "grouped": True},
    "stacked":   {"label": "Stacked bar chart",         "kind": "chart", "needs": ["x", "y", "z"]},
    "heatmap":   {"label": "Heatmap (grid)",            "kind": "chart", "needs": ["x", "y", "z"]},
    "scatter":   {"label": "Scatter plot",              "kind": "chart", "needs": ["x", "y"]},
    "bubble":    {"label": "Bubble chart",              "kind": "chart", "needs": ["x", "y", "size"]},
    "histogram": {"label": "Histogram",                 "kind": "chart", "needs": ["y"]},
}

COUNT_PSEUDO = "count"          # y column used when a bar chart has no numeric column
_COORD_NAMES = {"lat", "lon", "lng", "long", "latitude", "longitude", "x", "y"}
_NAME_HINTS = re.compile(r"(^|_)(name|label|country|city|region|state|county|category|type|title)($|_)")
_SUM_HINTS = re.compile(r"(count|total|amount|revenue|sales|orders|population|number|num|qty|quantity)")


def default_agg(column: str) -> str:
    """Counts and totals are summed when rows are grouped; rates and levels are averaged."""
    return "sum" if _SUM_HINTS.search(column.lower()) else "avg"


def numeric_candidates(columns: list[dict]) -> list[dict]:
    """Numeric columns that are worth plotting (no ids, coordinates or years)."""
    out = [c for c in columns
           if c["dtype"] == "numeric" and c["column_name"].lower() not in _COORD_NAMES]
    out.sort(key=lambda c: (-(c.get("n_distinct") or 0), c.get("null_frac") or 0))
    return out


def _categorical_x(columns: list[dict]) -> Optional[dict]:
    # Text-valued time columns (e.g. month names) also work as bar categories.
    cats = [c for c in columns
            if (c["dtype"] == "categorical" or (c["dtype"] == "temporal" and c.get("sql_type") == "VARCHAR"))
            and 2 <= (c.get("n_distinct") or 0) <= 60]
    if not cats:
        return None
    cats.sort(key=lambda c: (0 if _NAME_HINTS.search(c["column_name"]) else 1, c["ordinal"]))
    return cats[0]


def _second_cat(columns: list[dict], exclude: str, lo: int = 2, hi: int = 12) -> Optional[dict]:
    cats = [c for c in columns
            if c["column_name"] != exclude
            and (c["dtype"] == "categorical" or (c["dtype"] == "temporal" and c.get("sql_type") == "VARCHAR"))
            and lo <= (c.get("n_distinct") or 0) <= hi]
    cats.sort(key=lambda c: (c.get("n_distinct") or 0, c["ordinal"]))
    return cats[0] if cats else None


def _non_negative(nums: list[dict]) -> Optional[dict]:
    """First numeric column with no negative values: needed for donut and treemap parts."""
    for c in nums:
        if c.get("min_val") is not None and c["min_val"] >= 0:
            return c
    return None


def _label_field(columns: list[dict]) -> Optional[str]:
    for c in columns:
        if c["dtype"] in ("categorical", "text") and _NAME_HINTS.search(c["column_name"]):
            return c["column_name"]
    for c in columns:
        if c["dtype"] in ("categorical", "text"):
            return c["column_name"]
    return None


def recommend(card: dict, columns: list[dict]) -> list[dict]:
    """
    Return every feasible visual, best first:
      [{viz, label, score, reason, params:{x,y,label_field}}]
    The first element is the recommendation shown as "Suggested" in the UI.
    """
    nums = numeric_candidates(columns)
    temporal = [c for c in columns if c["dtype"] == "temporal"]
    cat_x = _categorical_x(columns)
    label = _label_field(columns)
    rows = card.get("row_count") or 0
    options: list[dict] = []

    def add(viz: str, score: float, reason: str, **params):
        options.append({"viz": viz, "label": VIZ_REGISTRY[viz]["label"],
                        "score": round(score, 2), "reason": reason, "params": params})

    # ── map ──────────────────────────────────────────────────────────
    gtype = card.get("geometry_type")
    if gtype:
        poly = "Polygon" in gtype
        if nums:
            add("map", 0.95 if poly else 0.90,
                f"Has {gtype} geometry, so values can be shown by location (colour by {nums[0]['column_name']}).",
                y=nums[0]["column_name"], label_field=label)
        elif label:
            add("map", 0.5, f"Has {gtype} geometry but no numeric column to colour by.",
                y=label, label_field=label)

    if gtype and "Point" in gtype and rows >= 100:
        s_col = nums[0]["column_name"] if nums else (label or "")
        add("map_heat", 0.62, f"{rows:,} points: a density surface shows where they cluster.",
            y=s_col, label_field=label)

    # ── time series: line + area ─────────────────────────────────────
    if temporal and nums:
        t = temporal[0]
        distinct = t.get("n_distinct") or 0
        if distinct >= 4:
            add("line", 0.88, f"{t['column_name']} is a time axis with {distinct} points, so a trend reads best.",
                x=t["column_name"], y=nums[0]["column_name"])
            add("area", 0.80, f"Same trend over {t['column_name']}, with the volume under the line filled in.",
                x=t["column_name"], y=nums[0]["column_name"])
        else:
            add("line", 0.40, f"{t['column_name']} has only {distinct} periods, too few for a trend.",
                x=t["column_name"], y=nums[0]["column_name"])

    # ── categories: bar, horizontal bar, donut, treemap ──────────────
    if cat_x and nums:
        d = cat_x["n_distinct"]
        xn, yn = cat_x["column_name"], nums[0]["column_name"]
        add("bar", 0.85 if d <= 25 else 0.60,
            f"Compares {yn} across {d} {xn} values" + ("" if d <= 25 else " (top 25 shown)") + ".",
            x=xn, y=yn)
        add("hbar", 0.75 + (0.07 if d > 8 else 0) if d <= 25 else 0.55,
            f"Horizontal bars keep {d} {xn} labels readable.", x=xn, y=yn)
        part = _non_negative(nums)
        if part and 2 <= d <= 8:
            add("donut", 0.74 if d <= 6 else 0.60,
                f"{d} {xn} values are few enough to show each one's share of {part['column_name']}.",
                x=xn, y=part["column_name"])
        if part and 6 <= d <= 60:
            add("treemap", 0.68, f"Shows how {part['column_name']} is split across {d} {xn} values by area.",
                x=xn, y=part["column_name"])
    elif cat_x and not nums:
        d = cat_x["n_distinct"]
        add("bar", 0.50, f"No numeric column, so it counts rows per {cat_x['column_name']}.",
            x=cat_x["column_name"], y=COUNT_PSEUDO)
        if 2 <= d <= 8:
            add("donut", 0.55, f"Share of rows in each of {d} {cat_x['column_name']} values.",
                x=cat_x["column_name"], y=COUNT_PSEUDO)

    # ── two categories: stacked bar + heatmap ────────────────────────
    x_for_pair = cat_x or (temporal[0] if temporal and temporal[0].get("sql_type") == "VARCHAR" else None)
    if x_for_pair and nums:
        zc = _second_cat(columns, x_for_pair["column_name"])
        if zc:
            add("stacked", 0.72,
                f"Splits each {x_for_pair['column_name']} into its {zc['column_name']} parts.",
                x=x_for_pair["column_name"], y=nums[0]["column_name"], z=zc["column_name"])
        zc2 = _second_cat(columns, x_for_pair["column_name"], 2, 30)
        if zc2 and (x_for_pair.get("n_distinct") or 0) <= 30:
            add("heatmap", 0.66,
                f"A grid of {x_for_pair['column_name']} by {zc2['column_name']}, shaded by {nums[0]['column_name']}.",
                x=x_for_pair["column_name"], y=nums[0]["column_name"], z=zc2["column_name"])

    # ── numbers against numbers: scatter, bubble, histogram ──────────
    if len(nums) >= 2 and rows >= 8:
        add("scatter", 0.70,
            f"Shows how {nums[1]['column_name']} relates to {nums[0]['column_name']}.",
            x=nums[1]["column_name"], y=nums[0]["column_name"])
    if len(nums) >= 3 and rows >= 8:
        add("bubble", 0.66,
            f"Adds a third measure: {nums[2]['column_name']} sets the bubble size.",
            x=nums[1]["column_name"], y=nums[0]["column_name"], size=nums[2]["column_name"])
    if nums and rows >= 20:
        hist_col = max(nums, key=lambda c: c.get("n_distinct") or 0)
        if (hist_col.get("n_distinct") or 0) >= 8:
            add("histogram", 0.62, f"Shows how the {rows:,} values of {hist_col['column_name']} are distributed.",
                y=hist_col["column_name"])

    # ── still possible, just not a natural fit: offered low in the list so the publisher can force it ──
    have = {o["viz"] for o in options}
    y_any = nums[0]["column_name"] if nums else (COUNT_PSEUDO if cat_x else None)
    if cat_x and y_any:
        xn = cat_x["column_name"]
        for v, why in (("bar", "bars"), ("hbar", "horizontal bars"), ("donut", "a donut"), ("treemap", "a treemap")):
            if v not in have:
                part = (_non_negative(nums) or nums[0])["column_name"] if (nums and v in ("donut", "treemap")) else y_any
                add(v, 0.30, f"Possible: {why} of {part} by {xn}, though the category count is not ideal for it.", x=xn, y=part)
                have.add(v)
    if "area" not in have and "line" in have:
        o = next(o for o in options if o["viz"] == "line")
        add("area", 0.35, "Possible: the same series drawn as an area.", **o["params"])
    pair_x = cat_x or (temporal[0] if temporal else None)
    if pair_x and nums:
        zc = _second_cat(columns, pair_x["column_name"], 2, 60)
        for v in ("stacked", "heatmap"):
            if zc and v not in have:
                add(v, 0.30, f"Possible: {pair_x['column_name']} by {zc['column_name']}.",
                    x=pair_x["column_name"], y=nums[0]["column_name"], z=zc["column_name"])
                have.add(v)
    if nums and rows >= 4 and "histogram" not in have:
        add("histogram", 0.30, f"Possible: distribution of {nums[0]['column_name']}.", y=nums[0]["column_name"])

    options.sort(key=lambda o: -o["score"])
    return options
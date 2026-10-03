"""
app/services/viz_recommender.py
===============================
Decides which visual suits a dataset. Pure functions, no database access:
it works from the column profile computed when the dataset was loaded
(see pipeline/add_dataset.py), so a suggestion costs microseconds.

Add a new visual type by adding one entry to VIZ_REGISTRY, one rule in
`recommend()`, and a renderer on the frontend.
"""
from __future__ import annotations

import re
from typing import Optional

VIZ_REGISTRY: dict[str, dict] = {
    "map":     {"label": "Map (choropleth / points)", "kind": "map"},
    "line":    {"label": "Line chart",                "kind": "chart"},
    "bar":     {"label": "Bar chart",                 "kind": "chart"},
    "scatter": {"label": "Scatter plot",              "kind": "chart"},
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

    # ── line ─────────────────────────────────────────────────────────
    if temporal and nums:
        t = temporal[0]
        distinct = t.get("n_distinct") or 0
        if distinct >= 4:
            add("line", 0.88, f"{t['column_name']} is a time axis with {distinct} points, so a trend reads best.",
                x=t["column_name"], y=nums[0]["column_name"])
        else:
            add("line", 0.40, f"{t['column_name']} has only {distinct} periods, too few for a trend.",
                x=t["column_name"], y=nums[0]["column_name"])

    # ── bar ──────────────────────────────────────────────────────────
    if cat_x and nums:
        d = cat_x["n_distinct"]
        add("bar", 0.85 if d <= 25 else 0.60,
            f"Compares {nums[0]['column_name']} across {d} {cat_x['column_name']} values"
            + ("" if d <= 25 else " (top 25 shown)") + ".",
            x=cat_x["column_name"], y=nums[0]["column_name"])
    elif cat_x and not nums:
        add("bar", 0.50, f"No numeric column, so it counts rows per {cat_x['column_name']}.",
            x=cat_x["column_name"], y=COUNT_PSEUDO)

    # ── scatter ──────────────────────────────────────────────────────
    if len(nums) >= 2 and rows >= 8:
        add("scatter", 0.70,
            f"Shows how {nums[1]['column_name']} relates to {nums[0]['column_name']}.",
            x=nums[1]["column_name"], y=nums[0]["column_name"])

    options.sort(key=lambda o: -o["score"])
    return options
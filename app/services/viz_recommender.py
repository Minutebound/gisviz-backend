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

# kind: chart -> drawn with D3, map -> drawn with MapLibre (cartogram: D3 on a map projection).
# needs: params the visual cannot render without; optional: params it can use.
# x: what the x column must be (cat = category/label, num = number, time = ordered/time, any).
# labels: how the post form names each column for this visual (the form builds its fields from these).
# count: y may be "count" (rows per group). grouped: one row per x (bar-like data).
def _v(label, kind, needs, optional=(), x="cat", count=False, grouped=False, **labels):
    return {"label": label, "kind": kind, "needs": list(needs), "optional": list(optional), "x": x,
            "count": count, "grouped": grouped, "labels": labels}


_TIME = "Time (adds a play button)"
TIME_MAPS = ("map", "bubble_map", "hexbin_map", "cartogram")   # maps that can play through a time column

VIZ_REGISTRY: dict[str, dict] = {
    # maps
    "map":            _v("Map (choropleth / points)", "map", ["y"], ["label_field", "z"], y="Value (colour)", z=_TIME),
    "map_heat":       _v("Heat map (point density)", "map", ["y"], ["label_field"], y="Weight"),
    "bubble_map":     _v("Bubble map", "map", ["y"], ["label_field", "z"], y="Bubble size", z=_TIME),
    "hexbin_map":     _v("Hexbin map", "map", ["y"], ["label_field", "z"], count=True, y="Value per cell", z=_TIME),
    "cartogram":      _v("Cartogram (Dorling)", "map", ["y"], ["label_field", "z"], y="Circle size", z=_TIME),
    "connection_map": _v("Connection map", "map", ["y"], ["x", "z"], count=True,
                         y="Line width", x="Origin name", z="Destination name"),
    # evolution
    "line":           _v("Line chart", "chart", ["x", "y"], x="time", count=True, grouped=True, x_="Time / order"),
    "area":           _v("Area chart", "chart", ["x", "y"], x="time", count=True, grouped=True, x_="Time / order"),
    "stacked_area":   _v("Stacked area chart", "chart", ["x", "y", "z"], x="time", count=True, z="Series"),
    "streamgraph":    _v("Streamgraph", "chart", ["x", "y", "z"], x="time", count=True, z="Series"),
    # ranking
    "bar":            _v("Bar chart", "chart", ["x", "y"], ["color"], count=True, grouped=True),
    "hbar":           _v("Horizontal bar chart", "chart", ["x", "y"], ["color"], count=True, grouped=True),
    "lollipop":       _v("Lollipop chart", "chart", ["x", "y"], ["color"], count=True, grouped=True),
    "circular_bar":   _v("Circular bar chart", "chart", ["x", "y"], count=True, grouped=True),
    "ranked_columns": _v("Connected ranking columns", "chart", ["x", "y"], ["z", "color"], x_="Items (e.g. countries)",
                         y="Value", z="Columns (long data: one row per item per column)", color="Colour by"),
    "radar":          _v("Radar / spider chart", "chart", ["x", "y"], ["z"], count=True, x_="Axes (3–12)", z="Series (max 5)"),
    "word_cloud":     _v("Word cloud", "chart", ["x", "y"], count=True, grouped=True, x_="Words", y="Word size"),
    "parallel_coordinates": _v("Parallel coordinates", "chart", ["y"], ["color"], x="any", y="First axis",
                               color="Colour lines by"),
    # part of a whole
    "donut":          _v("Donut chart", "chart", ["x", "y"], count=True, grouped=True),
    "pie":            _v("Pie chart", "chart", ["x", "y"], count=True, grouped=True),
    "treemap":        _v("Treemap", "chart", ["x", "y"], ["z"], count=True, grouped=True, z="Inside (second level)"),
    "stacked":        _v("Stacked bar chart", "chart", ["x", "y", "z"], count=True, z="Stack by"),
    "sunburst":       _v("Sunburst", "chart", ["x", "y", "z"], count=True, x_="Inner ring", z="Outer ring"),
    "circle_packing": _v("Circular packing", "chart", ["x", "y"], ["z"], count=True, x_="Groups", z="Inside (second level)"),
    "dendrogram":     _v("Dendrogram", "chart", ["x", "z"], ["y"], count=True, x_="Parent", z="Child"),
    # correlation
    "scatter":        _v("Scatter plot", "chart", ["x", "y"], ["label_field"], x="num"),
    "bubble":         _v("Bubble chart", "chart", ["x", "y", "size"], ["label_field"], x="num"),
    "heatmap":        _v("Heatmap (grid)", "chart", ["x", "y", "z"], count=True, z="Rows"),
    "correlogram":    _v("Correlogram", "chart", ["y"], x="any", y="Any measure (all are compared)"),
    "connected_scatter": _v("Connected scatter", "chart", ["x", "y", "z"], x="num", z="Order (time)"),
    "density_2d":     _v("2D density", "chart", ["x", "y"], x="num"),
    # distribution
    "histogram":      _v("Histogram", "chart", ["y"], x="any", y="Column"),
    "density":        _v("Density plot", "chart", ["y"], ["x"], y="Column", x_="Groups (max 6)"),
    "boxplot":        _v("Box plot", "chart", ["x", "y"], x_="Groups"),
    "violin":         _v("Violin plot", "chart", ["x", "y"], x_="Groups"),
    "ridgeline":      _v("Ridgeline", "chart", ["x", "y"], x_="Groups"),
    # flow
    "sankey":         _v("Sankey diagram", "chart", ["x", "z", "y"], count=True, x_="From", z="To", y="Flow"),
    "chord":          _v("Chord diagram", "chart", ["x", "z", "y"], count=True, x_="From", z="To", y="Flow"),
    "network":        _v("Network diagram", "chart", ["x", "z"], ["y"], count=True, x_="From", z="To", y="Link weight"),
    "arc_diagram":    _v("Arc diagram", "chart", ["x", "z"], ["y"], count=True, x_="From", z="To", y="Link weight"),
    "edge_bundling":  _v("Edge bundling", "chart", ["x", "z"], ["y", "color"], count=True, x_="From", z="To",
                         y="Link weight", color="Group of the 'From' item"),
}
for _m in VIZ_REGISTRY.values():               # "x_" is only a way to pass the x label through **labels
    if "x_" in _m["labels"]:
        _m["labels"]["x"] = _m["labels"].pop("x_")

# Chart types whose rows are long-format x / z / y (aggregated per pair)
GRID_VIZ = ("stacked", "heatmap", "stacked_area", "streamgraph", "sunburst", "dendrogram",
            "sankey", "chord", "network", "arc_diagram", "edge_bundling")
# ... raw (sampled) rows for distributions
RAW_VIZ = ("density", "boxplot", "violin", "ridgeline", "density_2d")
# flows: x = source, z = target
FLOW_VIZ = ("sankey", "chord", "network", "arc_diagram", "edge_bundling")

COUNT_PSEUDO = "count"          # y column used when a bar chart has no numeric column
_COORD_NAMES = {"lat", "lon", "lng", "long", "latitude", "longitude", "x", "y"}
_NAME_HINTS = re.compile(r"(^|_)(name|label|country|city|region|state|county|category|type|title)($|_)")
_SUM_HINTS = re.compile(r"(count|total|amount|revenue|sales|orders|population|number|num|qty|quantity|n_)")


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


_LON = r"(lon|lng|long|longitude|x)"
_LAT = r"(lat|latitude|y)"
_FROM = r"(o|orig|origin|from|src|source|start|dep|departure)"
_TO = r"(d|dest|destination|to|dst|target|end|arr|arrival)"


def coord_pairs(columns: list[dict]) -> Optional[dict]:
    """Origin and destination coordinate columns for a connection map, found by name:
    e.g. from_lon/from_lat/to_lon/to_lat, origin_lng/origin_lat/dest_lng/dest_lat, lon_o/lat_o/lon_d/lat_d.
    Returns {"from_lon", "from_lat", "to_lon", "to_lat"} column names, or None."""
    nums = [c["column_name"] for c in columns if c["dtype"] == "numeric"]
    found = {}
    for key, side, axis in (("from_lon", _FROM, _LON), ("from_lat", _FROM, _LAT), ("to_lon", _TO, _LON), ("to_lat", _TO, _LAT)):
        pat = re.compile(rf"^({side}[_ .-]?{axis}|{axis}[_ .-]?{side})$", re.I)
        hit = next((n for n in nums if pat.match(n)), None)
        if not hit:
            return None
        found[key] = hit
    return found if len(set(found.values())) == 4 else None


_FLOW_FROM = re.compile(r"(^|_)(source|from|origin|exporter|sender|parent|src)($|_)", re.I)
_FLOW_TO = re.compile(r"(^|_)(target|to|destination|dest|importer|receiver|child|dst)($|_)", re.I)


def _time_col(columns: list[dict], label: Optional[str], rows: int) -> Optional[dict]:
    """A time column for a map that plays over time: dates, or a year-like number, when the place names repeat."""
    lab = next((c for c in columns if c["column_name"] == label), None)
    if not lab or not rows or (lab.get("n_distinct") or 0) >= rows:
        return None
    cands = [c for c in columns if c["dtype"] == "temporal" or (c["dtype"] == "numeric" and re.search(r"(^|_)(year|yr|month|date|period|time)($|_)", c["column_name"], re.I))]
    cands = [c for c in cands if 2 <= (c.get("n_distinct") or 0) <= 200]
    return cands[0] if cands else None


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
        m = VIZ_REGISTRY[viz]
        options.append({"viz": viz, "label": m["label"], "kind": m["kind"], "score": round(score, 2), "reason": reason,
                        "params": {k: v for k, v in params.items() if v is not None},
                        # what the post form shows for this visual: required / optional columns and their names
                        "needs": m["needs"], "optional": m["optional"], "x_kind": m["x"], "count": m["count"],
                        "labels": m["labels"]})

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

    # ── a ranking: one row per name (country, city, company) and a number ──
    # e.g. PISA scores by country: every name gets a bar, sorted, coloured by a small group column.
    ranked = None
    if nums and 3 <= rows <= 80:
        uniq = [c for c in columns if c["dtype"] in ("categorical", "text") and (c.get("n_distinct") or 0) == rows]
        uniq.sort(key=lambda c: (0 if _NAME_HINTS.search(c["column_name"]) else 1, c["ordinal"]))
        ranked = uniq[0] if uniq else None
    if ranked and len(nums) >= 2:      # e.g. government / household / corporate debt per country
        add("ranked_columns", 0.80 if gtype else 0.95, f"Every {ranked['column_name']} ranked by each of {min(len(nums), 5)} measures in side-by-side "
            f"columns, with lines linking the same {ranked['column_name']} across them.", x=ranked["column_name"], y=nums[0]["column_name"])
    if ranked:
        xn, yn = ranked["column_name"], nums[0]["column_name"]
        group = _second_cat(columns, xn, 2, 12)
        params = {"x": xn, "y": yn, **({"color": group["column_name"]} if group else {})}
        add("hbar", 0.86 if gtype else 0.93, f"Each of the {rows} {xn} values has one {yn}: a sorted ranking"
            + (f", coloured by {group['column_name']}" if group else "") + ".", **params)

    # ── categories: bar, horizontal bar, donut, treemap ──────────────
    if cat_x and nums:
        d = cat_x["n_distinct"]
        xn, yn = cat_x["column_name"], nums[0]["column_name"]
        add("bar", 0.85 if d <= 25 else 0.60,
            f"Compares {yn} across {d} {xn} values" + ("" if d <= 25 else " (top 25 shown)") + ".",
            x=xn, y=yn)
        if not ranked:
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

    _more(add, card, columns, nums, temporal, cat_x, label, rows, gtype, {o["viz"] for o in options})

    options.sort(key=lambda o: -o["score"])
    seen, out = set(), []
    for o in options:                      # one entry per visual type: its best-scored variant
        if o["viz"] not in seen:
            seen.add(o["viz"]); out.append(o)
    return out


def _more(add, card, columns, nums, temporal, cat_x, label, rows, gtype, have):
    """The rest of the data-to-viz families (distribution, correlation, ranking, part of a whole, evolution,
    maps, flows): offered whenever the columns fit, ranked below the classic charts unless the data clearly
    asks for them (e.g. from/to columns -> a sankey)."""
    cats = [c for c in columns if c["dtype"] in ("categorical", "text") and 2 <= (c.get("n_distinct") or 0) <= 200]
    y0 = nums[0]["column_name"] if nums else None
    yc = y0 or "count"
    part = _non_negative(nums)
    yp = part["column_name"] if part else ("count" if not nums else None)

    # ── maps ──
    tcol = _time_col(columns, label, rows)
    if gtype and tcol and nums:          # several rows per place: the map plays through time (a video map)
        add("map", 0.97, f"Each {label} has a value per {tcol['column_name']}: an animated map with a play button.",
            y=y0, label_field=label, z=tcol["column_name"])
        add("bubble_map", 0.9, f"Bubbles that grow and shrink with {y0} over {tcol['column_name']} (play button).",
            y=y0, label_field=label, z=tcol["column_name"])
    if gtype:
        poly, point = "Polygon" in gtype, "Point" in gtype
        if y0:
            # counts and totals read best as size; rates and shares as colour (choropleth)
            amount = bool(_SUM_HINTS.search(y0.lower()))
            add("bubble_map", 0.96 if amount else (0.84 if point else 0.70),
                f"Circles sized by {y0} at each place: amounts (totals, counts) read best as size.",
                y=y0, label_field=label)
            add("cartogram", 0.55 if poly else 0.40, f"Every place becomes a circle sized by {y0}, pushed apart so none overlap.",
                y=y0, label_field=label)
        if point:
            add("hexbin_map", 0.66 if rows >= 300 else 0.42, f"{rows:,} points summed into equal hexagons: density without region-size bias.",
                y=y0 or "count", label_field=label)
    pairs = coord_pairs(columns)
    if pairs:
        frm = next((c["column_name"] for c in cats if _FLOW_FROM.search(c["column_name"])), None)
        to = next((c["column_name"] for c in cats if _FLOW_TO.search(c["column_name"])), None)
        weight = next((c["column_name"] for c in nums if c["column_name"] not in pairs.values()), None)
        add("connection_map", 0.97, f"Origin and destination coordinates ({pairs['from_lon']}, {pairs['to_lon']}…): "
            "each row is drawn as a curved line between two places.", y=weight or "count", x=frm, z=to)

    # ── distribution ──
    if y0 and rows >= 20:
        grp = _second_cat(columns, "", 2, 6)
        add("density", 0.52, f"A smooth curve of how {y0} is distributed" + (f", one per {grp['column_name']}." if grp else "."),
            y=y0, x=grp["column_name"] if grp else None)
        grp = _second_cat(columns, "", 2, 24)
        if grp:
            g = grp["column_name"]
            add("boxplot", 0.58, f"Median, spread and outliers of {y0} for each {g}.", x=g, y=y0)
            add("violin", 0.50, f"The shape of {y0} for each {g} (shows two peaks where a box plot cannot).", x=g, y=y0)
            add("ridgeline", 0.46, f"{y0} distributions of every {g}, stacked like ridges.", x=g, y=y0)

    # ── correlation ──
    if len(nums) >= 2 and rows >= 8:
        a, b = nums[1]["column_name"], nums[0]["column_name"]
        add("density_2d", 0.55 if rows >= 500 else 0.34, f"Where {a} × {b} points concentrate (contours instead of dots).", x=a, y=b)
        order = next((t for t in temporal), None) or next((c for c in columns if c["dtype"] == "numeric"
                                                           and re.search(r"(year|month|date|time|period)", c["column_name"], re.I)), None)
        if order:
            add("connected_scatter", 0.60, f"How {a} and {b} moved together over {order['column_name']}.",
                x=a, y=b, z=order["column_name"])
    if len(nums) >= 3 and rows >= 8:
        add("correlogram", 0.48, f"Correlation of every pair of the {min(len(nums), 10)} numeric columns at once.", y=y0)
        grp = _second_cat(columns, "", 2, 12)
        add("parallel_coordinates", 0.44, f"Each row is a line across {min(len(nums), 8)} numeric axes"
            + (f", coloured by {grp['column_name']}." if grp else "."), y=y0, color=grp["column_name"] if grp else None)

    # ── ranking / part of a whole (one category + a number) ──
    if cat_x and (y0 or not nums):
        xn, d = cat_x["column_name"], cat_x["n_distinct"]
        add("lollipop", 0.62 if d > 12 else 0.50, f"{yc} for {min(d, 40)} {xn} values as dots on sticks: lighter than bars.", x=xn, y=yc)
        add("circular_bar", 0.40, f"{yc} by {xn} as bars around a circle (eye-catching, less precise).", x=xn, y=yc)
        if yp and 2 <= d <= 8:
            add("pie", 0.56, f"Share of {yp} for each of the {d} {xn} values.", x=xn, y=yp)
        if 3 <= d <= 12:
            z = _second_cat(columns, xn, 2, 5)
            add("radar", 0.42, f"A profile across {d} {xn} axes" + (f", one shape per {z['column_name']}." if z else "."),
                x=xn, y=yc, z=z["column_name"] if z else None)
        if yp:
            add("circle_packing", 0.40, f"{yp} by {xn} as nested circles.", x=xn, y=yp)
    # words: a text column with repeats (unique ids / codes are not words), names first
    wc = [c for c in columns if c["dtype"] in ("text", "categorical") and 10 <= (c.get("n_distinct") or 0) < max(11, rows * 0.9)
          and not re.search(r"(^|_)(id|code|key|uuid)($|_)", c["column_name"], re.I)]
    wc.sort(key=lambda c: (0 if _NAME_HINTS.search(c["column_name"]) else 1, c["ordinal"]))
    words = wc[0] if wc else None
    if words:
        add("word_cloud", 0.36, f"The {words['column_name']} values sized by {yc}: a rough sense of the most frequent.",
            x=words["column_name"], y=yc)

    # ── long data: item × column × value (e.g. country, debt_type, share) -> connected ranking columns ──
    if cat_x and y0:
        cols_small = [c for c in cats if 2 <= (c.get("n_distinct") or 0) <= 5 and c["column_name"] != cat_x["column_name"]]
        if cols_small and (cat_x.get("n_distinct") or 0) >= 5:
            add("ranked_columns", 0.66, f"{cat_x['column_name']} ranked by {y0} in one column per {cols_small[0]['column_name']}, "
                "linked across columns.", x=cat_x["column_name"], y=y0, z=cols_small[0]["column_name"])

    # ── two categories: hierarchy, flows ──
    pair = [c for c in cats if (c.get("n_distinct") or 0) <= 60]
    if len(pair) >= 2:
        a = next((c for c in pair if _FLOW_FROM.search(c["column_name"])), None)
        b = next((c for c in pair if _FLOW_TO.search(c["column_name"]) and c is not a), None)
        flow = bool(a and b)
        if not flow:
            pair.sort(key=lambda c: (c.get("n_distinct") or 0))
            a, b = pair[0], pair[1]                       # few parents, more children
        an, bn = a["column_name"], b["column_name"]
        if yp:
            add("sunburst", 0.50, f"{yp} split by {an}, then by {bn}, as rings.", x=an, z=bn, y=yp)
            add("treemap", 0.47, f"{yp} by {an}, each split into its {bn} parts (nested rectangles).", x=an, z=bn, y=yp)
            add("circle_packing", 0.44, f"{yp} by {an}, each containing its {bn} values as circles.", x=an, z=bn, y=yp)
        add("dendrogram", 0.38 if not flow else 0.30, f"The {an} → {bn} tree.", x=an, z=bn, y=yp or "count")
        f = 0.88 if flow else 0.36
        add("sankey", f, f"Flows from {an} to {bn}" + (f", widths = {yc}." if y0 else " (rows counted)."), x=an, z=bn, y=yc)
        add("chord", f - 0.06, f"Two-way links between {an} and {bn} around a circle.", x=an, z=bn, y=yc)
        add("network", f - 0.10, f"Who connects to whom: {an} → {bn} as a network.", x=an, z=bn, y=yc)
        add("arc_diagram", f - 0.16, f"{an} → {bn} links as arcs over one line.", x=an, z=bn, y=yc)
        add("edge_bundling", f - 0.20, f"{an} → {bn} links around a circle, bundled to show the main routes.", x=an, z=bn, y=yc)

    # ── evolution: series over time ──
    t = temporal[0] if temporal else None
    if t and (t.get("n_distinct") or 0) >= 4:
        z = _second_cat(columns, t["column_name"], 2, 12)
        if z:
            add("stacked_area", 0.74, f"Total {yc} over {t['column_name']} and how each {z['column_name']} contributes.",
                x=t["column_name"], z=z["column_name"], y=yc)
            add("streamgraph", 0.58, f"The rise and fall of each {z['column_name']} over {t['column_name']}, as a flowing stream.",
                x=t["column_name"], z=z["column_name"], y=yc)
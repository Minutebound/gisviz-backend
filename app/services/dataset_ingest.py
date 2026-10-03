"""
app/services/dataset_ingest.py
==============================
Builds the published, per-dataset DuckDB files the API serves (DATASETS_DIR/<id>.duckdb, one
table `data`) and profiles them for the chart suggester.

* build_file()  - used by the DuckDB workspace publisher (app/scripts/duckdb_workspace.py): it exports
                  a workspace table to Parquet and calls this, which normalises column names, finds the
                  geometry (GeoJSON / WKT `geometry` column, or lat/lon) and swaps the file in atomically.
* profile_file() - used by dataset_sync on the API side to (re)register the dataset in Postgres.

Also reads CSV/TSV/GeoJSON/JSON (messy Excel exports included) for the sample loader.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import duckdb

from app.services import duckdb_service as store

TIME_NAMES = re.compile(r"(^|_)(year|yr|month|quarter|week|day|date|period|time|timestamp)($|_)")
LAT = ("latitude", "lat")
LON = ("longitude", "lon", "lng", "long")
GEOM_COLS = ("_gv_geom", "geometry", "geom", "geojson")
NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL",
           "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "REAL")
ALLOWED = (".parquet", ".csv", ".tsv", ".geojson", ".json", ".gpkg", ".shp", ".kml")


class IngestError(Exception):
    pass


# ── helpers ──────────────────────────────────────────────────────────

def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower() or "col"
    return "c_" + s if s[0].isdigit() else s


def unique_names(names: list[str]) -> list[str]:
    seen, out = {}, []
    for n in map(snake, names):
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


def _walk(coords, acc):
    if coords and isinstance(coords[0], (int, float)):
        acc.append(coords)
    else:
        for c in coords or []:
            _walk(c, acc)


# ── readers -> temp table `_stage` (optionally with _gv_geom) ────────

def _stage_geojson(con, path: Path) -> None:
    gj = json.loads(path.read_text(encoding="utf-8"))
    feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8") as tmp:
        for f in feats:
            if not f.get("geometry"):
                continue
            row = dict(f.get("properties") or {})
            row["_gv_geom"] = json.dumps(f["geometry"], separators=(",", ":"))
            tmp.write(json.dumps(row) + "\n")
    try:
        con.execute(f"CREATE TEMP TABLE _stage AS SELECT * FROM read_json_auto('{tmp.name}', "
                    "format='newline_delimited', sample_size=-1)")
    finally:
        os.unlink(tmp.name)


def _read_csv(con, p: str) -> None:
    """CSV/TSV with fallbacks for files exported by Excel and friends."""
    attempts = [
        "auto_detect=true, sample_size=-1, null_padding=true",                        # short rows padded, not dropped
        "auto_detect=true, sample_size=-1, null_padding=true, encoding='latin-1'",    # non-UTF-8 exports
        "auto_detect=true, sample_size=-1, strict_mode=false, null_padding=true",     # stray quotes
        "auto_detect=true, sample_size=-1, encoding='latin-1', strict_mode=false, null_padding=true, ignore_errors=true",
    ]
    last = None
    for opts in attempts:
        try:
            con.execute("DROP TABLE IF EXISTS _stage")
            con.execute(f"CREATE TEMP TABLE _stage AS SELECT * FROM read_csv('{p}', {opts})")
            return
        except duckdb.Error as e:
            last = e
    raise IngestError(f"Could not read the CSV: {str(last).splitlines()[0][:300]}")


_US_NUM = r"^-?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$"
_EU_NUM = r"^-?[0-9]{1,3}(\.[0-9]{3})+(,[0-9]+)?$|^-?[0-9]+,([0-9]{1,2}|[0-9]{4,})$"


def _coerce_numbers(con) -> None:
    """Text columns that are really numbers ("1,234", "1.234,5", "12 %", "$40", "n/a" gaps) -> DOUBLE."""
    for name, typ in [(r[0], r[1]) for r in con.execute("DESCRIBE _stage").fetchall()]:
        if typ != "VARCHAR" or name == "_gv_geom":
            continue
        c = f"trim({q(name)})"
        base = f"regexp_replace({c}, '[\\s$€£%]', '', 'g')"
        us = f"try_cast(replace({base}, ',', '') AS DOUBLE)"
        eu = f"try_cast(replace(replace({base}, '.', ''), ',', '.') AS DOUBLE)"
        n, n_us, n_eu, pat_eu, pat_us = con.execute(
            f"SELECT count(*) FILTER (WHERE {c} <> ''), count({us}) FILTER (WHERE {c} <> ''), "
            f"count({eu}) FILTER (WHERE {c} <> ''), count(*) FILTER (WHERE regexp_matches({base}, '{_EU_NUM}')), "
            f"count(*) FILTER (WHERE regexp_matches({base}, '{_US_NUM}')) FROM _stage").fetchone()
        if not n:
            continue
        expr = None
        if pat_eu > pat_us and n_eu >= 0.95 * n:
            expr = eu
        elif n_us >= 0.95 * n:
            expr = us
        if expr:
            con.execute(f"ALTER TABLE _stage ALTER {q(name)} TYPE DOUBLE USING {expr}")


_WKT_RE = re.compile(r"^\s*(MULTI)?(POINT|LINESTRING|POLYGON)\s*(Z|M|ZM)?\s*\(", re.I)


def _wkt_to_geojson(wkt: str) -> Optional[str]:
    """Minimal WKT reader for (MULTI)POINT / LINESTRING / POLYGON. Returns GeoJSON text or None."""
    m = _WKT_RE.match(wkt or "")
    if not m:
        return None
    body = wkt[wkt.index("("):].strip()
    try:
        nested = json.loads(re.sub(r"(-?[0-9.eE+-]+)\s+(-?[0-9.eE+-]+)(\s+-?[0-9.eE+-]+)*",
                                   r"[\1,\2]", body).replace("(", "[").replace(")", "]"))
    except ValueError:
        return None
    kind = ("Multi" if m.group(1) else "") + {"POINT": "Point", "LINESTRING": "LineString", "POLYGON": "Polygon"}[m.group(2).upper()]
    if kind == "Point":
        nested = nested[0]
    elif kind == "MultiPoint":
        nested = [p[0] if isinstance(p[0], list) else p for p in nested]
    return json.dumps({"type": kind, "coordinates": nested}, separators=(",", ":"))


def _geometry_column(con, geom: str) -> None:
    """Turn a GeoJSON-text or WKT column into _gv_geom; anything else stays an ordinary column."""
    sample = [r[0] for r in con.execute(
        f"SELECT CAST({q(geom)} AS VARCHAR) FROM _stage WHERE {q(geom)} IS NOT NULL LIMIT 20").fetchall()]
    if not sample:
        return
    if all(str(v).lstrip().startswith("{") for v in sample):
        con.execute(f"ALTER TABLE _stage RENAME COLUMN {q(geom)} TO _gv_geom")
        return
    if all(_WKT_RE.match(str(v)) for v in sample):
        con.execute("ALTER TABLE _stage ADD COLUMN _gv_rn BIGINT")
        con.execute("CREATE TEMP SEQUENCE _gv_seq")
        con.execute("UPDATE _stage SET _gv_rn = nextval('_gv_seq')")
        conv = [(rn, _wkt_to_geojson(str(v))) for rn, v in
                con.execute(f"SELECT _gv_rn, CAST({q(geom)} AS VARCHAR) FROM _stage WHERE {q(geom)} IS NOT NULL").fetchall()]
        con.execute("CREATE TEMP TABLE _gv_g (_gv_rn BIGINT, _gv_geom VARCHAR)")
        for i in range(0, len(conv), 5000):
            con.executemany("INSERT INTO _gv_g VALUES (?, ?)", conv[i:i + 5000])
        con.execute(f"CREATE TEMP TABLE _stage2 AS SELECT s.* EXCLUDE ({q(geom)}, _gv_rn), g._gv_geom "
                    "FROM _stage s LEFT JOIN _gv_g g USING (_gv_rn) ORDER BY s._gv_rn")
        con.execute("DROP TABLE _stage; DROP TABLE _gv_g; ALTER TABLE _stage2 RENAME TO _stage")


def _stage_tabular(con, path: Path, ext: str) -> None:
    p = str(path).replace("'", "''")
    if ext == ".parquet":
        con.execute(f"CREATE TEMP TABLE _stage AS SELECT * FROM read_parquet('{p}')")
    elif ext == ".json":
        con.execute(f"CREATE TEMP TABLE _stage AS SELECT * FROM read_json_auto('{p}')")
    else:
        _read_csv(con, p)
        _coerce_numbers(con)
    cols = [r[0] for r in con.execute("DESCRIBE _stage").fetchall()]
    low = {c.lower(): c for c in cols}
    geom = next((low[g] for g in GEOM_COLS if g in low), None)
    if geom and geom != "_gv_geom":
        _geometry_column(con, geom)
    if "_gv_geom" in [r[0] for r in con.execute("DESCRIBE _stage").fetchall()]:
        return
    lat = next((low[c] for c in LAT if c in low), None)
    lon = next((low[c] for c in LON if c in low), None)
    if lat and lon:     # coordinates -> point geometry
        con.execute(f"""ALTER TABLE _stage ADD COLUMN _gv_geom VARCHAR;
            UPDATE _stage SET _gv_geom = '{{"type":"Point","coordinates":[' || {q(lon)} || ',' || {q(lat)} || ']}}'
            WHERE {q(lat)} IS NOT NULL AND {q(lon)} IS NOT NULL""")


def _stage_spatial(con, path: Path, src_crs: Optional[str]) -> None:
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
    except Exception as e:
        raise IngestError("This format needs DuckDB's spatial extension; upload GeoJSON or Parquet instead") from e
    p = str(path).replace("'", "''")
    geom = f"ST_Transform(geom, '{src_crs}', 'EPSG:4326', always_xy := true)" if src_crs else "geom"
    con.execute(f"CREATE TEMP TABLE _stage AS SELECT * EXCLUDE (geom), ST_AsGeoJSON({geom}) AS _gv_geom FROM ST_Read('{p}')")


# ── profile ──────────────────────────────────────────────────────────

def classify(name: str, sql_type: str, n_distinct: int, rows: int) -> str:
    base = sql_type.split("(")[0]
    if base in ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"):
        return "temporal"
    if TIME_NAMES.search(name) and (base in NUMERIC or base == "VARCHAR") and n_distinct >= 2:
        return "temporal"
    if base in NUMERIC:
        return "id" if name.endswith("_id") or name == "id" else "numeric"
    if base == "VARCHAR":
        if n_distinct <= max(60, rows * 0.5) and n_distinct < rows * 0.95 + 1:
            return "categorical" if n_distinct < rows or rows <= 60 else "text"
        return "text"
    return "text"


def _create_data(con) -> None:
    """_stage -> table `data` (snake_case unique names, _gv_id row order, optional _gv_geom)."""
    cols = con.execute("DESCRIBE _stage").fetchall()
    raw = [c[0] for c in cols if c[0] != "_gv_geom"]
    if not raw:
        raise IngestError("The table has no columns")
    names = unique_names(raw)
    has_geom = any(c[0] == "_gv_geom" for c in cols)
    sel = ", ".join(f"{q(o)} AS {q(n)}" for o, n in zip(raw, names))
    con.execute("CREATE TABLE data AS SELECT row_number() OVER ()::BIGINT AS _gv_id, " + sel
                + (", _gv_geom::VARCHAR AS _gv_geom" if has_geom else "") + " FROM _stage")
    if con.execute("SELECT count(*) FROM data").fetchone()[0] == 0:
        raise IngestError("The table has no rows")


def profile(con) -> dict:
    """Profile table `data`: rows, per-column stats + dtype, geometry type and bbox."""
    schema = [(r[0], r[1]) for r in con.execute("DESCRIBE data").fetchall()]
    rows = con.execute("SELECT count(*) FROM data").fetchone()[0]
    has_geom = any(n == "_gv_geom" for n, _ in schema)
    out = []
    for i, (n, t) in enumerate([c for c in schema if c[0] not in ("_gv_id", "_gv_geom")]):
        nd, nulls = con.execute(f"SELECT count(DISTINCT {q(n)}), count(*) - count({q(n)}) FROM data").fetchone()
        mn = mx = None
        if t.split("(")[0] in NUMERIC:
            mn, mx = con.execute(f"SELECT min({q(n)})::DOUBLE, max({q(n)})::DOUBLE FROM data").fetchone()
        out.append({"column_name": n, "label": n.replace("_", " "), "dtype": classify(n, t, nd, rows), "sql_type": t,
                    "n_distinct": int(nd), "null_frac": (nulls / rows) if rows else 0.0,
                    "min_val": mn, "max_val": mx, "ordinal": i})

    geometry_type, bbox = None, None
    if has_geom:
        kinds, pts = set(), []
        for (g,) in con.execute("SELECT _gv_geom FROM data WHERE _gv_geom IS NOT NULL").fetchall():
            try:
                gj = json.loads(g)
            except (TypeError, ValueError):
                continue
            kinds.add(gj.get("type"))
            _walk(gj.get("coordinates"), pts)
        fam = {"Polygon": "MultiPolygon", "MultiPolygon": "MultiPolygon", "LineString": "MultiLineString",
               "MultiLineString": "MultiLineString", "Point": "MultiPoint", "MultiPoint": "MultiPoint"}
        if len(kinds) == 1:
            geometry_type = next(iter(kinds))
        elif kinds and len({fam.get(k, k) for k in kinds}) == 1:
            geometry_type = fam[next(iter(kinds))]
        else:
            geometry_type = "Mixed" if kinds else None
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
    return {"rows": int(rows), "columns": out, "geometry_type": geometry_type, "bbox": bbox, "crs": "EPSG:4326"}


def profile_file(path: Path) -> dict:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return profile(con)
    finally:
        con.close()


def stage_file(con, src: Path, original_name: str, src_crs: Optional[str] = None) -> str:
    """Read a source file into temp table `_stage` (with _gv_geom when it has geometry). Returns the format."""
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED:
        raise IngestError(f"Unsupported file type '{ext}'. Use one of: {', '.join(ALLOWED)}")
    if ext in (".geojson",) or (ext == ".json" and '"FeatureCollection"' in src.read_text(encoding="utf-8", errors="ignore")[:4000]):
        _stage_geojson(con, src); return "geojson"
    if ext in (".gpkg", ".shp", ".kml"):
        _stage_spatial(con, src, src_crs); return ext[1:]
    _stage_tabular(con, src, ext); return ext[1:]


# ── public API ───────────────────────────────────────────────────────

def build_file(dataset_id: str, src: Path, original_name: str, src_crs: Optional[str] = None) -> dict:
    """Build <DATASETS_DIR>/<dataset_id>.duckdb from a source file and swap it in atomically."""
    target = store.path_for(dataset_id)          # validates the id
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{dataset_id}.{uuid.uuid4().hex}.tmp")
    con = duckdb.connect(str(tmp))
    try:
        con.execute("SET threads=2")
        fmt = stage_file(con, src, original_name, src_crs)
        _create_data(con)
        info = profile(con)
        con.execute("DROP TABLE _stage")
        con.execute("CHECKPOINT")
    except IngestError:
        con.close(); tmp.unlink(missing_ok=True); raise
    except duckdb.Error as e:
        con.close(); tmp.unlink(missing_ok=True)
        raise IngestError(f"Could not read the data: {str(e).splitlines()[0][:300]}") from e
    con.close()
    os.replace(tmp, target)                      # atomic: API readers switch on their next query
    return {**info, "format": fmt}
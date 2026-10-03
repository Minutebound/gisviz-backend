"""
app/duckstore/convert.py
========================
Turns whatever the admin uploads into something DuckDB reads directly, before it is loaded as a
dataset table. Runs inside the DuckDB service.

  read by DuckDB as-is   .parquet .csv .tsv .txt .json .ndjson .jsonl .geojson
  converted to Parquet   .xlsx .xls .ods                       spreadsheets (first sheet with data)
                         .feather .arrow                        Arrow files
                         .gpkg .kml .kmz .gpx .fgb .gml .topojson .dxf   GIS files (via GDAL)
                         .zip   a zipped Shapefile (.shp+.shx+.dbf+.prj), File Geodatabase (.gdb),
                                MapInfo (.tab), or any single file of the formats above
  GeoParquet / Arrow geometry (WKB) is converted too.

Every geometry becomes GeoJSON text in a `geometry` column and is reprojected to WGS84
(EPSG:4326) from the file's own coordinate system, which is what the map renderer expects.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Optional

NATIVE = (".parquet", ".csv", ".tsv", ".txt", ".json", ".ndjson", ".jsonl", ".geojson")
SPREADSHEET = (".xlsx", ".xls", ".ods")
ARROW = (".feather", ".arrow")
GDAL = (".gpkg", ".kml", ".kmz", ".gpx", ".fgb", ".gml", ".topojson", ".dxf")
ARCHIVE = (".zip",)
SUPPORTED = NATIVE + SPREADSHEET + ARROW + GDAL + ARCHIVE

MAX_UNZIPPED_BYTES = 4 * 1024 ** 3          # zip-bomb guard
GEOM_NAMES = ("geometry", "geom", "wkb_geometry", "the_geom", "shape", "wkb")


class ConvertError(Exception):
    pass


# ── helpers ───────────────────────────────────────────────────────────

def _write_parquet(table, work: Path, stem: str) -> Path:
    import pyarrow.parquet as pq
    out = work / f"{stem}.converted.parquet"
    # drop GeoParquet/pandas metadata: the geometry is plain GeoJSON text now, and stale "geo"
    # metadata would make DuckDB treat that text column as a binary geometry
    pq.write_table(table.replace_schema_metadata(None), out)
    return out


def _to_wgs84_geojson(wkb_values, crs: Optional[str]):
    """WKB geometries -> list of GeoJSON strings in EPSG:4326 (None for empty)."""
    import numpy as np
    import shapely
    geoms = shapely.from_wkb(np.asarray(wkb_values, dtype=object), on_invalid="ignore")
    if crs:
        from pyproj import CRS, Transformer
        src = CRS.from_user_input(crs)
        if src.to_epsg() != 4326:
            t = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)
            geoms = shapely.transform(geoms, lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))
    else:
        b = shapely.total_bounds(geoms)
        if not np.isnan(b).any() and (abs(b[0]) > 180 or abs(b[2]) > 180 or abs(b[1]) > 90 or abs(b[3]) > 90):
            raise ConvertError("The geometry has no coordinate system and is not longitude/latitude; "
                               "include the projection (.prj) or export it in EPSG:4326")
    out = shapely.to_geojson(geoms)
    return [None if g is None or g == "null" else g for g in out.tolist()]


def _replace_geometry(table, geom_col: str, crs: Optional[str]):
    import pyarrow as pa
    gj = _to_wgs84_geojson(table.column(geom_col).to_pylist(), crs)
    table = table.drop_columns([geom_col])
    if "geometry" in table.column_names:                       # avoid a clash with an attribute called geometry
        table = table.rename_columns([c if c != "geometry" else "geometry_attr" for c in table.column_names])
    return table.append_column("geometry", pa.array(gj, type=pa.string()))


def _binary_geometry_column(table) -> Optional[str]:
    import pyarrow as pa
    for name in table.column_names:
        f = table.schema.field(name)
        if name.lower() in GEOM_NAMES and (pa.types.is_binary(f.type) or pa.types.is_large_binary(f.type)):
            return name
    return None


def _geoparquet_crs(table, col: str) -> Optional[str]:
    meta = (table.schema.metadata or {}).get(b"geo")
    if not meta:
        return None
    try:
        crs = json.loads(meta).get("columns", {}).get(col, {}).get("crs")
    except ValueError:
        return None
    return json.dumps(crs) if isinstance(crs, dict) else crs


# ── readers ───────────────────────────────────────────────────────────

def _arrow_like(src: Path, work: Path, ext: str) -> tuple[Path, str]:
    """Parquet / Feather / Arrow: pass through, unless geometry is stored as WKB (GeoParquet)."""
    if ext == ".parquet":
        import pyarrow.parquet as pq
        schema = pq.read_schema(src)
        is_geo = b"geo" in (schema.metadata or {})
        if not is_geo and not any(n.lower() in GEOM_NAMES for n in schema.names):
            return src, "parquet"
        table = pq.read_table(src)
        if is_geo:
            meta = json.loads(schema.metadata[b"geo"])
            col = meta.get("primary_column") or "geometry"
            enc = str(meta.get("columns", {}).get(col, {}).get("encoding", "WKB")).upper()
            if enc != "WKB":
                raise ConvertError(f"GeoParquet with '{enc}' geometry encoding is not supported; write it with WKB encoding")
            table = _replace_geometry(table, col, _geoparquet_crs(table, col))
            return _write_parquet(table, work, src.stem), "geoparquet"
    else:
        import pyarrow.feather as feather
        table = feather.read_table(src)
    col = _binary_geometry_column(table)
    if col:
        table = _replace_geometry(table, col, _geoparquet_crs(table, col))
    elif ext == ".parquet":
        return src, "parquet"
    return _write_parquet(table, work, src.stem), ext[1:]


def _xls(src: Path, work: Path) -> tuple[Path, str]:
    import pyarrow as pa
    import xlrd
    book = xlrd.open_workbook(str(src))
    for sheet in book.sheets():
        if sheet.nrows < 2:
            continue
        header = [str(v).strip() or f"col_{i + 1}" for i, v in enumerate(sheet.row_values(0))]
        cols = {h: [] for h in header}
        for r in range(1, sheet.nrows):
            for h, cell in zip(header, sheet.row(r)):
                v = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    v = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    v = None
                cols[h].append(v)
        arrays = {}
        for h, vals in cols.items():
            try:
                arrays[h] = pa.array(vals)
            except (pa.ArrowInvalid, pa.ArrowTypeError):     # mixed types in one column -> text
                arrays[h] = pa.array([None if v is None else str(v) for v in vals])
        return _write_parquet(pa.table(arrays), work, src.stem), "xls"
    raise ConvertError("The spreadsheet has no sheet with a header row and data")


def _gdal(path: str, work: Path, stem: str, fmt: str) -> tuple[Path, str]:
    """Anything GDAL reads (GIS files, xlsx/ods): first layer with rows -> Parquet with GeoJSON geometry."""
    import pyogrio
    pyogrio.set_gdal_config_options({"OGR_XLSX_HEADERS": "FORCE", "OGR_ODS_HEADERS": "FORCE",
                                     "OGR_XLSX_FIELD_TYPES": "AUTO", "OGR_ODS_FIELD_TYPES": "AUTO"})
    try:
        layers = pyogrio.list_layers(path)
    except Exception as e:
        raise ConvertError(f"Could not open the {fmt} file: {str(e)[:200]}")
    last_err = None
    for name, _geom_type in layers:
        try:
            meta, table = pyogrio.read_arrow(path, layer=name)
        except Exception as e:
            last_err = e
            continue
        if table.num_rows == 0:
            continue
        gcol = meta.get("geometry_name") or ("wkb_geometry" if "wkb_geometry" in table.column_names else None)
        if gcol and gcol in table.column_names:
            table = _replace_geometry(table, gcol, meta.get("crs"))
        return _write_parquet(table, work, stem), fmt
    raise ConvertError(f"No layer with data found in the {fmt} file" + (f" ({str(last_err)[:150]})" if last_err else ""))


def _zip(src: Path, work: Path) -> tuple[Path, str]:
    with zipfile.ZipFile(src) as z:
        infos = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
                 and not Path(i.filename).name.startswith(".")]
        if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
            raise ConvertError("The zip is too large once unpacked")
        names = [i.filename for i in infos]
    lower = [n.lower() for n in names]
    gdb = sorted({n.split(".gdb/")[0] + ".gdb" for n in lower if ".gdb/" in n})
    shp = [n for n in names if n.lower().endswith(".shp")]
    tab = [n for n in names if n.lower().endswith((".tab", ".mif"))]
    if shp:
        missing = [ext for ext in (".shx", ".dbf") if not any(m == shp[0].lower()[:-4] + ext for m in lower)]
        if missing:
            raise ConvertError(f"The shapefile in the zip is missing {', '.join(missing)}")
        return _gdal(f"/vsizip/{src}/{shp[0]}", work, Path(shp[0]).stem, "shapefile")
    if gdb:
        orig = next(n for n in names if n.lower().startswith(gdb[0]))
        return _gdal(f"/vsizip/{src}/{orig[:len(gdb[0])]}", work, Path(gdb[0]).stem, "filegdb")
    if tab:
        return _gdal(f"/vsizip/{src}/{tab[0]}", work, Path(tab[0]).stem, "mapinfo")
    data = [n for n in names if Path(n).suffix.lower() in SUPPORTED and Path(n).suffix.lower() != ".zip"]
    if len(data) != 1:
        raise ConvertError("The zip should contain one dataset: a shapefile (.shp .shx .dbf .prj), a .gdb "
                           "folder, or a single data file")
    target = work / Path(data[0]).name
    with zipfile.ZipFile(src) as z, z.open(data[0]) as fin, open(target, "wb") as fout:
        while chunk := fin.read(1 << 20):
            fout.write(chunk)
    return normalize(target, target.name, work)


# ── entry point ───────────────────────────────────────────────────────

def normalize(src: Path, original_name: str, work: Path) -> tuple[Path, str]:
    """(path DuckDB can read, format label). `work` must be inside DuckDB's allowed folder."""
    ext = Path(original_name).suffix.lower()
    if ext not in SUPPORTED:
        if ext == ".shp":
            raise ConvertError("Upload a shapefile as a .zip containing the .shp, .shx, .dbf and .prj files")
        raise ConvertError(f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED)}")
    try:
        if ext in (".parquet",) + ARROW:
            return _arrow_like(src, work, ext)
        if ext == ".xls":
            return _xls(src, work)
        if ext in (".xlsx", ".ods") + GDAL:
            return _gdal(str(src), work, src.stem, ext[1:])
        if ext in ARCHIVE:
            return _zip(src, work)
    except ConvertError:
        raise
    except zipfile.BadZipFile:
        raise ConvertError("The zip file is damaged or not a zip")
    except Exception as e:
        raise ConvertError(f"Could not convert the {ext[1:]} file: {type(e).__name__}: {str(e)[:200]}")
    return src, ext[1:]                                   # DuckDB reads it directly
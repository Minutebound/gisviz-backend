"""
app/services/dataset_sync.py
============================
Keeps the datasets DB (Postgres) and the analytical store (gisviz.duckdb) consistent.

  record with data  + table present, same version  -> in sync
  record with data  + table missing                -> "missing_table"  (fix: record -> inactive, no data)
  record with data  + table version differs        -> "version_mismatch" (fix: re-upload the file)
  table without any record                         -> "orphan_table"   (fix: drop the table)

Checked at API start-up (missing tables are fixed automatically) and from /admin/datasets.
"""
from __future__ import annotations

from app.services import catalog_service as catalog
from app.services import duckdb_service as store


def status() -> dict:
    records = catalog.data_versions()
    tables = {t["code"]: t for t in store.tables()}
    missing, mismatch, orphans = [], [], []
    for code, r in sorted(records.items()):
        if not r["has_data"]:
            continue
        t = tables.get(code)
        if t is None:
            missing.append(code)
        elif t.get("data_version") and r["data_version"] and t["data_version"] != r["data_version"]:
            mismatch.append(code)
    for code in sorted(tables):
        if code not in records:
            orphans.append({"code": code, "rows": tables[code].get("rows"), "source_file": tables[code].get("source_file")})
    return {"in_sync": not (missing or mismatch or orphans), "records": len(records), "tables": len(tables),
            "missing_table": missing, "version_mismatch": mismatch, "orphan_tables": orphans}


def report() -> dict:
    """status() plus the store's own figures, for /admin/datasets. Never raises: when the DuckDB
    service cannot be reached the report says so (state "unreachable")."""
    import datetime as dt
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "service": store.describe()}
    try:
        s = status()
    except Exception as e:
        msg = getattr(e, "message", None) or str(e) or type(e).__name__
        return {**out, "state": "unreachable", "error": msg[:300], "in_sync": False, "store": None,
                "records": len(catalog.data_versions()), "tables": None,
                "missing_table": [], "version_mismatch": [], "orphan_tables": []}
    try:
        st = store.stats()
    except Exception:                                   # older duckdb container without /stats
        st = None
    return {**out, **s, "state": "in_sync" if s["in_sync"] else "out_of_sync", "store": st}


def fix(drop_orphans: bool = False) -> dict:
    """Records whose table is missing become inactive; optionally drop tables without a record."""
    from app.services import visual_service
    s = status()
    for code in s["missing_table"]:
        catalog.mark_missing(code)
        visual_service.invalidate(code)
    dropped = []
    if drop_orphans:
        for o in s["orphan_tables"]:
            store.drop(o["code"])
            dropped.append(o["code"])
    return {**report(), "fixed_missing": s["missing_table"], "dropped_orphans": dropped}


def startup_check() -> None:
    try:
        s = status()
        if s["missing_table"]:
            fix()
        if not s["in_sync"]:
            print(f"[dataset_sync] {s}", flush=True)
    except Exception as e:                               # the store may still be starting: not fatal
        print(f"[dataset_sync] skipped at start-up: {e}", flush=True)
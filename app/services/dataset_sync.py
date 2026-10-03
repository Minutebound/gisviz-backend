"""
app/services/dataset_sync.py
============================
Keeps the Postgres catalog (datasets DB) in step with the published DuckDB files.

    DuckDB workspace table  --(workspace publisher)-->  DATASETS_DIR/<id>.duckdb  --(this sync)-->  datasets row

* new file            -> row created as `draft` (title = id) with the column profile; a manager edits
                         the details and publishes it on /datasets/manage
* file changed        -> profile, row count, geometry and version refreshed; cached charts cleared
* file removed        -> an `active` row becomes `hidden` (existing posts keep their PNG)

Runs at API start-up, every DATASET_SYNC_SECONDS in the background (one worker at a time, via a
Postgres advisory lock) and on demand from the manage page ("Sync now").
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager

from app.services import catalog_service as catalog
from app.services import dataset_ingest
from app.services import duckdb_service as store

INTERVAL = int(os.getenv("DATASET_SYNC_SECONDS", "30"))
_LOCK_KEY = 818181
_local_lock = threading.Lock()


def _version(path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]


@contextmanager
def _cluster_lock():
    """Only one API worker syncs at a time (Postgres advisory lock); falls back to a local lock."""
    conn = None
    got = True
    try:
        from sqlalchemy import text
        from app.db.database import datasets_engine
        conn = datasets_engine.connect()
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
    except Exception:
        conn = None                                   # no Postgres lock available: rely on the local lock
    try:
        with _local_lock:
            yield got
    finally:
        if conn is not None:
            try:
                if got:
                    from sqlalchemy import text
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            finally:
                conn.close()


def published_files() -> dict:
    d = store.DATASETS_DIR
    if not d.exists():
        return {}
    return {p.stem: p for p in d.glob("*.duckdb") if store._ID_RE.match(p.stem)}


def sync(force: bool = False) -> dict:
    """Register / refresh / hide datasets so Postgres matches the published files."""
    from app.services import visual_service
    summary = {"created": [], "updated": [], "hidden": [], "errors": {}, "skipped_busy": False}
    with _cluster_lock() as got:
        if not got:
            summary["skipped_busy"] = True
            return summary
        files = published_files()
        known = catalog.versions()
        for dataset_id, path in sorted(files.items()):
            try:
                ver = _version(path)
                if not force and known.get(dataset_id, (None, None))[0] == ver:
                    continue
                info = dataset_ingest.profile_file(path)
                info["data_version"] = ver
                created = catalog.record_data(dataset_id, info)
                store.evict(dataset_id)
                visual_service.invalidate(dataset_id)
                summary["created" if created else "updated"].append(dataset_id)
            except Exception as e:                    # one bad file must not stop the others
                summary["errors"][dataset_id] = f"{type(e).__name__}: {str(e)[:200]}"
        for dataset_id, (ver, _status) in known.items():
            if dataset_id not in files and ver is not None:
                catalog.mark_missing(dataset_id)
                store.evict(dataset_id)
                visual_service.invalidate(dataset_id)
                summary["hidden"].append(dataset_id)
    if summary["created"] or summary["updated"] or summary["hidden"] or summary["errors"]:
        print(f"[dataset_sync] {summary}", flush=True)
    return summary


def start_background() -> None:
    """Initial sync + periodic re-sync in a daemon thread (safe with several workers)."""
    if INTERVAL <= 0:
        return

    def loop():
        while True:
            try:
                sync()
            except Exception as e:
                print(f"[dataset_sync] failed: {e}", flush=True)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="dataset-sync", daemon=True).start()
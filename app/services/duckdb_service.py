"""
app/services/duckdb_service.py
==============================
Read-only access to the per-dataset DuckDB files.

Layout:  $DATASETS_DIR/<dataset_id>.duckdb   (default /app/data/datasets)
         each file holds ONE table, `data` (columns + _gv_id row order + optional _gv_geom GeoJSON)

* Files are written only by dataset_ingest (the role-gated upload endpoint): it builds a
  temp file and swaps it in with os.replace, so readers never see a half-written file.
* Each API worker keeps a small LRU of read-only connections, one per dataset file.
  When a file's mtime changes (new upload) the next query reopens it transparently.
* One cursor per query keeps concurrent requests thread-safe.
"""
from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

import duckdb

DATASETS_DIR = Path(os.getenv("DATASETS_DIR", "data/datasets"))
MAX_OPEN = int(os.getenv("DATASETS_MAX_OPEN", "64"))
_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

_lock = threading.Lock()
_cons: "OrderedDict[str, tuple[duckdb.DuckDBPyConnection, float]]" = OrderedDict()


class StoreUnavailable(RuntimeError):
    """The dataset's data file has not been uploaded yet."""


def path_for(dataset_id: str) -> Path:
    if not _ID_RE.match(dataset_id or ""):
        raise ValueError("invalid dataset id")
    return DATASETS_DIR / f"{dataset_id}.duckdb"


def has_data(dataset_id: str) -> bool:
    try:
        return path_for(dataset_id).exists()
    except ValueError:
        return False


def _connection(dataset_id: str) -> duckdb.DuckDBPyConnection:
    p = path_for(dataset_id)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError as exc:
        raise StoreUnavailable(f"No data uploaded for {dataset_id}") from exc
    with _lock:
        hit = _cons.get(dataset_id)
        if hit and hit[1] == mtime:
            _cons.move_to_end(dataset_id)
            return hit[0]
        if hit:                                   # file was replaced by a new upload
            _close(hit[0])
        con = duckdb.connect(str(p), read_only=True)
        con.execute("SET memory_limit='768MB'")
        con.execute("SET threads=2")
        _cons[dataset_id] = (con, mtime)
        while len(_cons) > MAX_OPEN:              # keep the most recently used files open
            _close(_cons.popitem(last=False)[1][0])
        return con


def _close(con) -> None:
    try:
        con.close()
    except Exception:
        pass


def query(dataset_id: str, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """Run SQL against one dataset file. The table is always called `data`."""
    for attempt in (1, 2):
        cur = _connection(dataset_id).cursor()
        try:
            res = cur.execute(sql, list(params))
            cols = [d[0] for d in res.description]
            return [dict(zip(cols, row)) for row in res.fetchall()]
        except duckdb.ConnectionException:
            # another thread swapped in a newer upload between cursor() and execute(): retry once
            if attempt == 2:
                raise
        finally:
            _close(cur)


def evict(dataset_id: str) -> None:
    with _lock:
        hit = _cons.pop(dataset_id, None)
    if hit:
        _close(hit[0])


def reset() -> None:
    """Close every cached connection (tests, shutdown)."""
    with _lock:
        items = list(_cons.values())
        _cons.clear()
    for con, _ in items:
        _close(con)
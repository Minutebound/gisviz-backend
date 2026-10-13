"""
app/services/duckdb_service.py
==============================
The API's client for the analytical store (gisviz.duckdb, one table per dataset code).

* DUCKDB_SERVICE_URL set (Docker / production): every call goes over HTTP to the `duckdb`
  container, the only process that opens gisviz.duckdb. Any number of API workers can share it.
* DUCKDB_SERVICE_URL unset (local dev without Docker, tests): the same engine runs embedded in
  this process. Use ONE worker in that mode.

Chart/map SQL refers to the dataset as `data`; the store maps it to the dataset's table.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

SERVICE_URL = os.getenv("DUCKDB_SERVICE_URL", "").rstrip("/")
TOKEN = os.getenv("DUCKDB_SERVICE_TOKEN", "")
TIMEOUT = float(os.getenv("DUCKDB_SERVICE_TIMEOUT", "60"))
UPLOAD_TIMEOUT = float(os.getenv("DUCKDB_UPLOAD_TIMEOUT", "1800"))

_client = None


class StoreUnavailable(RuntimeError):
    """The dataset has no table in the store (not uploaded yet, or dropped)."""


class EngineError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _http():
    global _client
    if _client is None:
        import httpx
        _client = httpx.Client(base_url=SERVICE_URL, timeout=TIMEOUT,
                               headers={"X-Duckdb-Token": TOKEN} if TOKEN else {},
                               limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _client


def _call(method: str, path: str, *, timeout: float | None = None, **kw):
    import httpx
    try:
        r = _http().request(method, path, timeout=timeout or TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise EngineError(f"The DuckDB service is not reachable ({type(e).__name__})", 503)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text[:300]
        if r.status_code == 404 and detail == "Not Found":
            # the route itself is missing: wrong DUCKDB_SERVICE_URL, or the duckdb container runs old code
            raise EngineError(f"The DuckDB service at {SERVICE_URL} has no {path} endpoint; check "
                              "DUCKDB_SERVICE_URL points to the duckdb container and rebuild it", 502)
        if r.status_code == 404:
            raise StoreUnavailable(detail or "No data loaded")
        raise EngineError(str(detail), r.status_code)
    return r.json()


def _embedded():
    from app.duckstore.engine import get_engine
    return get_engine()


def _wrap(fn, *a, **kw):
    from app.duckstore.engine import EngineError as _EE
    try:
        return fn(*a, **kw)
    except _EE as e:
        if e.status == 404:
            raise StoreUnavailable(e.message)
        raise EngineError(e.message, e.status)


# ── public API ───────────────────────────────────────────────────────

def query(dataset_id: str, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    if SERVICE_URL:
        return _call("POST", f"/tables/{dataset_id}/query", json={"sql": sql, "params": list(params)})
    return _wrap(_embedded().query, dataset_id, sql, params)


def load(dataset_id: str, src: Path, original_name: str) -> dict:
    """Create/replace the dataset's table from an uploaded file; returns its profile + version."""
    if SERVICE_URL:
        with open(src, "rb") as fh:
            return _call("POST", f"/tables/{dataset_id}", timeout=UPLOAD_TIMEOUT,
                         files={"file": (Path(original_name).name, fh)}, data={"name": Path(original_name).name})
    return _wrap(_embedded().load, dataset_id, Path(src), original_name)


def stage(dataset_id: str, src: Path, original_name: str) -> dict:
    """Load an upload as <id>__staged and return its profile; the live table is untouched."""
    if SERVICE_URL:
        with open(src, "rb") as fh:
            return _call("POST", f"/tables/{dataset_id}/stage", timeout=UPLOAD_TIMEOUT,
                         files={"file": (Path(original_name).name, fh)}, data={"name": Path(original_name).name})
    return _wrap(_embedded().stage, dataset_id, Path(src), original_name)


def staged_sample(dataset_id: str, changes: Optional[list] = None, limit: int = 20) -> dict:
    """Columns + sample rows of a staged upload, with column edits tried on them (not kept)."""
    if SERVICE_URL:
        return _call("POST", f"/tables/{dataset_id}/staged/sample", json={"changes": changes or [], "limit": limit})
    return _wrap(_embedded().staged_sample, dataset_id, changes, limit)


def apply_changes(dataset_id: str, changes: Optional[list]) -> dict:
    """Rebuild the staged upload with the column edits; returns its new profile."""
    if SERVICE_URL:
        return _call("POST", f"/tables/{dataset_id}/staged/apply", json={"changes": changes or []}, timeout=UPLOAD_TIMEOUT)
    return _wrap(_embedded().apply_changes, dataset_id, changes)


def commit(dataset_id: str) -> dict:
    """Make the staged upload live (replaces the table in one transaction)."""
    if SERVICE_URL:
        return _call("POST", f"/tables/{dataset_id}/commit", timeout=UPLOAD_TIMEOUT)
    return _wrap(_embedded().commit, dataset_id)


def discard(dataset_id: str) -> bool:
    """Throw away a staged upload."""
    if SERVICE_URL:
        return bool(_call("DELETE", f"/tables/{dataset_id}/staged").get("discarded"))
    return _wrap(_embedded().discard, dataset_id)


def drop(dataset_id: str) -> bool:
    if SERVICE_URL:
        return bool(_call("DELETE", f"/tables/{dataset_id}").get("dropped"))
    return _wrap(_embedded().drop, dataset_id)


def tables() -> list[dict]:
    if SERVICE_URL:
        return _call("GET", "/tables")
    return _wrap(_embedded().tables)


def stats() -> dict:
    """File, size, table and row counts of gisviz.duckdb."""
    if SERVICE_URL:
        return _call("GET", "/stats", timeout=10)
    return _wrap(_embedded().stats)


def describe() -> str:
    return SERVICE_URL or "embedded (no DUCKDB_SERVICE_URL)"


def preview(dataset_id: str, limit: int = 50) -> dict:
    if SERVICE_URL:
        return _call("GET", f"/tables/{dataset_id}/preview", params={"limit": limit})
    return _wrap(_embedded().preview, dataset_id, limit)


def evict(dataset_id: str) -> None:
    """Kept for compatibility (nothing is cached per dataset any more)."""
    return None

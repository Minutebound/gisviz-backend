"""
app/duckstore/server.py
=======================
The `duckdb` container: a tiny internal HTTP service and the ONLY process that opens
gisviz.duckdb. Run with ONE worker:

    uvicorn app.duckstore.server:app --host 0.0.0.0 --port 8100 --workers 1

Not published to the internet: only the API reaches it on the Docker network, and every request
must carry the shared secret header X-Duckdb-Token (DUCKDB_SERVICE_TOKEN).
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.duckstore.engine import EngineError, get_engine

TOKEN = os.getenv("DUCKDB_SERVICE_TOKEN", "")
app = FastAPI(title="GISVIZ DuckDB service", docs_url=None, redoc_url=None, openapi_url=None)


def auth(x_duckdb_token: Optional[str] = Header(None)):
    if TOKEN and not (x_duckdb_token and secrets.compare_digest(x_duckdb_token, TOKEN)):
        raise HTTPException(status_code=401, detail="bad service token")


@app.exception_handler(EngineError)
async def _engine_error(_req, exc: EngineError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.on_event("startup")
def _start():
    e = get_engine()
    print(f"[duckdb] serving {e.path} ({len(e.tables())} tables)", flush=True)


@app.get("/health")
def health():
    get_engine()
    return {"ok": True}


class QueryBody(BaseModel):
    sql: str
    params: list[Any] = []


@app.get("/tables", dependencies=[Depends(auth)])
def tables():
    return get_engine().tables()


@app.get("/stats", dependencies=[Depends(auth)])
def stats():
    return get_engine().stats()


@app.post("/tables/{code}", dependencies=[Depends(auth)])
def load(code: str, file: UploadFile = File(...), name: str = Form(...)):
    e = get_engine()
    tmp = _receive(e, file, name)
    try:
        return e.load(code, tmp, name)
    finally:
        tmp.unlink(missing_ok=True)


def _receive(e, file: UploadFile, name: str) -> Path:
    fd, tmp = tempfile.mkstemp(dir=e.incoming, suffix=Path(name).suffix)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(file.file, out, length=1 << 20)
    return Path(tmp)


@app.post("/tables/{code}/stage", dependencies=[Depends(auth)])
def stage(code: str, file: UploadFile = File(...), name: str = Form(...)):
    """Build <code>__staged; the live table is untouched until /commit."""
    e = get_engine()
    tmp = _receive(e, file, name)
    try:
        return e.stage(code, tmp, name)
    finally:
        tmp.unlink(missing_ok=True)


class ChangesBody(BaseModel):
    changes: list[dict] = []
    limit: int = 20


@app.post("/tables/{code}/staged/sample", dependencies=[Depends(auth)])
def staged_sample(code: str, body: ChangesBody):
    """The staged upload with (optional) column edits applied to a sample, without keeping them."""
    return get_engine().staged_sample(code, body.changes, body.limit)


@app.post("/tables/{code}/staged/apply", dependencies=[Depends(auth)])
def staged_apply(code: str, body: ChangesBody):
    """Apply the column edits to the staged table (before commit)."""
    return get_engine().apply_changes(code, body.changes)


@app.post("/tables/{code}/commit", dependencies=[Depends(auth)])
def commit(code: str):
    return get_engine().commit(code)


@app.delete("/tables/{code}/staged", dependencies=[Depends(auth)])
def discard(code: str):
    return {"discarded": get_engine().discard(code)}


@app.delete("/tables/{code}", dependencies=[Depends(auth)])
def drop(code: str):
    return {"dropped": get_engine().drop(code)}


@app.post("/tables/{code}/query", dependencies=[Depends(auth)])
def query(code: str, body: QueryBody):
    return get_engine().query(code, body.sql, body.params)


@app.get("/tables/{code}/preview", dependencies=[Depends(auth)])
def preview(code: str, limit: int = 50):
    return get_engine().preview(code, limit)

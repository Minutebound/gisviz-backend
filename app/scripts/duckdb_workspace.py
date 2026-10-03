"""
app/scripts/duckdb_workspace.py
===============================
The `duckdb` container. One process owns the DuckDB WORKSPACE file read-write and:

  1. serves the DuckDB UI (http://localhost:4213 through an SSH tunnel on the VPS) where you create
     and load tables - one table per dataset, the table name is the dataset id
     (lowercase letters, digits, underscores; tables starting with "_" are ignored);
  2. every PUBLISH_SECONDS, publishes each new/changed table as DATASETS_DIR/<table>.duckdb
     (atomic swap), and removes the published file when you DROP the table.

The API never touches the workspace: it reads the published files and registers them in the
datasets database (app/services/dataset_sync.py). Because the UI and the publisher run in the
same process, there is no DuckDB file-lock conflict.

Geometry for maps: a GEOMETRY column (spatial extension), a `geometry`/`geom` column holding
GeoJSON or WKT text, or `lat`/`lon` (`latitude`/`longitude`) columns.

    python -m app.scripts.duckdb_workspace          (the container command)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import duckdb

from app.services import dataset_ingest
from app.services import duckdb_service as store

WORKSPACE_DB = Path(os.getenv("WORKSPACE_DB", "/app/workspace/gisviz.duckdb"))
PUBLISH_SECONDS = int(os.getenv("PUBLISH_SECONDS", "10"))
UI_PORT = int(os.getenv("DUCKDB_UI_PORT", "4213"))
PROXY_PORT = int(os.getenv("DUCKDB_UI_PROXY_PORT", "4214"))
SEED = os.getenv("GISVIZ_SEED_SAMPLES", "false").lower() in ("1", "true", "yes")
ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def log(msg: str) -> None:
    print(f"[workspace] {msg}", flush=True)


# ── DuckDB UI (+ a tiny TCP proxy, because the UI only listens on localhost) ─────────────

def _ui_target():
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect((host, UI_PORT))
                return host
        except OSError:
            pass
    return None


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _run_proxy(host: str) -> None:
    async def handle(cr, cw):
        try:
            ur, uw = await asyncio.open_connection(host, UI_PORT)
        except OSError:
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))

    async def main():
        server = await asyncio.start_server(handle, "0.0.0.0", PROXY_PORT)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


def start_ui(con) -> None:
    try:
        con.execute("INSTALL ui; LOAD ui;")
        con.execute(f"SET ui_local_port = {UI_PORT}")
        con.execute("CALL start_ui_server()")
    except Exception as e:
        log(f"DuckDB UI not started ({str(e).splitlines()[0][:160]}); publishing still runs")
        return
    host = None
    for _ in range(40):
        host = _ui_target()
        if host:
            break
        time.sleep(0.25)
    if not host:
        log("DuckDB UI did not start listening; publishing still runs")
        return
    threading.Thread(target=_run_proxy, args=(host,), daemon=True).start()
    log(f"DuckDB UI ready -> http://localhost:{UI_PORT} (container port {PROXY_PORT})")


# ── publishing ────────────────────────────────────────────────────────

def _tables(cur) -> dict[str, list[tuple[str, str]]]:
    rows = cur.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = 'main' AND NOT internal ORDER BY table_name").fetchall()
    out = {}
    for (t,) in rows:
        out[t] = [(r[0], r[1]) for r in cur.execute(f'DESCRIBE main."{t}"').fetchall()]
    return out


def _fingerprint(cur, table: str, cols) -> str:
    n, h = cur.execute(f'SELECT count(*), bit_xor(hash(t)) FROM main."{table}" t').fetchone()
    sig = hashlib.sha1(repr(cols).encode()).hexdigest()[:12]      # stable across restarts
    return f"{n}:{h}:{sig}"


def publish_table(cur, table: str, cols) -> dict:
    """Workspace table -> Parquet (GEOMETRY as GeoJSON text) -> DATASETS_DIR/<table>.duckdb."""
    geom_cols = [c for c, t in cols if t.upper().startswith("GEOMETRY")]
    select = "*"
    if geom_cols:
        cur.execute("LOAD spatial")
        select = "* REPLACE (" + ", ".join(f'ST_AsGeoJSON("{c}") AS "{c}"' for c in geom_cols) + ")"
    with tempfile.TemporaryDirectory(dir=store.DATASETS_DIR) as d:
        pq = Path(d) / f"{table}.parquet"
        cur.execute(f"COPY (SELECT {select} FROM main.\"{table}\") TO '{pq}' (FORMAT parquet)")
        return dataset_ingest.build_file(table, pq, pq.name)


def publish_once(con, state: dict, warned: set) -> None:
    cur = con.cursor()
    try:
        tables = _tables(cur)
        published = dict(cur.execute("SELECT table_name, fingerprint FROM _gisviz.published").fetchall())
        for table, cols in tables.items():
            if table.startswith("_"):
                continue
            if not ID_RE.match(table):
                if table not in warned:
                    log(f"skipping table '{table}': rename it to lowercase letters, digits and underscores")
                    warned.add(table)
                continue
            fp = _fingerprint(cur, table, cols)
            if published.get(table) == fp and store.path_for(table).exists():
                continue
            try:
                info = publish_table(cur, table, cols)
                cur.execute("INSERT OR REPLACE INTO _gisviz.published VALUES (?, ?, ?)",
                            [table, fp, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)])
                log(f"published {table}: {info['rows']} rows, {len(info['columns'])} columns, geometry={info['geometry_type']}")
                state.pop(table, None)
            except Exception as e:
                msg = str(e).splitlines()[0][:200]
                if state.get(table) != msg:                 # log each distinct failure once
                    log(f"could not publish {table}: {msg}")
                    state[table] = msg
        for table in set(published) - set(tables):          # dropped in the UI -> unpublish
            store.path_for(table).unlink(missing_ok=True)
            cur.execute("DELETE FROM _gisviz.published WHERE table_name = ?", [table])
            log(f"unpublished {table} (table dropped)")
    finally:
        cur.close()


def main() -> int:
    WORKSPACE_DB.parent.mkdir(parents=True, exist_ok=True)
    store.DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WORKSPACE_DB))
    con.execute("CREATE SCHEMA IF NOT EXISTS _gisviz")
    con.execute("CREATE TABLE IF NOT EXISTS _gisviz.published (table_name VARCHAR PRIMARY KEY, "
                "fingerprint VARCHAR, published_at TIMESTAMP)")
    log(f"workspace {WORKSPACE_DB} -> publishing to {store.DATASETS_DIR} every {PUBLISH_SECONDS}s")
    if SEED:
        from app.scripts.seed_samples import load_into
        made = load_into(con)
        if made:
            log(f"created sample tables: {', '.join(made)}")
    start_ui(con)
    state, warned = {}, set()
    while True:
        try:
            publish_once(con, state, warned)
        except Exception as e:
            log(f"publish pass failed: {e}")
        time.sleep(PUBLISH_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
"""
app/duckstore/engine.py
=======================
The analytical store: ONE DuckDB database file, gisviz.duckdb, holding one table per dataset,
named by the dataset's code (ds_00001, ds_00002, ...). Exactly one process opens it read-write
(the `duckdb` container, app/duckstore/server.py) so there is never a file-lock conflict; the API
and every API worker talk to it over HTTP.

Table layout: the uploaded columns (snake_case, unique) + _gv_id (row order) + _gv_geom (GeoJSON
geometry text, when the data has geometry).

Loading is two-step so a replacement can be checked before it goes live:
  stage(code, file)  -> table <code>__staged (the live table <code> is untouched)
  commit(code)       -> <code>__staged becomes <code> in one transaction
  discard(code)      -> drop <code>__staged
load() = stage + commit. Staged tables are internal: never listed by tables() and removed at start-up.

Safety:
* table names are only ever dataset codes matching ^[a-z0-9_]{1,64}$ (quoted);
* file access is sandboxed to the incoming folder (allowed_directories + enable_external_access=false,
  locked), so queries cannot read or write any other files.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb

from app.duckstore import convert
from app.services import dataset_ingest as di

ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
INTERNAL_SCHEMA = "_gisviz"
STAGED = "__staged"
# column types the admin can convert to in the upload previewer (Compass-style)
CAST_TYPES = {"text": "VARCHAR", "integer": "BIGINT", "decimal": "DOUBLE", "date": "DATE",
              "datetime": "TIMESTAMP", "boolean": "BOOLEAN"}


def _kind(sql_type: str) -> str:
    t = sql_type.split("(")[0].upper()
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"):
        return "integer"
    if t in ("FLOAT", "DOUBLE", "DECIMAL", "REAL"):
        return "decimal"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "datetime"
    if t == "BOOLEAN":
        return "boolean"
    return "text"


def _staged(code: str) -> str:
    return f"{code}{STAGED}"


class EngineError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _json_safe(v: Any) -> Any:
    import datetime as dt
    import decimal
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if isinstance(v, (bytes, bytearray)):
        return None
    return v


class Engine:
    def __init__(self, db_path: str | Path, threads: int = 4, memory_limit: str = "2GB"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.incoming = self.path.parent / "incoming"
        if self.incoming.exists():
            shutil.rmtree(self.incoming, ignore_errors=True)       # leftovers from a crash
        self.incoming.mkdir(parents=True, exist_ok=True)
        self._threads, self._memory = int(threads), memory_limit
        self._open()
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {INTERNAL_SCHEMA}")
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {INTERNAL_SCHEMA}.tables ("
                         "code VARCHAR PRIMARY KEY, source_file VARCHAR, format VARCHAR, rows BIGINT, "
                         "data_version VARCHAR, loaded_at TIMESTAMP DEFAULT current_timestamp)")
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {INTERNAL_SCHEMA}.staged ("
                         "code VARCHAR PRIMARY KEY, source_file VARCHAR, format VARCHAR, rows BIGINT, "
                         "data_version VARCHAR, staged_at TIMESTAMP DEFAULT current_timestamp)")
        self.con.execute(f"ALTER TABLE {INTERNAL_SCHEMA}.staged ADD COLUMN IF NOT EXISTS bytes BIGINT")
        self._drop_leftovers()
        self._write = threading.Lock()                               # one load/drop at a time

    def _open(self) -> None:
        self.con = duckdb.connect(str(self.path))
        self.con.execute(f"SET threads = {self._threads}")
        self.con.execute(f"SET memory_limit = '{self._memory}'")
        self.con.execute(f"SET temp_directory = '{self.path.parent / 'tmp'}'")
        self.con.execute(f"SET allowed_directories = ['{self.incoming}/', '{self.path.parent / 'tmp'}/']")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

    def _recover(self, err: Exception) -> bool:
        """DuckDB invalidates the database after an internal/fatal error: reopen it (committed data is
        safe in the file). Returns True when it recovered, so callers can report a clean error."""
        msg = str(err)
        if not isinstance(err, (duckdb.FatalException, duckdb.InternalException)) and "invalidated" not in msg:
            return False
        print(f"[duckdb] reopening {self.path} after: {msg.splitlines()[0][:200]}", flush=True)
        try:
            self.con.close()
        except Exception:
            pass
        self._open()
        return True

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def check(code: str) -> str:
        if not ID_RE.match(code or ""):
            raise EngineError("Invalid dataset code")
        return code

    def _cursor(self):
        return self.con.cursor()

    def exists(self, code: str) -> bool:
        cur = self._cursor()
        try:
            return bool(cur.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() "
                                    "AND schema_name = 'main' AND table_name = ?", [code]).fetchone()[0])
        finally:
            cur.close()

    def _drop_leftovers(self) -> None:
        """Half-built tables from a crash (<code>__new_*) and uncommitted staged uploads."""
        rows = self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() AND schema_name = 'main' "
            f"AND (table_name LIKE '%\\_\\_new\\_%' ESCAPE '\\' OR table_name LIKE '%{STAGED}')").fetchall()
        for (t,) in rows:
            self.con.execute(f'DROP TABLE IF EXISTS main."{t}"')
        self.con.execute(f"DELETE FROM {INTERNAL_SCHEMA}.staged")

    # ── writes ────────────────────────────────────────────────────────
    def stage(self, code: str, src: Path, original_name: str) -> dict:
        """Build table <code>__staged from an uploaded file and return its profile. The live table
        <code> is not touched until commit(code)."""
        self.check(code)
        work = self.incoming / uuid.uuid4().hex
        work.mkdir()
        staged_file = work / Path(original_name).name
        shutil.copyfile(str(src), staged_file)                     # into the sandboxed incoming folder
        size = staged_file.stat().st_size
        new = f"{code}__new_{uuid.uuid4().hex[:8]}"
        target = _staged(code)
        cur = self._cursor()
        try:
            with self._write:
                try:
                    readable, fmt = convert.normalize(staged_file, staged_file.name, work)   # any format -> DuckDB-readable
                    di.stage_file(cur, readable, readable.name, tmp_dir=work)
                    di._create_data(cur, new)
                    info = di.profile(cur, new)
                    n, h = cur.execute(f'SELECT count(*), bit_xor(hash(t)) FROM main."{new}" t').fetchone()
                    version = f"{int(n):x}-{int(h) & 0xffffffffffff:x}"
                    cur.execute("BEGIN TRANSACTION")
                    cur.execute(f'DROP TABLE IF EXISTS main."{target}"')
                    cur.execute(f'ALTER TABLE main."{new}" RENAME TO "{target}"')
                    cur.execute(f"INSERT OR REPLACE INTO {INTERNAL_SCHEMA}.staged (code, source_file, format, rows, data_version, bytes) "
                                "VALUES (?, ?, ?, ?, ?, ?)", [code, staged_file.name, fmt, info["rows"], version, size])
                    cur.execute("COMMIT")
                except (di.IngestError, convert.ConvertError) as e:
                    self._cleanup(cur, new)
                    raise EngineError(str(e), 422)
                except duckdb.Error as e:
                    if self._recover(e):
                        raise EngineError("The file could not be loaded (it uses a feature DuckDB could not "
                                          "read); convert it to plain Parquet or CSV and try again", 422)
                    self._cleanup(cur, new)
                    raise EngineError(f"Could not read the file: {str(e).splitlines()[0][:300]}", 422)
        finally:
            cur.close()
            shutil.rmtree(work, ignore_errors=True)
        return {**info, "format": fmt, "data_version": version, "table": code, "staged": True, "bytes": size}

    # ── upload previewer: look at a staged upload, try column edits, apply them ──
    def _staged_select(self, cur, table: str, changes: Optional[list] = None) -> tuple[str, list[dict]]:
        """SELECT list rebuilding `table` with the admin's edits: rename, convert type, drop column."""
        schema = [(r[0], r[1]) for r in cur.execute(f'DESCRIBE main."{table}"').fetchall()]
        by = {}
        for ch in changes or []:
            if isinstance(ch, dict) and ch.get("column"):
                by[str(ch["column"])] = ch
        unknown = set(by) - {n for n, _ in schema}
        if unknown:
            raise EngineError(f"Unknown column(s): {', '.join(sorted(unknown))}", 422)
        cols, names = [], []
        for name, sql_type in schema:
            if name in ("_gv_id", "_gv_geom"):
                continue
            ch = by.get(name, {})
            if ch.get("include") is False:
                continue
            new_type = ch.get("type") or None
            if new_type and new_type not in CAST_TYPES:
                raise EngineError(f"Unknown type '{new_type}' (use {', '.join(CAST_TYPES)})", 422)
            if new_type == _kind(sql_type):
                new_type = None
            cols.append({"source": name, "sql_type": sql_type, "kind": _kind(sql_type), "type": new_type})
            names.append(di.snake(str(ch.get("rename") or name)) or name)
        if not cols:
            raise EngineError("Keep at least one column", 422)
        for c, n in zip(cols, di.unique_names(names)):
            c["name"] = n
        parts = []
        for c in cols:
            expr = f'TRY_CAST({di.q(c["source"])} AS {CAST_TYPES[c["type"]]})' if c["type"] else di.q(c["source"])
            parts.append(f'{expr} AS {di.q(c["name"])}')
        parts.append("_gv_id")
        if any(n == "_gv_geom" for n, _ in schema):
            parts.append("_gv_geom")
        return ", ".join(parts), cols

    def staged_sample(self, code: str, changes: Optional[list] = None, limit: int = 20) -> dict:
        """The staged upload as the previewer shows it: columns (detected type, edit), sample rows, and how
        many values each type conversion would lose (they become empty)."""
        self.check(code)
        target = _staged(code)
        if not self.exists(target):
            raise EngineError(f"No staged upload for {code}", 404)
        cur = self._cursor()
        try:
            select, cols = self._staged_select(cur, target, changes)
            meta = cur.execute(f"SELECT source_file, format, rows, bytes FROM {INTERNAL_SCHEMA}.staged WHERE code = ?",
                               [code]).fetchone() or (None, None, None, None)
            res = cur.execute(f'SELECT {select} FROM main."{target}" ORDER BY _gv_id LIMIT {max(1, min(int(limit), 200))}')
            names = [d[0] for d in res.description]
            rows = []
            for r in res.fetchall():
                row = {k: _json_safe(v) for k, v in zip(names, r) if k != "_gv_id"}
                g = row.get("_gv_geom")
                if isinstance(g, str) and len(g) > 80:
                    row["_gv_geom"] = g[:77] + "..."
                rows.append(row)
            lost = {}
            casts = [c for c in cols if c["type"]]
            if casts:
                q = ", ".join(f'count(*) FILTER (WHERE {di.q(c["source"])} IS NOT NULL AND '
                              f'TRY_CAST({di.q(c["source"])} AS {CAST_TYPES[c["type"]]}) IS NULL)' for c in casts)
                counts = cur.execute(f'SELECT {q} FROM main."{target}"').fetchone()
                lost = {c["name"]: int(n) for c, n in zip(casts, counts)}
            nulls = cur.execute("SELECT " + ", ".join(
                f'count(*) - count({di.q(c["source"])})' for c in cols) + f' FROM main."{target}"').fetchone()
            for c, n in zip(cols, nulls):
                c["empty"] = int(n)
                c["lost"] = lost.get(c["name"], 0)
            return {"code": code, "source_file": meta[0], "format": meta[1], "rows": meta[2], "bytes": meta[3],
                    "columns": cols, "sample": rows, "types": list(CAST_TYPES)}
        except duckdb.Error as e:
            raise EngineError(str(e).splitlines()[0][:300], 400)
        finally:
            cur.close()

    def apply_changes(self, code: str, changes: Optional[list]) -> dict:
        """Rebuild the staged table with the edits (renamed / converted / dropped columns); returns the new
        profile. The live table is still untouched until commit()."""
        self.check(code)
        target = _staged(code)
        if not changes:
            cur = self._cursor()
            try:
                info = di.profile(cur, target)
                version = cur.execute(f"SELECT data_version FROM {INTERNAL_SCHEMA}.staged WHERE code = ?", [code]).fetchone()
            finally:
                cur.close()
            return {**info, "data_version": version[0] if version else None}
        new = f"{code}__new_{uuid.uuid4().hex[:8]}"
        cur = self._cursor()
        try:
            with self._write:
                if not self.exists(target):
                    raise EngineError(f"No staged upload for {code}", 404)
                select, _ = self._staged_select(cur, target, changes)
                try:
                    cur.execute(f'CREATE TABLE main."{new}" AS SELECT {select} FROM main."{target}" ORDER BY _gv_id')
                    info = di.profile(cur, new)
                    n, h = cur.execute(f'SELECT count(*), bit_xor(hash(t)) FROM main."{new}" t').fetchone()
                    version = f"{int(n):x}-{int(h) & 0xffffffffffff:x}"
                    cur.execute("BEGIN TRANSACTION")
                    cur.execute(f'DROP TABLE main."{target}"')
                    cur.execute(f'ALTER TABLE main."{new}" RENAME TO "{target}"')
                    cur.execute(f"UPDATE {INTERNAL_SCHEMA}.staged SET rows = ?, data_version = ? WHERE code = ?",
                                [info["rows"], version, code])
                    cur.execute("COMMIT")
                except duckdb.Error as e:
                    self._cleanup(cur, new)
                    raise EngineError(f"Could not apply the column changes: {str(e).splitlines()[0][:300]}", 422)
        finally:
            cur.close()
        return {**info, "data_version": version}

    def commit(self, code: str) -> dict:
        """Make the staged upload live: <code>__staged replaces <code> in one transaction."""
        self.check(code)
        target = _staged(code)
        cur = self._cursor()
        try:
            with self._write:
                meta = cur.execute(f"SELECT source_file, format, rows, data_version FROM {INTERNAL_SCHEMA}.staged "
                                   "WHERE code = ?", [code]).fetchone()
                if meta is None or not self.exists(target):
                    raise EngineError(f"No staged upload for {code}", 404)
                cur.execute("BEGIN TRANSACTION")
                cur.execute(f'DROP TABLE IF EXISTS main."{code}"')
                cur.execute(f'ALTER TABLE main."{target}" RENAME TO "{code}"')
                cur.execute(f"INSERT OR REPLACE INTO {INTERNAL_SCHEMA}.tables (code, source_file, format, rows, data_version) "
                            "VALUES (?, ?, ?, ?, ?)", [code, *meta])
                cur.execute(f"DELETE FROM {INTERNAL_SCHEMA}.staged WHERE code = ?", [code])
                cur.execute("COMMIT")
                cur.execute("CHECKPOINT")
        except duckdb.Error as e:
            self._cleanup(cur, "")
            raise EngineError(f"Could not switch to the new data: {str(e).splitlines()[0][:300]}", 500)
        finally:
            cur.close()
        return {"code": code, "source_file": meta[0], "format": meta[1], "rows": meta[2], "data_version": meta[3]}

    def discard(self, code: str) -> bool:
        """Drop an uncommitted staged upload (the live table stays as it was)."""
        self.check(code)
        cur = self._cursor()
        try:
            with self._write:
                existed = self.exists(_staged(code))
                cur.execute(f'DROP TABLE IF EXISTS main."{_staged(code)}"')
                cur.execute(f"DELETE FROM {INTERNAL_SCHEMA}.staged WHERE code = ?", [code])
                return existed
        finally:
            cur.close()

    def load(self, code: str, src: Path, original_name: str) -> dict:
        """Create or replace table <code> from an uploaded file in one go (stage + commit).
        Atomic: readers see the old table until the new one is complete."""
        info = self.stage(code, src, original_name)
        self.commit(code)
        info.pop("staged", None)
        return info

    @staticmethod
    def _cleanup(cur, new: str) -> None:
        try:
            cur.execute("ROLLBACK")
        except duckdb.Error:
            pass
        if not new:
            return
        try:
            cur.execute(f'DROP TABLE IF EXISTS main."{new}"')
        except duckdb.Error:
            pass

    def drop(self, code: str) -> bool:
        self.check(code)
        cur = self._cursor()
        try:
            with self._write:
                existed = self.exists(code)
                cur.execute(f'DROP TABLE IF EXISTS main."{code}"')
                cur.execute(f'DROP TABLE IF EXISTS main."{_staged(code)}"')
                cur.execute(f"DELETE FROM {INTERNAL_SCHEMA}.tables WHERE code = ?", [code])
                cur.execute(f"DELETE FROM {INTERNAL_SCHEMA}.staged WHERE code = ?", [code])
                cur.execute("CHECKPOINT")
                return existed
        finally:
            cur.close()

    # ── reads ─────────────────────────────────────────────────────────
    def query(self, code: str, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        """Run the API's chart/map SQL. The SQL refers to the dataset as `data`."""
        self.check(code)
        if not self.exists(code):
            raise EngineError(f"No data loaded for {code}", 404)
        cur = self._cursor()
        try:
            res = cur.execute(f'WITH data AS (SELECT * FROM main."{code}") {sql}', list(params))
            cols = [d[0] for d in res.description]
            return [dict(zip(cols, (_json_safe(v) for v in row))) for row in res.fetchall()]
        except duckdb.Error as e:
            if self._recover(e):
                raise EngineError("The analytical store was restarted after an internal error; please retry", 503)
            raise EngineError(str(e).splitlines()[0][:300], 400)
        finally:
            cur.close()

    def tables(self) -> list[dict]:
        cur = self._cursor()
        try:
            rows = cur.execute(
                f"SELECT t.table_name AS code, t.estimated_size AS rows, m.data_version, m.source_file, m.format, "
                f"m.loaded_at FROM duckdb_tables() t LEFT JOIN {INTERNAL_SCHEMA}.tables m ON m.code = t.table_name "
                "WHERE t.database_name = current_database() AND t.schema_name = 'main' "
                f"AND t.table_name NOT LIKE '%{STAGED}' AND t.table_name NOT LIKE '%\\_\\_new\\_%' ESCAPE '\\' ORDER BY 1").fetchall()
            cols = ["code", "rows", "data_version", "source_file", "format", "loaded_at"]
            return [dict(zip(cols, (_json_safe(v) for v in r))) for r in rows]
        finally:
            cur.close()

    def stats(self) -> dict:
        """What the admin page shows about the store: file, size, tables, rows, last load."""
        size = self.path.stat().st_size if self.path.exists() else 0
        wal = Path(str(self.path) + ".wal")
        cur = self._cursor()
        try:
            n, rows, last = cur.execute(
                f"SELECT count(*), coalesce(sum(m.rows), 0), max(m.loaded_at) FROM duckdb_tables() t "
                f"LEFT JOIN {INTERNAL_SCHEMA}.tables m ON m.code = t.table_name "
                "WHERE t.database_name = current_database() AND t.schema_name = 'main' "
                f"AND t.table_name NOT LIKE '%{STAGED}' AND t.table_name NOT LIKE '%\\_\\_new\\_%' ESCAPE '\\'").fetchone()
        finally:
            cur.close()
        return {"file": str(self.path), "size_bytes": size, "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "tables": int(n), "rows": int(rows), "last_loaded_at": _json_safe(last),
                "duckdb_version": duckdb.__version__}

    def preview(self, code: str, limit: int = 50) -> dict:
        rows = self.query(code, f"SELECT * EXCLUDE (_gv_id) FROM data ORDER BY _gv_id LIMIT {max(1, min(int(limit), 500))}")
        for r in rows:                                            # geometry text is long: trim it
            g = r.get("_gv_geom")
            if isinstance(g, str) and len(g) > 120:
                r["_gv_geom"] = g[:117] + "..."
        return {"code": code, "rows": rows}


_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Process-wide engine (the DuckDB service; or the API itself in embedded/dev mode)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine(os.getenv("DUCKDB_PATH", "data/duckdb/gisviz.duckdb"),
                             threads=int(os.getenv("DUCKDB_THREADS", "4")),
                             memory_limit=os.getenv("DUCKDB_MEMORY_LIMIT", "2GB"))
        return _engine


def reset_engine() -> None:
    """Tests: close and forget the engine."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.con.close()
            except Exception:
                pass
        _engine = None

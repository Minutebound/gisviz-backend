"""
app/services/catalog_service.py
===============================
Dataset catalog in Postgres (the DATASETS database, table `datasets`). Posts link to it by
dataset_id; the /datasets page and the post picker list it.

Rows are created and refreshed by dataset_sync from the published DuckDB files; managers
edit the descriptive fields and the status on /datasets/manage.

Lifecycle of a row
  draft   - found in DuckDB, not published yet (title = id until edited) -> hidden from readers/pickers
  active  - published by a manager                                       -> listed everywhere
  hidden  - temporarily hidden, or its DuckDB table was removed; existing posts keep their PNG
  retired - no longer offered; existing posts keep working

The column profile the chart suggester needs is stored on the row (`columns`, JSONB),
so listing/searching never opens DuckDB files.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import case, func, or_

from app.db.database import DatasetsSessionLocal, PostsSessionLocal
from app.db.models import DatasetRecord

STATUSES = ("draft", "active", "hidden", "retired")
META_FIELDS = ("title", "description", "category", "source_name", "source_url", "license", "publisher")
_FIELDS = ("dataset_id", "title", "description", "category", "format", "source_name", "source_url",
           "license", "publisher", "geometry_type", "row_count", "crs", "bbox", "data_version", "updated_timestamp")


def _card(r: DatasetRecord) -> dict:
    d = {k: getattr(r, k) for k in _FIELDS}
    d["updated_at"] = d.pop("updated_timestamp")
    return d


def _admin_card(r: DatasetRecord) -> dict:
    d = _card(r)
    d.update(status=r.status, column_count=len(r.columns or []),
             created_at=r.created_timestamp, data_uploaded_at=r.data_uploaded_at)
    return d


def _keywords(r: DatasetRecord) -> str:
    cols = " ".join(c.get("label") or c.get("column_name") or "" for c in (r.columns or []))
    return " ".join(filter(None, [r.dataset_id, r.title, r.description, r.category, r.source_name, cols])).lower()


# ── public / publisher reads (active only) ───────────────────────────

def search(term: str, limit: int) -> list[dict]:
    with DatasetsSessionLocal() as db:
        q = db.query(DatasetRecord).filter(DatasetRecord.status == "active")
        term = (term or "").strip().lower()
        if term:
            like = f"%{term}%"
            q = q.filter(or_(DatasetRecord.dataset_id.ilike(like), DatasetRecord.title.ilike(like),
                             DatasetRecord.category.ilike(like), DatasetRecord.keywords.ilike(like)))
            q = q.order_by(case((DatasetRecord.dataset_id == term, 0), else_=1), DatasetRecord.title)
        else:
            q = q.order_by(DatasetRecord.updated_timestamp.desc())
        return [_card(r) for r in q.limit(limit).all()]


def get(dataset_id: str) -> Optional[dict]:
    with DatasetsSessionLocal() as db:
        r = db.query(DatasetRecord).filter(
            DatasetRecord.dataset_id == dataset_id, DatasetRecord.status == "active").first()
        return _card(r) if r else None


def get_columns(dataset_id: str) -> list[dict]:
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        return list(r.columns or []) if r else []


def list_page(term: str, category: Optional[str], skip: int, limit: int, kind: Optional[str] = None) -> dict:
    """Public /datasets listing: active datasets, optional text / category / kind (spatial|tabular) filter."""
    with DatasetsSessionLocal() as db:
        q = db.query(DatasetRecord).filter(DatasetRecord.status == "active")
        term = (term or "").strip().lower()
        if term:
            like = f"%{term}%"
            q = q.filter(or_(DatasetRecord.dataset_id.ilike(like), DatasetRecord.title.ilike(like),
                             DatasetRecord.description.ilike(like), DatasetRecord.category.ilike(like),
                             DatasetRecord.source_name.ilike(like), DatasetRecord.keywords.ilike(like)))
        if category:
            q = q.filter(DatasetRecord.category == category)
        if kind == "spatial":
            q = q.filter(DatasetRecord.geometry_type.isnot(None))
        elif kind == "tabular":
            q = q.filter(DatasetRecord.geometry_type.is_(None))
        total = q.with_entities(func.count()).scalar()
        rows = q.order_by(DatasetRecord.updated_timestamp.desc(), DatasetRecord.title).offset(skip).limit(limit).all()
        return {"total": total, "items": [_card(r) for r in rows]}


def categories() -> list[dict]:
    with DatasetsSessionLocal() as db:
        rows = (db.query(DatasetRecord.category, func.count())
                .filter(DatasetRecord.status == "active", DatasetRecord.category.isnot(None))
                .group_by(DatasetRecord.category).order_by(DatasetRecord.category).all())
        return [{"category": c, "count": n} for c, n in rows]


def post_count(dataset_id: str) -> int:
    from app.db.models import PostRecord
    with PostsSessionLocal() as db:
        return db.query(func.count()).select_from(PostRecord).filter(
            PostRecord.dataset_id == dataset_id, PostRecord.is_active == 1).scalar() or 0


# ── dataset managers (admin / publisher) ─────────────────────────────

def exists(dataset_id: str) -> bool:
    with DatasetsSessionLocal() as db:
        return db.get(DatasetRecord, dataset_id) is not None


def list_admin(term: str = "", status: Optional[str] = None) -> list[dict]:
    with DatasetsSessionLocal() as db:
        q = db.query(DatasetRecord)
        if status:
            q = q.filter(DatasetRecord.status == status)
        term = (term or "").strip().lower()
        if term:
            like = f"%{term}%"
            q = q.filter(or_(DatasetRecord.dataset_id.ilike(like), DatasetRecord.title.ilike(like)))
        return [_admin_card(r) for r in q.order_by(DatasetRecord.updated_timestamp.desc()).limit(500).all()]


def update_meta(dataset_id: str, meta: dict) -> dict:
    """Edit a dataset's descriptive fields and/or status (rows are created by the sync, not here)."""
    status = meta.get("status")
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        for k in META_FIELDS:
            if k in meta and meta[k] is not None:
                setattr(r, k, (meta[k] or "").strip() or None)
        if not r.title:
            raise ValueError("A title is required")
        if status:
            if status not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            if status == "active" and not r.columns:
                raise ValueError("This dataset has no data in DuckDB yet")
            r.status = status
        r.keywords = _keywords(r)
        db.commit()
        db.refresh(r)
        return _admin_card(r)


def versions() -> dict[str, tuple[Optional[str], str]]:
    """{dataset_id: (data_version, status)} for the sync."""
    with DatasetsSessionLocal() as db:
        return {i: (v, s) for i, v, s in db.query(DatasetRecord.dataset_id, DatasetRecord.data_version, DatasetRecord.status)}


def record_data(dataset_id: str, info: dict) -> bool:
    """Create (as draft) or refresh a dataset row from its published DuckDB file. Returns True if created."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        created = r is None
        if created:
            r = DatasetRecord(dataset_id=dataset_id, title=dataset_id.replace("_", " ").capitalize(), status="draft")
            db.add(r)
        r.columns = info["columns"]
        r.row_count = info["rows"]
        r.geometry_type = info["geometry_type"]
        r.bbox = info["bbox"]
        r.crs = info["crs"]
        r.format = "duckdb"
        r.data_version = info["data_version"]
        r.data_uploaded_at = dt.datetime.now(dt.timezone.utc)
        r.keywords = _keywords(r)
        db.commit()
        return created


def mark_missing(dataset_id: str) -> None:
    """The DuckDB table behind an active row disappeared: hide it (posts keep their PNG)."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is not None and r.data_version is not None:
            if r.status == "active":
                r.status = "hidden"
            r.data_version = None
            r.columns = None
            db.commit()
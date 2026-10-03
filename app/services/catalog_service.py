"""
app/services/catalog_service.py
===============================
Dataset catalog in Postgres (the DATASETS database, table `datasets`). Posts link to it by
dataset_id; the /datasets page and the post picker list it.

Admins manage it on /admin/datasets:
  create  -> a code is generated (ds_00001, ds_00002, ...) and the row starts `inactive`
  upload  -> the Parquet file becomes DATASETS_DIR/<code>.duckdb and the row turns `active`
  toggle  -> active / inactive (inactive = hidden from /datasets and the post picker;
             posts already built on it keep working)
  edit    -> title, description, category, source, licence, publisher
  delete  -> only when no post uses it (otherwise deactivate it)
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import case, func, or_

from app.db.database import DatasetsSessionLocal, PostsSessionLocal
from app.db.models import DatasetRecord

STATUSES = ("inactive", "active")
CODE_PREFIX = "ds_"
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


def get(dataset_id: str, include_inactive: bool = False) -> Optional[dict]:
    """Active dataset card; include_inactive=True also returns deactivated ones that still have data
    (used to keep existing posts working after a dataset is deactivated)."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None or (r.status != "active" and not (include_inactive and r.columns)):
            return None
        return _card(r)


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


# ── dataset managers (admin) ──────────────────────────────────────────

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
        return [_admin_card(r) for r in q.order_by(DatasetRecord.created_timestamp.desc()).limit(1000).all()]


def _clean(meta: dict) -> dict:
    return {k: ((meta[k] or "").strip() or None) for k in META_FIELDS if k in meta and meta[k] is not None}


def next_code(db) -> str:
    from sqlalchemy import text
    n = db.execute(text("SELECT nextval('dataset_code_seq')")).scalar()
    return f"{CODE_PREFIX}{int(n):05d}"


def create(meta: dict) -> dict:
    """New dataset record with a generated code; inactive until its file is uploaded."""
    fields = _clean(meta)
    if not fields.get("title"):
        raise ValueError("A title is required")
    with DatasetsSessionLocal() as db:
        r = DatasetRecord(dataset_id=next_code(db), status="inactive", row_count=0, **fields)
        r.keywords = _keywords(r)
        db.add(r)
        db.commit()
        db.refresh(r)
        return _admin_card(r)


def update_meta(dataset_id: str, meta: dict) -> dict:
    """Edit a dataset's descriptive fields."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        for k, v in _clean(meta).items():
            setattr(r, k, v)
        if "title" in meta and not (meta["title"] or "").strip():
            raise ValueError("A title is required")
        r.keywords = _keywords(r)
        db.commit()
        db.refresh(r)
        return _admin_card(r)


def set_active(dataset_id: str, active: bool) -> dict:
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        if active and not r.columns:
            raise ValueError("Upload the dataset's file before activating it")
        r.status = "active" if active else "inactive"
        db.commit()
        db.refresh(r)
        return _admin_card(r)


def delete(dataset_id: str) -> None:
    """Remove the record. Refused while posts use the dataset (deactivate it instead)."""
    n = post_count(dataset_id)
    if n:
        raise PermissionError(f"{n} post(s) use this dataset; deactivate it instead of deleting")
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        db.delete(r)
        db.commit()


def record_upload(dataset_id: str, info: dict) -> None:
    """Called after the uploaded file is in place: store its profile and make the dataset active."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        r.columns = info["columns"]
        r.row_count = info["rows"]
        r.geometry_type = info["geometry_type"]
        r.bbox = info["bbox"]
        r.crs = info["crs"]
        r.format = info.get("format")
        r.data_version = info["data_version"]
        r.data_uploaded_at = dt.datetime.now(dt.timezone.utc)
        r.status = "active"
        r.keywords = _keywords(r)
        db.commit()
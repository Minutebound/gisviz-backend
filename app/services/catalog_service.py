"""
app/services/catalog_service.py
===============================
Dataset catalog in Postgres (the DATASETS database, table `datasets`). Posts link to it by
dataset_id; the /datasets page and the post picker list it.

Admins manage it on /admin/datasets:
  create  -> a code is generated (ds_00001, ds_00002, ...) and the row starts `inactive`
  upload  -> the Parquet file becomes table <code> in gisviz.duckdb (DuckDB service); row turns `active`
  toggle  -> active / inactive (inactive = hidden from /datasets and the post picker;
             posts already built on it keep working)
  edit    -> title, description, category (posts DB categories.slug), region (misc DB regions.code),
             source, licence, publisher
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
META_FIELDS = ("title", "description", "category", "region", "source_name", "source_url", "license", "publisher",
               "license_code", "attribution", "copyright_holder")
PIPELINES = ("batch", "stream")
_FIELDS = ("dataset_id", "title", "description", "category", "region", "format", "source_name", "source_url",
           "license", "publisher", "geometry_type", "row_count", "crs", "bbox", "data_version", "file_size_bytes",
           "updated_timestamp", "visibility", "pipeline", "refresh_seconds", "retention_rows", "last_ingested_at",
           "license_code", "attribution", "copyright_holder")


def _card(r: DatasetRecord) -> dict:
    from app.services import licences
    d = {k: getattr(r, k) for k in _FIELDS}
    d["updated_at"] = d.pop("updated_timestamp")
    d["show_data"] = bool(r.show_data)
    d["column_count"] = len(r.columns or [])
    d["visibility"] = r.visibility or "public"
    d["pipeline"] = r.pipeline or "batch"
    d["owner_user_id"] = str(r.owner_user_id) if r.owner_user_id else None
    d["contains_personal_data"] = bool(r.contains_personal_data)
    d["licence"] = licences.summary(r.license_code)
    d["has_ingest_key"] = bool(r.ingest_key_hash)
    return d


def _admin_card(r: DatasetRecord) -> dict:
    d = _card(r)
    d.update(status=r.status,
             created_at=r.created_timestamp, data_uploaded_at=r.data_uploaded_at)
    return d


def _keywords(r: DatasetRecord) -> str:
    cols = " ".join(c.get("label") or c.get("column_name") or "" for c in (r.columns or []))
    return " ".join(filter(None, [r.dataset_id, r.title, r.description, r.category, r.region, r.source_name, cols])).lower()


# ── public / publisher reads (active only) ───────────────────────────

def search(term: str, limit: int, viewer=None, pipeline: Optional[str] = None) -> list[dict]:
    """The post picker: active datasets this viewer may use (public + private ones they can see)."""
    from app.services import access_service
    with DatasetsSessionLocal() as db:
        q = db.query(DatasetRecord).filter(DatasetRecord.status == "active")
        q = access_service.visible_dataset_filter(q, viewer or access_service.ANON, db)
        if pipeline in PIPELINES:
            q = q.filter(DatasetRecord.pipeline == pipeline)
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


def list_page(term: str, category: Optional[str], skip: int, limit: int, kind: Optional[str] = None,
              region: Optional[str] = None, viewer=None, pipeline: Optional[str] = None) -> dict:
    """/datasets listing: active datasets the viewer may see (anonymous: public only), optional text / category /
    region (with its sub-regions) / kind (spatial|tabular) / pipeline (batch|stream) filter."""
    from app.services import access_service
    with DatasetsSessionLocal() as db:
        q = db.query(DatasetRecord).filter(DatasetRecord.status == "active")
        q = access_service.visible_dataset_filter(q, viewer or access_service.ANON, db)
        if pipeline in PIPELINES:
            q = q.filter(DatasetRecord.pipeline == pipeline)
        term = (term or "").strip().lower()
        if term:
            like = f"%{term}%"
            q = q.filter(or_(DatasetRecord.dataset_id.ilike(like), DatasetRecord.title.ilike(like),
                             DatasetRecord.description.ilike(like), DatasetRecord.category.ilike(like),
                             DatasetRecord.source_name.ilike(like), DatasetRecord.keywords.ilike(like)))
        if category:
            q = q.filter(DatasetRecord.category == category)
        if region and region.strip().lower() != "global":       # Global = everything; a continent includes its countries
            from app.services import region_service
            q = q.filter(DatasetRecord.region.in_(region_service.with_descendants(region)))
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
                .filter(DatasetRecord.status == "active", DatasetRecord.category.isnot(None),
                        DatasetRecord.visibility != "private")
                .group_by(DatasetRecord.category).order_by(DatasetRecord.category).all())
        return [{"category": c, "count": n} for c, n in rows]


def category_exists(slug: str) -> bool:
    """Dataset categories are the post categories (posts DB, categories.slug)."""
    from app.db.models import CategoryRecord
    with PostsSessionLocal() as db:
        return db.query(CategoryRecord.category_id).filter(CategoryRecord.slug == slug).first() is not None


def region_usage(code: str) -> int:
    """How many datasets are tagged with a region (any status)."""
    with DatasetsSessionLocal() as db:
        return db.query(func.count()).select_from(DatasetRecord).filter(DatasetRecord.region == code).scalar() or 0


def posts_using(dataset_id: str) -> list[dict]:
    """Active posts built on a dataset, with their saved visual spec (checked before a file is replaced)."""
    from app.db.models import PostRecord
    with PostsSessionLocal() as db:
        rows = (db.query(PostRecord.post_id, PostRecord.title, PostRecord.share_slug, PostRecord.visual_spec)
                .filter(PostRecord.dataset_id == dataset_id, PostRecord.is_active == 1).all())
        return [{"post_id": str(r.post_id), "title": r.title, "share_slug": r.share_slug, "visual_spec": r.visual_spec}
                for r in rows]


def post_count(dataset_id: str, active_only: bool = True) -> int:
    from app.db.models import PostRecord
    with PostsSessionLocal() as db:
        q = db.query(func.count()).select_from(PostRecord).filter(PostRecord.dataset_id == dataset_id)
        if active_only:
            q = q.filter(PostRecord.is_active == 1)
        return q.scalar() or 0


def cascade_posts(dataset_id: str, active: bool) -> int:
    """Posts follow their dataset: when it becomes inactive its active posts become inactive too (and are
    marked as switched off by the dataset); when it is active again exactly those posts come back. Posts an
    admin switched off themselves stay off. Returns the number of posts changed."""
    from sqlalchemy import update
    from app.db.models import PostRecord
    with PostsSessionLocal() as db:
        if active:
            stmt = (update(PostRecord).where(PostRecord.dataset_id == dataset_id, PostRecord.deactivated_by_dataset == 1)
                    .values(is_active=1, deactivated_by_dataset=0))
        else:
            stmt = (update(PostRecord).where(PostRecord.dataset_id == dataset_id, PostRecord.is_active == 1)
                    .values(is_active=0, deactivated_by_dataset=1))
        n = db.execute(stmt).rowcount or 0
        db.commit()
    if n:
        try:
            from app.services.cache_service import cache_service
            cache_service.delete_pattern("gisviz-cache:posts:featured:*")
            cache_service.delete("trending_posts")
        except Exception:
            pass
    return n


def sync_post_activity() -> int:
    """Start-up: posts of datasets that are inactive (or gone) are switched off, posts switched off by a dataset
    that is active again come back."""
    from sqlalchemy import update
    from app.db.models import PostRecord
    with DatasetsSessionLocal() as ddb:
        status = dict(ddb.query(DatasetRecord.dataset_id, DatasetRecord.status).all())
    with PostsSessionLocal() as pdb:
        ids = {d for (d,) in pdb.query(PostRecord.dataset_id).filter(PostRecord.dataset_id.isnot(None)).distinct()}
    n = 0
    for d in ids:
        n += cascade_posts(d, status.get(d) == "active")
    return n


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
    out = {k: ((meta[k] or "").strip() or None) for k in META_FIELDS if k in meta and meta[k] is not None}
    if out.get("license_code"):
        out["license_code"] = out["license_code"].lower()
    return out


def _apply_policy(r: DatasetRecord, meta: dict) -> None:
    """Visibility, pipeline, licence and GDPR fields, with the rules that keep them consistent."""
    from app.services import licences
    if meta.get("visibility") is not None:
        if meta["visibility"] not in ("public", "private"):
            raise ValueError("Visibility must be public or private")
        r.visibility = meta["visibility"]
    if meta.get("pipeline") is not None:
        if meta["pipeline"] not in PIPELINES:
            raise ValueError("Pipeline must be batch or stream")
        r.pipeline = meta["pipeline"]
    if meta.get("refresh_seconds") is not None:
        n = int(meta["refresh_seconds"])
        if not 5 <= n <= 86400:
            raise ValueError("Refresh interval must be between 5 seconds and 1 day")
        r.refresh_seconds = n
    if "retention_rows" in meta:
        n = meta["retention_rows"]
        if n in (None, "", 0):
            r.retention_rows = None
        else:
            n = int(n)
            if not 100 <= n <= 5_000_000:
                raise ValueError("Keep between 100 and 5,000,000 rows (or leave it empty to keep everything)")
            r.retention_rows = n
    if meta.get("contains_personal_data") is not None:
        r.contains_personal_data = 1 if meta["contains_personal_data"] else 0
    if not licences.valid(r.license_code):
        raise ValueError(f"Unknown licence '{r.license_code}'")
    # public datasets need a licence that allows publishing on GisViz (checked when visibility / licence is set)
    if (meta.get("visibility") == "public" or meta.get("license_code")) and r.visibility == "public":
        if not r.license_code:
            raise ValueError("Choose the dataset's licence before making it public")
        if not licences.public_ok(r.license_code):
            raise ValueError("'All rights reserved' data can only be private on GisViz")
    if licences.needs_attribution(r.license_code) and not (r.attribution or "").strip():
        holder = r.copyright_holder or r.source_name or r.publisher
        if not holder:
            raise ValueError("This licence requires attribution: fill in the copyright holder or the attribution text")
        r.attribution = f"© {holder}"
    if r.show_data and (r.contains_personal_data or not licences.rows_public(r.license_code)):
        r.show_data = 0                                # rows of personal / non-redistributable data are never public


def next_code(db) -> str:
    from sqlalchemy import text
    n = db.execute(text("SELECT nextval('dataset_code_seq')")).scalar()
    return f"{CODE_PREFIX}{int(n):05d}"


def create(meta: dict, owner_user_id=None) -> dict:
    """New dataset record with a generated code; inactive until its file is uploaded."""
    fields = _clean(meta)
    if not fields.get("title"):
        raise ValueError("A title is required")
    with DatasetsSessionLocal() as db:
        r = DatasetRecord(dataset_id=next_code(db), status="inactive", row_count=0, visibility="public",
                          pipeline="batch", refresh_seconds=60, contains_personal_data=0,
                          owner_user_id=owner_user_id, **fields)
        # the admin page always sends visibility (and a public dataset then needs a licence); older callers
        # that do not, get a public batch dataset as before
        _apply_policy(r, {k: meta.get(k) for k in ("visibility", "pipeline", "refresh_seconds", "retention_rows",
                                                    "contains_personal_data") if k in meta})
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
        if "copyright_holder" in meta and "attribution" not in meta and r.copyright_holder \
                and (r.attribution or "") == f"© {r.copyright_holder}":
            r.attribution = None                          # generated from the old holder: follow the new one
        for k, v in _clean(meta).items():
            setattr(r, k, v)
        if "title" in meta and not (meta["title"] or "").strip():
            raise ValueError("A title is required")
        if "owner_user_id" in meta:
            r.owner_user_id = meta["owner_user_id"]
        _apply_policy(r, meta)
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
        out = _admin_card(r)
    out["posts_changed"] = cascade_posts(dataset_id, active)
    return out


def set_show_data(dataset_id: str, show: bool) -> dict:
    """Whether the dataset's rows may be shown publicly on the pages of posts built on it."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        from app.services import licences
        if show and r.contains_personal_data:
            raise ValueError("This dataset contains personal data: its rows cannot be shown to readers")
        if show and not licences.rows_public(r.license_code):
            raise ValueError("The dataset's licence does not allow showing its rows (only the visual)")
        r.show_data = 1 if show else 0
        db.commit()
        db.refresh(r)
        return _admin_card(r)


def metadata(dataset_id: str) -> Optional[dict]:
    """Everything the catalog holds about one dataset, any status (admin metadata panel)."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            return None
        d = _admin_card(r)
        d["columns"] = list(r.columns or [])
        return d


def delete(dataset_id: str) -> None:
    """Remove the record. Refused while posts use the dataset, active or not (deactivate it instead)."""
    n = post_count(dataset_id, active_only=False)
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
        if info.get("bytes") is not None:
            r.file_size_bytes = int(info["bytes"])
        r.status = "active"
        r.keywords = _keywords(r)
        db.commit()
    cascade_posts(dataset_id, True)


def raw(dataset_id: str) -> Optional[DatasetRecord]:
    """The record itself (detached), any status: access checks need owner / visibility / licence fields."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is not None:
            db.expunge(r)
        return r


def set_ingest_key(dataset_id: str, key_hash: Optional[str]) -> None:
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            raise LookupError(f"Dataset '{dataset_id}' does not exist")
        r.ingest_key_hash = key_hash
        db.commit()


def record_ingest(dataset_id: str, total_rows: int, data_version: str) -> None:
    """After rows were appended to a stream dataset."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is None:
            return
        r.row_count = int(total_rows)
        r.data_version = data_version
        r.last_ingested_at = dt.datetime.now(dt.timezone.utc)
        db.commit()


# ── sync with the DuckDB store ────────────────────────────────────────

def data_versions() -> dict[str, dict]:
    """{code: {status, data_version, has_data}} for every dataset row."""
    with DatasetsSessionLocal() as db:
        return {i: {"status": st, "data_version": v, "has_data": bool(c)}
                for i, st, v, c in db.query(DatasetRecord.dataset_id, DatasetRecord.status,
                                            DatasetRecord.data_version, DatasetRecord.columns)}


def mark_missing(dataset_id: str) -> None:
    """The store has no table for this row: make it inactive and forget the old profile."""
    with DatasetsSessionLocal() as db:
        r = db.get(DatasetRecord, dataset_id)
        if r is not None:
            r.status = "inactive"
            r.columns = None
            r.row_count = 0
            r.data_version = None
            r.file_size_bytes = None
            db.commit()
    cascade_posts(dataset_id, False)
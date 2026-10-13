"""
app/api/v0/endpoints/datasets.py
================================
Datasets: the catalog (Postgres, datasets DB) and the admin tools that load each dataset's data
into the analytical store (gisviz.duckdb, one table per dataset code ds_00001, ...).
Charts and maps built FROM a dataset are in visuals.py.

  Public (the /datasets page)
  GET    /datasets/                          active datasets: ?q= &category= &region= (incl. sub-regions) &kind=spatial|tabular &skip= &limit=
  GET    /datasets/{id}                      one dataset: card + column profile + number of posts using it
                                             (the "Metadata" button on /datasets; loaded on click)
  GET    /datasets/{id}/rows?limit=&offset=  every column, a page at a time (the visual's Data tab) — public only
                                             when show_data is on; publishers/admins always (to build posts)

  Publishers (post upload / edit: the dataset picker)
  GET    /datasets/search?q=&limit=&pipeline=   active datasets this publisher may use (public + private ones shared
                                             with them / their organisation), matching a code or words; pipeline
                                             batch (standard posts) | stream (live posts)
  GET    /datasets/licences                  the licence categories (app/services/licences.py)

  Stream datasets (live posts) — machines push rows with the dataset's ingest key
  POST   /datasets/{id}/stream               {"rows": [{column: value, ...}, ...]}  header X-Ingest-Key: gvk_...
                                             appended to the live table; live posts pick them up on their next refresh

  Admin (/admin/datasets) — every change is written to the admin audit log
  GET    /datasets/manage                    every dataset, active and inactive (?q= &status=)
  POST   /datasets/manage                    create -> code ds_00001..., inactive
  PUT    /datasets/manage/{id}               edit details (title, description, category, region, source, ...)
  PUT    /datasets/manage/{id}/status        {"active": true|false}
  PUT    /datasets/manage/{id}/show-data     {"show": true|false}  expose the rows on post pages or not
  GET    /datasets/manage/{id}/metadata      full record (any status) + columns + posts using it + store table info
  POST   /datasets/manage/{id}/data          upload the data file in one step (any supported format) -> table <id>, active
  POST   /datasets/manage/{id}/stage         upload previewer, step 1: stage the file, return columns + sample rows
  POST   /datasets/manage/{id}/stage/sample  step 2: try column edits (rename / type / drop) on the sample
  POST   /datasets/manage/{id}/stage/commit  step 3: apply the edits and go live (409 if published posts would break)
  DELETE /datasets/manage/{id}/stage         cancel: drop the staged upload.
                                             Replacing: if published posts use columns the new file lacks,
                                             409 + broken_posts and nothing changes; ?force=true replaces anyway
  GET    /datasets/manage/{id}/preview       first rows of the table
  DELETE /datasets/manage/{id}               delete record + table (refused while posts use it)
  POST   /datasets/manage/{id}/ingest-key    (stream) new ingest key, shown once; DELETE revokes it
  Visibility / sharing of a dataset: /access/dataset/{id} (access.py)
  GET    /datasets/manage/sync               datasets DB vs gisviz.duckdb: in_sync / out_of_sync / unreachable
  POST   /datasets/manage/sync               fix: missing tables -> inactive; ?drop_orphans=true drops tables without a record
"""
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_admin_db
from app.db.models import PlatformUserRecord, log_admin_action
from fastapi import Header

from app.services import access_service, catalog_service, dataset_ingest, dataset_sync, duckdb_service, licences, \
    visual_service as vs
from app.services.auth_service import get_optional_current_user, require_dataset_manager, require_publisher

router = APIRouter()
MAX_UPLOAD_MB = int(os.getenv("DATASET_MAX_UPLOAD_MB", "2048"))


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except vs.VisualError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except duckdb_service.StoreUnavailable:
        raise HTTPException(status_code=409, detail="This dataset's data has not been uploaded yet")
    except duckdb_service.EngineError as e:
        raise HTTPException(status_code=e.status if e.status >= 500 else 400, detail=e.message)


def _safe(d: dict) -> dict:
    return {k: vs._json_safe(v) for k, v in d.items()}


# ── public catalog (the /datasets page) ──────────────────────────────
# Fixed paths (/search, /manage...) are declared before /{dataset_id} so they are never read as a code.

@router.get("/")
def catalog_list(q: str = Query("", max_length=80), category: Optional[str] = Query(None, max_length=80),
                 region: Optional[str] = Query(None, max_length=60),
                 kind: Optional[str] = Query(None, pattern="^(spatial|tabular)$"),
                 pipeline: Optional[str] = Query(None, pattern="^(batch|stream)$"),
                 skip: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=60),
                 user=Depends(get_optional_current_user)):
    """Active datasets: public ones for everybody, plus the private ones a signed-in viewer may see."""
    viewer = access_service.viewer_for(user)
    page = catalog_service.list_page(q, category, skip, limit, kind, region, viewer, pipeline)
    page["items"] = [_public_card(r) for r in page["items"]]
    page["categories"] = catalog_service.categories()
    return JSONResponse(page, headers={"Cache-Control": "private, max-age=30" if user else "public, max-age=60"})


def _public_card(r: dict) -> dict:
    """What readers see of a dataset (the owner id stays internal)."""
    r = dict(r)
    r.pop("owner_user_id", None)
    return _safe(r)


@router.get("/licences")
def licence_list():
    return JSONResponse(licences.LICENCES, headers={"Cache-Control": "public, max-age=3600"})


# ── publisher: dataset picker on post upload / edit ──────────────────

@router.get("/search")
def search_datasets(
    q: str = Query("", max_length=80),
    limit: int = Query(12, ge=1, le=50),
    pipeline: Optional[str] = Query(None, pattern="^(batch|stream)$"),
    _user: PlatformUserRecord = Depends(require_publisher),
):
    viewer = access_service.viewer_for(_user)
    rows = catalog_service.search(q, limit, viewer, pipeline)
    out = []
    for r in rows:
        # private datasets: may this publisher make PUBLIC posts on it, or only private ones?
        r["can_publish_publicly"] = r.get("visibility") != "private" or \
            access_service.can_publish_publicly(viewer, catalog_service.raw(r["dataset_id"]))
        out.append(_public_card(r))
    return JSONResponse(out)


# ── admin: create, upload, activate, edit, delete, sync ──────────────

class DatasetMeta(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=5000)
    category: Optional[str] = Field(None, max_length=80)     # categories.slug (posts DB); "" clears it
    region: Optional[str] = Field(None, max_length=60)       # regions.code (misc DB); "" clears it
    source_name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = Field(None, max_length=1024)
    license: Optional[str] = Field(None, max_length=80)
    publisher: Optional[str] = Field(None, max_length=120)
    # access, pipeline, licensing, GDPR
    visibility: Optional[str] = Field(None, pattern="^(public|private)$")
    owner_handle: Optional[str] = Field(None, max_length=51)  # @handle of the owning user / organisation; "" clears
    pipeline: Optional[str] = Field(None, pattern="^(batch|stream)$")
    refresh_seconds: Optional[int] = Field(None, ge=5, le=86400)
    retention_rows: Optional[int] = Field(None, ge=0, le=5_000_000)
    license_code: Optional[str] = Field(None, max_length=40)
    attribution: Optional[str] = Field(None, max_length=1000)
    copyright_holder: Optional[str] = Field(None, max_length=255)
    contains_personal_data: Optional[bool] = None


REQUIRED = {"title": "A title", "source_name": "The source name", "copyright_holder": "The copyright holder"}


def _require(fields: dict, row: Optional[dict] = None) -> None:
    """Title, source name and copyright holder are mandatory: in a new dataset, and they cannot be blanked."""
    for k, name in REQUIRED.items():
        v = fields.get(k, (row or {}).get(k) if row is not None else None)
        if (k in fields or row is None) and not (v or "").strip():
            raise HTTPException(status_code=422, detail=f"{name} is required")


def _licence_text(fields: dict) -> dict:
    """The licence is picked from the list (license_code); its name is kept in the old free-text field too."""
    if fields.get("license_code"):
        from app.services import licences
        lic = licences.get(fields["license_code"])
        if lic:
            fields["license"] = lic["name"]
    return fields


def _resolve_owner(fields: dict) -> dict:
    """owner_handle -> owner_user_id (users DB)."""
    if "owner_handle" not in fields:
        return fields
    h = (fields.pop("owner_handle") or "").strip().lstrip("@")
    if not h:
        fields["owner_user_id"] = None
        return fields
    from sqlalchemy import func
    from app.db.database import UsersSessionLocal
    with UsersSessionLocal() as udb:
        u = udb.query(PlatformUserRecord).filter(func.lower(PlatformUserRecord.user_handle) == h.lower()).first()
        if u is None:
            raise HTTPException(status_code=422, detail=f"No account @{h}")
        fields["owner_user_id"] = u.user_id
    return fields


def _owner_handles(rows: list[dict]) -> list[dict]:
    ids = {r.get("owner_user_id") for r in rows if r.get("owner_user_id")}
    if ids:
        import uuid as _uuid
        from app.db.database import UsersSessionLocal
        with UsersSessionLocal() as udb:
            names = {str(u.user_id): u.user_handle for u in udb.query(PlatformUserRecord)
                     .filter(PlatformUserRecord.user_id.in_([_uuid.UUID(i) for i in ids])).all()}
        for r in rows:
            r["owner_handle"] = names.get(r.get("owner_user_id") or "")
    return rows


class DatasetStatus(BaseModel):
    active: bool


class DatasetShowData(BaseModel):
    show: bool


def _check_id(dataset_id: str) -> str:
    try:
        return vs._check_id(dataset_id)
    except vs.VisualError:
        raise HTTPException(status_code=400, detail="Invalid dataset code")


def _audit(admin_db, user, request: Request, action: str, dataset_id: str, payload: Optional[dict] = None):
    try:
        log_admin_action(admin_db, admin_user_id=user.user_id, admin_handle=getattr(user, "user_handle", None),
                         action_type=action, target_type="dataset", target_id=dataset_id, payload=payload or {},
                         ip_address=request.client.host if request.client else None)
    except Exception as e:                       # auditing must never block the action
        print(f"[datasets] audit log failed: {e}")


def _check_refs(meta: dict) -> None:
    """category and region must exist in their own databases (no cross-database foreign keys)."""
    from app.services import region_service
    cat = (meta.get("category") or "").strip()
    if cat and not catalog_service.category_exists(cat):
        raise HTTPException(status_code=422, detail=f"Unknown category '{cat}'")
    reg = (meta.get("region") or "").strip()
    if reg and not region_service.exists(reg):
        raise HTTPException(status_code=422, detail=f"Unknown region '{reg}'")


def _row(dataset_id: str, row: dict, status_code: int = 200) -> JSONResponse:
    row["has_data"] = bool(row.get("column_count"))
    return JSONResponse(_safe(row), status_code=status_code)


@router.get("/manage")
def manage_list(q: str = Query("", max_length=80), status: Optional[str] = Query(None, max_length=12),
                _user: PlatformUserRecord = Depends(require_dataset_manager)):
    rows = _owner_handles(catalog_service.list_admin(q, status))
    for r in rows:
        r["has_data"] = bool(r.get("column_count"))
    return JSONResponse([_safe(r) for r in rows])


@router.post("/manage")
def manage_create(meta: DatasetMeta, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    """New dataset record; the code (ds_00001, ...) is generated. Inactive until its file is uploaded.
    Title, source name and copyright holder are required; the publisher is the account creating it."""
    fields = _resolve_owner(meta.model_dump(exclude_unset=True))
    _require(fields)
    fields["publisher"] = f"@{user.user_handle}"
    _licence_text(fields)
    _check_refs(fields)
    try:
        row = catalog_service.create(fields, owner_user_id=fields.pop("owner_user_id", None))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "dataset.create", row["dataset_id"], {"title": row["title"]})
    return _row(row["dataset_id"], row, 201)


@router.put("/manage/{dataset_id}")
def manage_update(dataset_id: str, meta: DatasetMeta, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    changes = _resolve_owner(meta.model_dump(exclude_unset=True))
    _require(changes, {})
    changes.pop("publisher", None)                    # the publisher is who created the dataset
    _licence_text(changes)
    _check_refs(changes)
    try:
        row = catalog_service.update_meta(dataset_id, changes)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    vs.invalidate(dataset_id)
    _audit(admin_db, user, request, "dataset.update", dataset_id,
           {"fields": sorted(changes), **{k: changes[k] for k in ("visibility", "pipeline", "license_code") if k in changes}})
    return _row(dataset_id, _owner_handles([row])[0])


@router.put("/manage/{dataset_id}/status")
def manage_status(dataset_id: str, body: DatasetStatus, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    try:
        row = catalog_service.set_active(dataset_id, body.active)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "dataset.activate" if body.active else "dataset.deactivate", dataset_id)
    return _row(dataset_id, row)


@router.post("/manage/{dataset_id}/data")
def manage_upload(dataset_id: str, request: Request, file: UploadFile = File(...),
                  force: bool = Query(False, description="replace even if published posts would break"),
                  admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Upload the dataset's Parquet file: loaded into gisviz.duckdb as table <code> (via the DuckDB
    service), profiled, and the dataset becomes active. Uploading again replaces the table atomically."""
    dataset_id = _check_id(dataset_id)
    if not catalog_service.exists(dataset_id):
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' does not exist")
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in dataset_ingest.ALLOWED:
        raise HTTPException(status_code=415, detail=f"Unsupported file type. Upload one of: {', '.join(dataset_ingest.ALLOWED)}")

    limit = MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp_name = tempfile.mkstemp(suffix=ext)
    tmp = Path(tmp_name)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"The file is larger than {MAX_UPLOAD_MB} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The file is empty")
        try:
            result = dataset_ingest.ingest(dataset_id, tmp, name, force=force)
        except dataset_ingest.ReplaceConflict as e:
            # nothing changed: the live table and every post keep working
            raise HTTPException(status_code=409, detail={
                "message": f"{len(e.broken)} published post(s) use columns that are missing or different in "
                           f"{name}. The current data was kept. Upload a file with those columns, or replace anyway "
                           "and then edit those posts.",
                "broken_posts": e.broken})
        except dataset_ingest.IngestError as e:
            print(f"[datasets] upload rejected for {dataset_id} ({name}): {e}")
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Upload failed while processing the file: {type(e).__name__}: {str(e)[:200]}")
        _audit(admin_db, user, request, "dataset.upload", dataset_id,
               {"file": name, "bytes": size, "rows": result["rows"], "columns": result["columns"],
                "forced": bool(force), "broken_posts": [b["post_id"] for b in result["broken_posts"]]})
        return JSONResponse({**result, "bytes": size})
    finally:
        tmp.unlink(missing_ok=True)


# ── upload previewer (Compass-style): stage -> sample with column edits -> commit / discard ──

class StagedChanges(BaseModel):
    """Column edits: [{"column": "<staged name>", "rename": "...", "type": "text|integer|decimal|date|datetime|boolean",
    "include": true|false}] — unlisted columns are kept as detected."""
    changes: list[dict] = Field(default_factory=list, max_length=500)
    limit: int = Field(20, ge=1, le=200)
    force: bool = False


def _receive_upload(file: UploadFile) -> tuple[Path, str, int]:
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in dataset_ingest.ALLOWED:
        raise HTTPException(status_code=415, detail=f"Unsupported file type. Upload one of: {', '.join(dataset_ingest.ALLOWED)}")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp_name = tempfile.mkstemp(suffix=ext)
    tmp = Path(tmp_name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"The file is larger than {MAX_UPLOAD_MB} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The file is empty")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, name, size


@router.post("/manage/{dataset_id}/stage")
def manage_stage(dataset_id: str, file: UploadFile = File(...),
                 _user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Step 1 of an upload: convert + load the file as a STAGED table (the live data is untouched) and
    return columns with detected types and sample rows for the previewer."""
    dataset_id = _check_id(dataset_id)
    if not catalog_service.exists(dataset_id):
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' does not exist")
    tmp, name, size = _receive_upload(file)
    try:
        return JSONResponse(dataset_ingest.stage_upload(dataset_id, tmp, name))
    except dataset_ingest.IngestError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/manage/{dataset_id}/stage/sample")
def manage_stage_sample(dataset_id: str, body: StagedChanges, _user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Step 2: try column edits (rename / type / drop) on the staged sample; shows values a type change would lose."""
    try:
        return JSONResponse(dataset_ingest.sample_staged(_check_id(dataset_id), body.changes, body.limit))
    except dataset_ingest.IngestError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/manage/{dataset_id}/stage/commit")
def manage_stage_commit(dataset_id: str, body: StagedChanges, request: Request, admin_db: Session = Depends(get_admin_db),
                        user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Step 3: apply the edits and make the staged data live (409 with the posts it would break, unless force)."""
    dataset_id = _check_id(dataset_id)
    try:
        result = dataset_ingest.commit_staged(dataset_id, body.changes, force=body.force)
    except dataset_ingest.ReplaceConflict as e:
        raise HTTPException(status_code=409, detail={
            "message": f"{len(e.broken)} published post(s) use columns that are missing or different in the new "
                       "data. The current data was kept and the upload is still staged: change the column edits, "
                       "or replace anyway and then edit those posts.",
            "broken_posts": e.broken})
    except dataset_ingest.IngestError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "dataset.upload", dataset_id,
           {"rows": result["rows"], "columns": result["columns"], "bytes": result["bytes"], "edits": len(body.changes),
            "forced": bool(body.force), "broken_posts": [b["post_id"] for b in result["broken_posts"]]})
    return JSONResponse(result)


@router.delete("/manage/{dataset_id}/stage")
def manage_stage_discard(dataset_id: str, _user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Cancel an upload: drop the staged table (the live data stays as it was)."""
    return {"discarded": _store_guard(dataset_ingest.discard_staged, _check_id(dataset_id))}


@router.delete("/manage/{dataset_id}")
def manage_delete(dataset_id: str, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    try:
        catalog_service.delete(dataset_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    try:
        dataset_ingest.remove(dataset_id)
    except duckdb_service.EngineError as e:
        print(f"[datasets] record {dataset_id} deleted but its table could not be dropped: {e.message}")
    vs.invalidate(dataset_id)
    _audit(admin_db, user, request, "dataset.delete", dataset_id)
    return {"deleted": dataset_id}


@router.put("/manage/{dataset_id}/show-data")
def manage_show_data(dataset_id: str, body: DatasetShowData, request: Request, admin_db: Session = Depends(get_admin_db),
                     user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    try:
        row = catalog_service.set_show_data(dataset_id, body.show)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "dataset.show_data_on" if body.show else "dataset.show_data_off", dataset_id)
    return _row(dataset_id, row)


@router.get("/manage/{dataset_id}/metadata")
def manage_metadata(dataset_id: str, _user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    meta = catalog_service.metadata(dataset_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' does not exist")
    meta["has_data"] = bool(meta.get("column_count"))
    meta = _owner_handles([meta])[0]
    meta["columns"] = [_safe(c) for c in meta["columns"]]
    meta["post_count"] = catalog_service.post_count(dataset_id)
    try:                                       # what gisviz.duckdb itself says about the table
        meta["store"] = next((t for t in duckdb_service.tables() if t.get("code") == dataset_id), None)
        meta["store_error"] = None
    except duckdb_service.EngineError as e:
        meta["store"], meta["store_error"] = None, e.message
    return JSONResponse(_safe(meta))


def _store_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except duckdb_service.StoreUnavailable as e:
        raise HTTPException(status_code=404, detail=str(e) or "No data loaded for this dataset")
    except duckdb_service.EngineError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.get("/manage/{dataset_id}/preview")
def manage_preview(dataset_id: str, limit: int = Query(50, ge=1, le=500),
                   _user: PlatformUserRecord = Depends(require_dataset_manager)):
    return JSONResponse(_store_guard(duckdb_service.preview, _check_id(dataset_id), limit))


@router.post("/manage/{dataset_id}/ingest-key")
def manage_ingest_key(dataset_id: str, request: Request, admin_db: Session = Depends(get_admin_db),
                      user: PlatformUserRecord = Depends(require_dataset_manager)):
    """A new ingest key for a stream dataset (the old one stops working). Returned ONCE; only its hash is kept."""
    import hashlib
    import secrets
    dataset_id = _check_id(dataset_id)
    ds = catalog_service.raw(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' does not exist")
    if (ds.pipeline or "batch") != "stream":
        raise HTTPException(status_code=409, detail="Only stream datasets take pushed rows (set the pipeline to stream)")
    key = "gvk_" + secrets.token_hex(24)
    catalog_service.set_ingest_key(dataset_id, hashlib.sha256(key.encode()).hexdigest())
    _audit(admin_db, user, request, "dataset.ingest_key_rotated", dataset_id)
    return {"dataset_id": dataset_id, "ingest_key": key,
            "endpoint": f"/api/v0/datasets/{dataset_id}/stream", "header": "X-Ingest-Key"}


@router.delete("/manage/{dataset_id}/ingest-key")
def manage_ingest_key_revoke(dataset_id: str, request: Request, admin_db: Session = Depends(get_admin_db),
                             user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    try:
        catalog_service.set_ingest_key(dataset_id, None)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    _audit(admin_db, user, request, "dataset.ingest_key_revoked", dataset_id)
    return {"dataset_id": dataset_id, "ingest_key": None}


@router.get("/manage/sync")
def manage_sync_status(_user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Always 200, so the page can show the store's state, including "unreachable"."""
    return JSONResponse(dataset_sync.report())


@router.post("/manage/sync")
def manage_sync_fix(request: Request, drop_orphans: bool = Query(False), admin_db: Session = Depends(get_admin_db),
                    user: PlatformUserRecord = Depends(require_dataset_manager)):
    result = _store_guard(dataset_sync.fix, drop_orphans)
    _audit(admin_db, user, request, "dataset.sync_fix", "*",
           {"fixed_missing": result["fixed_missing"], "dropped_orphans": result["dropped_orphans"]})
    return JSONResponse(result)


# ── public detail (declared last: /{dataset_id} would otherwise catch /search and /manage) ──

@router.get("/{dataset_id}/rows")
def public_rows(dataset_id: str, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                user=Depends(get_optional_current_user)):
    """Every column of the dataset, a page at a time, for the Data tab of a visual. Public when an
    admin switched show_data on; publishers and admins can always read it (to build posts)."""
    dataset_id = _check_id(dataset_id)
    card = catalog_service.get(dataset_id, include_inactive=True)    # posts keep working after deactivation
    ds = catalog_service.raw(dataset_id)
    if card is None or ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    viewer = access_service.viewer_for(user)
    if not access_service.can_view_dataset(viewer, ds):
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not access_service.can_see_rows(viewer, ds):
        raise HTTPException(status_code=403, detail="The data of this dataset is not published")
    excl = "_gv_id, _gv_geom" if card.get("geometry_type") else "_gv_id"   # geometry stays internal
    rows = _store_guard(duckdb_service.query, dataset_id,
                        f"SELECT * EXCLUDE ({excl}) FROM data ORDER BY _gv_id LIMIT ? OFFSET ?", [limit, offset])
    cols = [c["column_name"] for c in catalog_service.get_columns(dataset_id)] or (list(rows[0].keys()) if rows else [])
    return JSONResponse({"dataset_id": dataset_id, "total_rows": card.get("row_count"), "offset": offset,
                         "limit": limit, "columns": cols, "rows": rows},
                        headers={"Cache-Control": "public, max-age=300" if card.get("show_data") and ds.visibility != "private"
                                 and ds.pipeline != "stream" else "private, max-age=30"})


class StreamRows(BaseModel):
    rows: list = Field(..., min_length=1, max_length=5000)


INGEST_PER_MINUTE = int(os.getenv("STREAM_INGEST_PER_MINUTE", "120"))


@router.post("/{dataset_id}/stream")
def stream_ingest(dataset_id: str, body: StreamRows, x_ingest_key: Optional[str] = Header(None),
                  authorization: Optional[str] = Header(None)):
    """Machines push rows to a STREAM dataset with its ingest key (header X-Ingest-Key, or Authorization: Bearer).
    Rows must use the dataset's column names; point data may send latitude/longitude or a GeoJSON `geometry`."""
    import hashlib
    import hmac as _hmac
    from app.services.cache_service import cache_service
    dataset_id = _check_id(dataset_id)
    ds = catalog_service.raw(dataset_id)
    key = x_ingest_key or ((authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else None)
    if ds is None or not ds.ingest_key_hash or not key or not _hmac.compare_digest(
            hashlib.sha256(key.encode()).hexdigest(), ds.ingest_key_hash):
        raise HTTPException(status_code=401, detail="Invalid ingest key")
    if (ds.pipeline or "batch") != "stream":
        raise HTTPException(status_code=409, detail="This dataset is not a stream dataset")
    if not ds.columns:
        raise HTTPException(status_code=409, detail="Upload a first file to define the dataset's columns")
    if cache_service.hit(f"gisviz:ingest:{dataset_id}", 60) > INGEST_PER_MINUTE:
        raise HTTPException(status_code=429, detail=f"Too many pushes: at most {INGEST_PER_MINUTE} per minute")
    res = _store_guard(duckdb_service.append, dataset_id, body.rows, ds.retention_rows)
    catalog_service.record_ingest(dataset_id, res["rows"], res["data_version"])
    vs.invalidate(dataset_id)
    try:                                        # the feed / share images of live posts follow, at most every 10 minutes
        from app.services.post_service import post_service
        post_service.refresh_live_images(dataset_id)
    except Exception as e:
        print(f"[datasets] live image refresh skipped: {e}")
    return {"dataset_id": dataset_id, "added": res["added"], "rows": res["rows"], "data_version": res["data_version"]}


@router.get("/{dataset_id}")
def catalog_detail(dataset_id: str, user=Depends(get_optional_current_user)):
    viewer = access_service.viewer_for(user)
    ds = catalog_service.raw(_check_id(dataset_id))
    if ds is None or not access_service.can_view_dataset(viewer, ds):
        raise HTTPException(status_code=404, detail="Dataset not found")

    def run():
        card = vs.get_card(dataset_id)
        card.pop("owner_user_id", None)
        card["can_see_rows"] = access_service.can_see_rows(viewer, ds)
        cols = [_safe(c) for c in vs.get_columns(card["dataset_id"])]
        return {"dataset": card, "columns": cols, "post_count": catalog_service.post_count(card["dataset_id"])}
    private = (ds.visibility or "public") == "private"
    return JSONResponse(_guard(run), headers={"Cache-Control": "private, max-age=30" if private or user else "public, max-age=60"})
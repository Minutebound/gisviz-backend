"""
app/api/v0/endpoints/visuals.py
===============================
Dataset catalog, visual suggestions and chart/map data. Each dataset's data is one DuckDB
file (DATASETS_DIR/<id>.duckdb), read-only for the API.

  GET  /visuals/catalog                       public dataset list (q, category, kind, page) (public)
  GET  /visuals/catalog/{id}                  public dataset card + columns                (public)
  GET  /visuals/datasets?q=                   search active datasets         (admin/publisher/editor/support)
  GET  /visuals/datasets/{id}                 card + column profile          (admin/publisher/editor/support)
  GET  /visuals/datasets/{id}/suggest         ranked visual options          (admin/publisher/editor/support)
  GET  /visuals/datasets/{id}/spec            visual_spec for one choice     (admin/publisher/editor/support)
  GET  /visuals/datasets/{id}/data            rows / GeoJSON for a spec      (public, Redis-cached)

  Admin (/admin/datasets), every change written to the admin audit log:
  GET    /visuals/manage/datasets                 every dataset, active and inactive
  POST   /visuals/manage/datasets                 create -> code ds_00001..., inactive
  PUT    /visuals/manage/datasets/{id}            edit details
  PUT    /visuals/manage/datasets/{id}/status     {"active": true|false}
  POST   /visuals/manage/datasets/{id}/data       upload the Parquet file -> DuckDB, becomes active
  DELETE /visuals/manage/datasets/{id}            delete record + file (refused while posts use it)
"""
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_admin_db
from app.db.models import PlatformUserRecord, log_admin_action
from app.services import catalog_service, dataset_ingest, duckdb_service, visual_service as vs
from app.services.auth_service import require_dataset_manager, require_publisher
from app.services.viz_recommender import VIZ_REGISTRY

router = APIRouter()
MAX_UPLOAD_MB = int(os.getenv("DATASET_MAX_UPLOAD_MB", "2048"))


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except vs.VisualError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except duckdb_service.StoreUnavailable:
        raise HTTPException(status_code=409, detail="This dataset's data has not been uploaded yet")


def _safe(d: dict) -> dict:
    return {k: vs._json_safe(v) for k, v in d.items()}


@router.get("/types")
def list_types():
    return [{"viz": k, **v} for k, v in VIZ_REGISTRY.items()]


# ── public catalog (the /datasets page) ──────────────────────────────

@router.get("/catalog")
def catalog_list(q: str = Query("", max_length=80), category: Optional[str] = Query(None, max_length=80),
                 kind: Optional[str] = Query(None, pattern="^(spatial|tabular)$"),
                 skip: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=60)):
    page = catalog_service.list_page(q, category, skip, limit, kind)
    page["items"] = [_safe(r) for r in page["items"]]
    page["categories"] = catalog_service.categories()
    return JSONResponse(page, headers={"Cache-Control": "public, max-age=60"})


@router.get("/catalog/{dataset_id}")
def catalog_detail(dataset_id: str):
    def run():
        card = vs.get_card(dataset_id)
        cols = [_safe(c) for c in vs.get_columns(card["dataset_id"])]
        return {"dataset": card, "columns": cols, "post_count": catalog_service.post_count(card["dataset_id"])}
    return JSONResponse(_guard(run), headers={"Cache-Control": "public, max-age=60"})


# ── publisher tools (upload / edit post) ─────────────────────────────

@router.get("/datasets")
def search_datasets(
    q: str = Query("", max_length=80),
    limit: int = Query(12, ge=1, le=50),
    _user: PlatformUserRecord = Depends(require_publisher),
):
    return JSONResponse(_guard(lambda: [_safe(r) for r in vs.search(q, limit)]))


@router.get("/datasets/{dataset_id}")
def dataset_detail(dataset_id: str, _user: PlatformUserRecord = Depends(require_publisher)):
    def run():
        card = vs.get_card(dataset_id)
        return {"dataset": card, "columns": [_safe(c) for c in vs.get_columns(card["dataset_id"])]}
    return JSONResponse(_guard(run))


@router.get("/datasets/{dataset_id}/suggest")
def suggest(dataset_id: str, _user: PlatformUserRecord = Depends(require_publisher)):
    def run():
        info = vs.suggest(dataset_id)
        info["columns"] = [_safe(c) for c in info["columns"]]
        return info
    return JSONResponse(_guard(run))


@router.get("/datasets/{dataset_id}/spec")
def spec(
    dataset_id: str,
    viz: Optional[str] = None, x: Optional[str] = None, y: Optional[str] = None,
    label_field: Optional[str] = None, accent: Optional[str] = None,
    z: Optional[str] = None, size: Optional[str] = None,
    _user: PlatformUserRecord = Depends(require_publisher),
):
    return JSONResponse(_guard(vs.build_spec, dataset_id, viz, x, y, label_field, accent, z, size))


@router.get("/datasets/{dataset_id}/data")
def data(dataset_id: str, viz: Optional[str] = None, x: Optional[str] = None,
         y: Optional[str] = None, label_field: Optional[str] = None,
         z: Optional[str] = None, size: Optional[str] = None):
    params = {"viz": viz, "x": x, "y": y, "z": z, "size": size, "label_field": label_field}
    # deactivated datasets keep serving the posts already built on them
    body, hit = _guard(vs.payload_json, dataset_id, params, True)
    return Response(body, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=300", "X-Cache": "HIT" if hit else "MISS"})


# ── dataset managers: register + upload (admin / publisher) ──────────

class DatasetMeta(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=5000)
    category: Optional[str] = Field(None, max_length=80)
    source_name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = Field(None, max_length=1024)
    license: Optional[str] = Field(None, max_length=80)
    publisher: Optional[str] = Field(None, max_length=120)


class DatasetStatus(BaseModel):
    active: bool


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


def _row(dataset_id: str, row: dict, status_code: int = 200) -> JSONResponse:
    row["has_data"] = duckdb_service.has_data(dataset_id)
    return JSONResponse(_safe(row), status_code=status_code)


@router.get("/manage/datasets")
def manage_list(q: str = Query("", max_length=80), status: Optional[str] = Query(None, max_length=12),
                _user: PlatformUserRecord = Depends(require_dataset_manager)):
    rows = catalog_service.list_admin(q, status)
    for r in rows:
        r["has_data"] = duckdb_service.has_data(r["dataset_id"])
    return JSONResponse([_safe(r) for r in rows])


@router.post("/manage/datasets")
def manage_create(meta: DatasetMeta, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    """New dataset record; the code (ds_00001, ...) is generated. Inactive until its file is uploaded."""
    try:
        row = catalog_service.create(meta.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "dataset.create", row["dataset_id"], {"title": row["title"]})
    return _row(row["dataset_id"], row, 201)


@router.put("/manage/datasets/{dataset_id}")
def manage_update(dataset_id: str, meta: DatasetMeta, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    changes = meta.model_dump(exclude_unset=True)
    try:
        row = catalog_service.update_meta(dataset_id, changes)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    vs.invalidate(dataset_id)
    _audit(admin_db, user, request, "dataset.update", dataset_id, {"fields": sorted(changes)})
    return _row(dataset_id, row)


@router.put("/manage/datasets/{dataset_id}/status")
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


@router.post("/manage/datasets/{dataset_id}/data")
def manage_upload(dataset_id: str, request: Request, file: UploadFile = File(...),
                  admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Upload the dataset's Parquet file: written into the gisviz data folder as <code>.duckdb,
    profiled, and the dataset becomes active. Uploading again replaces the data atomically."""
    dataset_id = _check_id(dataset_id)
    if not catalog_service.exists(dataset_id):
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' does not exist")
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in dataset_ingest.ALLOWED:
        raise HTTPException(status_code=415, detail="Upload a .parquet file (CSV and GeoJSON also work)")

    limit = MAX_UPLOAD_MB * 1024 * 1024
    incoming = duckdb_service.DATASETS_DIR / ".incoming"
    try:
        incoming.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=500, detail=f"The server cannot write to {duckdb_service.DATASETS_DIR}")
    fd, tmp_name = tempfile.mkstemp(suffix=ext, dir=incoming)
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
            result = dataset_ingest.ingest(dataset_id, tmp, name)
        except dataset_ingest.IngestError as e:
            print(f"[datasets] upload rejected for {dataset_id} ({name}): {e}")
            raise HTTPException(status_code=422, detail=str(e))
        except PermissionError as e:
            print(f"[datasets] cannot write dataset files: {e}")
            raise HTTPException(status_code=500, detail=f"The server cannot write to {duckdb_service.DATASETS_DIR}")
        except HTTPException:
            raise
        except Exception as e:
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Upload failed while processing the file: {type(e).__name__}: {str(e)[:200]}")
        _audit(admin_db, user, request, "dataset.upload", dataset_id,
               {"file": name, "bytes": size, "rows": result["rows"], "columns": result["columns"]})
        return JSONResponse({**result, "bytes": size})
    finally:
        tmp.unlink(missing_ok=True)


@router.delete("/manage/datasets/{dataset_id}")
def manage_delete(dataset_id: str, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_dataset_manager)):
    dataset_id = _check_id(dataset_id)
    try:
        catalog_service.delete(dataset_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    dataset_ingest.remove_file(dataset_id)
    vs.invalidate(dataset_id)
    _audit(admin_db, user, request, "dataset.delete", dataset_id)
    return {"deleted": dataset_id}
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

  GET  /visuals/manage/datasets               every dataset incl. drafts              (admin/publisher)
  PUT  /visuals/manage/datasets/{id}          edit metadata / publish (status)        (admin/publisher)
  POST /visuals/manage/datasets/sync          register tables published from DuckDB  (admin/publisher)

Data is never uploaded through the API: tables are created in the DuckDB workspace (DuckDB UI),
published to DATASETS_DIR/<id>.duckdb by the workspace container, and registered here by dataset_sync.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.db.models import PlatformUserRecord
from app.services import catalog_service, dataset_sync, duckdb_service, visual_service as vs
from app.services.auth_service import require_dataset_manager, require_publisher
from app.services.viz_recommender import VIZ_REGISTRY

router = APIRouter()


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
    body, hit = _guard(vs.payload_json, dataset_id, params)
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
    status: Optional[str] = Field(None, max_length=12)


def _check_id(dataset_id: str) -> str:
    try:
        return vs._check_id(dataset_id)
    except vs.VisualError as e:
        raise HTTPException(status_code=400, detail="Dataset id must be lowercase letters, digits and underscores (max 64)")


@router.get("/manage/datasets")
def manage_list(q: str = Query("", max_length=80), status: Optional[str] = Query(None, max_length=12),
                _user: PlatformUserRecord = Depends(require_dataset_manager)):
    rows = catalog_service.list_admin(q, status)
    for r in rows:
        r["has_data"] = duckdb_service.has_data(r["dataset_id"])
    return JSONResponse([_safe(r) for r in rows])


@router.put("/manage/datasets/{dataset_id}")
def manage_update(dataset_id: str, meta: DatasetMeta,
                  _user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Edit title/description/source/licence and publish (status). Rows are created by the sync."""
    dataset_id = _check_id(dataset_id)
    try:
        row = catalog_service.update_meta(dataset_id, meta.model_dump(exclude_unset=True))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    vs.invalidate(dataset_id)
    row["has_data"] = duckdb_service.has_data(dataset_id)
    return JSONResponse(_safe(row))


@router.post("/manage/datasets/sync")
def manage_sync(_user: PlatformUserRecord = Depends(require_dataset_manager)):
    """Pick up tables published from the DuckDB workspace right now (also runs every 30 s)."""
    return JSONResponse(dataset_sync.sync(force=False))
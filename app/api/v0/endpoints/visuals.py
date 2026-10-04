"""
app/api/v0/endpoints/visuals.py
===============================
Charts and maps built from a dataset. The dataset itself (catalog, upload, admin) is in datasets.py.

How a visual is made
  1. suggest  the picker asks which charts/maps fit the dataset's columns (ranked, best first)
  2. spec     for the chosen chart + columns, the server returns a VISUAL SPEC: a small JSON recipe
              (kind, chart type, which columns go on x / y / colour / size, allowed chart types,
              accent colour, and a `data` URL). It contains no rows. The upload page renders the live
              preview from it, and on publish the server builds the same spec again with the same
              function (it never trusts a spec sent by the browser) and stores it on the post.
  3. data     the browser fetches the spec's `data` URL to draw the chart/map: aggregated rows or
              GeoJSON from gisviz.duckdb, Redis-cached. This URL is saved inside every published
              post's visual_spec, so its path must never change.

  GET  /visuals/catalog                  the visual catalog from the DB: categories + types   (public)
                                         ?all=true also lists types that are not enabled / not built yet
  GET  /visuals/datasets/{id}/suggest    ranked visual options + column profile        (publishers)
  GET  /visuals/datasets/{id}/spec       visual spec for one choice:                     (publishers)
                                         ?viz=bar&x=...&y=...&z=...&size=...&label_field=...&accent=%2306ba24
                                         (anything left out is filled from the recommender)
  POST /visuals/datasets/{id}/spec       the same, with the presentation options in a JSON body           (publishers)
                                         {viz, x, y, z, size, label_field, color, accent, story: {title, subtitle,
                                          y_label, colors, reference_lines, filter_default, visual_type}}
  GET  /visuals/datasets/{id}/data       rows / GeoJSON for a spec (same query as the spec's data URL)  (public, cached)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db.models import PlatformUserRecord
from app.services import duckdb_service, visual_service as vs
from app.services.auth_service import require_publisher

router = APIRouter()


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


@router.get("/catalog")
def visual_catalog(all: bool = Query(False)):
    """Categories and visual types, as seeded by GET /seed (app/main.py, misc DB)."""
    from app.db.database import MiscSessionLocal
    from app.db.models import VisualCategoryRecord, VisualTypeRecord
    with MiscSessionLocal() as db:
        cats = db.query(VisualCategoryRecord).order_by(VisualCategoryRecord.sort_order).all()
        q = db.query(VisualTypeRecord)
        if not all:
            q = q.filter(VisualTypeRecord.enabled == 1)
        types = q.order_by(VisualTypeRecord.sort_order).all()
        return JSONResponse({
            "categories": [{"code": c.code, "name": c.name, "color": c.color, "question": c.question,
                            "guide_goals": c.guide_goals} for c in cats],
            "types": [{"code": t.code, "name": t.name, "category": t.category, "kind": t.kind, "roles": t.roles,
                       "best_when": t.best_when, "avoid_when": t.avoid_when, "status": t.status,
                       "interactions": t.interactions, "renderer_code": t.legacy_code or t.code,
                       "enabled": bool(t.enabled)} for t in types],
        }, headers={"Cache-Control": "public, max-age=300"})


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


class SpecRequest(BaseModel):
    viz: Optional[str] = None
    x: Optional[str] = None
    y: Optional[str] = None
    z: Optional[str] = None
    size: Optional[str] = None
    label_field: Optional[str] = None
    color: Optional[str] = None
    accent: Optional[str] = None
    story: Optional[dict] = None        # title, subtitle, y_label, colors, reference_lines, filter_default, visual_type


@router.post("/datasets/{dataset_id}/spec")
def spec_with_story(dataset_id: str, body: SpecRequest, _user: PlatformUserRecord = Depends(require_publisher)):
    """Same as GET /spec, plus the presentation options (the upload page's live preview)."""
    return JSONResponse(_guard(vs.build_spec, dataset_id, body.viz, body.x, body.y, body.label_field, body.accent,
                               body.z, body.size, False, body.color, body.story or {}))


@router.get("/datasets/{dataset_id}/data")
def data(dataset_id: str, viz: Optional[str] = None, x: Optional[str] = None,
         y: Optional[str] = None, label_field: Optional[str] = None,
         z: Optional[str] = None, size: Optional[str] = None, color: Optional[str] = None,
         part: Optional[str] = None):
    params = {"viz": viz, "x": x, "y": y, "z": z, "size": size, "label_field": label_field, "color": color,
              "part": part if part == "series" else None}
    # deactivated datasets keep serving the posts already built on them
    body, hit = _guard(vs.payload_json, dataset_id, params, True)
    return Response(body, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=300", "X-Cache": "HIT" if hit else "MISS"})
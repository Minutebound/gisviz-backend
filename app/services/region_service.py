"""
app/services/region_service.py
==============================
Regions live in the MISC database (table `regions`). Datasets store a region's `code`
(datasets.region); other features (posts, users, filters) can use the same table later.

Seeded by GET /seed (app/main.py SEED_REGIONS + SEED_COUNTRIES): Global, the 7 continents (parent = global) and
every ISO 3166 country (kind="country", parent_code = its continent, iso_a2/iso_a3, m49, bbox). /seed upserts them
and switches off any other row. Filters list only Global + continents; posts and datasets can be tagged with a country.
"""
from __future__ import annotations

import re
from typing import Optional


from app.db.database import MiscSessionLocal
from app.db.models import RegionRecord

KINDS = ("global", "continent", "subregion", "country", "custom")
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


EDITABLE = ("name", "kind", "parent_code", "iso_a2", "iso_a3", "m49", "bbox", "sort_order", "is_active")


def _row(r: RegionRecord) -> dict:
    return {"code": r.code, "name": r.name, "kind": r.kind, "parent_code": r.parent_code,
            "iso_a2": r.iso_a2, "iso_a3": r.iso_a3, "m49": r.m49, "bbox": r.bbox,
            "sort_order": r.sort_order, "is_active": bool(r.is_active)}


def list_regions(include_inactive: bool = False) -> list[dict]:
    with MiscSessionLocal() as db:
        q = db.query(RegionRecord)
        if not include_inactive:
            q = q.filter(RegionRecord.is_active == 1)
        rows = q.order_by(RegionRecord.sort_order, RegionRecord.name).all()
        return [_row(r) for r in rows]


# "global" is the top of the tree (global > continent > country). The feed and dataset filters treat it as
# "the whole world" and skip filtering; with_descendants() keeps it out of the expansion so a stray call
# never builds a list of every region.
NON_CONTAINERS = ("global",)


def with_descendants(code: str) -> list[str]:
    """The region and every region below it (parent_code chain), e.g. africa -> [africa, eastern-africa, ke, ...].
    Inactive sub-regions are included so content tagged with them before they were switched off still matches.
    Used by the feed and dataset filters: picking a continent also shows its sub-regions' content."""
    code = (code or "").strip().lower()
    if not code or code in NON_CONTAINERS:
        return [code] if code else []
    with MiscSessionLocal() as db:
        pairs = db.query(RegionRecord.code, RegionRecord.parent_code).all()
    children: dict[str, list[str]] = {}
    for c, parent in pairs:
        if parent:
            children.setdefault(parent, []).append(c)
    out, todo = [], [code]
    while todo:
        c = todo.pop()
        if c in out:
            continue                      # guards against a parent loop typed in by hand
        out.append(c)
        todo.extend(children.get(c, []))
    return out


def exists(code: str) -> bool:
    with MiscSessionLocal() as db:
        return db.query(RegionRecord.region_id).filter(RegionRecord.code == code,
                                                       RegionRecord.is_active == 1).first() is not None


def _validate(fields: dict, db, code: Optional[str] = None) -> dict:
    out = {}
    for k in EDITABLE:
        if k not in fields:
            continue
        v = fields[k]
        if isinstance(v, str):
            v = v.strip() or None
        out[k] = v
    if "name" in out and not out["name"]:
        raise ValueError("A name is required")
    if out.get("kind") and out["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    parent = out.get("parent_code")
    if parent:
        if parent == code:
            raise ValueError("A region cannot be its own parent")
        if not db.query(RegionRecord.region_id).filter(RegionRecord.code == parent).first():
            raise ValueError(f"Unknown parent region '{parent}'")
    for k, n in (("iso_a2", 2), ("iso_a3", 3), ("m49", 3)):
        if out.get(k) and len(out[k]) != n:
            raise ValueError(f"{k} must be {n} characters")
    if out.get("bbox") is not None and (not isinstance(out["bbox"], list) or len(out["bbox"]) != 4):
        raise ValueError("bbox must be [minx, miny, maxx, maxy]")
    if "is_active" in out:
        out["is_active"] = 1 if out["is_active"] else 0
    return out


def create(fields: dict) -> dict:
    code = (fields.get("code") or "").strip().lower()
    if not CODE_RE.match(code):
        raise ValueError("code must be lowercase letters, digits and dashes (e.g. kenya or ke)")
    with MiscSessionLocal() as db:
        if db.query(RegionRecord.region_id).filter(RegionRecord.code == code).first():
            raise ValueError(f"Region '{code}' already exists")
        data = _validate(fields, db, code)
        if not data.get("name"):
            raise ValueError("A name is required")
        r = RegionRecord(code=code, **{"kind": "custom", **data})
        db.add(r)
        db.commit()
        db.refresh(r)
        return _row(r)


def update(code: str, fields: dict) -> dict:
    with MiscSessionLocal() as db:
        r = db.query(RegionRecord).filter(RegionRecord.code == code).first()
        if not r:
            raise LookupError(code)
        for k, v in _validate(fields, db, code).items():
            setattr(r, k, v)
        db.commit()
        db.refresh(r)
        return _row(r)


def delete(code: str) -> None:
    with MiscSessionLocal() as db:
        r = db.query(RegionRecord).filter(RegionRecord.code == code).first()
        if not r:
            raise LookupError(code)
        if db.query(RegionRecord.region_id).filter(RegionRecord.parent_code == code).first():
            raise ValueError("This region has sub-regions; delete or move them first")
        db.delete(r)
        db.commit()
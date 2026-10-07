"""
app/api/v0/endpoints/access.py
==============================
Visibility and sharing of posts and datasets, and what was shared with me. Rules: app/services/access_service.py.

  GET    /access/{kind}/{id}                      visibility + who has access + the full history (who shared
                                                  with whom, when, who revoked)          (owner / admin; editor for posts)
  PUT    /access/{kind}/{id}/visibility           {"visibility": "public" | "private"}
  POST   /access/{kind}/{id}/grants               {"handle": "@someone" | "@an_organisation"}   share (view)
  DELETE /access/{kind}/{id}/grants/{share_id}    revoke (the row is kept, with who revoked it)
  GET    /access/shared-with-me                   posts and datasets shared with me or my organisations

  kind = post | dataset   (datasets are managed by admins and their owner)
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import DatasetsSessionLocal, get_admin_db, get_posts_db, get_users_db
from app.db.models import (
    DatasetRecord, DatasetShareRecord, PlatformUserRecord, PostRecord, PostShareRecord, log_admin_action,
)
from app.services import access_service as acc
from app.services import catalog_service
from app.services.auth_service import get_current_authenticated_user

router = APIRouter()
KINDS = ("post", "dataset")


class VisibilityBody(BaseModel):
    visibility: str = Field(..., pattern="^(public|private)$")


class GrantBody(BaseModel):
    handle: str = Field(..., min_length=1, max_length=51)


def _post(posts_db, resource_id: str) -> PostRecord:
    try:
        pid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == pid).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Not found")
    return post


def _resource(kind: str, resource_id: str, viewer, posts_db):
    """(record, db session for its shares, share model, key column name, key) — managers only."""
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="Not found")
    if kind == "post":
        post = _post(posts_db, resource_id)
        if not acc.can_manage_post(viewer, post):
            raise HTTPException(status_code=404 if not acc.can_view_post(viewer, post, posts_db) else 403,
                                detail="Only the publisher (or an admin / editor) can change who sees this post")
        return post, posts_db, PostShareRecord, "post_id", post.post_id
    ds = catalog_service.raw((resource_id or "").strip().lower())
    if ds is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not acc.can_manage_dataset(viewer, ds):
        raise HTTPException(status_code=404 if not acc.can_view_dataset(viewer, ds) else 403,
                            detail="Only the dataset's owner or an admin can change who sees it")
    return ds, None, DatasetShareRecord, "dataset_id", ds.dataset_id


def _audit(admin_db, user, request: Request, action: str, kind: str, rid: str, payload: dict) -> None:
    try:
        log_admin_action(admin_db, admin_user_id=user.user_id, admin_handle=user.user_handle, action_type=action,
                         target_type=kind, target_id=rid, payload=payload,
                         ip_address=request.client.host if request.client else None)
    except Exception as e:                      # the share row itself is the record; this is a second copy
        print(f"[access] audit log failed: {e}")


@router.get("/shared-with-me")
def shared_with_me(
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    """The "Shared" tab of my profile: private posts and datasets shared with me (or with an organisation I am a
    member of), plus my organisations' own private ones — with who shared them and when."""
    from app.services.post_service import post_service
    v = acc.viewer_for(user, users_db)
    org_names = {}
    if v.org_ids:
        from app.db.models import OrganizationRecord
        for o in users_db.query(OrganizationRecord).filter(
                OrganizationRecord.org_id.in_([uuid.UUID(i) for i in v.org_ids])).all():
            org_names[str(o.org_id)] = o.name
            org_names[str(o.account_user_id)] = o.name

    def via(row) -> str:
        return "you" if row.grantee_type == "user" else f"{org_names.get(str(row.grantee_id), 'your organisation')}"

    # posts
    grants = posts_db.query(PostShareRecord).filter(acc._grant_filter(PostShareRecord, v)) \
        .order_by(PostShareRecord.created_timestamp.desc()).all()
    labels = acc._labels(users_db, grants)
    post_info: dict = {}
    for g in grants:
        post_info.setdefault(g.post_id, {"label": (labels.get(acc._uid(g.granted_by_user_id)) or {}).get("label"),
                                         "handle": (labels.get(acc._uid(g.granted_by_user_id)) or {}).get("handle"),
                                         "at": g.created_timestamp.isoformat() if g.created_timestamp else None,
                                         "via": via(g)})
    q = posts_db.query(PostRecord).filter(PostRecord.is_active == 1, PostRecord.publisher_user_id != user.user_id)
    conds = [PostRecord.post_id.in_(list(post_info))] if post_info else []
    if v.org_account_ids:
        conds.append((PostRecord.visibility == "private") &
                     PostRecord.publisher_user_id.in_([uuid.UUID(a) for a in v.org_account_ids]))
    posts = []
    if conds:
        from sqlalchemy import or_
        for p in q.filter(or_(*conds)).order_by(PostRecord.created_timestamp.desc()).limit(200).all():
            item = post_service._format_post_response(posts_db, users_db, p, user.user_id, v)
            item["shared_by"] = post_info.get(p.post_id) or {
                "label": org_names.get(str(p.publisher_user_id)), "handle": None,
                "at": p.created_timestamp.isoformat() if p.created_timestamp else None,
                "via": org_names.get(str(p.publisher_user_id), "your organisation")}
            posts.append(item)

    # datasets
    datasets = []
    with DatasetsSessionLocal() as ddb:
        dgrants = ddb.query(DatasetShareRecord).filter(acc._grant_filter(DatasetShareRecord, v)) \
            .order_by(DatasetShareRecord.created_timestamp.desc()).all()
        dlabels = acc._labels(users_db, dgrants)
        info: dict = {}
        for g in dgrants:
            info.setdefault(g.dataset_id, {"label": (dlabels.get(acc._uid(g.granted_by_user_id)) or {}).get("label"),
                                           "handle": (dlabels.get(acc._uid(g.granted_by_user_id)) or {}).get("handle"),
                                           "at": g.created_timestamp.isoformat() if g.created_timestamp else None,
                                           "via": via(g)})
        dq = ddb.query(DatasetRecord).filter(DatasetRecord.status == "active")
        dconds = [DatasetRecord.dataset_id.in_(list(info))] if info else []
        if v.org_account_ids:
            dconds.append((DatasetRecord.visibility == "private") &
                          DatasetRecord.owner_user_id.in_([uuid.UUID(a) for a in v.org_account_ids]))
        if dconds:
            from sqlalchemy import or_
            for r in dq.filter(or_(*dconds)).order_by(DatasetRecord.updated_timestamp.desc()).limit(200).all():
                card = catalog_service._card(r)
                card.pop("owner_user_id", None)
                card["can_see_rows"] = acc.can_see_rows(v, r, ddb)
                card["shared_by"] = info.get(r.dataset_id) or {
                    "label": org_names.get(str(r.owner_user_id)), "handle": None, "at": None,
                    "via": org_names.get(str(r.owner_user_id), "your organisation")}
                datasets.append({k: (x.isoformat() if hasattr(x, "isoformat") else x) for k, x in card.items()})
    return {"posts": posts, "datasets": datasets}


@router.get("/{kind}/{resource_id}")
def access_details(
    kind: str, resource_id: str,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    v = acc.viewer_for(user, users_db)
    rec, db, model, key_name, key = _resource(kind, resource_id, v, posts_db)
    if db is None:
        with DatasetsSessionLocal() as ddb:
            history = acc.share_rows(model, getattr(model, key_name), key, ddb, users_db)
    else:
        history = acc.share_rows(model, getattr(model, key_name), key, db, users_db)
    out = {"kind": kind, "id": str(key), "visibility": rec.visibility or "public",
           "grants": [h for h in history if h["active"]], "history": history}
    if kind == "post":
        ds = catalog_service.raw(rec.dataset_id) if rec.dataset_id else None
        owner = users_db.get(PlatformUserRecord, rec.publisher_user_id)
        out["dataset_visibility"] = (ds.visibility or "public") if ds is not None else None
        out["can_be_public"] = ds is None or acc.can_publish_publicly(acc.viewer_for(owner, users_db) if owner else acc.ANON, ds)
    return out


@router.put("/{kind}/{resource_id}/visibility")
def set_visibility(
    kind: str, resource_id: str, body: VisibilityBody, request: Request,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    v = acc.viewer_for(user, users_db)
    rec, db, *_ = _resource(kind, resource_id, v, posts_db)
    if kind == "post":
        if body.visibility == "public" and rec.dataset_id:
            ds = catalog_service.raw(rec.dataset_id)
            owner = users_db.get(PlatformUserRecord, rec.publisher_user_id)
            pv = v if v.is_admin else (acc.viewer_for(owner, users_db) if owner else acc.ANON)
            if ds is not None and not acc.can_publish_publicly(pv, ds):
                raise HTTPException(status_code=403, detail="This post is built on a private dataset that was only "
                                                             "shared with its publisher: it must stay private")
        rec.visibility = body.visibility
        posts_db.commit()
    else:
        try:
            catalog_service.update_meta(rec.dataset_id, {"visibility": body.visibility})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        from app.services import visual_service
        visual_service.invalidate(rec.dataset_id)
    _audit(admin_db, user, request, f"{kind}.visibility", kind, resource_id, {"visibility": body.visibility})
    return {"kind": kind, "id": resource_id, "visibility": body.visibility}


@router.post("/{kind}/{resource_id}/grants", status_code=201)
def add_grant(
    kind: str, resource_id: str, body: GrantBody, request: Request,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    v = acc.viewer_for(user, users_db)
    rec, db, model, key_name, key = _resource(kind, resource_id, v, posts_db)
    try:
        gtype, gid, label = acc.resolve_grantee(users_db, body.handle)
    except acc.AccessError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    if gtype == "user" and gid == user.user_id:
        raise HTTPException(status_code=422, detail="You already have access")
    if db is None:
        with DatasetsSessionLocal() as ddb:
            row = acc.grant(ddb, model, key_name, key, gtype, gid, user.user_id)
            share_id = row.share_id
    else:
        share_id = acc.grant(db, model, key_name, key, gtype, gid, user.user_id).share_id
    _audit(admin_db, user, request, f"{kind}.share", kind, resource_id, {"grantee": label, "grantee_type": gtype})
    _notify(users_db, gtype, gid, user, kind, rec)
    return {"share_id": str(share_id), "grantee_type": gtype, "grantee": label}


@router.delete("/{kind}/{resource_id}/grants/{share_id}")
def remove_grant(
    kind: str, resource_id: str, share_id: str, request: Request,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    v = acc.viewer_for(user, users_db)
    rec, db, model, key_name, key = _resource(kind, resource_id, v, posts_db)
    try:
        sid = uuid.UUID(share_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Share not found")
    try:
        if db is None:
            with DatasetsSessionLocal() as ddb:
                acc.revoke(ddb, model, key_name, key, sid, user.user_id)
        else:
            acc.revoke(db, model, key_name, key, sid, user.user_id)
    except acc.AccessError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    _audit(admin_db, user, request, f"{kind}.unshare", kind, resource_id, {"share_id": share_id})
    return {"share_id": share_id, "revoked": True}


def _notify(users_db, gtype: str, gid, by: PlatformUserRecord, kind: str, rec) -> None:
    """Tell a person (not a whole organisation) that something was shared with them. Best effort."""
    if gtype != "user":
        return
    try:
        from app.core.config import settings
        from app.services.auth_service import auth_service
        to = users_db.get(PlatformUserRecord, gid)
        if to is None:
            return
        what = f"the post “{rec.title}”" if kind == "post" else f"the dataset “{rec.title}”"
        link = f"{settings.FRONTEND_URL}/post/{rec.post_id}" if kind == "post" else f"{settings.FRONTEND_URL}/profile/{to.user_handle}"
        html = auth_service._build_html_email(
            title="Shared with you",
            content_html=f"<p>Hello <strong>@{to.user_handle}</strong>,</p><p>@{by.user_handle} shared {what} with you "
                         f"on GisViz.</p><div style='text-align:center;'><a href='{link}' class='btn'>Open it</a></div>"
                         "<p>You can find everything shared with you in the Shared tab of your profile.</p>")
        auth_service._email(to.email_address, f"@{by.user_handle} shared {what} with you",
                            f"@{by.user_handle} shared {what} with you on GisViz: {link}", html)
    except Exception as e:
        print(f"[access] share notification skipped: {e}")
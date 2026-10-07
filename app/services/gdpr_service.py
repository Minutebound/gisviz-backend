"""
app/services/gdpr_service.py
============================
Data-subject rights (GDPR Art. 15, 17, 20):

  export(user)  everything GisViz holds about a person, as one JSON document (access + portability):
                account, consent records, organisation, posts, comments, likes, bookmarks, follows, shares made and
                received, support tickets, page views recorded under the account
  erase(user)   permanently delete the account and the personal data linked to it (right to erasure):
                posts (and their images), comments, likes, bookmarks, follows, memberships, the organisation it
                runs, shares made with / by it (the history keeps a pseudonymous row: "deleted account"),
                view records become anonymous, support tickets lose their contact. Admin audit logs are kept
                (legal obligation / security), they hold only ids.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

from sqlalchemy import func, or_

from app.db.models import (
    ConsentRecord, DatasetRecord, DatasetShareRecord, FollowCurrentRecord, FollowEventRecord, OrganizationMemberRecord,
    OrganizationRecord, PlatformUserRecord, PostBookmarkRecord, PostCommentRecord, PostLikeRecord, PostRecord,
    PostShareRecord, PostViewRecord, SupportTicketRecord,
)


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None and not isinstance(v, (int, float, str, bool, list, dict)) else v)


def export(users_db, user: PlatformUserRecord) -> dict:
    from app.db.database import DatasetsSessionLocal, PostsSessionLocal
    uid = user.user_id
    loc = user.location
    out: dict = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "account": {
            "user_id": str(uid), "handle": user.user_handle, "email": user.email_address,
            "account_type": user.account_type or "individual", "verified": bool(user.is_verified),
            "active": bool(user.is_active), "title": user.title, "linkedin_url": user.linkedin_url,
            "medium_url": user.medium_url, "website_url": user.website_url, "avatar_path": user.avatar_path,
            "banner_path": user.banner_path, "role": user.role.name if user.role else None,
            "location": {"place": loc.place, "state": loc.state, "country": loc.country} if loc else None,
            "created_at": _iso(user.created_timestamp), "updated_at": _iso(user.updated_timestamp),
        },
        "consents": [{"document": c.document, "version": c.version, "accepted_at": _iso(c.accepted_timestamp)}
                     for c in users_db.query(ConsentRecord).filter(ConsentRecord.user_id == uid).all()],
    }
    org = users_db.query(OrganizationRecord).filter(OrganizationRecord.account_user_id == uid).first()
    out["organization"] = {"name": org.name, "domain": org.email_domain, "status": org.status,
                           "created_at": _iso(org.created_timestamp)} if org else None
    out["memberships"] = [{"organization": o.name, "domain": o.email_domain, "role": m.role, "source": m.source,
                           "joined_at": _iso(m.joined_timestamp)}
                          for m, o in users_db.query(OrganizationMemberRecord, OrganizationRecord)
                          .join(OrganizationRecord, OrganizationRecord.org_id == OrganizationMemberRecord.org_id)
                          .filter(OrganizationMemberRecord.user_id == uid).all()]
    handles = lambda ids: {u.user_id: u.user_handle for u in users_db.query(PlatformUserRecord)
                           .filter(PlatformUserRecord.user_id.in_(list(ids))).all()} if ids else {}
    following = users_db.query(FollowCurrentRecord).filter(FollowCurrentRecord.actor_user_id == uid).all()
    followers = users_db.query(FollowCurrentRecord).filter(FollowCurrentRecord.target_user_id == uid).all()
    names = handles({f.target_user_id for f in following} | {f.actor_user_id for f in followers})
    out["following"] = [{"handle": names.get(f.target_user_id), "since": _iso(f.followed_since)} for f in following]
    out["followers"] = [{"handle": names.get(f.actor_user_id), "since": _iso(f.followed_since)} for f in followers]
    out["support_tickets"] = [{"subject": t.subject, "category": t.category, "description": t.description,
                               "status": t.status, "created_at": _iso(t.created_timestamp)}
                              for t in users_db.query(SupportTicketRecord).filter(SupportTicketRecord.user_id == uid).all()]
    with PostsSessionLocal() as pdb:
        out["posts"] = [{"post_id": str(p.post_id), "title": p.title, "description": p.description, "note": p.note,
                         "visibility": p.visibility, "post_type": p.post_type, "dataset_id": p.dataset_id,
                         "region": p.region, "source_name": p.source_name, "source_url": p.source_url,
                         "created_at": _iso(p.created_timestamp), "active": bool(p.is_active)}
                        for p in pdb.query(PostRecord).filter(PostRecord.publisher_user_id == uid).all()]
        out["comments"] = [{"post_id": str(c.post_id), "content": c.content, "created_at": _iso(c.created_timestamp)}
                           for c in pdb.query(PostCommentRecord).filter(PostCommentRecord.user_id == uid).all()]
        out["likes"] = [{"post_id": str(l.post_id), "at": _iso(l.created_timestamp)}
                        for l in pdb.query(PostLikeRecord).filter(PostLikeRecord.user_id == uid).all()]
        out["bookmarks"] = [{"post_id": str(b.post_id), "at": _iso(b.created_timestamp)}
                            for b in pdb.query(PostBookmarkRecord).filter(PostBookmarkRecord.user_id == uid).all()]
        out["post_shares_made"] = [{"post_id": str(s.post_id), "to": f"{s.grantee_type}:{s.grantee_id}",
                                    "at": _iso(s.created_timestamp), "revoked_at": _iso(s.revoked_timestamp)}
                                   for s in pdb.query(PostShareRecord).filter(PostShareRecord.granted_by_user_id == uid).all()]
        out["post_shares_received"] = [{"post_id": str(s.post_id), "at": _iso(s.created_timestamp),
                                        "revoked_at": _iso(s.revoked_timestamp)}
                                       for s in pdb.query(PostShareRecord).filter(PostShareRecord.grantee_type == "user",
                                                                                   PostShareRecord.grantee_id == uid).all()]
    with DatasetsSessionLocal() as ddb:
        out["datasets_owned"] = [{"dataset_id": d.dataset_id, "title": d.title}
                                 for d in ddb.query(DatasetRecord).filter(DatasetRecord.owner_user_id == uid).all()]
        out["dataset_shares_received"] = [{"dataset_id": s.dataset_id, "at": _iso(s.created_timestamp),
                                           "revoked_at": _iso(s.revoked_timestamp)}
                                          for s in ddb.query(DatasetShareRecord).filter(
                                              DatasetShareRecord.grantee_type == "user", DatasetShareRecord.grantee_id == uid).all()]
    with PostsSessionLocal() as pdb:
        out["post_views"] = [{"post_id": str(v.post_id), "at": _iso(v.viewed_timestamp)}
                             for v in pdb.query(PostViewRecord).filter(PostViewRecord.user_id == uid)
                             .order_by(PostViewRecord.viewed_timestamp.desc()).limit(10000).all()]
    return out


def _remove_file(path: str | None) -> None:
    if path and path.startswith("/uploads/"):
        try:
            os.remove(path.lstrip("/"))
        except OSError:
            pass


def erase(users_db, user: PlatformUserRecord) -> dict:
    from app.db.database import DatasetsSessionLocal, PostsSessionLocal
    uid = user.user_id
    counts: dict = {}
    with PostsSessionLocal() as pdb:
        # likes / comments / bookmarks this person left on other posts (keep the counters right)
        for like in pdb.query(PostLikeRecord).filter(PostLikeRecord.user_id == uid).all():
            p = pdb.get(PostRecord, like.post_id)
            if p is not None and p.publisher_user_id != uid:
                p.total_likes_count = max(0, (p.total_likes_count or 0) - 1)
            pdb.delete(like)
        n_comments = 0
        for c in pdb.query(PostCommentRecord).filter(PostCommentRecord.user_id == uid).all():
            p = pdb.get(PostRecord, c.post_id)
            if p is not None and p.publisher_user_id != uid:
                p.total_comments_count = max(0, (p.total_comments_count or 0) - 1)
            pdb.delete(c)
            n_comments += 1
        pdb.query(PostBookmarkRecord).filter(PostBookmarkRecord.user_id == uid).delete()
        # shares: received ones go; the ones they made stay as pseudonymous history
        pdb.query(PostShareRecord).filter(PostShareRecord.grantee_type == "user", PostShareRecord.grantee_id == uid).delete()
        pdb.query(PostShareRecord).filter(PostShareRecord.granted_by_user_id == uid).update({"granted_by_user_id": None})
        pdb.query(PostShareRecord).filter(PostShareRecord.revoked_by_user_id == uid).update({"revoked_by_user_id": None})
        posts = pdb.query(PostRecord).filter(PostRecord.publisher_user_id == uid).all()
        for p in posts:
            _remove_file(p.visual_image_path)
            pdb.delete(p)
        pdb.commit()
        counts.update(posts=len(posts), comments=n_comments)
    with DatasetsSessionLocal() as ddb:
        ddb.query(DatasetShareRecord).filter(DatasetShareRecord.grantee_type == "user", DatasetShareRecord.grantee_id == uid).delete()
        ddb.query(DatasetShareRecord).filter(DatasetShareRecord.granted_by_user_id == uid).update({"granted_by_user_id": None})
        ddb.query(DatasetShareRecord).filter(DatasetShareRecord.revoked_by_user_id == uid).update({"revoked_by_user_id": None})
        ddb.query(DatasetRecord).filter(DatasetRecord.owner_user_id == uid).update({"owner_user_id": None})
        ddb.commit()
    with PostsSessionLocal() as pdb:          # views stay counted, no longer linked to the person
        pdb.query(PostViewRecord).filter(PostViewRecord.user_id == uid).update({"user_id": None})
        pdb.commit()
    # social graph + counters of the people they followed / who followed them
    for f in users_db.query(FollowCurrentRecord).filter(FollowCurrentRecord.actor_user_id == uid).all():
        t = users_db.get(PlatformUserRecord, f.target_user_id)
        if t is not None:
            t.follower_count = max(0, (t.follower_count or 0) - 1)
        users_db.delete(f)
    for f in users_db.query(FollowCurrentRecord).filter(FollowCurrentRecord.target_user_id == uid).all():
        a = users_db.get(PlatformUserRecord, f.actor_user_id)
        if a is not None:
            a.following_count = max(0, (a.following_count or 0) - 1)
        users_db.delete(f)
    users_db.query(FollowEventRecord).filter(or_(FollowEventRecord.actor_user_id == uid,
                                                 FollowEventRecord.target_user_id == uid)).delete()
    users_db.query(SupportTicketRecord).filter(SupportTicketRecord.user_id == uid).update(
        {"user_id": None, "contact_email": None})
    org = users_db.query(OrganizationRecord).filter(OrganizationRecord.account_user_id == uid).first()
    if org is not None:
        users_db.query(OrganizationMemberRecord).filter(OrganizationMemberRecord.org_id == org.org_id).delete()
        users_db.delete(org)
    _remove_file(user.avatar_path)
    _remove_file(user.banner_path)
    users_db.delete(user)          # cascades: location, memberships, consent records
    users_db.commit()
    return {"erased": True, **counts}
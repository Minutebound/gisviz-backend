"""
app/services/access_service.py
==============================
Who may see what. One place for every rule, so the post pages, the feeds, the dataset catalog, the Data tab and
the chart data all decide the same way (tests/test_security.py checks the whole matrix).

Accounts
  individual      a person
  organization    an organisation's own account (organizations row). An admin verifies its email domain; from
                  then on every user whose email is on that domain is a member (organization_members).

Visibility
  public          everyone (posts: when active; datasets: when active)
  private         the owner, admins (posts: also editors, for moderation), the people and organisations it is
                  shared with (post_shares / dataset_shares, with who shared it and when; revoking keeps the row),
                  and the members of the owning organisation (when the owner is an organisation account)

Posts on datasets
  * a public post on a PRIVATE dataset shows the visual (aggregates) to everyone, never the rows;
  * only someone who controls the dataset (admin, owner, member of the owning organisation) may publish a
    public post from a private dataset; people it was merely shared with may only make private posts;
  * the rows (Data tab) follow can_see_rows(): dataset access + licence + personal-data flag + show_data.

Chart data URLs carry a short-lived signed token (data_token) for private datasets, issued only to viewers who
may see the post / dataset, so the browser can fetch the visual without sending its session.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import and_, or_

from app.core.config import settings
from app.db.models import (
    DatasetRecord, DatasetShareRecord, OrganizationMemberRecord, OrganizationRecord, PlatformUserRecord,
    PostRecord, PostShareRecord,
)
from app.services import licences

PUBLISHING_ROLES = ("admin", "publisher", "editor", "support")
POST_STAFF_ROLES = ("admin", "editor")          # may see every post (moderation)
VISIBILITIES = ("public", "private")
DATA_TOKEN_TTL = 24 * 3600

# Free / public mail providers: never accepted as an organisation's domain (anyone could join).
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "yahoo.fr", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.de", "gmx.net", "mail.com", "yandex.com",
    "yandex.ru", "zoho.com", "qq.com", "163.com", "126.com", "rediffmail.com", "web.de", "tutanota.com",
    "fastmail.com", "hey.com", "duck.com", "mail.ru", "inbox.com", "example.com",
}


class AccessError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message, self.status = message, status


# ── the viewer ──────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Viewer:
    user_id: Optional[uuid.UUID] = None
    role: str = "anonymous"
    org_ids: frozenset = field(default_factory=frozenset)          # verified organisations the user belongs to
    org_account_ids: frozenset = field(default_factory=frozenset)  # those organisations' own account ids

    @property
    def signed_in(self) -> bool:
        return self.user_id is not None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_post_staff(self) -> bool:
        return self.role in POST_STAFF_ROLES


ANON = Viewer()


def _uid(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    return str(v).lower()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def viewer_for(user: Optional[PlatformUserRecord], users_db=None) -> Viewer:
    """The access context of a signed-in user (None -> anonymous)."""
    if user is None:
        return ANON
    role = user.role.name if getattr(user, "role", None) else "viewer"
    if getattr(user, "user_id", None) is None:          # a bare role (internal callers, tests): no organisations
        return Viewer(user_id=uuid.UUID(int=0), role=role)
    close = False
    if users_db is None:
        from app.db.database import UsersSessionLocal
        users_db, close = UsersSessionLocal(), True
    try:
        rows = (users_db.query(OrganizationRecord.org_id, OrganizationRecord.account_user_id)
                .join(OrganizationMemberRecord, OrganizationMemberRecord.org_id == OrganizationRecord.org_id)
                .filter(OrganizationMemberRecord.user_id == user.user_id, OrganizationRecord.status == "verified")
                .all())
    finally:
        if close:
            users_db.close()
    return Viewer(user_id=user.user_id, role=role,
                  org_ids=frozenset(_uid(o) for o, _ in rows),
                  org_account_ids=frozenset(_uid(a) for _, a in rows))


# ── grants ──────────────────────────────────────────────────────────────────────────────────────────

def _grant_filter(model, v: Viewer):
    """Active shares that reach this viewer: to the user, or to one of their organisations."""
    conds = [and_(model.grantee_type == "user", model.grantee_id == v.user_id)]
    if v.org_ids:
        conds.append(and_(model.grantee_type == "org", model.grantee_id.in_([uuid.UUID(o) for o in v.org_ids])))
    return and_(model.revoked_timestamp.is_(None), or_(*conds))


def _has_post_grant(posts_db, post_id, v: Viewer) -> bool:
    close = False
    if posts_db is None:
        from app.db.database import PostsSessionLocal
        posts_db, close = PostsSessionLocal(), True
    try:
        return posts_db.query(PostShareRecord.share_id).filter(
            PostShareRecord.post_id == post_id, _grant_filter(PostShareRecord, v)).first() is not None
    finally:
        if close:
            posts_db.close()


def _has_dataset_grant(dataset_id: str, v: Viewer, datasets_db=None) -> bool:
    close = False
    if datasets_db is None:
        from app.db.database import DatasetsSessionLocal
        datasets_db, close = DatasetsSessionLocal(), True
    try:
        return datasets_db.query(DatasetShareRecord.share_id).filter(
            DatasetShareRecord.dataset_id == dataset_id, _grant_filter(DatasetShareRecord, v)).first() is not None
    finally:
        if close:
            datasets_db.close()


# ── posts ───────────────────────────────────────────────────────────────────────────────────────────

def is_post_owner_side(v: Viewer, post) -> bool:
    """Publisher, or a member of the organisation that published it."""
    pub = _uid(_get(post, "publisher_user_id"))
    return v.signed_in and (pub == _uid(v.user_id) or pub in v.org_account_ids)


def can_view_post(v: Viewer, post, posts_db=None) -> bool:
    """Visibility only (inactive / deleted posts are handled by the caller)."""
    if (_get(post, "visibility") or "public") != "private":
        return True
    if not v.signed_in:
        return False
    if v.is_post_staff or is_post_owner_side(v, post):
        return True
    return _has_post_grant(posts_db, _get(post, "post_id"), v)


def can_manage_post(v: Viewer, post) -> bool:
    return v.signed_in and (v.is_post_staff or _uid(_get(post, "publisher_user_id")) == _uid(v.user_id))


def public_posts(query):
    """Feeds, search, trending, profiles of others: only public posts (private ones are reached by link/shares)."""
    return query.filter(PostRecord.visibility == "public")


# ── datasets ────────────────────────────────────────────────────────────────────────────────────────

def can_manage_dataset(v: Viewer, ds) -> bool:
    return v.signed_in and (v.is_admin or _uid(_get(ds, "owner_user_id")) == _uid(v.user_id))


def can_view_dataset(v: Viewer, ds, datasets_db=None) -> bool:
    if ds is None:
        return False
    if (_get(ds, "visibility") or "public") != "private":
        return True
    if not v.signed_in:
        return False
    if can_manage_dataset(v, ds) or _uid(_get(ds, "owner_user_id")) in v.org_account_ids:
        return True
    return _has_dataset_grant(_get(ds, "dataset_id"), v, datasets_db)


def can_publish_publicly(v: Viewer, ds) -> bool:
    """May this viewer put a PUBLIC post on this dataset? (shown visual only; rows stay hidden)"""
    if (_get(ds, "visibility") or "public") != "private":
        return True
    return can_manage_dataset(v, ds) or (v.signed_in and _uid(_get(ds, "owner_user_id")) in v.org_account_ids)


def can_see_rows(v: Viewer, ds, datasets_db=None) -> bool:
    """The Data tab / GET /datasets/{id}/rows: the dataset's own rows, not just the visual."""
    if ds is None:
        return False
    if can_manage_dataset(v, ds):
        return True
    if not can_view_dataset(v, ds, datasets_db):
        return False
    if int(_get(ds, "contains_personal_data") or 0):
        return False                                   # GDPR: personal data never leaves its managers
    if not licences.rows_public(_get(ds, "license_code")):
        return False                                   # the licence allows the visual, not redistribution
    if _get(ds, "show_data"):
        return True
    return v.role in PUBLISHING_ROLES                   # publishers read rows to build posts


def visible_dataset_filter(query, v: Viewer, datasets_db=None):
    """Catalog / picker query: public datasets plus the private ones this viewer may see."""
    if v.is_admin:
        return query
    if not v.signed_in:
        return query.filter(DatasetRecord.visibility != "private")
    owners = [v.user_id, *[uuid.UUID(a) for a in v.org_account_ids]]
    shared = datasets_db.query(DatasetShareRecord.dataset_id).filter(_grant_filter(DatasetShareRecord, v)) \
        if datasets_db is not None else None
    conds = [DatasetRecord.visibility != "private", DatasetRecord.owner_user_id.in_(owners)]
    if shared is not None:
        conds.append(DatasetRecord.dataset_id.in_(shared))
    return query.filter(or_(*conds))


# ── signed chart-data tokens ────────────────────────────────────────────────────────────────────────

def _sig(dataset_id: str, exp: int) -> str:
    key = (settings.JWT_SECRET_KEY or "gisviz").encode()
    return hmac.new(key, f"data:{dataset_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def data_token(dataset_id: str, ttl: int = DATA_TOKEN_TTL) -> str:
    exp = int(time.time()) + ttl
    return f"{exp}.{_sig(dataset_id, exp)}"


def verify_data_token(dataset_id: str, token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sig(dataset_id, exp))


def with_token(url: Optional[str], dataset_id: str) -> Optional[str]:
    if not url or not isinstance(url, str):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={data_token(dataset_id)}"


def sign_spec(spec: Optional[dict], dataset_id: Optional[str]) -> Optional[dict]:
    """A copy of a visual spec whose data URLs carry a data token (private datasets only)."""
    if not spec or not dataset_id:
        return spec
    out = dict(spec)
    out["data"] = with_token(out.get("data"), dataset_id)
    if isinstance(out.get("time"), dict) and out["time"].get("series"):
        out["time"] = {**out["time"], "series": with_token(out["time"]["series"], dataset_id)}
    return out


# ── organisations: membership by verified email domain ──────────────────────────────────────────────

def email_domain(email: Optional[str]) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower() if email and "@" in email else ""


def org_of_account(users_db, user_id) -> Optional[OrganizationRecord]:
    return users_db.query(OrganizationRecord).filter(OrganizationRecord.account_user_id == user_id).first()


def sync_memberships(users_db, user: PlatformUserRecord, commit: bool = True) -> None:
    """Domain memberships follow the user's current, verified email: joined when it matches a verified
    organisation's domain, removed when it no longer does. The organisation account itself is the owner."""
    if user is None:
        return
    dom = email_domain(user.email_address) if user.is_verified else ""
    orgs = users_db.query(OrganizationRecord).filter(OrganizationRecord.status == "verified",
                                                     OrganizationRecord.email_domain == dom).all() if dom else []
    want = {o.org_id for o in orgs}
    current = {m.org_id: m for m in users_db.query(OrganizationMemberRecord)
               .filter(OrganizationMemberRecord.user_id == user.user_id).all()}
    for org_id, m in current.items():
        if m.source == "domain" and org_id not in want:
            users_db.delete(m)
    for o in orgs:
        if o.org_id not in current:
            users_db.add(OrganizationMemberRecord(org_id=o.org_id, user_id=user.user_id,
                                                  role="owner" if o.account_user_id == user.user_id else "member",
                                                  source="account" if o.account_user_id == user.user_id else "domain"))
    if commit:
        users_db.commit()


def backfill_org(users_db, org: OrganizationRecord) -> int:
    """After an admin verifies an organisation: every verified user on its domain becomes a member."""
    n = 0
    users = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.email_address.ilike(f"%@{org.email_domain}"), PlatformUserRecord.is_verified == 1).all()
    for u in users:
        if email_domain(u.email_address) != org.email_domain:
            continue
        if users_db.get(OrganizationMemberRecord, (org.org_id, u.user_id)) is None:
            users_db.add(OrganizationMemberRecord(org_id=org.org_id, user_id=u.user_id,
                                                  role="owner" if u.user_id == org.account_user_id else "member",
                                                  source="account" if u.user_id == org.account_user_id else "domain"))
            n += 1
    users_db.commit()
    return n


def drop_org_members(users_db, org: OrganizationRecord) -> None:
    users_db.query(OrganizationMemberRecord).filter(OrganizationMemberRecord.org_id == org.org_id).delete()
    users_db.commit()


# ── sharing ─────────────────────────────────────────────────────────────────────────────────────────

def resolve_grantee(users_db, handle: str) -> tuple[str, uuid.UUID, str]:
    """'@someone' -> ('user', user_id, label); an organisation account's handle -> ('org', org_id, label)."""
    h = (handle or "").strip().lstrip("@").strip()
    if not h or len(h) > 50:
        raise AccessError("Enter the @handle of a person or an organisation", 422)
    from sqlalchemy import func
    user = users_db.query(PlatformUserRecord).filter(func.lower(PlatformUserRecord.user_handle) == h.lower()).first()
    if user is None or not user.is_active or not user.is_verified:
        raise AccessError(f"No active account @{h}", 404)
    if user.account_type == "organization":
        org = org_of_account(users_db, user.user_id)
        if org is not None and org.status == "verified":
            return "org", org.org_id, f"{org.name} (@{user.user_handle}, everyone @{org.email_domain})"
    return "user", user.user_id, f"@{user.user_handle}"


def _labels(users_db, rows: Iterable) -> dict:
    """grantee / granter ids -> display labels (users DB)."""
    user_ids, org_ids = set(), set()
    for r in rows:
        (org_ids if r.grantee_type == "org" else user_ids).add(r.grantee_id)
        for k in ("granted_by_user_id", "revoked_by_user_id"):
            if getattr(r, k, None):
                user_ids.add(getattr(r, k))
    out = {}
    if user_ids:
        for u in users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(user_ids)).all():
            out[_uid(u.user_id)] = {"handle": u.user_handle, "label": f"@{u.user_handle}",
                                    "account_type": u.account_type or "individual"}
    if org_ids:
        orgs = users_db.query(OrganizationRecord).filter(OrganizationRecord.org_id.in_(org_ids)).all()
        acc = {o.account_user_id for o in orgs}
        handles = {u.user_id: u.user_handle for u in users_db.query(PlatformUserRecord)
                   .filter(PlatformUserRecord.user_id.in_(acc)).all()} if acc else {}
        for o in orgs:
            out[_uid(o.org_id)] = {"handle": handles.get(o.account_user_id), "label": o.name,
                                   "account_type": "organization", "domain": o.email_domain}
    return out


def share_rows(model, key_col, key, db, users_db, include_revoked: bool = True) -> list[dict]:
    q = db.query(model).filter(key_col == key)
    if not include_revoked:
        q = q.filter(model.revoked_timestamp.is_(None))
    rows = q.order_by(model.created_timestamp.desc()).all()
    names = _labels(users_db, rows)
    deleted = {"handle": None, "label": "deleted account"}
    return [{
        "share_id": str(r.share_id), "grantee_type": r.grantee_type, "grantee_id": str(r.grantee_id),
        "grantee": names.get(_uid(r.grantee_id), deleted),
        "permission": r.permission,
        "granted_by": names.get(_uid(r.granted_by_user_id), deleted) if r.granted_by_user_id else None,
        "granted_at": r.created_timestamp.isoformat() if r.created_timestamp else None,
        "revoked_at": r.revoked_timestamp.isoformat() if r.revoked_timestamp else None,
        "revoked_by": names.get(_uid(r.revoked_by_user_id), deleted) if r.revoked_by_user_id else None,
        "active": r.revoked_timestamp is None,
    } for r in rows]


def grant(db, model, key_name: str, key, grantee_type: str, grantee_id, by_user_id) -> Any:
    """Share with a user / organisation (idempotent: an existing active share is returned)."""
    existing = db.query(model).filter(getattr(model, key_name) == key, model.grantee_type == grantee_type,
                                      model.grantee_id == grantee_id, model.revoked_timestamp.is_(None)).first()
    if existing is not None:
        return existing
    row = model(**{key_name: key}, grantee_type=grantee_type, grantee_id=grantee_id, permission="view",
                granted_by_user_id=by_user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def revoke(db, model, key_name: str, key, share_id, by_user_id) -> None:
    import datetime as dt
    row = db.query(model).filter(model.share_id == share_id, getattr(model, key_name) == key).first()
    if row is None:
        raise AccessError("Share not found", 404)
    if row.revoked_timestamp is None:
        row.revoked_timestamp = dt.datetime.now(dt.timezone.utc)
        row.revoked_by_user_id = by_user_id
        db.commit()
"""
app/analytics/snapshot.py

Orchestrator-agnostic ETL — called by Dagster, cron, or manual trigger.
Reads users_db + posts_db, writes one snapshot per day into analytics_db.
Re-running for the same date is safe (UPSERT on primary key).

This version populates BOTH:
  • the daily totals + per-post/per-category facts
  • the ranked lists (top posts/users/commenters) + weekly deltas,
    so the ENTIRE analytics page can be served from analytics_db.

CLI:
    python -m app.analytics.snapshot            # today (UTC)
    python -m app.analytics.snapshot 2026-07-01 # a specific date
"""

from __future__ import annotations

import sys
import uuid
import logging
from datetime import datetime, timezone, timedelta, date

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.database import UsersSessionLocal, PostsSessionLocal, AnalyticsSessionLocal
from app.db.models import (
    PlatformUserRecord, PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, CategoryRecord, KeywordRecord,
    FollowCurrentRecord, PostReportRecord,
)
from app.db.analytics_models import (
    DimDate, DimUser, DimPost, DimCategory,
    FactDailySnapshot, FactPostEngagement, FactCategoryDaily, EtlRunLog,
    FactWeeklyDelta, FactTopPost, FactTopUser, FactTopCommenter,
)

log = logging.getLogger("gisviz.analytics.snapshot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOP_N = 10


# ── helpers ──────────────────────────────────────────────────────────

def _ensure_dim_date(a_db: Session, d: date) -> None:
    stmt = pg_insert(DimDate.__table__).values(
        date_key=d, year=d.year, quarter=(d.month - 1) // 3 + 1,
        month=d.month, day=d.day, day_of_week=d.weekday(),
        week_of_year=int(d.strftime("%V")),
        is_weekend=1 if d.weekday() >= 5 else 0,
    ).on_conflict_do_nothing(index_elements=["date_key"])
    a_db.execute(stmt)


def _upsert(a_db: Session, table, values: dict, pk_cols: list) -> None:
    stmt = pg_insert(table).values(**values)
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in table.columns
        if c.name not in pk_cols and c.name != "captured_timestamp"
    }
    stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
    a_db.execute(stmt)


# ── main entry point ─────────────────────────────────────────────────

def run_daily_snapshot(snapshot_date: date | None = None) -> dict:
    """Build and persist the analytics snapshot for snapshot_date."""
    snap = snapshot_date or datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc)
    window_start = datetime.combine(snap, datetime.min.time(), tzinfo=timezone.utc)
    window_end   = window_start + timedelta(days=1)

    u_db = UsersSessionLocal()
    p_db = PostsSessionLocal()
    a_db = AnalyticsSessionLocal()

    run = EtlRunLog(run_id=uuid.uuid4(), job_name="run_daily_snapshot",
                    snapshot_date=snap, status="running")
    a_db.add(run)
    a_db.commit()

    rows_written = 0
    try:
        _ensure_dim_date(a_db, snap)

        # ── platform-wide totals ──────────────────────────────────────
        total_users      = u_db.query(PlatformUserRecord).count()
        total_posts      = p_db.query(PostRecord).count()
        total_likes      = p_db.query(PostLikeRecord).count()
        total_bookmarks  = p_db.query(PostBookmarkRecord).count()
        total_comments   = p_db.query(PostCommentRecord).count()
        total_follows    = u_db.query(FollowCurrentRecord).count()
        total_categories = p_db.query(CategoryRecord).count()
        total_keywords   = p_db.query(KeywordRecord).count()
        open_reports     = p_db.query(PostReportRecord).filter(
            PostReportRecord.status == "open"
        ).count()

        new_users = u_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.created_timestamp >= window_start,
            PlatformUserRecord.created_timestamp <  window_end,
        ).count()
        new_posts = p_db.query(PostRecord).filter(
            PostRecord.created_timestamp >= window_start,
            PostRecord.created_timestamp <  window_end,
        ).count()

        _upsert(a_db, FactDailySnapshot.__table__, {
            "snapshot_date": snap,
            "total_users": total_users, "total_posts": total_posts,
            "total_likes": total_likes, "total_bookmarks": total_bookmarks,
            "total_comments": total_comments, "total_follows": total_follows,
            "total_categories": total_categories, "total_keywords": total_keywords,
            "open_reports": open_reports,
            "new_users": new_users, "new_posts": new_posts,
        }, pk_cols=["snapshot_date"])
        rows_written += 1

        # ── per-post engagement ───────────────────────────────────────
        bm_counts = dict(
            p_db.query(PostBookmarkRecord.post_id, func.count())
                .group_by(PostBookmarkRecord.post_id).all()
        )
        for post in p_db.query(PostRecord).all():
            _upsert(a_db, FactPostEngagement.__table__, {
                "snapshot_date": snap, "post_id": post.post_id,
                "likes":     post.total_likes_count or 0,
                "bookmarks": int(bm_counts.get(post.post_id, 0)),
                "comments":  post.total_comments_count or 0,
            }, pk_cols=["snapshot_date", "post_id"])
            rows_written += 1
            _upsert(a_db, DimPost.__table__, {
                "post_id": post.post_id, "title": post.title,
                "publisher_user_id": post.publisher_user_id,
                "created_date": post.created_timestamp.date() if post.created_timestamp else None,
            }, pk_cols=["post_id"])

        # ── per-category usage ────────────────────────────────────────
        for cat in p_db.query(CategoryRecord).all():
            _upsert(a_db, FactCategoryDaily.__table__, {
                "snapshot_date": snap, "category_id": cat.category_id,
                "usage_count": cat.usage_count or 0,
                "post_count":  len(cat.post_links) if cat.post_links is not None else 0,
            }, pk_cols=["snapshot_date", "category_id"])
            rows_written += 1
            _upsert(a_db, DimCategory.__table__, {
                "category_id": cat.category_id, "slug": cat.slug, "label": cat.label,
            }, pk_cols=["category_id"])

        # ── user dimension refresh + handle/role lookup ───────────────
        _all_users = u_db.query(PlatformUserRecord).all()
        _handle_by_id = {u.user_id: u.user_handle for u in _all_users}
        _role_by_id   = {u.user_id: (u.role.name if u.role else "viewer") for u in _all_users}
        for usr in _all_users:
            _upsert(a_db, DimUser.__table__, {
                "user_id": usr.user_id, "user_handle": usr.user_handle,
                "role_name": usr.role.name if usr.role else None,
                "first_seen_date": usr.created_timestamp.date() if usr.created_timestamp else None,
            }, pk_cols=["user_id"])

        # ══════════════════════════════════════════════════════════════
        #  RANKED LISTS + WEEKLY DELTAS  (so the whole page is DB-backed)
        # ══════════════════════════════════════════════════════════════

        # weekly deltas
        _week_ago  = window_start - timedelta(days=7)
        _two_weeks = window_start - timedelta(days=14)
        tw_users = u_db.query(PlatformUserRecord).filter(PlatformUserRecord.created_timestamp >= _week_ago).count()
        tw_posts = p_db.query(PostRecord).filter(PostRecord.created_timestamp >= _week_ago).count()
        lw_users = u_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.created_timestamp >= _two_weeks,
            PlatformUserRecord.created_timestamp <  _week_ago).count()
        lw_posts = p_db.query(PostRecord).filter(
            PostRecord.created_timestamp >= _two_weeks,
            PostRecord.created_timestamp <  _week_ago).count()
        _upsert(a_db, FactWeeklyDelta.__table__, {
            "snapshot_date": snap,
            "this_week_new_users": tw_users, "this_week_new_posts": tw_posts,
            "last_week_new_users": lw_users, "last_week_new_posts": lw_posts,
        }, pk_cols=["snapshot_date"])

        # wipe prior ranked rows for this snapshot (idempotent re-run)
        a_db.query(FactTopPost).filter(FactTopPost.snapshot_date == snap).delete()
        a_db.query(FactTopUser).filter(FactTopUser.snapshot_date == snap).delete()
        a_db.query(FactTopCommenter).filter(FactTopCommenter.snapshot_date == snap).delete()

        # top posts by likes / comments
        def _store_top_posts(rank_by, posts):
            for i, p in enumerate(posts, start=1):
                metric = (p.total_likes_count if rank_by == "likes"
                          else p.total_comments_count if rank_by == "comments" else 0)
                a_db.add(FactTopPost(
                    snapshot_date=snap, rank_by=rank_by, rank=i,
                    post_id=p.post_id, title=p.title,
                    publisher_handle=_handle_by_id.get(p.publisher_user_id, "unknown"),
                    total_likes_count=p.total_likes_count or 0,
                    total_comments_count=p.total_comments_count or 0,
                    metric_value=metric or 0,
                ))

        _store_top_posts("likes",
            p_db.query(PostRecord).order_by(PostRecord.total_likes_count.desc()).limit(TOP_N).all())
        _store_top_posts("comments",
            p_db.query(PostRecord).order_by(PostRecord.total_comments_count.desc()).limit(TOP_N).all())

        # top posts by bookmarks
        _bm_rank = (p_db.query(PostBookmarkRecord.post_id, func.count().label("cnt"))
                    .group_by(PostBookmarkRecord.post_id)
                    .order_by(func.count().desc()).limit(TOP_N).all())
        _bm_ids   = [r.post_id for r in _bm_rank]
        _bm_cnt   = {r.post_id: r.cnt for r in _bm_rank}
        _bm_posts = {p.post_id: p for p in p_db.query(PostRecord).filter(PostRecord.post_id.in_(_bm_ids)).all()}
        for i, pid in enumerate(_bm_ids, start=1):
            p = _bm_posts.get(pid)
            if not p:
                continue
            a_db.add(FactTopPost(
                snapshot_date=snap, rank_by="bookmarks", rank=i,
                post_id=p.post_id, title=p.title,
                publisher_handle=_handle_by_id.get(p.publisher_user_id, "unknown"),
                total_likes_count=p.total_likes_count or 0,
                total_comments_count=p.total_comments_count or 0,
                metric_value=int(_bm_cnt.get(pid, 0)),
            ))

        # top users by followers / posts
        def _store_top_users(rank_by, users):
            for i, u in enumerate(users, start=1):
                a_db.add(FactTopUser(
                    snapshot_date=snap, rank_by=rank_by, rank=i,
                    user_id=u.user_id, user_handle=u.user_handle,
                    role_name=_role_by_id.get(u.user_id, "viewer"),
                    follower_count=u.follower_count or 0,
                    post_count=u.post_count or 0,
                ))

        _store_top_users("followers",
            u_db.query(PlatformUserRecord).order_by(PlatformUserRecord.follower_count.desc()).limit(TOP_N).all())
        _store_top_users("posts",
            u_db.query(PlatformUserRecord).order_by(PlatformUserRecord.post_count.desc()).limit(TOP_N).all())

        # top commenters
        _cm_rank = (p_db.query(PostCommentRecord.user_id, func.count().label("cnt"))
                    .group_by(PostCommentRecord.user_id)
                    .order_by(func.count().desc()).limit(TOP_N).all())
        for i, row in enumerate(_cm_rank, start=1):
            a_db.add(FactTopCommenter(
                snapshot_date=snap, rank=i,
                user_id=row.user_id,
                user_handle=_handle_by_id.get(row.user_id, "unknown"),
                comment_count=int(row.cnt),
            ))

        a_db.commit()

        run.status        = "success"
        run.rows_written  = rows_written
        run.finished_timestamp = datetime.now(timezone.utc)
        run.duration_seconds   = (run.finished_timestamp - now).total_seconds()
        a_db.commit()

        summary = {
            "snapshot_date": str(snap), "rows_written": rows_written,
            "totals": {"users": total_users, "posts": total_posts,
                       "likes": total_likes, "comments": total_comments},
        }
        log.info("snapshot OK: %s", summary)
        return summary

    except Exception as exc:
        a_db.rollback()
        run.status        = "failed"
        run.error_message = str(exc)
        run.finished_timestamp = datetime.now(timezone.utc)
        a_db.commit()
        log.exception("snapshot FAILED for %s", snap)
        raise
    finally:
        u_db.close()
        p_db.close()
        a_db.close()


if __name__ == "__main__":
    target = None
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    run_daily_snapshot(target)
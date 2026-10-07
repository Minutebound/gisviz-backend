"""
app/analytics/snapshot.py
=========================
The analytics snapshot: copies daily numbers from the users and posts databases into the analytics
database, so the admin growth charts have history (the live databases only know "now").

Writes (analytics DB):
  daily_platform_stats   one row per day   totals + what happened that day (new users/posts, views)
  daily_category_stats   one row per category per day
  snapshot_runs          one row per run (success / failure, how long, how many rows)

When it runs:
  - automatically every SNAPSHOT_EVERY_SECONDS (default 1 hour) from the API (start_scheduler);
    today's row is refreshed in place, yesterday's is finalised once after midnight UTC;
  - on demand: POST /admin/run-snapshot (Refresh button on /admin/analytics);
  - CLI: python -m app.analytics.snapshot
Re-running is always safe (upserts). On the first run, days with no row (up to BACKFILL_DAYS back)
are rebuilt from creation dates: those totals count rows that still exist, so deletions before the
first run are not reflected.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from itertools import accumulate

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import AnalyticsSessionLocal, PostsSessionLocal, UsersSessionLocal, analytics_engine
from app.db.models import DailyCategoryStats, DailyPlatformStats, SnapshotRun

log = logging.getLogger("gisviz.analytics.snapshot")

BACKFILL_DAYS = 365
RUN_LOG_DAYS = 60
SNAPSHOT_EVERY_SECONDS = int(os.getenv("SNAPSHOT_EVERY_SECONDS", "3600"))   # 0 = no automatic runs
_LOCK_ID = 424250


def _start(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=timezone.utc)


def _per_day(db, sql: str, until: date, params: dict | None = None) -> dict:
    """{day: count} for a "SELECT day, n" query grouped by
    UTC day; rows with no timestamp count as old."""
    rows = db.execute(text(sql), {"end": _start(until + timedelta(days=1)), **(params or {})}).all()
    out: dict = defaultdict(int)
    for d, n in rows:
        out[d] += n
    return out


def _totals(per_day: dict, days: list[date]) -> dict:
    """Running totals at the end of each day in days (sorted), from per-day counts."""
    base = sum(n for d, n in per_day.items() if d is None or d < days[0])
    span = [per_day.get(d, 0) for d in _all_days(days)]
    run = dict(zip(_all_days(days), accumulate(span, initial=None)))
    return {d: base + run[d] for d in days}


def _all_days(days: list[date]) -> list[date]:
    return [days[0] + timedelta(days=i) for i in range((days[-1] - days[0]).days + 1)]


_DAY = "(({col}) AT TIME ZONE 'UTC')::date"


def _count_by_day(db, table: str, col: str, until: date, where: str = "") -> dict:
    return _per_day(db, f"SELECT {_DAY.format(col=col)} AS d, count(*) FROM {table} "
                        f"WHERE ({col} IS NULL OR {col} < :end) {where} GROUP BY 1", until)


def compute(days: list[date]) -> tuple[list[dict], list[dict]]:
    """Rows for daily_platform_stats and daily_category_stats for the given days (sorted)."""
    last = days[-1]
    lo, hi = _start(days[0]), _start(last + timedelta(days=1))
    with UsersSessionLocal() as udb, PostsSessionLocal() as pdb:
        users = _count_by_day(udb, "platform_users", "created_timestamp", last)
        follows = _count_by_day(udb, "follows_current", "followed_since", last)
        posts = _count_by_day(pdb, "posts", "created_timestamp", last)
        likes = _count_by_day(pdb, "post_likes", "created_timestamp", last)
        bookmarks = _count_by_day(pdb, "post_bookmarks", "created_timestamp", last)
        comments = _count_by_day(pdb, "post_comments", "created_timestamp", last)
        reports = _count_by_day(pdb, "post_reports", "created_timestamp", last, "AND status = 'open'")
        views = {d: (n, v) for d, n, v in pdb.execute(text(
            f"SELECT {_DAY.format(col='viewed_timestamp')}, count(*), count(DISTINCT viewer_hash) FROM post_views "
            "WHERE viewed_timestamp >= :lo AND viewed_timestamp < :hi GROUP BY 1"), {"lo": lo, "hi": hi}).all()}
        cats = pdb.execute(text("SELECT category_id, label FROM categories")).all()
        cat_posts: dict = defaultdict(lambda: defaultdict(int))
        for cid, d, n in pdb.execute(text(
                f"SELECT pc.category_id, {_DAY.format(col='p.created_timestamp')}, count(*) FROM post_categories pc "
                "JOIN posts p ON p.post_id = pc.post_id WHERE p.created_timestamp IS NULL OR p.created_timestamp < :end "
                "GROUP BY 1, 2"), {"end": hi}).all():
            cat_posts[cid][d] += n
        cat_views: dict = defaultdict(dict)
        for cid, d, n in pdb.execute(text(
                f"SELECT pc.category_id, {_DAY.format(col='v.viewed_timestamp')}, count(*) FROM post_views v "
                "JOIN post_categories pc ON pc.post_id = v.post_id "
                "WHERE v.viewed_timestamp >= :lo AND v.viewed_timestamp < :hi GROUP BY 1, 2"), {"lo": lo, "hi": hi}).all():
            cat_views[cid][d] = n

    t = {k: _totals(v, days) for k, v in (("users", users), ("posts", posts), ("likes", likes), ("bookmarks", bookmarks),
                                          ("comments", comments), ("follows", follows), ("open_reports", reports))}
    platform = [{"stat_date": d, **{k: t[k][d] for k in t},
                 "new_users": users.get(d, 0), "new_posts": posts.get(d, 0),
                 "views": views.get(d, (0, 0))[0], "viewers": views.get(d, (0, 0))[1]} for d in days]
    category = []
    for cid, label in cats:
        totals = _totals(cat_posts[cid], days)
        category += [{"stat_date": d, "category_id": cid, "label": label, "posts": totals[d],
                      "new_posts": cat_posts[cid].get(d, 0), "views": cat_views[cid].get(d, 0)} for d in days]
    return platform, category


def _upsert(db, model, rows: list[dict], keys: list[str]) -> None:
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        stmt = pg_insert(model.__table__).values(chunk)
        cols = {c: stmt.excluded[c] for c in chunk[0] if c not in keys}
        cols["captured_timestamp"] = datetime.now(timezone.utc)
        db.execute(stmt.on_conflict_do_update(index_elements=keys, set_=cols))


def _days_to_compute(db, today: date) -> tuple[list[date], bool]:
    """Today, yesterday if it was captured before midnight, and every missing day since the first
    data (at most BACKFILL_DAYS back). Returns (days, backfilling)."""
    since = today - timedelta(days=BACKFILL_DAYS)
    have = {d: ts for d, ts in db.execute(text(
        "SELECT stat_date, captured_timestamp FROM daily_platform_stats WHERE stat_date >= :s"), {"s": since}).all()}
    with UsersSessionLocal() as udb, PostsSessionLocal() as pdb:
        first = [x for x in (udb.execute(text("SELECT min(created_timestamp) FROM platform_users")).scalar(),
                             pdb.execute(text("SELECT min(created_timestamp) FROM posts")).scalar()) if x]
    start = max(since, min(first).astimezone(timezone.utc).date()) if first else today
    missing = [start + timedelta(days=i) for i in range((today - start).days) if start + timedelta(days=i) not in have]
    days = set(missing) | {today}
    y = today - timedelta(days=1)
    if y in have and (have[y] is None or have[y] < _start(today)):
        days.add(y)
    return sorted(days), bool(missing)


def run_snapshot(trigger: str = "manual", today: date | None = None) -> dict:
    """Capture today (and fill any gaps). Safe to run any time, from any worker."""
    today = today or datetime.now(timezone.utc).date()
    started = datetime.now(timezone.utc)
    with AnalyticsSessionLocal() as db:
        run = SnapshotRun(run_id=uuid.uuid4(), trigger=trigger, stat_date=today, status="running")
        db.add(run); db.commit()
        try:
            days, backfill = _days_to_compute(db, today)
            platform, category = compute(days)
            _upsert(db, DailyPlatformStats, platform, ["stat_date"])
            if category:
                _upsert(db, DailyCategoryStats, category, ["stat_date", "category_id"])
            db.execute(text("DELETE FROM snapshot_runs WHERE started_timestamp < :t"),
                       {"t": started - timedelta(days=RUN_LOG_DAYS)})
            run.status = "success"
            run.trigger = "backfill" if backfill and trigger == "schedule" else trigger
            run.rows_written = len(platform) + len(category)
        except Exception as exc:
            db.rollback()
            run = db.get(SnapshotRun, run.run_id) or run
            run.status, run.error_message = "failed", f"{type(exc).__name__}: {exc}"[:2000]
            log.exception("analytics snapshot failed")
        run.finished_timestamp = datetime.now(timezone.utc)
        run.duration_seconds = round((run.finished_timestamp - started).total_seconds(), 3)
        db.commit()
        today_row = next((p for p in (platform if run.status == "success" else []) if p["stat_date"] == today), None)
        return {"run_id": str(run.run_id), "status": run.status, "trigger": run.trigger,
                "stat_date": today.isoformat(), "days_written": len(platform) if run.status == "success" else 0,
                "rows_written": run.rows_written, "duration_seconds": float(run.duration_seconds),
                "error_message": run.error_message,
                "today": {k: v for k, v in today_row.items() if k != "stat_date"} if today_row else None}


def _scheduled_tick() -> None:
    """One automatic run, unless another worker holds the lock or a run finished in the last
    0.9 × interval (several API workers each have a scheduler)."""
    with analytics_engine.connect() as conn:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_ID}).scalar():
            return
        try:
            recent = conn.execute(text(
                "SELECT 1 FROM snapshot_runs WHERE status = 'success' AND started_timestamp > :t LIMIT 1"),
                {"t": datetime.now(timezone.utc) - timedelta(seconds=SNAPSHOT_EVERY_SECONDS * 0.9)}).first()
            today = datetime.now(timezone.utc).date()
            fresh_day = conn.execute(text("SELECT 1 FROM daily_platform_stats WHERE stat_date = :d"), {"d": today}).first()
            if recent is None or fresh_day is None:
                run_snapshot("schedule")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_ID})
            conn.commit()


def start_scheduler() -> None:
    """Background thread in each API process: a tick shortly after start-up, then every interval."""
    if SNAPSHOT_EVERY_SECONDS <= 0:
        return

    def loop() -> None:
        time.sleep(20)
        while True:
            try:
                _scheduled_tick()
            except Exception as e:      # keep the thread alive; the next tick retries
                log.warning("analytics snapshot tick failed: %s", e)
            time.sleep(min(SNAPSHOT_EVERY_SECONDS, 3600))

    threading.Thread(target=loop, name="analytics-snapshot", daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run_snapshot("manual"))
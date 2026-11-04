"""Scheduled work. Called every few minutes, from anywhere:
- in the cloud, an Azure Function timer calls POST /internal/cron/hourly (every 5 minutes)
- locally, the in-process APScheduler (scheduler.py) calls run_hourly()

Each user's plan goes out at the morning hour *they* chose (6-9, their timezone). People whose
days vary get a check-in instead ("how does today look?") and their day is planned once they
answer. The risk check runs at RISK_CHECK_HOUR; on Sundays there's a weekly ideas email. last_*_run makes this idempotent: running it twice,
or every 5 minutes, never double-sends.

Sending at scale (thousands of people at 7:00):
- emails go out in parallel batches (EMAIL_CONCURRENCY)
- the job never exceeds the provider's limits (EMAIL_RATE_PER_MINUTE / _PER_HOUR),
  counted across runs from the email_sends log
- if the provider still says "slow down" (HTTP 429), the run stops and those people
  are picked up by the next run a few minutes later
- one run stops after EMAIL_RUN_SECONDS so it always finishes inside App Service's
  230 s request limit; the next run carries on
"""
import datetime as dt
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from . import models, planner, mailing, config
from .auth import user_now, delete_user_data
from .emailer import send_email

log = logging.getLogger("planner.jobs")

# kept as module attributes so tests (and you) can tweak them
RISK_CHECK_HOUR = config.RISK_CHECK_HOUR
GUEST_TTL_HOURS = config.GUEST_TTL_HOURS


def cleanup_guests(db: Session) -> int:
    cutoff = models.utcnow() - dt.timedelta(hours=GUEST_TTL_HOURS)
    stale = db.query(models.User.id).filter(models.User.is_guest.is_(True),
                                            models.User.created_at < cutoff).all()
    for (uid,) in stale:
        delete_user_data(db, uid)
    db.commit()
    return len(stale)


class SendBudget:
    """Sliding-window limiter seeded from the send log, so limits hold across runs."""

    def __init__(self, db: Session, per_minute: int, per_hour: int):
        self.per_minute, self.per_hour = per_minute, per_hour
        now = models.utcnow()
        rows = db.query(models.EmailSend.sent_at).filter(
            models.EmailSend.sent_at >= now - dt.timedelta(hours=1),
            models.EmailSend.status != "throttled").all()
        # store as monotonic timestamps
        mono, wall = time.monotonic(), now
        self.stamps = deque(sorted(mono - (wall - r[0]).total_seconds() for r in rows))

    def _trim(self):
        cutoff = time.monotonic() - 3600
        while self.stamps and self.stamps[0] < cutoff:
            self.stamps.popleft()

    def available(self) -> int:
        self._trim()
        now = time.monotonic()
        last_minute = sum(1 for t in self.stamps if t > now - 60)
        return max(0, min(self.per_minute - last_minute, self.per_hour - len(self.stamps)))

    def hour_left(self) -> int:
        self._trim()
        return self.per_hour - len(self.stamps)

    def wait_seconds(self) -> float:
        """Seconds until the per-minute window frees a slot."""
        now = time.monotonic()
        in_minute = [t for t in self.stamps if t > now - 60]
        return (in_minute[0] + 60 - now) if in_minute else 0

    def spend(self, n: int):
        now = time.monotonic()
        self.stamps.extend([now] * n)


def _due_work(db: Session, stats: dict) -> list[tuple]:
    """Do what needs no email right away; return (user, kind, today) for emails to send.
    kinds: plan (morning plan), checkin (varying days: ask first), risk, digest (Sundays)."""
    from .routines import day_status
    due = []
    for user in db.query(models.User).filter(models.User.is_guest.is_(False)) \
                  .order_by(models.User.id).all():
        try:
            now = user_now(user)
            today = now.date()
            morning = user.email_morning_hour or config.MORNING_HOUR
            emailable = mailing.can_receive(user)
            if now.hour >= morning and user.last_morning_run != today:
                varies = (user.day_pattern or "steady") == "varies" and day_status(db, user.id, today) is None
                if varies:
                    # don't guess: ask how the day looks, plan once they answer
                    if emailable and user.email_morning_plan:
                        due.append((user, "checkin", today))
                    else:
                        user.last_morning_run = today
                elif emailable and user.email_morning_plan:
                    due.append((user, "plan", today))
                else:   # nothing to email: just plan their day
                    planner.generate_daily_plan(db, user.id, today)
                    user.last_morning_run = today
                    stats["morning_plans"] += 1
            if now.hour >= RISK_CHECK_HOUR and user.last_risk_run != today:
                if emailable and user.email_risk_alerts and mailing.build_risk_alert(db, user):
                    due.append((user, "risk", today))
                else:
                    user.last_risk_run = today
                    stats["risk_checks"] += 1
            if (today.weekday() == 6 and now.hour >= morning and user.last_digest_date != today
                    and emailable and user.email_weekly_digest):
                due.append((user, "digest", today))
            db.commit()
        except Exception:  # one bad user must not stop the others
            db.rollback()
            log.exception("scheduled job failed for user %s", user.id)
    return due


def _build(db: Session, user: models.User, kind: str, today):
    if kind == "plan":
        planner.generate_daily_plan(db, user.id, today)
        return mailing.build_daily_plan(db, user)
    if kind == "checkin":
        return mailing.build_checkin(db, user)
    if kind == "digest":
        return mailing.build_digest(db, user)
    return mailing.build_risk_alert(db, user)


def _mark_done(user: models.User, kind: str, today, stats: dict):
    if kind in ("plan", "checkin"):
        user.last_morning_run = today
        stats["morning_plans" if kind == "plan" else "checkins"] += 1
    elif kind == "risk":
        user.last_risk_run = today
        stats["risk_checks"] += 1
    else:
        user.last_digest_date = today
        stats["digests"] += 1


def run_hourly(db: Session) -> dict:
    started = time.monotonic()
    stats = {"morning_plans": 0, "checkins": 0, "risk_checks": 0, "digests": 0, "emails_sent": 0,
             "emails_failed": 0, "deferred": 0, "throttled": False, "guests_deleted": 0}
    due = _due_work(db, stats)
    budget = SendBudget(db, config.EMAIL_RATE_PER_MINUTE, config.EMAIL_RATE_PER_HOUR)

    with ThreadPoolExecutor(max_workers=max(1, config.EMAIL_CONCURRENCY)) as pool:
        while due and not stats["throttled"]:
            left = config.EMAIL_RUN_SECONDS - (time.monotonic() - started)
            if left <= 1 or budget.hour_left() <= 0:
                break
            n = budget.available()
            if n == 0:
                wait = budget.wait_seconds()
                if wait >= left:
                    break
                time.sleep(max(wait, 0.2))
                continue

            batch, due = due[:n], due[n:]
            messages = []
            for user, kind, today in batch:     # DB work stays on this thread
                try:
                    m = _build(db, user, kind, today)
                    if m:
                        messages.append((user, kind, today, m))
                    else:                         # nothing worth sending (e.g. no ideas this week)
                        _mark_done(user, kind, today, stats)
                except Exception:
                    db.rollback()
                    log.exception("building %s email failed for user %s", kind, user.id)
            budget.spend(len(messages))
            futures = [(u, k, t, pool.submit(send_email, u.email, *m, mailing.unsubscribe_url(u)))
                       for u, k, t, m in messages]
            for user, kind, today, fut in futures:
                status = fut.result()
                mailing.record(db, user.id, kind, status)
                if status == "throttled":
                    stats["throttled"] = True
                    due.append((user, kind, today))    # try again next run
                    continue
                stats["emails_sent" if status == "sent" else "emails_failed"] += 1
                _mark_done(user, kind, today, stats)
            db.commit()

    stats["deferred"] = len(due)
    if due:
        log.info("%d emails deferred to the next run", len(due))
    stats["guests_deleted"] = cleanup_guests(db)
    return stats

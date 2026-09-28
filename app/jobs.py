"""Background jobs, written to run once an hour from anywhere:
- in the cloud, an Azure Function timer calls POST /internal/cron/hourly
- locally, the in-process APScheduler (scheduler.py) calls run_hourly()

Each user gets their morning plan at MORNING_HOUR and a risk check at
RISK_CHECK_HOUR in *their own* timezone. last_*_run makes the jobs idempotent,
so an extra or retried call never double-sends.
"""
import datetime as dt
import logging

from sqlalchemy.orm import Session

from . import models, planner, notifications
from .auth import user_now, delete_user_data
from .config import MORNING_HOUR, RISK_CHECK_HOUR, GUEST_TTL_HOURS

log = logging.getLogger("planner.jobs")


def morning_plan_for(db: Session, user: models.User, today: dt.date):
    items = planner.generate_daily_plan(db, user.id, today)
    if user.ntfy_topic:
        first = None
        if items:
            t = db.get(models.Task, items[0].task_id)
            first = t.title if t else None
        notifications.notify_daily_plan(user.ntfy_topic, len(items), first)


def risk_check_for(db: Session, user: models.User, today: dt.date):
    if not user.ntfy_topic:
        return
    for goal in db.query(models.Goal).filter_by(user_id=user.id, status="active").all():
        risk = planner.goal_risk(db, goal, today)
        if risk and risk["at_risk"]:
            notifications.notify_risk(user.ntfy_topic, goal.title, risk["detail"])


def cleanup_guests(db: Session) -> int:
    cutoff = models.utcnow() - dt.timedelta(hours=GUEST_TTL_HOURS)
    stale = db.query(models.User.id).filter(models.User.is_guest.is_(True),
                                            models.User.created_at < cutoff).all()
    for (uid,) in stale:
        delete_user_data(db, uid)
    db.commit()
    return len(stale)


def run_hourly(db: Session) -> dict:
    stats = {"morning_plans": 0, "risk_checks": 0, "guests_deleted": 0}
    for user in db.query(models.User).filter(models.User.is_guest.is_(False)).all():
        try:
            now = user_now(user)
            today = now.date()
            if now.hour >= MORNING_HOUR and user.last_morning_run != today:
                user.last_morning_run = today
                db.commit()
                morning_plan_for(db, user, today)
                stats["morning_plans"] += 1
            if now.hour >= RISK_CHECK_HOUR and user.last_risk_run != today:
                user.last_risk_run = today
                db.commit()
                risk_check_for(db, user, today)
                stats["risk_checks"] += 1
        except Exception:  # one bad user must not stop the others
            db.rollback()
            log.exception("hourly job failed for user %s", user.id)
    stats["guests_deleted"] = cleanup_guests(db)
    return stats

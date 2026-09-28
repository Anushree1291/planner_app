import datetime as dt
from apscheduler.schedulers.background import BackgroundScheduler
from .database import SessionLocal
from . import models, planner, notifications

_scheduler = None


def _job_generate_and_notify_daily_plan():
    """Runs every morning: generates today's plan and pushes a notification
    to phone + laptop via ntfy. This is Feature #5/#25 (proactive planning)."""
    db = SessionLocal()
    try:
        users = db.query(models.User).all()
        for user in users:
            today = dt.date.today()
            items = planner.generate_daily_plan(db, user.id, today)
            if user.ntfy_topic:
                first = None
                if items:
                    t = db.query(models.Task).get(items[0].task_id)
                    first = t.title if t else None
                notifications.notify_daily_plan(user.ntfy_topic, len(items), first)
    finally:
        db.close()


def _job_check_goal_risk():
    """Runs daily: flags goals whose remaining workload no longer fits the
    remaining time before deadline (Feature #16: goal risk detection)."""
    db = SessionLocal()
    try:
        today = dt.date.today()
        for goal in db.query(models.Goal).filter_by(status="active").all():
            if not goal.deadline:
                continue
            days_left = (goal.deadline - today).days
            if days_left <= 0:
                continue
            pending = db.query(models.Task).filter_by(goal_id=goal.id, status="pending").all()
            remaining_minutes = sum(t.estimated_minutes for t in pending)
            required_daily_minutes = remaining_minutes / max(days_left, 1)
            user = db.query(models.User).get(goal.user_id)
            if user and user.ntfy_topic and required_daily_minutes > 180:  # >3h/day needed
                notifications.notify_risk(
                    user.ntfy_topic, goal.title,
                    f"At this pace you need ~{round(required_daily_minutes/60,1)}h/day "
                    f"for the next {days_left} days to hit the deadline."
                )
    finally:
        db.close()


def start_scheduler():
    global _scheduler
    if _scheduler:
        return
    _scheduler = BackgroundScheduler()
    # 7:00 AM daily plan + notification; adjust to your wake time
    _scheduler.add_job(_job_generate_and_notify_daily_plan, "cron", hour=7, minute=0)
    # 8:00 PM daily risk check
    _scheduler.add_job(_job_check_goal_risk, "cron", hour=20, minute=0)
    _scheduler.start()

"""Local-development scheduler. In the cloud this is switched off
(ENABLE_LOCAL_SCHEDULER=false) and an Azure Function timer calls
POST /internal/cron/hourly instead, because free-tier web apps go to sleep."""
from apscheduler.schedulers.background import BackgroundScheduler

from .database import SessionLocal
from . import jobs

_scheduler = None


def _hourly():
    db = SessionLocal()
    try:
        jobs.run_hourly(db)
    finally:
        db.close()


def start_scheduler():
    global _scheduler
    if _scheduler:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_hourly, "cron", minute=2)
    _scheduler.start()


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None

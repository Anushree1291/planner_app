import datetime as dt
import hmac
import threading
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import models, schemas, planner, ai_decompose, jobs, auth, mailing, routines, goal_chat
from .auth import current_user, user_now, user_today
from .config import (SECRET_KEY, CRON_SECRET, IS_PROD, ENABLE_LOCAL_SCHEDULER,
                     MAX_GOALS_PER_USER, MAX_TASKS_PER_USER, MAX_BLOCKS_PER_USER)
from .database import Base, engine, get_db, add_missing_columns

logging.basicConfig(level=logging.INFO)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    add_missing_columns()
    if ENABLE_LOCAL_SCHEDULER:
        from .scheduler import start_scheduler, stop_scheduler
        start_scheduler()
        yield
        stop_scheduler()
    else:
        yield


app = FastAPI(title="AI Execution Planner", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="planner_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=IS_PROD)
app.include_router(auth.router)
app.include_router(mailing.router)
app.include_router(mailing.day_router)
app.include_router(routines.router)
app.include_router(goal_chat.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")  # email preview iframe
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if IS_PROD:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


# ---------- helpers ----------

def _now_minutes(user: models.User) -> int:
    """Minutes since local midnight, rounded up to the next 5 min."""
    now = user_now(user)
    m = now.hour * 60 + now.minute
    return m + (-m % 5)


def _not_before(user: models.User, date: dt.date) -> int | None:
    return _now_minutes(user) if date == user_today(user) else None


def _own(db: Session, model, obj_id: int, user: models.User):
    """Fetch a row only if it belongs to the current user (404 otherwise, never 403,
    so ids of other users' data aren't confirmed to exist)."""
    obj = db.get(model, obj_id)
    if not obj or obj.user_id != user.id:
        raise HTTPException(404, "not found")
    return obj


def _check_quota(db: Session, model, user: models.User, limit: int, adding: int = 1):
    if db.query(model).filter_by(user_id=user.id).count() + adding > limit:
        raise HTTPException(400, f"Limit reached for this demo ({limit}). Delete some first.")


# ---------- health & scheduled jobs ----------

@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}


_cron_lock = threading.Lock()


@app.post("/internal/cron/hourly", include_in_schema=False)
def cron_hourly(x_cron_secret: str = Header(default=""), db: Session = Depends(get_db)):
    """Called every 5 minutes by the Azure Function timer (see azure_function/).
    The lock stops two runs overlapping, which could otherwise email someone twice."""
    if not CRON_SECRET or not hmac.compare_digest(x_cron_secret, CRON_SECRET):
        raise HTTPException(401, "bad cron secret")
    if not _cron_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        return jobs.run_hourly(db)
    finally:
        _cron_lock.release()


# ---------- Goals ----------

@app.post("/goals")
def create_goal(payload: schemas.GoalCreate, user: models.User = Depends(current_user),
                db: Session = Depends(get_db)):
    _check_quota(db, models.Goal, user, MAX_GOALS_PER_USER)
    goal = models.Goal(user_id=user.id, title=payload.title.strip(), deadline=payload.deadline)
    db.add(goal)
    db.commit()
    db.refresh(goal)

    result = {"id": goal.id, "title": goal.title, "tasks_created": 0}
    if payload.auto_decompose:
        tasks, source, note = ai_decompose.decompose_goal(
            db, user, payload.title, str(payload.deadline) if payload.deadline else None)
        room = MAX_TASKS_PER_USER - db.query(models.Task).filter_by(user_id=user.id).count()
        for t in tasks[:max(room, 0)]:
            db.add(models.Task(goal_id=goal.id, user_id=user.id, deadline=payload.deadline, **t))
        db.commit()
        result.update(tasks_created=min(len(tasks), max(room, 0)), source=source, note=note)
    return result


@app.get("/goals")
def list_goals(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    today = user_today(user)
    goals = db.query(models.Goal).filter_by(user_id=user.id).order_by(models.Goal.id).all()
    return [goal_json(db, g, today) for g in goals]


def goal_json(db: Session, g: models.Goal, today) -> dict:
    import json, re
    try:
        d = json.loads(g.details or "{}")
    except json.JSONDecodeError:
        d = {}
    out = {"id": g.id, "title": g.title, "deadline": g.deadline, "status": g.status or "active",
           "kind": g.kind or "general", "risk": planner.goal_risk(db, g, today) if g.status != "planning" else None,
           **planner.goal_progress(db, g.id)}
    book = (d.get("facts") or {}).get("book")
    if g.kind == "book" and book:
        read = 0
        for (title,) in db.query(models.Task.title).filter_by(goal_id=g.id, status="done").all():
            m = re.search(r"pages \d+–(\d+)", title)
            if m:
                read = max(read, int(m.group(1)))
        out["book"] = {"title": book.get("title"), "author": book.get("author"), "pages": book.get("pages"),
                       "read": read, "url": book.get("url")}
    if d.get("plan"):
        out["plan_summary"] = d["plan"].get("summary")
    habits = db.query(models.Recurring).filter_by(goal_id=g.id).all()
    if habits:
        out["habits"] = [{"title": h.title, "minutes": h.minutes,
                          "days": [int(x) for x in h.weekdays.split(",") if x]} for h in habits]
    return out


@app.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, user: models.User = Depends(current_user),
                db: Session = Depends(get_db)):
    goal = _own(db, models.Goal, goal_id, user)
    db.query(models.GoalMessage).filter_by(goal_id=goal.id).delete(synchronize_session=False)
    db.query(models.Recurring).filter_by(goal_id=goal.id).delete(synchronize_session=False)
    task_ids = [tid for (tid,) in db.query(models.Task.id).filter_by(goal_id=goal.id).all()]
    if task_ids:
        db.query(models.PlanItem).filter(models.PlanItem.task_id.in_(task_ids)) \
          .delete(synchronize_session=False)
        db.query(models.Task).filter(models.Task.depends_on_id.in_(task_ids)) \
          .update({models.Task.depends_on_id: None}, synchronize_session=False)
        db.query(models.Task).filter(models.Task.id.in_(task_ids)).delete(synchronize_session=False)
    db.delete(goal)
    db.commit()
    return {"ok": True}


# ---------- Tasks ----------

@app.post("/tasks")
def create_task(payload: schemas.TaskCreate, user: models.User = Depends(current_user),
                db: Session = Depends(get_db)):
    _check_quota(db, models.Task, user, MAX_TASKS_PER_USER)
    if payload.goal_id is not None:
        _own(db, models.Goal, payload.goal_id, user)
    if payload.depends_on_id is not None:
        _own(db, models.Task, payload.depends_on_id, user)
    task = models.Task(user_id=user.id, **payload.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id}


@app.get("/tasks")
def list_tasks(status: str | None = None, goal_id: int | None = None,
               user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(models.Task).filter_by(user_id=user.id)
    if status:
        q = q.filter_by(status=status)
    if goal_id:
        q = q.filter_by(goal_id=goal_id)
    return [
        {"id": t.id, "goal_id": t.goal_id, "title": t.title, "status": t.status,
         "priority": t.priority, "estimated_minutes": t.estimated_minutes,
         "actual_minutes": t.actual_minutes, "energy_required": t.energy_required,
         "deadline": t.deadline, "postponed_count": t.postponed_count}
        for t in q.order_by(models.Task.id).all()
    ]


@app.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: schemas.TaskUpdate,
                user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    task = _own(db, models.Task, task_id, user)

    if payload.actual_minutes is not None:
        task.actual_minutes = payload.actual_minutes
        planner.update_overrun_profile(db, user.id, task)

    if payload.status:
        today = user_today(user)
        if payload.status in ("skipped", "postponed"):
            planner.replan_after_miss(db, user.id, task, today, _not_before(user, today))
            return {"ok": True, "replanned": True}
        task.status = payload.status
        if payload.status == "done":
            db.query(models.PlanItem).filter_by(user_id=user.id, task_id=task.id,
                                                status="scheduled") \
              .update({models.PlanItem.status: "done"}, synchronize_session=False)

    db.commit()
    return {"ok": True}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, user: models.User = Depends(current_user),
                db: Session = Depends(get_db)):
    task = _own(db, models.Task, task_id, user)
    db.query(models.PlanItem).filter_by(task_id=task.id).delete(synchronize_session=False)
    db.query(models.Task).filter_by(depends_on_id=task.id) \
      .update({models.Task.depends_on_id: None}, synchronize_session=False)
    db.delete(task)
    db.commit()
    return {"ok": True}


# ---------- Calendar (manual busy blocks, stand-in for Google/Outlook sync) ----------

@app.post("/calendar")
def add_calendar_block(payload: schemas.CalendarBlockCreate,
                       user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    _check_quota(db, models.CalendarBlock, user, MAX_BLOCKS_PER_USER)
    block = models.CalendarBlock(user_id=user.id, **payload.model_dump())
    db.add(block)
    db.commit()
    return {"id": block.id}


@app.get("/calendar")
def list_calendar(date: dt.date | None = None, user: models.User = Depends(current_user),
                  db: Session = Depends(get_db)):
    """Busy time on a date: one-off blocks plus routines (office, gym...) that apply."""
    date = date or user_today(user)
    return [{**b, "date": date} for b in routines.busy_blocks(db, user.id, date)]


@app.delete("/calendar/{block_id}")
def delete_calendar_block(block_id: int, user: models.User = Depends(current_user),
                          db: Session = Depends(get_db)):
    db.delete(_own(db, models.CalendarBlock, block_id, user))
    db.commit()
    return {"ok": True}


# ---------- Planning ----------

@app.get("/capacity")
def capacity(date: dt.date | None = None, user: models.User = Depends(current_user),
             db: Session = Depends(get_db)):
    date = date or user_today(user)
    return {"date": date,
            "free_minutes": planner.calculate_capacity(db, user.id, date, _not_before(user, date))}


@app.post("/plan/generate")
def generate_plan(date: dt.date | None = None, user: models.User = Depends(current_user),
                  db: Session = Depends(get_db)):
    date = date or user_today(user)
    items = planner.generate_daily_plan(db, user.id, date, _not_before(user, date))
    return {"date": date, "scheduled": len(items)}


@app.get("/plan")
def get_plan(date: dt.date | None = None, user: models.User = Depends(current_user),
             db: Session = Depends(get_db)):
    date = date or user_today(user)
    items = db.query(models.PlanItem).filter_by(user_id=user.id, date=date) \
              .order_by(models.PlanItem.start_time).all()
    out = []
    for i in items:
        task = db.get(models.Task, i.task_id)
        out.append({"start": i.start_time, "end": i.end_time, "task": task.title if task else "?",
                    "status": i.status, "task_id": i.task_id,
                    "task_status": task.status if task else None,
                    "energy": task.energy_required if task else None})
    return out


@app.post("/what-now")
def what_now(payload: schemas.WhatNowRequest, user: models.User = Depends(current_user),
             db: Session = Depends(get_db)):
    task = planner.what_should_i_do_now(db, user.id, payload.free_minutes, payload.energy,
                                        user_today(user))
    if not task:
        return {"task": None, "message": "Nothing pending — enjoy the gap."}
    return {"task": task.title, "task_id": task.id, "estimated_minutes": task.estimated_minutes,
            "priority": task.priority, "energy": task.energy_required}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

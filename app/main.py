import datetime as dt
from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import models, schemas, planner, notifications, ai_decompose
from .database import Base, engine, get_db
from .scheduler import start_scheduler

Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Execution Planner")

DEFAULT_USER_ID = 1  # single-user MVP; extend with real auth for multi-user


def ensure_default_user(db: Session):
    user = db.query(models.User).get(DEFAULT_USER_ID)
    if not user:
        user = models.User(id=DEFAULT_USER_ID, name="me")
        db.add(user)
        db.add(models.UserProfile(user_id=DEFAULT_USER_ID))
        db.commit()
    return user


@app.on_event("startup")
def startup():
    db = next(get_db())
    ensure_default_user(db)
    start_scheduler()


# ---------- Setup ----------

@app.post("/setup/notifications")
def setup_notifications(topic: str, db: Session = Depends(get_db)):
    """Set your ntfy.sh topic once. See README for how to pick/subscribe to one."""
    user = ensure_default_user(db)
    user.ntfy_topic = topic
    db.commit()
    notifications.send_notification(topic, "Connected", "Your planner can now reach this device.")
    return {"ok": True}


# ---------- Goals ----------

@app.post("/goals")
def create_goal(payload: schemas.GoalCreate, db: Session = Depends(get_db)):
    ensure_default_user(db)
    goal = models.Goal(user_id=DEFAULT_USER_ID, title=payload.title, deadline=payload.deadline)
    db.add(goal)
    db.commit()
    db.refresh(goal)

    if payload.auto_decompose:
        tasks = ai_decompose.decompose_goal(payload.title, str(payload.deadline) if payload.deadline else None)
        for t in tasks:
            db.add(models.Task(
                goal_id=goal.id, user_id=DEFAULT_USER_ID,
                title=t["title"], estimated_minutes=t.get("estimated_minutes", 30),
                priority=t.get("priority", 3), energy_required=t.get("energy_required", "medium"),
            ))
        db.commit()

    return {"id": goal.id, "title": goal.title}


@app.get("/goals")
def list_goals(db: Session = Depends(get_db)):
    goals = db.query(models.Goal).filter_by(user_id=DEFAULT_USER_ID).all()
    return [
        {"id": g.id, "title": g.title, "deadline": g.deadline,
         **planner.goal_progress(db, g.id)}
        for g in goals
    ]


# ---------- Tasks ----------

@app.post("/tasks")
def create_task(payload: schemas.TaskCreate, db: Session = Depends(get_db)):
    ensure_default_user(db)
    task = models.Task(user_id=DEFAULT_USER_ID, **payload.dict())
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id}


@app.get("/tasks")
def list_tasks(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Task).filter_by(user_id=DEFAULT_USER_ID)
    if status:
        q = q.filter_by(status=status)
    return [
        {"id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
         "estimated_minutes": t.estimated_minutes, "energy_required": t.energy_required,
         "deadline": t.deadline, "postponed_count": t.postponed_count}
        for t in q.all()
    ]


@app.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: schemas.TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(models.Task).get(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    if payload.actual_minutes is not None:
        task.actual_minutes = payload.actual_minutes
        planner.update_overrun_profile(db, DEFAULT_USER_ID, task)

    if payload.status:
        task.status = payload.status
        if payload.status in ("skipped", "postponed"):
            db.commit()
            planner.replan_after_miss(db, DEFAULT_USER_ID, task_id)
            return {"ok": True, "replanned": True}

    db.commit()
    return {"ok": True}


# ---------- Calendar (manual busy blocks, stand-in for Google/Outlook sync) ----------

@app.post("/calendar")
def add_calendar_block(payload: schemas.CalendarBlockCreate, db: Session = Depends(get_db)):
    ensure_default_user(db)
    block = models.CalendarBlock(user_id=DEFAULT_USER_ID, **payload.dict())
    db.add(block)
    db.commit()
    return {"id": block.id}


# ---------- Planning ----------

@app.get("/capacity")
def capacity(date: dt.date = dt.date.today(), db: Session = Depends(get_db)):
    return {"date": date, "free_minutes": planner.calculate_capacity(db, DEFAULT_USER_ID, date)}


@app.post("/plan/generate")
def generate_plan(date: dt.date = dt.date.today(), db: Session = Depends(get_db)):
    items = planner.generate_daily_plan(db, DEFAULT_USER_ID, date)
    user = db.query(models.User).get(DEFAULT_USER_ID)
    if user and user.ntfy_topic:
        first = db.query(models.Task).get(items[0].task_id).title if items else None
        notifications.notify_daily_plan(user.ntfy_topic, len(items), first)
    return {"scheduled": len(items)}


@app.get("/plan")
def get_plan(date: dt.date = dt.date.today(), db: Session = Depends(get_db)):
    items = db.query(models.PlanItem).filter_by(user_id=DEFAULT_USER_ID, date=date).order_by(models.PlanItem.start_time).all()
    out = []
    for i in items:
        task = db.query(models.Task).get(i.task_id)
        out.append({"start": i.start_time, "end": i.end_time, "task": task.title if task else "?",
                     "status": i.status, "task_id": i.task_id})
    return out


@app.post("/what-now")
def what_now(payload: schemas.WhatNowRequest, db: Session = Depends(get_db)):
    task = planner.what_should_i_do_now(db, DEFAULT_USER_ID, payload.free_minutes, payload.energy)
    if not task:
        return {"task": None, "message": "Nothing pending — enjoy the gap."}
    user = db.query(models.User).get(DEFAULT_USER_ID)
    if user and user.ntfy_topic:
        notifications.notify_what_now(user.ntfy_topic, task.title, task.estimated_minutes)
    return {"task": task.title, "task_id": task.id, "estimated_minutes": task.estimated_minutes,
            "priority": task.priority}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")

import datetime as dt
from typing import List, Tuple
from sqlalchemy.orm import Session
from . import models

ENERGY_WINDOWS = {
    # hour ranges considered to have this natural energy level, used when
    # the user hasn't set a custom profile / as a sensible default
    "high": [(9, 12)],
    "medium": [(12, 17)],
    "low": [(17, 22)],
}
DAY_START, DAY_END = "07:00", "23:00"


def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _to_hhmm(minutes: int) -> str:
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def free_slots_for_day(db: Session, user_id: int, date: dt.date,
                       day_start=DAY_START, day_end=DAY_END,
                       not_before: int | None = None) -> List[Tuple[int, int]]:
    """Return free (start_min, end_min) windows for the day after subtracting
    calendar blocks. `not_before` (minutes since midnight) drops time that has
    already passed when planning for today. Stands in for real calendar sync (Phase 4)."""
    blocks = db.query(models.CalendarBlock).filter_by(user_id=user_id, date=date).all()
    busy = sorted([(_to_minutes(b.start_time), _to_minutes(b.end_time)) for b in blocks])

    start, end = _to_minutes(day_start), _to_minutes(day_end)
    if not_before is not None:
        start = max(start, not_before)
    free = []
    cursor = start
    for b_start, b_end in busy:
        if b_start > cursor:
            free.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(s, e) for s, e in free if e > s]


def calculate_capacity(db: Session, user_id: int, date: dt.date,
                       not_before: int | None = None) -> int:
    """Total free minutes available for real work on `date`."""
    return sum(e - s for s, e in free_slots_for_day(db, user_id, date, not_before=not_before))


def _energy_at(minute: int) -> str:
    hour = minute // 60
    for level, windows in ENERGY_WINDOWS.items():
        for s, e in windows:
            if s <= hour < e:
                return level
    return "low"


def _urgency_score(task: models.Task, today: dt.date) -> float:
    score = task.priority * 10
    if task.deadline:
        days_left = max((task.deadline - today).days, 0)
        score += max(30 - days_left, 0) * 2  # closer deadline => higher urgency
    elif task.goal is not None and task.goal.deadline:
        days_left = max((task.goal.deadline - today).days, 0)
        score += max(30 - days_left, 0)      # inherit (half) urgency from the goal
    score += task.postponed_count * 15  # things you keep dodging float to the top
    return score


def _pending_ready_tasks(db: Session, user_id: int) -> list[models.Task]:
    tasks = db.query(models.Task).filter_by(user_id=user_id, status="pending").all()
    done_ids = {tid for (tid,) in db.query(models.Task.id)
                .filter_by(user_id=user_id, status="done").all()}
    all_ids = {tid for (tid,) in db.query(models.Task.id).filter_by(user_id=user_id).all()}
    # a dependency is met if it's done or no longer exists
    return [t for t in tasks
            if not t.depends_on_id or t.depends_on_id in done_ids or t.depends_on_id not in all_ids]


def generate_daily_plan(db: Session, user_id: int, date: dt.date,
                        not_before: int | None = None, wipe_existing=True):
    """Greedy capacity-aware, energy-aware scheduler.
    This is the core replacement for 'dump 14 tasks into a 6-hour day'."""
    if wipe_existing:
        db.query(models.PlanItem).filter_by(user_id=user_id, date=date, status="scheduled") \
          .delete(synchronize_session=False)
        db.commit()

    profile = db.query(models.UserProfile).filter_by(user_id=user_id).first()
    overrun = profile.avg_overrun_factor if profile else 1.0
    min_chunk = profile.preferred_session_min if profile else 30

    slots = free_slots_for_day(db, user_id, date, not_before=not_before)
    tasks = _pending_ready_tasks(db, user_id)
    tasks.sort(key=lambda t: _urgency_score(t, date), reverse=True)
    # minutes still to schedule per task, tracked locally so regenerating a plan
    # never mutates the stored estimates
    remaining = {t.id: max(int(t.estimated_minutes * overrun), 5) for t in tasks}

    plan_items = []
    for slot_start, slot_end in slots:
        cursor = slot_start
        while cursor < slot_end and tasks:
            window_energy = _energy_at(cursor)
            # prefer a task matching this window's energy; else take the most urgent
            candidates = [t for t in tasks if t.energy_required == window_energy] or tasks
            task = candidates[0]

            need = remaining[task.id]
            available = slot_end - cursor
            if need <= available:
                chunk = need
            elif available >= min_chunk:
                chunk = available  # split: schedule what fits, carry the rest over
            else:
                break  # slot too small for meaningful work; move to next slot

            item = models.PlanItem(user_id=user_id, date=date, task_id=task.id,
                                   start_time=_to_hhmm(cursor), end_time=_to_hhmm(cursor + chunk))
            db.add(item)
            plan_items.append(item)
            cursor += chunk

            if chunk < need:
                remaining[task.id] = need - chunk
                break  # slot consumed
            tasks.remove(task)

            # short break after a full work session
            if profile and chunk >= profile.preferred_session_min:
                cursor += profile.break_min

    db.commit()
    return plan_items


def what_should_i_do_now(db: Session, user_id: int, free_minutes: int, energy: str,
                         today: dt.date):
    tasks = _pending_ready_tasks(db, user_id)
    # must fit in the time available
    fitting = [t for t in tasks if t.estimated_minutes <= free_minutes] or tasks
    # prefer matching energy, then urgency
    matching = [t for t in fitting if t.energy_required == energy] or fitting
    matching.sort(key=lambda t: _urgency_score(t, today), reverse=True)
    return matching[0] if matching else None


def replan_after_miss(db: Session, user_id: int, task: models.Task, today: dt.date,
                      not_before: int | None = None):
    """Called when a task is skipped/postponed. Bumps postponed_count so it
    surfaces higher in urgency next time, then regenerates today's + tomorrow's plan."""
    task.status = "pending"
    task.postponed_count += 1
    db.commit()
    generate_daily_plan(db, user_id, today, not_before=not_before)
    generate_daily_plan(db, user_id, today + dt.timedelta(days=1))


def update_overrun_profile(db: Session, user_id: int, task: models.Task):
    """Learn from actual vs estimated time (Feature #9: task difficulty prediction)."""
    if not task.actual_minutes or not task.estimated_minutes:
        return
    profile = db.query(models.UserProfile).filter_by(user_id=user_id).first()
    if not profile:
        return
    ratio = task.actual_minutes / max(task.estimated_minutes, 1)
    ratio = min(max(ratio, 0.25), 4.0)  # one wild entry shouldn't wreck future plans
    # exponential moving average so one outlier doesn't skew estimates too hard
    profile.avg_overrun_factor = round(0.8 * profile.avg_overrun_factor + 0.2 * ratio, 3)
    db.commit()


def goal_progress(db: Session, goal_id: int) -> dict:
    tasks = db.query(models.Task).filter_by(goal_id=goal_id).all()
    if not tasks:
        return {"progress_pct": 0, "total": 0, "done": 0}
    done = sum(1 for t in tasks if t.status == "done")
    return {"progress_pct": round(100 * done / len(tasks)), "total": len(tasks), "done": done}


def goal_risk(db: Session, goal: models.Goal, today: dt.date) -> dict | None:
    """Feature #16: flags goals whose remaining workload no longer fits before the deadline."""
    if not goal.deadline or goal.status != "active":
        return None
    days_left = (goal.deadline - today).days
    pending = db.query(models.Task).filter_by(goal_id=goal.id, status="pending").all()
    remaining = sum(t.estimated_minutes for t in pending)
    if not pending:
        return None
    if days_left <= 0:
        return {"at_risk": True, "detail": "The deadline has passed with tasks still open."}
    per_day = remaining / days_left
    if per_day > 180:  # >3h/day needed
        return {"at_risk": True,
                "detail": f"At this pace you need ~{round(per_day / 60, 1)}h/day "
                          f"for the next {days_left} days to hit the deadline."}
    return {"at_risk": False, "detail": f"~{round(per_day)} min/day needed."}

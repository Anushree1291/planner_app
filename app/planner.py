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


def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _to_hhmm(minutes: int) -> str:
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def free_slots_for_day(db: Session, user_id: int, date: dt.date,
                        day_start="07:00", day_end="23:00") -> List[Tuple[int, int]]:
    """Return free (start_min, end_min) windows for the day after subtracting
    calendar blocks. This stands in for real calendar sync (Phase 4)."""
    blocks = db.query(models.CalendarBlock).filter_by(user_id=user_id, date=date).all()
    busy = sorted([(_to_minutes(b.start_time), _to_minutes(b.end_time)) for b in blocks])

    start, end = _to_minutes(day_start), _to_minutes(day_end)
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


def calculate_capacity(db: Session, user_id: int, date: dt.date) -> int:
    """Total free minutes available for real work today."""
    return sum(e - s for s, e in free_slots_for_day(db, user_id, date))


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
    score += task.postponed_count * 15  # things you keep dodging float to the top
    return score


def _dependency_met(db: Session, task: models.Task) -> bool:
    if not task.depends_on_id:
        return True
    dep = db.query(models.Task).get(task.depends_on_id)
    return dep is None or dep.status == "done"


def generate_daily_plan(db: Session, user_id: int, date: dt.date, wipe_existing=True):
    """Greedy capacity-aware, energy-aware scheduler.
    This is the core replacement for 'dump 14 tasks into a 6-hour day'."""
    if wipe_existing:
        db.query(models.PlanItem).filter_by(user_id=user_id, date=date, status="scheduled").delete()
        db.commit()

    profile = db.query(models.UserProfile).filter_by(user_id=user_id).first()
    overrun = profile.avg_overrun_factor if profile else 1.0

    slots = free_slots_for_day(db, user_id, date)
    tasks = (
        db.query(models.Task)
        .filter_by(user_id=user_id, status="pending")
        .all()
    )
    tasks = [t for t in tasks if _dependency_met(db, t)]
    tasks.sort(key=lambda t: _urgency_score(t, date), reverse=True)

    plan_items = []
    for slot_start, slot_end in slots:
        cursor = slot_start
        while cursor < slot_end and tasks:
            window_energy = _energy_at(cursor)
            # prefer a task matching this window's energy; else take the most urgent that fits
            candidates = [t for t in tasks if t.energy_required == window_energy] or tasks
            task = candidates[0]

            need = int(task.estimated_minutes * overrun)
            available = slot_end - cursor
            if need <= available:
                chunk = need
            elif available >= (profile.preferred_session_min if profile else 30):
                # split: schedule what fits, leave the rest as a residual pending task
                chunk = available
            else:
                break  # slot too small for meaningful work; move to next slot

            item = models.PlanItem(
                user_id=user_id, date=date, task_id=task.id,
                start_time=_to_hhmm(cursor), end_time=_to_hhmm(cursor + chunk),
            )
            db.add(item)
            plan_items.append(item)
            cursor += chunk

            if chunk < need:
                # shrink the remaining task instead of marking it scheduled/done
                task.estimated_minutes = need - chunk
                break  # slot consumed
            else:
                tasks.remove(task)

            # insert a short break after a work chunk if a preferred session length exists
            if profile and chunk >= profile.preferred_session_min:
                cursor += profile.break_min

    db.commit()
    return plan_items


def what_should_i_do_now(db: Session, user_id: int, free_minutes: int, energy: str):
    tasks = (
        db.query(models.Task)
        .filter_by(user_id=user_id, status="pending")
        .all()
    )
    today = dt.date.today()
    tasks = [t for t in tasks if _dependency_met(db, t)]
    # must fit in the time available
    fitting = [t for t in tasks if t.estimated_minutes <= free_minutes] or tasks
    # prefer matching energy, then urgency
    matching = [t for t in fitting if t.energy_required == energy] or fitting
    matching.sort(key=lambda t: _urgency_score(t, today), reverse=True)
    return matching[0] if matching else None


def replan_after_miss(db: Session, user_id: int, task_id: int):
    """Called when a task is skipped/missed. Bumps postponed_count so it
    surfaces higher in urgency next time, then regenerates today's + tomorrow's plan."""
    task = db.query(models.Task).get(task_id)
    if not task:
        return
    task.status = "pending"
    task.postponed_count += 1
    db.commit()

    today = dt.date.today()
    generate_daily_plan(db, user_id, today)
    generate_daily_plan(db, user_id, today + dt.timedelta(days=1))


def update_overrun_profile(db: Session, user_id: int, task: models.Task):
    """Learn from actual vs estimated time (Feature #9: task difficulty prediction)."""
    if not task.actual_minutes or not task.estimated_minutes:
        return
    profile = db.query(models.UserProfile).filter_by(user_id=user_id).first()
    if not profile:
        return
    ratio = task.actual_minutes / max(task.estimated_minutes, 1)
    # exponential moving average so one outlier doesn't skew estimates too hard
    profile.avg_overrun_factor = round(0.8 * profile.avg_overrun_factor + 0.2 * ratio, 3)
    db.commit()


def goal_progress(db: Session, goal_id: int) -> dict:
    tasks = db.query(models.Task).filter_by(goal_id=goal_id).all()
    if not tasks:
        return {"progress_pct": 0, "total": 0, "done": 0}
    done = sum(1 for t in tasks if t.status == "done")
    return {"progress_pct": round(100 * done / len(tasks)), "total": len(tasks), "done": done}

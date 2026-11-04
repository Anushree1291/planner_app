"""Not every day is the same.

- Routines: regular busy time with rules ("office Mon-Fri 9-6", "office on the 2nd and 4th
  Saturday", "gym only when I go").
- Day status: a particular day is usual, off (leave, holiday, weekend off) or light (busy:
  plan very little).
- day_pattern "varies": tomorrow is never planned ahead; each morning the person says how the
  day looks (in the app, or with one tap in the morning email) and only then is it planned.
"""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import current_user, user_today
from .database import get_db

router = APIRouter(tags=["week"])

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# On a day off, these routines don't happen. Gym and "other" still might.
OFF_SKIPS = {"work", "class", "commute"}
LIGHT_DAY_MINUTES = 90          # a busy day gets at most this much planned work


def _ints(csv: str | None) -> list[int]:
    return [int(x) for x in (csv or "").split(",") if x.strip().isdigit()]


def nth_weekday_of_month(date: dt.date) -> int:
    """1 for the first Saturday of the month, 2 for the second, and so on."""
    return (date.day - 1) // 7 + 1


def routine_applies(r: models.Routine, date: dt.date) -> bool:
    if not r.auto or date.weekday() not in _ints(r.weekdays):
        return False
    weeks = _ints(r.weeks_of_month)
    return not weeks or nth_weekday_of_month(date) in weeks


def day_status(db: Session, user_id: int, date: dt.date) -> str | None:
    row = db.query(models.DayStatus).filter_by(user_id=user_id, date=date).first()
    return row.kind if row else None


def busy_blocks(db: Session, user_id: int, date: dt.date) -> list[dict]:
    """Everything that's busy on `date`: one-off blocks plus routines that apply,
    minus anything skipped or switched off by the day's status."""
    status = day_status(db, user_id, date)
    skipped = {rid for (rid,) in db.query(models.RoutineSkip.routine_id)
               .filter_by(user_id=user_id, date=date).all()}
    out = [{"id": b.id, "title": b.title, "start": b.start_time, "end": b.end_time,
            "source": "block", "kind": "other"}
           for b in db.query(models.CalendarBlock).filter_by(user_id=user_id, date=date).all()]
    for r in db.query(models.Routine).filter_by(user_id=user_id).all():
        if r.id in skipped or not routine_applies(r, date):
            continue
        if status == "off" and r.kind in OFF_SKIPS:
            continue
        out.append({"id": r.id, "title": r.title, "start": r.start_time, "end": r.end_time,
                    "source": "routine", "kind": r.kind, "note": r.note or ""})
    return sorted(out, key=lambda b: b["start"])


def describe_days(r: models.Routine) -> str:
    if not r.auto:
        return "only on days you say you're going"
    days = _ints(r.weekdays)
    if days == [0, 1, 2, 3, 4]:
        text = "weekdays"
    elif days == list(range(7)):
        text = "every day"
    elif days == [5, 6]:
        text = "weekends"
    else:
        text = ", ".join(WEEKDAY_NAMES[d] for d in days)
    weeks = _ints(r.weeks_of_month)
    if weeks:
        ords = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
        text += f" ({' and '.join(ords[w] for w in weeks)} of the month)"
    return text


def routine_json(r: models.Routine) -> dict:
    return {"id": r.id, "title": r.title, "kind": r.kind, "weekdays": _ints(r.weekdays),
            "weeks_of_month": _ints(r.weeks_of_month), "start": r.start_time, "end": r.end_time,
            "auto": r.auto, "note": r.note or "", "days_text": describe_days(r)}


def week_summary(db: Session, user_id: int) -> str:
    """Plain-language summary of someone's week, for the AI planner."""
    rows = db.query(models.Routine).filter_by(user_id=user_id).all()
    if not rows:
        return "No regular commitments recorded."
    return "; ".join(f"{r.title} {r.start_time}-{r.end_time} {describe_days(r)}" for r in rows)


# ---------- routes ----------

@router.get("/routines")
def list_routines(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(models.Routine).filter_by(user_id=user.id).order_by(models.Routine.start_time).all()
    return [routine_json(r) for r in rows]


@router.post("/routines")
def create_routine(payload: schemas.RoutineCreate, user: models.User = Depends(current_user),
                   db: Session = Depends(get_db)):
    if db.query(models.Routine).filter_by(user_id=user.id).count() >= 30:
        raise HTTPException(400, "That's a lot of routines. Remove one first.")
    r = models.Routine(user_id=user.id, title=payload.title.strip(), kind=payload.kind,
                       weekdays=",".join(map(str, sorted(set(payload.weekdays)))),
                       weeks_of_month=",".join(map(str, sorted(set(payload.weeks_of_month)))),
                       start_time=payload.start_time, end_time=payload.end_time,
                       auto=payload.auto, note=payload.note.strip())
    db.add(r)
    if payload.replaces_block_id and payload.auto:
        # the one-off block it was made from is now covered by the routine
        b = db.get(models.CalendarBlock, payload.replaces_block_id)
        if b and b.user_id == user.id and routine_applies(r, b.date):
            db.delete(b)
    db.commit()
    return routine_json(r)


@router.delete("/routines/{routine_id}")
def delete_routine(routine_id: int, user: models.User = Depends(current_user),
                   db: Session = Depends(get_db)):
    r = db.get(models.Routine, routine_id)
    if not r or r.user_id != user.id:
        raise HTTPException(404, "not found")
    db.query(models.RoutineSkip).filter_by(routine_id=r.id).delete(synchronize_session=False)
    db.delete(r)
    db.commit()
    return {"ok": True}


@router.post("/routines/{routine_id}/skip")
def skip_routine_today(routine_id: int, user: models.User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """'Not today': e.g. office is cancelled, or no gym this evening."""
    r = db.get(models.Routine, routine_id)
    if not r or r.user_id != user.id:
        raise HTTPException(404, "not found")
    today = user_today(user)
    if not db.query(models.RoutineSkip).filter_by(routine_id=r.id, date=today).first():
        db.add(models.RoutineSkip(user_id=user.id, routine_id=r.id, date=today))
        db.commit()
    return {"ok": True}


@router.post("/routines/{routine_id}/going")
def going_today(routine_id: int, user: models.User = Depends(current_user),
                db: Session = Depends(get_db)):
    """For 'only when I go' routines: block its usual time today."""
    r = db.get(models.Routine, routine_id)
    if not r or r.user_id != user.id:
        raise HTTPException(404, "not found")
    today = user_today(user)
    exists = db.query(models.CalendarBlock).filter_by(user_id=user.id, date=today, title=r.title,
                                                      start_time=r.start_time).first()
    if not exists:
        db.add(models.CalendarBlock(user_id=user.id, date=today, title=r.title,
                                    start_time=r.start_time, end_time=r.end_time))
        db.commit()
    return {"ok": True}


@router.get("/days/{date}")
def get_day(date: dt.date, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    return day_json(db, user, date)


def day_json(db: Session, user: models.User, date: dt.date) -> dict:
    status = day_status(db, user.id, date)
    on_demand = [routine_json(r) for r in db.query(models.Routine)
                 .filter_by(user_id=user.id, auto=False).all()]
    marked = {(b.title, b.start_time) for b in db.query(models.CalendarBlock)
              .filter_by(user_id=user.id, date=date).all()}
    for r in on_demand:
        r["going"] = (r["title"], r["start"]) in marked
    return {"date": date, "status": status, "pattern": user.day_pattern or "steady",
            "needs_checkin": (user.day_pattern == "varies" and status is None),
            "on_demand": on_demand}


@router.put("/days/{date}")
def set_day(date: dt.date, payload: schemas.DayUpdate, user: models.User = Depends(current_user),
            db: Session = Depends(get_db)):
    set_day_status(db, user.id, date, payload.kind, payload.note)
    return day_json(db, user, date)


def set_day_status(db: Session, user_id: int, date: dt.date, kind: str, note: str = ""):
    row = db.query(models.DayStatus).filter_by(user_id=user_id, date=date).first()
    if not row:
        row = models.DayStatus(user_id=user_id, date=date)
        db.add(row)
    row.kind, row.note = kind, note
    db.commit()


@router.delete("/days/{date}")
def clear_day(date: dt.date, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(models.DayStatus).filter_by(user_id=user.id, date=date).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.get("/days")
def upcoming_days_off(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    today = user_today(user)
    rows = db.query(models.DayStatus).filter(models.DayStatus.user_id == user.id,
                                             models.DayStatus.date >= today,
                                             models.DayStatus.kind == "off") \
             .order_by(models.DayStatus.date).limit(30).all()
    return [{"date": r.date, "kind": r.kind, "note": r.note} for r in rows]

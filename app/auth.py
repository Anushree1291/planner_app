"""Email + password accounts, one-click guest demo accounts, and session handling.

Sessions are a signed, httpOnly cookie (Starlette SessionMiddleware) holding only
the user id. Passwords are hashed with bcrypt.
"""
import time
import datetime as dt
from collections import defaultdict, deque
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import models, schemas, demo
from .database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------- helpers ----------

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode())
    except ValueError:
        return False


# Pre-computed so a login for an unknown email costs the same time as a real one.
_DUMMY_HASH = hash_password("timing-equaliser")


def clean_timezone(tz: str | None) -> str:
    try:
        ZoneInfo(tz or "UTC")
        return tz or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def user_now(user: models.User) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(clean_timezone(user.timezone)))


def user_today(user: models.User) -> dt.date:
    return user_now(user).date()


def client_ip(request: Request) -> str:
    # Azure App Service puts the caller in X-Client-IP / X-Forwarded-For ("ip:port").
    raw = (request.headers.get("x-client-ip")
           or request.headers.get("x-forwarded-for", "").split(",")[0]
           or (request.client.host if request.client else "unknown"))
    raw = raw.strip()
    if raw.count(":") == 1:  # strip ":port" from IPv4
        raw = raw.split(":")[0]
    return raw


class RateLimiter:
    """Tiny in-memory sliding-window limiter. Good enough for a single instance;
    swap for Redis if you ever scale out."""

    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits, self.window = max_hits, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_hits:
            raise HTTPException(429, "Too many attempts, please wait a few minutes.")
        q.append(now)

    def reset(self):
        self.hits.clear()


login_limiter = RateLimiter(10, 15 * 60)    # 10 logins / 15 min / IP
signup_limiter = RateLimiter(5, 60 * 60)    # 5 signups / hour / IP
demo_limiter = RateLimiter(10, 60 * 60)     # 10 demo sandboxes / hour / IP


def _start_session(request: Request, user: models.User):
    request.session.clear()
    request.session["uid"] = user.id


def _new_user(db: Session, **fields) -> models.User:
    user = models.User(**fields)
    db.add(user)
    db.flush()
    db.add(models.UserProfile(user_id=user.id))
    return user


# ---------- dependency used by every protected route ----------

def current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    uid = request.session.get("uid")
    user = db.get(models.User, uid) if uid else None
    if not user:
        request.session.clear()
        raise HTTPException(401, "Please log in.")
    return user


def public_user(user: models.User, db: Session) -> dict:
    from .ai_decompose import ai_calls_left, daily_limit, active_providers  # avoids a cycle
    from .emailer import email_enabled
    return {
        "id": user.id, "email": user.email, "name": user.name,
        "timezone": user.timezone, "is_guest": user.is_guest,
        "email_enabled": email_enabled(),
        "email_verified": bool(user.email_verified),
        "email_morning_plan": bool(user.email_morning_plan),
        "email_risk_alerts": bool(user.email_risk_alerts),
        "email_morning_hour": user.email_morning_hour or 7,
        "email_bounced": bool(user.email_bounced),
        "email_weekly_digest": bool(user.email_weekly_digest),
        "day_pattern": user.day_pattern or "steady",
        "ai_calls_left_today": ai_calls_left(db, user),
        "ai_daily_limit": daily_limit(user),
        "ai_providers": active_providers(),
    }


# ---------- routes ----------

@router.post("/signup")
def signup(payload: schemas.SignupRequest, request: Request, background: BackgroundTasks,
           db: Session = Depends(get_db)):
    signup_limiter.check(client_ip(request))
    email = payload.email.strip().lower()
    if db.query(models.User).filter_by(email=email).first():
        raise HTTPException(409, "An account with this email already exists.")
    user = _new_user(db, email=email, password_hash=hash_password(payload.password),
                     name=payload.name.strip() or email.split("@")[0],
                     timezone=clean_timezone(payload.timezone))
    db.commit()
    _start_session(request, user)
    from .mailing import send_verification  # local import avoids a cycle
    background.add_task(send_verification, user.id, True)   # welcome + confirm-your-email
    return public_user(user, db)


@router.post("/login")
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    login_limiter.check(client_ip(request))
    user = db.query(models.User).filter_by(email=payload.email.strip().lower()).first()
    if not user:
        verify_password(payload.password, _DUMMY_HASH)
        raise HTTPException(401, "Wrong email or password.")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Wrong email or password.")
    _start_session(request, user)
    return public_user(user, db)


@router.post("/demo")
def start_demo(payload: schemas.DemoRequest, request: Request, db: Session = Depends(get_db)):
    """One click, no signup: a private sandbox with sample data, deleted after 24h."""
    demo_limiter.check(client_ip(request))
    user = _new_user(db, name="Guest", is_guest=True, timezone=clean_timezone(payload.timezone))
    demo.seed_sample_data(db, user, user_today(user))
    db.commit()
    _start_session(request, user)
    return public_user(user, db)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    return public_user(user, db)


@router.patch("/me")
def update_me(payload: schemas.SettingsUpdate, user: models.User = Depends(current_user),
              db: Session = Depends(get_db)):
    if payload.timezone is not None:
        user.timezone = clean_timezone(payload.timezone)
    if payload.name is not None:
        user.name = payload.name.strip() or user.name
    if payload.email_morning_plan is not None:
        user.email_morning_plan = payload.email_morning_plan
    if payload.email_risk_alerts is not None:
        user.email_risk_alerts = payload.email_risk_alerts
    if payload.email_morning_hour is not None:
        user.email_morning_hour = payload.email_morning_hour
    if payload.email_weekly_digest is not None:
        user.email_weekly_digest = payload.email_weekly_digest
    if payload.day_pattern is not None:
        user.day_pattern = payload.day_pattern
    db.commit()
    return public_user(user, db)


@router.delete("/me")
def delete_me(request: Request, user: models.User = Depends(current_user),
              db: Session = Depends(get_db)):
    """Delete the account and every row it owns."""
    delete_user_data(db, user.id)
    db.commit()
    request.session.clear()
    return {"ok": True}


def delete_user_data(db: Session, user_id: int):
    # Explicit deletes so this works the same on SQLite (no FK cascades by default) and Postgres.
    for model in (models.PlanItem, models.CalendarBlock, models.AiUsage, models.EmailSend,
                  models.GoalMessage, models.Recurring, models.RoutineSkip, models.Routine,
                  models.DayStatus, models.Task, models.Goal, models.UserProfile):
        db.query(model).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(models.User).filter_by(id=user_id).delete(synchronize_session=False)

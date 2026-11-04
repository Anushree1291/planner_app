"""Personalised email: what goes into each message, signed links, and the /email routes.

Only *verified* addresses get the daily plan and risk alerts, so nobody can sign up with
someone else's address and have mail sent to them. Every scheduled email carries a
one-click unsubscribe link.
"""
import datetime as dt
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import models, planner, email_templates as tpl
from .auth import current_user, user_now, RateLimiter
from .config import SECRET_KEY, PUBLIC_URL, EMAIL_EVENTS_KEY
from .database import get_db, SessionLocal
from .emailer import send_email, email_enabled

log = logging.getLogger("planner.mailing")
router = APIRouter(prefix="/email", tags=["email"])
_signer = URLSafeTimedSerializer(SECRET_KEY)
VERIFY_MAX_AGE = 7 * 24 * 3600

send_now_limiter = RateLimiter(3, 60 * 60)      # "email me my plan" 3/hour/user
resend_limiter = RateLimiter(3, 60 * 60)        # verification resends 3/hour/user


# ---------- signed links ----------

def make_token(purpose: str, user: models.User, **extra) -> str:
    # Binding the email means a link stops working if the address changes.
    return _signer.dumps({"uid": user.id, "email": user.email, **extra}, salt=purpose)


def read_token(db: Session, purpose: str, token: str, max_age: int | None) -> models.User:
    return read_token_data(db, purpose, token, max_age)[0]


def read_token_data(db: Session, purpose: str, token: str, max_age: int | None):
    try:
        data = _signer.loads(token, salt=purpose, max_age=max_age)
    except SignatureExpired:
        raise HTTPException(400, "This link has expired. Request a new one from Settings.")
    except BadSignature:
        raise HTTPException(400, "This link isn't valid.")
    user = db.get(models.User, data.get("uid"))
    if not user or user.email != data.get("email"):
        raise HTTPException(400, "This link isn't valid any more.")
    return user, data


def _url(path: str) -> str:
    return PUBLIC_URL.rstrip("/") + path


def verify_url(user) -> str:
    return _url(f"/email/verify?token={make_token('verify', user)}")


def unsubscribe_url(user) -> str:
    return _url(f"/email/unsubscribe?token={make_token('unsubscribe', user)}")


# ---------- building messages ----------

def plan_items_for(db: Session, user: models.User, date):
    items = db.query(models.PlanItem).filter_by(user_id=user.id, date=date) \
              .order_by(models.PlanItem.start_time).all()
    out = []
    for i in items:
        t = db.get(models.Task, i.task_id)
        if t and t.status != "done":
            out.append((i.start_time, i.end_time, t.title, t.energy_required))
    return out


def goal_summaries(db: Session, user: models.User, today):
    out = []
    for g in db.query(models.Goal).filter_by(user_id=user.id, status="active") \
               .order_by(models.Goal.id).all():
        prog = planner.goal_progress(db, g.id)
        risk = planner.goal_risk(db, g, today) or {}
        out.append((g.title, prog["progress_pct"], bool(risk.get("at_risk")), risk.get("detail", "")))
    out.sort(key=lambda g: not g[2])  # at-risk goals first
    return out


def build_daily_plan(db: Session, user: models.User):
    now = user_now(user)
    today = now.date()
    items = plan_items_for(db, user, today)
    minute = now.hour * 60 + now.minute
    free = planner.calculate_capacity(db, user.id, today, not_before=minute)
    return tpl.daily_plan(user, now, items, free, goal_summaries(db, user, today),
                          PUBLIC_URL, unsubscribe_url(user))


def build_risk_alert(db: Session, user: models.User):
    """Returns (subject, html, text), or None when no goal is slipping."""
    risky = [g for g in goal_summaries(db, user, user_now(user).date()) if g[2]]
    if not risky:
        return None
    return tpl.risk_alert(user, risky, PUBLIC_URL, unsubscribe_url(user))


def build_checkin(db: Session, user: models.User):
    now = user_now(user)
    today = now.date().isoformat()
    links = {k: _url(f"/day/checkin?token={make_token('checkin', user, date=today, kind=k)}")
             for k in ("usual", "light", "off")}
    waiting = [t.title for t in db.query(models.Task).filter_by(user_id=user.id, status="pending")
               .order_by(models.Task.priority.desc(), models.Task.id).limit(3).all()]
    return tpl.checkin(user, now, links, waiting, PUBLIC_URL, unsubscribe_url(user))


def build_digest(db: Session, user: models.User):
    """Returns the weekly ideas email, or None when there's nothing worth sending."""
    from .goal_chat import suggestions_for
    sections = []
    goals = db.query(models.Goal).filter(models.Goal.user_id == user.id, models.Goal.status == "active",
                                         models.Goal.kind.in_(["book", "course", "exam", "fitness", "habit"])) \
              .order_by(models.Goal.id.desc()).limit(2).all()
    for g in goals:
        s = suggestions_for(db, g)
        if s.get("books") or s.get("newsletters"):
            sections.append({"goal": g.title, "books": s.get("books", [])[:3],
                             "newsletters": s.get("newsletters", [])[:2]})
    if not sections:
        return None
    return tpl.digest(user, sections, PUBLIC_URL, unsubscribe_url(user))


def record(db: Session, user_id: int, kind: str, status: str):
    """Log every attempt: the scheduler reads this to stay under the provider's limits."""
    if status in ("sent", "throttled", "failed"):
        db.add(models.EmailSend(user_id=user_id, kind=kind, status=status))
        db.commit()


def deliver(db: Session, user: models.User, kind: str, message, with_unsubscribe=True) -> str:
    subject, html, text = message
    status = send_email(user.email, subject, html, text,
                        unsubscribe_url(user) if with_unsubscribe else None)
    record(db, user.id, kind, status)
    return status


def send_daily_plan(db: Session, user: models.User) -> str:
    return deliver(db, user, "plan", build_daily_plan(db, user))


def send_risk_alert(db: Session, user: models.User) -> str:
    message = build_risk_alert(db, user)
    return deliver(db, user, "risk", message) if message else "off"


def send_verification(user_id: int, welcome: bool = True):
    """Runs as a background task, so it opens its own DB session."""
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        if user and user.email and not user.email_verified:
            deliver(db, user, "verify",
                    tpl.verification(user, verify_url(user), PUBLIC_URL, welcome), with_unsubscribe=False)
    finally:
        db.close()


def can_receive(user: models.User) -> bool:
    return bool(user.email and user.email_verified and not user.email_bounced
                and not user.is_guest and email_enabled())


# ---------- routes ----------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#F1F3F8;color:#1E2235;
font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;padding:16px}}main{{max-width:420px;background:#fff;
border-radius:16px;padding:28px}}h1{{font-size:22px;margin:0 0 8px}}p{{line-height:1.55;color:#5F6680}}
button,a.btn{{display:inline-block;background:#1E2235;color:#fff;border:0;border-radius:10px;padding:12px 18px;
font:600 15px inherit;text-decoration:none;cursor:pointer}}</style></head><body><main>{body}</main></body></html>"""


day_router = APIRouter(tags=["email"])


@day_router.get("/day/checkin")
def checkin_from_email(token: str, db: Session = Depends(get_db)):
    """The buttons in the morning check-in email: record how today looks, plan it, open the app."""
    from .routines import set_day_status
    user, data = read_token_data(db, "checkin", token, 2 * 24 * 3600)
    if data.get("kind") not in ("usual", "light", "off"):
        raise HTTPException(400, "This link isn't valid.")
    date = dt.date.fromisoformat(data["date"])
    set_day_status(db, user.id, date, data["kind"])
    now = user_now(user)
    planner.generate_daily_plan(db, user.id, date,
                                not_before=now.hour * 60 + now.minute if date == now.date() else None)
    return RedirectResponse(f"/?checkin={data['kind']}", status_code=303)


@router.get("/verify")
def verify(token: str, db: Session = Depends(get_db)):
    user = read_token(db, "verify", token, VERIFY_MAX_AGE)
    user.email_verified = True
    db.commit()
    return RedirectResponse("/?verified=1", status_code=303)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(token: str, db: Session = Depends(get_db)):
    # A GET only shows a button: mail scanners follow links, and they mustn't unsubscribe people.
    read_token(db, "unsubscribe", token, None)
    body = (f'<h1>Stop planner emails?</h1><p>You won\'t get the morning plan or goal alerts any more. '
            f'You can turn them back on in Settings.</p><form method="post"><button>Stop emails</button></form>')
    return _PAGE.format(title="Stop emails", body=body)


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    """Also the target of the one-click List-Unsubscribe-Post header."""
    user = read_token(db, "unsubscribe", token, None)
    user.email_morning_plan = False
    user.email_risk_alerts = False
    db.commit()
    body = ('<h1>Emails stopped</h1><p>You won\'t get planner emails any more. '
            'Changed your mind? Turn them back on in Settings.</p><a class="btn" href="/">Open my planner</a>')
    return _PAGE.format(title="Emails stopped", body=body)


@router.post("/resend-verification")
def resend_verification(background: BackgroundTasks, user: models.User = Depends(current_user)):
    if user.is_guest or not user.email:
        raise HTTPException(400, "Demo accounts don't have an email address. Sign up to get emails.")
    if user.email_verified:
        return {"ok": True, "already_verified": True}
    if not email_enabled():
        raise HTTPException(503, "Email isn't set up on this server yet.")
    resend_limiter.check(f"user:{user.id}")
    background.add_task(send_verification, user.id, False)
    return {"ok": True}


@router.post("/send-plan")
def send_plan_now(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    if user.is_guest or not user.email:
        raise HTTPException(400, "Demo accounts don't have an email address. Sign up to get emails.")
    if not email_enabled():
        raise HTTPException(503, "Email isn't set up on this server yet.")
    if not user.email_verified:
        raise HTTPException(400, "Confirm your email first. Check your inbox for the link.")
    send_now_limiter.check(f"user:{user.id}")
    status = send_daily_plan(db, user)
    if status == "throttled":
        raise HTTPException(429, "Lots of emails are going out right now. Try again in a few minutes.")
    if status != "sent":
        raise HTTPException(502, "The email couldn't be sent. Try again in a few minutes.")
    return {"ok": True, "to": user.email}


@router.post("/retry")
def retry_after_bounce(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """The user says their address works again: clear the bounce flag."""
    user.email_bounced = False
    db.commit()
    return {"ok": True}


# ---------- delivery reports (bounces) from Azure Event Grid ----------
STOP_STATUSES = {"Bounced", "Suppressed"}   # permanent: the address doesn't take mail


@router.options("/events", include_in_schema=False)
def events_handshake(request: Request):
    """CloudEvents-schema webhook validation."""
    origin = request.headers.get("webhook-request-origin", "*")
    return Response(headers={"WebHook-Allowed-Origin": origin, "WebHook-Allowed-Rate": "*"})


@router.post("/events", include_in_schema=False)
async def delivery_events(request: Request, key: str = "", db: Session = Depends(get_db)):
    """Event Grid posts EmailDeliveryReportReceived events here. Addresses that bounce
    stop getting scheduled mail, which protects the sender reputation Azure checks
    before raising sending limits."""
    if not EMAIL_EVENTS_KEY or not hmac.compare_digest(key, EMAIL_EVENTS_KEY):
        raise HTTPException(401, "bad key")
    payload = await request.json()
    events = payload if isinstance(payload, list) else [payload]
    stopped = 0
    for ev in events:
        etype = ev.get("eventType") or ev.get("type") or ""
        data = ev.get("data") or {}
        if etype == "Microsoft.EventGrid.SubscriptionValidationEvent":
            return JSONResponse({"validationResponse": data.get("validationCode")})
        if etype.endswith("EmailDeliveryReportReceived") and data.get("status") in STOP_STATUSES:
            addr = (data.get("recipient") or "").strip().lower()
            user = db.query(models.User).filter_by(email=addr).first() if addr else None
            if user and not user.email_bounced:
                user.email_bounced = True
                stopped += 1
                log.warning("email to user %s %s; scheduled mail stopped", user.id, data.get("status"))
    db.commit()
    return {"ok": True, "stopped": stopped}


@router.get("/preview", response_class=HTMLResponse)
def preview(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """The exact morning email for the current user, so they can see it before it arrives."""
    _, html, _ = build_daily_plan(db, user)
    return html

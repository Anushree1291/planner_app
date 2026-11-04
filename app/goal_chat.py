"""Plan a goal by chatting about it.

Someone types a goal ("Read Atomic Habits by December"). The planner asks a few follow-up
questions (how fast they read, which days, what time of day...), looks facts up where it
can (a book's page count), then proposes a plan they can accept or adjust.

Two engines share one flow:
- AI: an LLM (Azure AI Foundry or Gemini) runs the conversation and fills in plan *parameters*.
- Script: built-in questions per goal type, used without AI, after the daily AI limit, or if
  the AI fails mid-chat. It picks up from whatever is already known.

Either way the plan itself (sessions, minutes, page ranges, finish date) is computed here in
code from the parameters, so the numbers are always right.
"""
import datetime as dt
import json
import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from . import models, schemas, books, ai_decompose
from .auth import current_user, user_today
from .config import MAX_GOALS_PER_USER, MAX_TASKS_PER_USER
from .database import get_db
from .routines import week_summary

log = logging.getLogger("planner.chat")
router = APIRouter(tags=["goal chat"])

KINDS = ("book", "fitness", "course", "exam", "habit", "general")
WINDOWS = ("morning", "afternoon", "evening")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_AI_TURNS = 12


# ======================= small parsers (used by the script engine) =======================

def detect_kind(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(read|book|novel|finish reading|chapters?)\b", t):
        return "book"
    if re.search(r"\b(gym|workout|work out|run|running|marathon|10k|5k|yoga|fitness|lose weight|"
                 r"weight loss|exercise|lift|strength|cardio|swim)\b", t):
        return "fitness"
    if re.search(r"\b(exam|test|gate|cat|gre|gmat|ielts|toefl|upsc|neet|jee|boards?|interview prep|certification exam)\b", t):
        return "exam"
    if re.search(r"\b(course|learn|lessons?|certification|bootcamp|tutorial|udemy|coursera|module)\b", t):
        return "course"
    if re.search(r"\b(every day|daily|habit|meditat\w*|journal\w*|practi[cs]e|drink water|sleep by|wake up)\b", t):
        return "habit"
    return "general"


def parse_int(text: str) -> int | None:
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        return None
    v = float(m.group())
    if re.search(r"\bhours?\b|\bhrs?\b", text, re.I) and v <= 6:
        v *= 60
    return int(round(v))


def parse_minutes(text: str) -> int | None:
    t = text.lower()
    if re.search(r"\b(an|one|1) hour\b", t):
        return 60
    v = parse_int(t)
    return v if v and 5 <= v <= 300 else None


def parse_days(text: str) -> list[int] | None:
    """'Weekdays and Sunday' -> Mon-Fri + Sun; 'Mon, Wed, Fri'; '3 days'; 'every day'."""
    t = text.lower()
    if re.search(r"every ?day|daily|all days|7 days|all week", t):
        return list(range(7))
    days = set()
    if re.search(r"weekdays?|mon-fri|mon to fri|working days", t):
        days |= {0, 1, 2, 3, 4}
    if "weekend" in t:
        days |= {5, 6}
    days |= {i for i, n in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
             if re.search(rf"\b{n}", t)}
    if days:
        return sorted(days)
    m = re.search(r"(\d)\s*(?:days|times|x)", t)
    if m:
        return {1: [5], 2: [1, 4], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 3, 4],
                6: [0, 1, 2, 3, 4, 5], 7: list(range(7))}.get(int(m.group(1)))
    return None


def parse_window(text: str) -> str | None:
    t = text.lower()
    if "morning" in t or "before work" in t or "early" in t:
        return "morning"
    if "afternoon" in t or "lunch" in t:
        return "afternoon"
    if "evening" in t or "night" in t or "bed" in t or "after work" in t:
        return "evening"
    if "any" in t or "whenever" in t or "flexible" in t or "doesn't matter" in t:
        return "any"
    return None


def parse_time(text: str) -> str | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text.lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_split(text: str) -> list[dict]:
    """'Mon legs, Wed upper body, Fri cardio' -> one session per day."""
    out = []
    for part in re.split(r"[,;\n]+", text):
        days = parse_days(part)
        label = re.sub(r"\b(mon|tue|wed|thu|fri|sat|sun)\w*\b", "", part, flags=re.I).strip(" :-")
        if days and label:
            out.append({"title": label[:60], "days": days})
    return out


def days_text(days: list[int]) -> str:
    if days == list(range(7)):
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "weekdays"
    if days == [5, 6]:
        return "weekends"
    return ", ".join(DAY_NAMES[d] for d in days)


# ======================= plan building (always deterministic) =======================

def _reading_days_until(today: dt.date, end: dt.date, days: list[int]) -> int:
    n, d = 0, today
    while d <= end:
        if d.weekday() in days:
            n += 1
        d += dt.timedelta(days=1)
    return n


def _finish_date(today: dt.date, sessions: int, days: list[int]) -> dt.date:
    d, left = today, sessions
    while True:
        if d.weekday() in days:
            left -= 1
            if left <= 0:
                return d
        d += dt.timedelta(days=1)


def _clamp(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def _clean_days(v, default=None) -> list[int]:
    if isinstance(v, list):
        out = sorted({int(x) for x in v if str(x).lstrip("-").isdigit() and 0 <= int(x) <= 6})
        if out:
            return out
    return default if default is not None else list(range(7))


def _window(v) -> str | None:
    return v if v in WINDOWS else None


def missing_params(kind: str, p: dict, goal: models.Goal) -> list[str]:
    need = {
        "book": ["pages", "pace_pages_per_hour", "days"] + ([] if goal.deadline else ["minutes_per_day"]) + ["window"],
        "course": ["lessons", "minutes_per_lesson", "days", "window"],
        "exam": (["exam_date"] if not goal.deadline else []) + ["topics", "minutes_per_session", "days", "window"],
        "fitness": ["activity", "sessions", "minutes", "start"],
        "habit": ["minutes", "days", "window"],
        "general": ["steps"],
    }[kind]
    return [k for k in need if p.get(k) in (None, "", [])]


def build_plan(kind: str, p: dict, goal: models.Goal, today: dt.date, facts: dict) -> dict:
    """Turn parameters into concrete steps, recurring sessions and routines."""
    deadline = goal.deadline or (dt.date.fromisoformat(p["exam_date"]) if p.get("exam_date") else None)
    window = _window(p.get("window"))
    plan = {"kind": kind, "steps": [], "recurring": [], "routines": [], "window": window,
            "finish": None, "summary": "", "lines": []}
    when = f", {window}s" if window else ""

    if kind == "book":
        title = (facts.get("book") or {}).get("title") or goal.title
        pages = _clamp(p.get("pages"), 10, 5000, 250)
        pace = _clamp(p.get("pace_pages_per_hour"), 5, 120, 20)
        days = _clean_days(p.get("days"))
        if deadline:
            n_days = max(_reading_days_until(today, deadline, days), 1)
            per_day = max(math.ceil(pages / n_days), 3)
        else:
            per_day = max(round(pace * _clamp(p.get("minutes_per_day"), 10, 240, 30) / 60), 3)
        if p.get("pages_per_day"):
            per_day = _clamp(p["pages_per_day"], 3, 300, per_day)
        minutes = max(10, round(per_day / pace * 60 / 5) * 5)
        start = 1
        while start <= pages:
            end = min(start + per_day - 1, pages)
            plan["steps"].append({"title": f"Read {title}: pages {start}–{end}", "minutes": minutes,
                                  "energy": "low" if window == "evening" else "medium"})
            start = end + 1
        plan["allowed_days"] = days
        plan["finish"] = _finish_date(today, len(plan["steps"]), days)
        plan["summary"] = f"{per_day} pages a session (~{minutes} min), {days_text(days)}{when}"
        plan["lines"] = [f"{pages} pages at about {pace} pages an hour",
                         f"{len(plan['steps'])} reading sessions"]

    elif kind == "course":
        n = _clamp(p.get("lessons"), 1, 300, 10)
        mins = _clamp(p.get("minutes_per_lesson"), 5, 240, 30)
        days = _clean_days(p.get("days"), [0, 2, 4])
        plan["steps"] = [{"title": f"{goal.title}: lesson {i} of {n}", "minutes": mins, "energy": "high"}
                         for i in range(1, n + 1)]
        plan["allowed_days"] = days
        plan["finish"] = _finish_date(today, n, days)
        plan["summary"] = f"1 lesson (~{mins} min) on {days_text(days)}{when}"
        plan["lines"] = [f"{n} lessons"]

    elif kind == "exam":
        topics = p.get("topics")
        if isinstance(topics, (int, float)) or (isinstance(topics, str) and topics.strip().isdigit()):
            topics = [f"Topic {i}" for i in range(1, _clamp(topics, 1, 60, 8) + 1)]
        topics = [str(t).strip()[:80] for t in (topics or []) if str(t).strip()][:60] or ["Topic 1"]
        mins = _clamp(p.get("minutes_per_session"), 20, 240, 60)
        days = _clean_days(p.get("days"))
        steps = [{"title": f"Study: {t}", "minutes": mins, "energy": "high"} for t in topics]
        steps += [{"title": f"Revision round {i}: all topics", "minutes": mins, "energy": "high"} for i in (1, 2)]
        steps += [{"title": f"Mock test {i} (timed)", "minutes": max(mins, 90), "energy": "high"} for i in (1, 2)]
        plan["steps"] = steps
        plan["allowed_days"] = days
        plan["finish"] = _finish_date(today, len(steps), days)
        plan["summary"] = f"{len(topics)} topics, then 2 revision rounds and 2 mock tests, {days_text(days)}{when}"
        plan["lines"] = [f"{mins} min a session"]

    elif kind == "fitness":
        activity = str(p.get("activity") or "Workout").strip()[:40]
        mins = _clamp(p.get("minutes"), 10, 240, 45)
        sessions = p.get("sessions") or [{"title": activity, "days": [0, 2, 4]}]
        start = p.get("start") if isinstance(p.get("start"), str) and re.match(r"^\d{2}:\d{2}$", p.get("start", "")) else None
        for s in sessions[:7]:
            label = str(s.get("title") or activity).strip()[:60]
            title = label if label.lower().startswith(activity.lower()) else f"{activity}: {label}"
            days = _clean_days(s.get("days"), [0, 2, 4])
            if start:    # fixed time: it's busy time on those days, like any routine
                h, m = map(int, start.split(":"))
                end_m = min(h * 60 + m + mins, 23 * 60 + 59)
                plan["routines"].append({"title": title, "kind": "gym", "weekdays": days, "start": start,
                                         "end": f"{end_m // 60:02d}:{end_m % 60:02d}"})
            else:        # flexible: a session the planner fits into free time
                plan["recurring"].append({"title": title, "minutes": mins, "energy": "high",
                                          "weekdays": days, "window": window})
        all_days = sorted({d for s in sessions[:7] for d in _clean_days(s.get("days"), [0, 2, 4])})
        plan["summary"] = (f"{len(sessions[:7])} session type{'s' if len(sessions[:7]) > 1 else ''}, "
                           f"{days_text(all_days)}, {mins} min" + (f" at {start}" if start else when))
        plan["lines"] = [f"{x['title']}: {days_text(_clean_days(x.get('days'), [0, 2, 4]))}" for x in sessions[:7]]

    elif kind == "habit":
        title = str(p.get("title") or goal.title).strip()[:120]
        mins = _clamp(p.get("minutes"), 2, 180, 10)
        days = _clean_days(p.get("days"))
        plan["recurring"] = [{"title": title, "minutes": mins, "energy": "low", "weekdays": days, "window": window}]
        plan["summary"] = f"{mins} min, {days_text(days)}{when}"

    else:  # general
        steps = p.get("steps") or ai_decompose.fallback_breakdown(goal.title)
        plan["steps"] = [{"title": str(s.get("title", ""))[:200], "minutes": _clamp(s.get("minutes", s.get("estimated_minutes")), 5, 600, 30),
                          "energy": s.get("energy", s.get("energy_required", "medium")) if s.get("energy", s.get("energy_required")) in ("high", "medium", "low") else "medium"}
                         for s in steps if isinstance(s, dict) and str(s.get("title", "")).strip()][:20]
        plan["summary"] = f"{len(plan['steps'])} steps{when}"
        plan["sequential"] = False

    if deadline and plan["finish"]:
        early = (deadline - plan["finish"]).days
        plan["lines"].append(f"Finishes {plan['finish']:%d %b}" +
                             (f", {early} days before your deadline" if early > 0 else
                              ", on your deadline" if early == 0 else f", {-early} days after your deadline"))
        plan["late"] = early < 0
    elif plan["finish"]:
        plan["lines"].append(f"Finishes around {plan['finish']:%d %b}")
    if len(plan["steps"]) > 150:
        plan["steps"] = plan["steps"][:150]
        plan["lines"].append("Showing the first 150 sessions")
    plan["finish"] = plan["finish"].isoformat() if plan["finish"] else None
    return plan


def apply_plan(db: Session, user: models.User, goal: models.Goal, plan: dict):
    """Create the tasks, recurring sessions and routines for an accepted plan."""
    room = MAX_TASKS_PER_USER - db.query(models.Task).filter_by(user_id=user.id).count()
    if len(plan["steps"]) > room:
        raise HTTPException(400, f"This plan needs {len(plan['steps'])} tasks but you have room for {room}. "
                                 "Finish or delete some first.")
    allowed = ",".join(map(str, plan.get("allowed_days") or [])) or None
    prev = None
    for i, s in enumerate(plan["steps"]):
        t = models.Task(user_id=user.id, goal_id=goal.id, title=s["title"], estimated_minutes=s["minutes"],
                        energy_required=s["energy"], priority=4 if plan["kind"] in ("exam", "book", "course") else 3,
                        deadline=goal.deadline, preferred_window=plan.get("window"), allowed_days=allowed,
                        depends_on_id=prev.id if (prev and plan.get("sequential", True)) else None)
        db.add(t)
        db.flush()
        prev = t
    for r in plan["recurring"]:
        db.add(models.Recurring(user_id=user.id, goal_id=goal.id, title=r["title"], minutes=r["minutes"],
                                energy=r["energy"], weekdays=",".join(map(str, r["weekdays"])),
                                preferred_window=r.get("window")))
    for r in plan["routines"]:
        db.add(models.Routine(user_id=user.id, title=r["title"], kind=r["kind"], start_time=r["start"],
                              end_time=r["end"], weekdays=",".join(map(str, r["weekdays"])), auto=True))
    goal.status = "active"


# ======================= chat storage helpers =======================

def _details(goal: models.Goal) -> dict:
    try:
        return json.loads(goal.details or "{}")
    except json.JSONDecodeError:
        return {}


def _save(goal: models.Goal, d: dict):
    goal.details = json.dumps(d, default=str)


def _say(db: Session, user, goal, role: str, content: str, **extra):
    db.add(models.GoalMessage(user_id=user.id, goal_id=goal.id, role=role, content=content[:4000],
                              extra=json.dumps(extra, default=str)))


def message_json(m: models.GoalMessage) -> dict:
    try:
        extra = json.loads(m.extra or "{}")
    except json.JSONDecodeError:
        extra = {}
    return {"id": m.id, "role": m.role, "content": m.content, **extra}


def chat_json(db: Session, goal: models.Goal) -> dict:
    d = _details(goal)
    msgs = db.query(models.GoalMessage).filter_by(goal_id=goal.id).order_by(models.GoalMessage.id).all()
    return {"goal": {"id": goal.id, "title": goal.title, "kind": goal.kind, "status": goal.status,
                     "deadline": goal.deadline},
            "engine": d.get("engine", "script"), "messages": [message_json(m) for m in msgs],
            "plan": d.get("plan")}


# ======================= the script engine =======================

QUESTIONS = {
    # key: (question, quick replies)
    "pages": ("How many pages does your copy have?", ["200", "300", "400", "500"]),
    "pace_pages_per_hour": ("How many pages do you usually read in an hour?", ["10", "20", "30", "40", "Not sure"]),
    "days": ("Which days work for you?", ["Every day", "Weekdays", "Weekends", "Mon, Wed, Fri"]),
    "minutes_per_day": ("How long do you want each session to be?", ["15 min", "30 min", "45 min", "1 hour"]),
    "window": ("What time of day suits you best?", ["Morning", "Afternoon", "Evening", "Any time"]),
    "lessons": ("Roughly how many lessons or modules does it have?", ["10", "20", "30", "50"]),
    "minutes_per_lesson": ("About how long is each lesson, including practice?", ["15 min", "30 min", "45 min", "1 hour"]),
    "exam_date": ("When is the exam? (for example 15 Mar 2027 or 2027-03-15)", []),
    "topics": ("Which topics or chapters do you need to cover? List them separated by commas, or just tell me how many.", ["8", "12", "20"]),
    "minutes_per_session": ("How long can you study in one sitting?", ["45 min", "1 hour", "90 min", "2 hours"]),
    "activity": ("What kind of exercise is it?", ["Gym", "Running", "Yoga", "Home workout"]),
    "sessions": ("What do you do on which days? For example: Mon legs, Wed upper body, Fri cardio.",
                 ["Same workout, 3 days", "Same workout, 5 days", "Same workout, every day"]),
    "minutes": ("How long is one session?", ["15 min", "30 min", "45 min", "1 hour"]),
    "start": ("Do you go at a fixed time? If yes I'll block it as busy time on those days.",
              ["Yes, 7:00", "Yes, 18:00", "No, fit it in"]),
    "steps": ("Want me to suggest some starter steps?", ["Yes, suggest steps"]),
}


def _parse_answer(key: str, text: str, p: dict):
    t = text.strip()
    if key in ("pages", "lessons"):
        v = parse_int(t)
        return v if v and v > 0 else None
    if key == "pace_pages_per_hour":
        return 20 if "not sure" in t.lower() else (parse_int(t) or None)
    if key in ("minutes_per_day", "minutes_per_lesson", "minutes_per_session", "minutes"):
        return parse_minutes(t)
    if key == "days":
        return parse_days(t)
    if key == "window":
        return parse_window(t)
    if key == "exam_date":
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%b %d %Y", "%B %d %Y"):
            try:
                return dt.datetime.strptime(t.replace(",", ""), fmt).date().isoformat()
            except ValueError:
                pass
        return None
    if key == "topics":
        if re.fullmatch(r"\s*\d+\s*(topics|chapters)?\s*", t, re.I):
            return int(re.search(r"\d+", t).group())
        parts = [x.strip() for x in re.split(r"[,;\n]+", t) if x.strip()]
        return parts if len(parts) >= 2 else None
    if key == "activity":
        return t.split(",")[0][:40] or None
    if key == "sessions":
        if "same" in t.lower() or parse_days(t) and not parse_split(t):
            return [{"title": p.get("activity") or "Workout", "days": parse_days(t) or [0, 2, 4]}]
        return parse_split(t) or None
    if key == "start":
        if re.search(r"\bno\b|fit it|flexib|any", t.lower()):
            return "flexible"
        return parse_time(t)
    if key == "steps":
        return [] if "no" in t.lower().split() else ai_decompose.fallback_breakdown("")
    return t


def script_turn(db, user, goal, d: dict, user_text: str | None) -> None:
    """Ask the next missing question, or propose the plan when nothing is missing."""
    p = d.setdefault("params", {})
    kind = goal.kind
    asked = d.get("asked")

    if user_text is not None and d.get("plan") and asked is None:
        # after a proposal: let them change one thing
        t = user_text.lower()
        for key, words in (("pace_pages_per_hour", ["pace", "faster", "slower", "pages an hour"]),
                           ("days", ["day", "weekend", "weekday"]), ("window", ["time", "morning", "evening", "afternoon"]),
                           ("minutes_per_day", ["longer", "shorter", "minutes", "session"]),
                           ("minutes", ["longer", "shorter", "minutes"]), ("minutes_per_session", ["longer", "shorter"])):
            if key in p and any(w in t for w in words):
                p.pop(key, None)
                d["plan"] = None
                break
        else:
            _say(db, user, goal, "assistant", "Tell me what to change: the days, the time of day, or how long each session is.",
                 quick_replies=["Change the days", "Change the time", "Change session length"])
            return

    if user_text is not None and asked:
        value = _parse_answer(asked, user_text, p)
        if asked == "book_confirm":
            if re.search(r"\b(yes|yeah|yep|that's it|correct|right)\b", user_text.lower()):
                p["pages"] = d["facts"]["book"]["pages"]
            elif (n := parse_int(user_text)) and n > 20:
                p["pages"] = n
        elif value is None:
            q, quick = QUESTIONS[asked]
            _say(db, user, goal, "assistant", f"Sorry, I didn't catch that. {q}", quick_replies=quick)
            return
        else:
            if asked == "start" and value == "flexible":
                value = "none"
            p[asked] = value
        d["asked"] = None

    if kind == "general" and not p.get("steps"):
        p["steps"] = ai_decompose.fallback_breakdown(goal.title)
    missing = missing_params(kind, p, goal)
    if kind == "book" and "pages" in missing and d.get("facts", {}).get("book") and not d.get("confirmed_book"):
        b = d["facts"]["book"]
        d["asked"], d["confirmed_book"] = "book_confirm", True
        _say(db, user, goal, "assistant",
             f"I found {b['title']}{' by ' + b['author'] if b['author'] else ''}: about {b['pages']} pages. "
             "Is that your book? If your copy is different, just tell me its page count.",
             quick_replies=["Yes, that's it", "Mine has a different page count"])
        return
    if missing:
        key = missing[0]
        d["asked"] = key
        q, quick = QUESTIONS[key]
        _say(db, user, goal, "assistant", q, quick_replies=quick)
        return
    _propose(db, user, goal, d)


def _propose(db, user, goal, d):
    plan = build_plan(goal.kind, d.get("params", {}), goal, user_today(user), d.get("facts", {}))
    d["plan"], d["asked"] = plan, None
    text = "Here's a plan that fits your week." + (" It runs past your deadline, so you may want longer sessions or more days."
                                                  if plan.get("late") else " Add it, or tell me what to change.")
    _say(db, user, goal, "assistant", text, plan=plan,
         quick_replies=["Make sessions longer", "Change the days", "Change the time"])


# ======================= the AI engine =======================

AI_GUIDE = """You are the planning assistant inside a personal goal planner. You help the user turn
one goal into a realistic plan by asking short follow-up questions, then you fill in plan parameters.
The app computes the actual schedule from your parameters, so never compute dates or page ranges yourself.

Rules:
- Ask at most 2 questions per reply, keep replies under 60 words, warm and plain. No markdown.
- Ask only what you need; use what you already know (the user's week, the goal, facts below).
- Never invent facts about a book (page count, chapters). If the goal is a book and no book facts are
  known yet, set "lookup_book" to a search query (title and author) and ask nothing else this turn.
  If facts are known, mention them and ask the user to confirm their edition's page count.
- Never suggest or discuss explicit, sexual or sensual content.
- Offer 2 to 4 short quick replies (max 28 characters each) the user can tap.
- When every parameter for the goal kind is known, set "ready": true and reply with a one-line intro to the plan.

Parameters per kind (days are 0=Mon..6=Sun; window is "morning", "afternoon", "evening" or "any"):
- book: pages, pace_pages_per_hour, days, window, and minutes_per_day (only if there's no deadline);
  add pages_per_day only if the user asks for a specific number of pages
- course: lessons, minutes_per_lesson, days, window
- exam: exam_date (YYYY-MM-DD, only if no deadline), topics (list of names, or a number), minutes_per_session, days, window
- fitness: activity, sessions (list of {"title": what they do, "days": [..]}), minutes, start ("HH:MM" if they go at a fixed time, or "none")
- habit: title (short, e.g. "Meditate"), minutes, days, window
- general: steps (list of {"title", "minutes", "energy": "high"|"medium"|"low"}), 4 to 10 steps

Return ONLY a JSON object, no markdown:
{"reply": str, "quick_replies": [str], "kind": "book|fitness|course|exam|habit|general",
 "lookup_book": str or null, "params": {only the parameters you know so far}, "ready": bool}"""


def _transcript(db, goal) -> str:
    rows = db.query(models.GoalMessage).filter(models.GoalMessage.goal_id == goal.id,
                                               models.GoalMessage.role != "tool") \
             .order_by(models.GoalMessage.id).all()
    return "\n".join(f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" for m in rows[-16:])


def _ai_prompt(db, user, goal, d) -> str:
    today = user_today(user)
    facts = d.get("facts", {})
    return f"""{AI_GUIDE}

Today: {today:%A %d %B %Y}
Goal: {goal.title}
Deadline: {goal.deadline or "none"}
Current goal kind guess: {goal.kind}
The user's regular week: {week_summary(db, user.id)}
Known facts: {json.dumps(facts) if facts else "none"}
Parameters known so far: {json.dumps(d.get("params", {}))}

Conversation so far:
{_transcript(db, goal) or "(nothing yet: open the conversation)"}"""


def _parse_ai(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise ValueError("no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or not str(data.get("reply", "")).strip():
        raise ValueError("no reply")
    return data


def _clean_quick(q) -> list[str]:
    return [str(x)[:28] for x in (q if isinstance(q, list) else []) if str(x).strip()][:4]


def ai_turn(db, user, goal, d: dict) -> None:
    for _ in range(2):    # at most one book lookup per turn
        data = _parse_ai(ai_decompose.complete(_ai_prompt(db, user, goal, d))[0])
        if data.get("kind") in KINDS and data["kind"] != goal.kind and goal.kind == "general":
            goal.kind = data["kind"]
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        d.setdefault("params", {}).update({k: v for k, v in params.items() if v not in (None, "", [])})
        q = data.get("lookup_book")
        if q and not d.get("facts", {}).get("book") and not d.get("looked_up"):
            d["looked_up"] = True
            _lookup(db, user, goal, d, str(q))
            continue
        reply = str(data["reply"]).strip()[:600]
        if not books.is_safe_text(reply):
            reply = "Let's keep going with your plan."
        if data.get("ready") and not missing_params(goal.kind, d["params"], goal):
            plan = build_plan(goal.kind, d["params"], goal, user_today(user), d.get("facts", {}))
            d["plan"] = plan
            _say(db, user, goal, "assistant", reply, plan=plan,
                 quick_replies=_clean_quick(data.get("quick_replies")) or ["Make sessions longer", "Change the days"])
        else:
            _say(db, user, goal, "assistant", reply, quick_replies=_clean_quick(data.get("quick_replies")))
        return
    _say(db, user, goal, "assistant", "Tell me a bit more about how you'd like to fit this in.")


def _lookup(db, user, goal, d, query: str):
    b = books.lookup_book(query)
    if b:
        d.setdefault("facts", {})["book"] = {k: b[k] for k in ("title", "author", "pages", "categories", "url", "source")}
        _say(db, user, goal, "tool", f"Found {b['title']}", card={
            "type": "book", "title": b["title"], "author": b["author"], "pages": b["pages"],
            "url": b["url"], "source": b["source"]})
    else:
        d.setdefault("facts", {})["book_not_found"] = query


# ======================= routes =======================

def _own_goal(db, user, goal_id) -> models.Goal:
    g = db.get(models.Goal, goal_id)
    if not g or g.user_id != user.id:
        raise HTTPException(404, "not found")
    return g


def _run_turn(db, user, goal, d, user_text):
    if d.get("engine") == "ai":
        try:
            ai_turn(db, user, goal, d)
            return
        except Exception as e:
            log.warning("AI chat failed, switching to built-in questions: %s", e)
            if not d.get("refunded"):           # the AI conversation didn't happen: give it back
                ai_decompose.refund_ai_call(db, user)
                d["refunded"] = True
            d["engine"] = "script"
            d["asked"] = None
            user_text = None          # don't re-parse their answer as a script answer
            if goal.kind == "book" and not d.get("facts", {}).get("book") and not d.get("looked_up"):
                d["looked_up"] = True
                _lookup(db, user, goal, d, goal.title)
    script_turn(db, user, goal, d, user_text)


@router.post("/goals/chat")
def start_chat(payload: schemas.GoalChatStart, user: models.User = Depends(current_user),
               db: Session = Depends(get_db)):
    if db.query(models.Goal).filter_by(user_id=user.id).count() >= MAX_GOALS_PER_USER:
        raise HTTPException(400, f"Limit reached for this demo ({MAX_GOALS_PER_USER}). Delete some first.")
    title = payload.title.strip()
    goal = models.Goal(user_id=user.id, title=title, deadline=payload.deadline, status="planning",
                       kind=detect_kind(title))
    db.add(goal)
    db.flush()
    allowed, reason = ai_decompose.reserve_ai_call(db, user)   # one AI conversation per goal
    d = {"engine": "ai" if allowed else "script", "params": {}, "facts": {}, "turns": 0}
    _say(db, user, goal, "user", title + (f" (by {payload.deadline:%d %b %Y})" if payload.deadline else ""))
    if not allowed and ai_decompose.ai_configured():
        d["note"] = reason
    if goal.kind == "book" and d["engine"] == "script":
        _lookup(db, user, goal, d, title)
    _run_turn(db, user, goal, d, None)
    _save(goal, d)
    db.commit()
    out = chat_json(db, goal)
    out["note"] = d.get("note")
    return out


@router.get("/goals/{goal_id}/chat")
def get_chat(goal_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    return chat_json(db, _own_goal(db, user, goal_id))


@router.post("/goals/{goal_id}/chat")
def send_message(goal_id: int, payload: schemas.ChatMessage, user: models.User = Depends(current_user),
                 db: Session = Depends(get_db)):
    goal = _own_goal(db, user, goal_id)
    if goal.status != "planning":
        raise HTTPException(400, "This goal is already planned.")
    d = _details(goal)
    d["turns"] = d.get("turns", 0) + 1
    if d["turns"] > 30:
        raise HTTPException(400, "That's a long chat. Add the plan, or start this goal again.")
    if d.get("engine") == "ai" and d["turns"] > MAX_AI_TURNS:
        d["engine"], d["asked"] = "script", None
    text = payload.message.strip()
    _say(db, user, goal, "user", text)
    db.flush()
    _run_turn(db, user, goal, d, text)
    _save(goal, d)
    db.commit()
    return chat_json(db, goal)


@router.post("/goals/{goal_id}/plan/accept")
def accept_plan(goal_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    goal = _own_goal(db, user, goal_id)
    d = _details(goal)
    if goal.status != "planning" or not d.get("plan"):
        raise HTTPException(400, "There's no plan to add yet.")
    apply_plan(db, user, goal, d["plan"])
    d["accepted"] = True
    _save(goal, d)
    db.commit()
    return {"ok": True, "tasks": len(d["plan"]["steps"]), "recurring": len(d["plan"]["recurring"]),
            "routines": len(d["plan"]["routines"])}


@router.post("/goals/{goal_id}/plan/quick")
def quick_plan(goal_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """Skip the questions: plan with sensible defaults for anything not answered yet."""
    goal = _own_goal(db, user, goal_id)
    if goal.status != "planning":
        raise HTTPException(400, "This goal is already planned.")
    d = _details(goal)
    defaults = {"pace_pages_per_hour": 20, "days": list(range(7)), "window": "any", "minutes_per_day": 30,
                "lessons": 10, "minutes_per_lesson": 30, "topics": 8, "minutes_per_session": 60,
                "activity": "Workout", "minutes": 45, "start": "none",
                "pages": (d.get("facts", {}).get("book") or {}).get("pages") or 250}
    p = d.setdefault("params", {})
    for k, v in defaults.items():
        p.setdefault(k, v)
    if goal.kind == "exam" and not goal.deadline:
        p.setdefault("exam_date", (user_today(user) + dt.timedelta(days=30)).isoformat())
    if goal.kind == "fitness":
        p.setdefault("sessions", [{"title": p["activity"], "days": [0, 2, 4]}])
    _propose(db, user, goal, d)
    _save(goal, d)
    db.commit()
    return chat_json(db, goal)


@router.get("/goals/{goal_id}/suggestions")
def goal_suggestions(goal_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    goal = _own_goal(db, user, goal_id)
    return suggestions_for(db, goal)


def suggestions_for(db: Session, goal: models.Goal) -> dict:
    """Safe books (for book goals) and verified Substacks; cached for a week."""
    d = _details(goal)
    week = dt.date.today().isocalendar()[:2]
    cached = d.get("suggestions")
    if cached and tuple(cached.get("week", [])) == tuple(week):
        return cached
    book = (d.get("facts") or {}).get("book")
    out = {"week": list(week), "books": [], "newsletters": []}
    if goal.kind == "book" and book:
        out["books"] = books.similar_books(book)
        topic = ", ".join(book.get("categories", [])[:2]) or book.get("title", "")
    else:
        topic = goal.title
    if goal.kind in ("book", "course", "exam", "fitness", "habit"):
        out["newsletters"] = books.newsletters(topic)
    d["suggestions"] = out
    _save(goal, d)
    db.commit()
    return out

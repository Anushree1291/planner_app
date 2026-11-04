import datetime as dt
import json
import re

from app import books, goal_chat, jobs, models, emailer, ai_decompose, routines
from app.database import SessionLocal
from tests.conftest import make_user

H = {"X-Cron-Secret": "cron-test-secret"}


def next_weekday(wd: int, start: dt.date | None = None) -> dt.date:
    d = (start or dt.date.today()) + dt.timedelta(days=1)
    while d.weekday() != wd:
        d += dt.timedelta(days=1)
    return d


def busy(client, date):
    return [(b["title"], b["start"]) for b in client.get(f"/calendar?date={date}").json()]


# ======================= the week =======================

def test_office_on_weekdays_and_2nd_4th_saturdays(user_a):
    user_a.post("/routines", json={"title": "Office", "kind": "work", "weekdays": [0, 1, 2, 3, 4],
                                   "start_time": "09:00", "end_time": "18:00"})
    user_a.post("/routines", json={"title": "Office", "kind": "work", "weekdays": [5], "weeks_of_month": [2, 4],
                                   "start_time": "10:00", "end_time": "14:00"})
    monday = next_weekday(0)
    assert ("Office", "09:00") in busy(user_a, monday)
    assert busy(user_a, next_weekday(6)) == []                       # Sunday off
    sat = next_weekday(5)
    while routines.nth_weekday_of_month(sat) not in (1, 2):
        sat += dt.timedelta(days=7)
    first_or_second = routines.nth_weekday_of_month(sat)
    assert (("Office", "10:00") in busy(user_a, sat)) == (first_or_second == 2)
    texts = [r["days_text"] for r in user_a.get("/routines").json()]
    assert "weekdays" in texts and "Sat (2nd and 4th of the month)" in texts


def test_day_off_drops_work_but_keeps_gym(user_a):
    user_a.post("/routines", json={"title": "Office", "kind": "work", "weekdays": list(range(7)),
                                   "start_time": "09:00", "end_time": "18:00"})
    user_a.post("/routines", json={"title": "Gym", "kind": "gym", "weekdays": list(range(7)),
                                   "start_time": "19:00", "end_time": "20:00"})
    day = next_weekday(2)
    user_a.put(f"/days/{day}", json={"kind": "off", "note": "Leave"})
    assert busy(user_a, day) == [("Gym", "19:00")]
    assert user_a.get("/days").json()[0]["date"] == day.isoformat()
    user_a.delete(f"/days/{day}")
    assert len(busy(user_a, day)) == 2


def test_capacity_counts_routines(user_a):
    day = next_weekday(1)
    before = user_a.get(f"/capacity?date={day}").json()["free_minutes"]
    user_a.post("/routines", json={"title": "Office", "kind": "work", "weekdays": [day.weekday()],
                                   "start_time": "09:00", "end_time": "17:00"})
    assert user_a.get(f"/capacity?date={day}").json()["free_minutes"] == before - 8 * 60


def test_gym_only_when_i_go(user_a):
    rid = user_a.post("/routines", json={"title": "Gym", "kind": "gym", "auto": False,
                                         "start_time": "18:00", "end_time": "19:00"}).json()["id"]
    today = user_a.get("/auth/me").json()  # noqa: just to be logged in
    day = user_a.get(f"/days/{dt.date.today()}").json()
    assert day["on_demand"][0]["title"] == "Gym" and not day["on_demand"][0]["going"]
    user_a.post(f"/routines/{rid}/going")
    titles = [b["title"] for b in user_a.get("/calendar").json()]
    assert "Gym" in titles


def test_skip_a_routine_today(user_a):
    wd = (dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))).weekday()
    rid = user_a.post("/routines", json={"title": "Class", "kind": "class", "weekdays": [wd],
                                         "start_time": "10:00", "end_time": "12:00"}).json()["id"]
    assert any(b["title"] == "Class" for b in user_a.get("/calendar").json())
    user_a.post(f"/routines/{rid}/skip")
    assert not any(b["title"] == "Class" for b in user_a.get("/calendar").json())


def test_routine_from_a_one_off_block_replaces_it(user_a):
    day = next_weekday(3)
    bid = user_a.post("/calendar", json={"date": str(day), "start_time": "09:00", "end_time": "18:00",
                                         "title": "Office"}).json()["id"]
    user_a.post("/routines", json={"title": "Office", "kind": "work", "weekdays": [0, 1, 2, 3, 4],
                                   "start_time": "09:00", "end_time": "18:00", "replaces_block_id": bid})
    assert busy(user_a, day) == [("Office", "09:00")]       # one block, not two


def test_light_day_plans_at_most_90_minutes(user_a):
    day = next_weekday(4)
    for i in range(6):
        user_a.post("/tasks", json={"title": f"T{i}", "estimated_minutes": 60})
    user_a.put(f"/days/{day}", json={"kind": "light"})
    user_a.post(f"/plan/generate?date={day}")
    plan = user_a.get(f"/plan?date={day}").json()
    total = sum(int(p["end"][:2]) * 60 + int(p["end"][3:]) - int(p["start"][:2]) * 60 - int(p["start"][3:]) for p in plan)
    assert 0 < total <= routines.LIGHT_DAY_MINUTES


def test_varying_days_get_a_checkin_not_a_plan(client, user_a, monkeypatch):
    from tests.test_app import _verify, _clock
    _verify(user_a)
    user_a.patch("/auth/me", json={"day_pattern": "varies"})
    user_a.post("/tasks", json={"title": "Write report"})
    assert user_a.get(f"/days/{dt.date.today()}").json()["needs_checkin"] is True
    _clock(monkeypatch, 10)
    emailer.OUTBOX.clear()
    stats = client.post("/internal/cron/hourly", headers=H).json()
    assert stats["checkins"] == 1 and stats["morning_plans"] == 0
    mail = emailer.OUTBOX[0]
    assert mail["subject"].startswith("How does your") and "Write report" in mail["html"]
    assert user_a.get("/plan").json() == []                   # not planned yet
    link = re.search(r"http://testserver(/day/checkin\?token=[\w\-\.]+)", mail["html"].split("light")[0] + mail["text"])
    light = re.search(r"Busy, keep it light: http://testserver(\S+)", mail["text"]).group(1)
    r = client.get(light, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?checkin=light"
    assert user_a.get(f"/days/{dt.date.today()}").json()["status"] == "light"
    assert client.get("/day/checkin?token=forged", follow_redirects=False).status_code == 400


def test_varying_days_dont_plan_tomorrow_on_skip(user_a):
    user_a.patch("/auth/me", json={"day_pattern": "varies"})
    t = user_a.post("/tasks", json={"title": "T"}).json()["id"]
    user_a.patch(f"/tasks/{t}", json={"status": "skipped"})
    tomorrow = dt.date.today() + dt.timedelta(days=2)
    assert user_a.get(f"/plan?date={tomorrow}").json() == []


def test_habits_repeat_and_missed_ones_lapse(user_a):
    db = SessionLocal()
    uid = db.query(models.User).filter_by(email="a@example.com").one().id
    g = models.Goal(user_id=uid, title="Meditate", kind="habit")
    db.add(g)
    db.flush()
    db.add(models.Recurring(user_id=uid, goal_id=g.id, title="Meditate", minutes=10, weekdays="0,1,2,3,4,5,6"))
    db.commit()
    db.close()
    d1, d2 = next_weekday(0), next_weekday(1)
    user_a.post(f"/plan/generate?date={d1}")
    user_a.post(f"/plan/generate?date={d2}")
    tasks = user_a.get("/tasks").json()
    assert len([t for t in tasks if t["title"] == "Meditate"]) == 2
    assert {t["status"] for t in tasks} == {"missed", "pending"}       # Monday's lapsed
    assert [p["task"] for p in user_a.get(f"/plan?date={d2}").json()] == ["Meditate"]


# ======================= goal chat: built-in questions =======================

def no_ai(monkeypatch):
    monkeypatch.setattr(ai_decompose.config, "AI_PROVIDERS", ["none"])


def fake_book(monkeypatch, pages=320, categories=("Self-Help",)):
    monkeypatch.setattr(books, "lookup_book", lambda q: {
        "title": "Atomic Habits", "author": "James Clear", "pages": pages, "categories": list(categories),
        "url": "https://books.example/atomic", "source": "Google Books", "description": "", "mature": False})


def say(client, gid, text):
    r = client.post(f"/goals/{gid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_book_chat_without_ai(user_a, monkeypatch):
    no_ai(monkeypatch)
    fake_book(monkeypatch)
    r = user_a.post("/goals/chat", json={"title": "Read Atomic Habits"}).json()
    gid = r["goal"]["id"]
    assert r["goal"]["kind"] == "book" and r["engine"] == "script"
    assert any(m.get("card", {}).get("pages") == 320 for m in r["messages"])
    assert "320 pages" in r["messages"][-1]["content"]
    say(user_a, gid, "Yes, that's it")
    say(user_a, gid, "20")                          # pages an hour
    say(user_a, gid, "Weekdays")
    say(user_a, gid, "30 min")
    out = say(user_a, gid, "Evening")
    plan = out["plan"]
    assert plan and plan["kind"] == "book" and plan["allowed_days"] == [0, 1, 2, 3, 4]
    assert plan["steps"][0]["title"] == "Read Atomic Habits: pages 1–10" and plan["steps"][0]["minutes"] == 30
    assert len(plan["steps"]) == 32 and plan["window"] == "evening"
    assert user_a.post(f"/goals/{gid}/plan/accept").json()["tasks"] == 32
    goal = [g for g in user_a.get("/goals").json() if g["id"] == gid][0]
    assert goal["status"] == "active" and goal["book"]["pages"] == 320
    # reading only happens on the chosen days, one session at a time, in the evening
    sat = next_weekday(5)
    user_a.post(f"/plan/generate?date={sat}")
    assert user_a.get(f"/plan?date={sat}").json() == []
    mon = next_weekday(0)
    user_a.post(f"/plan/generate?date={mon}")
    plan_mon = user_a.get(f"/plan?date={mon}").json()
    assert [p["task"] for p in plan_mon] == ["Read Atomic Habits: pages 1–10"] and plan_mon[0]["start"] >= "17:00"


def test_book_deadline_sets_pages_per_day(user_a, monkeypatch):
    no_ai(monkeypatch)
    fake_book(monkeypatch, pages=300)
    deadline = dt.date.today() + dt.timedelta(days=29)          # 30 days incl. today
    gid = user_a.post("/goals/chat", json={"title": "Read Atomic Habits", "deadline": str(deadline)}).json()["goal"]["id"]
    say(user_a, gid, "yes")
    say(user_a, gid, "Not sure")
    say(user_a, gid, "Every day")
    plan = say(user_a, gid, "Morning")["plan"]
    assert plan["steps"][0]["title"].endswith("pages 1–10") and not plan.get("late")


def test_unclear_answers_are_asked_again(user_a, monkeypatch):
    no_ai(monkeypatch)
    monkeypatch.setattr(books, "lookup_book", lambda q: None)
    gid = user_a.post("/goals/chat", json={"title": "Read my grandmother's diary"}).json()["goal"]["id"]
    out = say(user_a, gid, "a lot")
    assert out["messages"][-1]["content"].startswith("Sorry, I didn't catch that")


def test_gym_split_with_fixed_time_becomes_busy_time(user_a, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Get fit at the gym"}).json()["goal"]["id"]
    say(user_a, gid, "Gym")
    say(user_a, gid, "Mon legs, Wed upper body, Fri cardio")
    say(user_a, gid, "1 hour")
    plan = say(user_a, gid, "Yes, 18:00")["plan"]
    assert [r["title"] for r in plan["routines"]] == ["Gym: legs", "Gym: upper body", "Gym: cardio"]
    assert plan["routines"][0] == {"title": "Gym: legs", "kind": "gym", "weekdays": [0], "start": "18:00", "end": "19:00"}
    user_a.post(f"/goals/{gid}/plan/accept")
    assert ("Gym: legs", "18:00") in busy(user_a, next_weekday(0))
    assert busy(user_a, next_weekday(1)) == []


def test_flexible_workouts_become_repeating_sessions(user_a, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Start running"}).json()["goal"]["id"]
    say(user_a, gid, "Running")
    say(user_a, gid, "Same workout, 3 days")
    say(user_a, gid, "30 min")
    plan = say(user_a, gid, "No, fit it in")["plan"]
    assert plan["routines"] == [] and plan["recurring"][0]["weekdays"] == [0, 2, 4]


def test_exam_chat(user_a, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Pass the GRE exam"}).json()["goal"]["id"]
    assert user_a.get(f"/goals/{gid}/chat").json()["goal"]["kind"] == "exam"
    exam = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    say(user_a, gid, exam)
    say(user_a, gid, "Algebra, Geometry, Reading, Vocabulary, Essays")
    say(user_a, gid, "90 min")
    say(user_a, gid, "Weekends")
    plan = say(user_a, gid, "Morning")["plan"]
    titles = [s["title"] for s in plan["steps"]]
    assert titles[0] == "Study: Algebra" and titles[-1] == "Mock test 2 (timed)" and len(titles) == 9


def test_habit_and_adjusting_a_plan(user_a, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Meditate every morning"}).json()["goal"]["id"]
    say(user_a, gid, "10")
    say(user_a, gid, "Every day")
    plan = say(user_a, gid, "Morning")["plan"]
    assert plan["recurring"][0]["minutes"] == 10
    out = say(user_a, gid, "Change the days")
    assert out["plan"] is None and "Which days" in out["messages"][-1]["content"]
    plan = say(user_a, gid, "Weekdays")["plan"]
    assert plan["recurring"][0]["weekdays"] == [0, 1, 2, 3, 4]


def test_quick_plan_skips_questions(user_a, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Learn Spanish on Duolingo course"}).json()["goal"]["id"]
    plan = user_a.post(f"/goals/{gid}/plan/quick").json()["plan"]
    assert plan["kind"] == "course" and len(plan["steps"]) == 10


def test_other_users_cannot_read_a_chat(user_a, user_b, monkeypatch):
    no_ai(monkeypatch)
    gid = user_a.post("/goals/chat", json={"title": "Read a book"}).json()["goal"]["id"]
    assert user_b.get(f"/goals/{gid}/chat").status_code == 404
    assert user_b.post(f"/goals/{gid}/chat", json={"message": "hi"}).status_code == 404


# ======================= goal chat: with AI =======================

def test_ai_chat_looks_up_the_book_and_builds_the_plan(user_a, monkeypatch):
    fake_book(monkeypatch)
    replies = iter([
        {"reply": "", "lookup_book": "Atomic Habits James Clear", "kind": "book", "params": {}, "ready": False},
        {"reply": "Atomic Habits is about 320 pages. How many pages do you read in an hour?",
         "quick_replies": ["10", "20", "30"], "kind": "book", "params": {"pages": 320}, "ready": False},
        {"reply": "Great. Here's a plan.", "quick_replies": ["Looks good"], "kind": "book",
         "params": {"pace_pages_per_hour": 40, "days": [0, 1, 2, 3, 4, 5, 6], "window": "evening",
                    "minutes_per_day": 30}, "ready": True},
    ])
    prompts = []

    def fake_complete(prompt):
        prompts.append(prompt)
        r = next(replies)
        r["reply"] = r["reply"] or "Let me look that up."
        return json.dumps(r), "azure"
    monkeypatch.setattr(ai_decompose, "complete", fake_complete)
    out = user_a.post("/goals/chat", json={"title": "Read Atomic Habits"}).json()
    gid = out["goal"]["id"]
    assert out["engine"] == "ai" and "320 pages" in out["messages"][-1]["content"]
    assert "Atomic Habits" in prompts[1] and '"pages": 320' in prompts[1]      # facts passed back in
    plan = say(user_a, gid, "about 40")["plan"]
    assert plan["steps"][0]["title"] == "Read Atomic Habits: pages 1–20" and len(plan["steps"]) == 16
    assert user_a.get("/auth/me").json()["ai_calls_left_today"] == 1          # one per conversation


def test_ai_failure_falls_back_to_questions_and_refunds(user_a, monkeypatch):
    fake_book(monkeypatch)

    def boom(prompt):
        raise RuntimeError("down")
    monkeypatch.setattr(ai_decompose, "complete", boom)
    out = user_a.post("/goals/chat", json={"title": "Read Atomic Habits"}).json()
    assert out["engine"] == "script" and "Is that your book" in out["messages"][-1]["content"]
    assert user_a.get("/auth/me").json()["ai_calls_left_today"] == 2


def test_unsafe_ai_replies_are_replaced(user_a, monkeypatch):
    monkeypatch.setattr(ai_decompose, "complete", lambda p: (json.dumps(
        {"reply": "Try this steamy erotic novel", "kind": "general", "params": {}, "ready": False}), "azure"))
    out = user_a.post("/goals/chat", json={"title": "Relax more"}).json()
    assert "steamy" not in out["messages"][-1]["content"]


# ======================= suggestions =======================

class R:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.payload


def vol(title, cats, mature=False, desc=""):
    return {"volumeInfo": {"title": title, "authors": ["A"], "pageCount": 200, "categories": cats,
                           "maturityRating": "MATURE" if mature else "NOT_MATURE", "description": desc,
                           "infoLink": f"https://b.example/{title}"}}


def test_similar_books_are_filtered_for_safety(monkeypatch):
    items = [vol("Atomic Habits", ["Self-Help"]), vol("Deep Work", ["Self-Help"]),
             vol("Bad One", ["Self-Help"], mature=True), vol("Hearts", ["Fiction / Romance"]),
             vol("Night Vows", ["Self-Help"], desc="A steamy, sensual story"), vol("Make Time", ["Self-Help"]),
             vol("Indistractable", ["Self-Help"])]
    monkeypatch.setattr(books.requests, "get", lambda url, params=None, timeout=None, **k: R({"items": items}))
    out = books.similar_books({"title": "Atomic Habits", "categories": ["Self-Help"]})
    assert [b["title"] for b in out] == ["Deep Work", "Make Time", "Indistractable"]


def test_newsletters_must_exist_and_be_safe(monkeypatch):
    monkeypatch.setattr(ai_decompose, "complete", lambda p: (json.dumps([
        {"name": "Real One", "subdomain": "realone", "why": "Great habits writing"},
        {"name": "Made Up", "subdomain": "madeup", "why": "Doesn't exist"},
        {"name": "Spicy Stories", "subdomain": "spicy", "why": "sensual fiction"},
        {"name": "Bad Sub", "subdomain": "bad sub!", "why": "invalid"}]), "azure"))
    monkeypatch.setattr(books.requests, "get", lambda url, **k: R(status=200 if "realone" in url or "spicy" in url else 302))
    out = books.newsletters("Self-Help")
    assert out == [{"name": "Real One", "url": "https://realone.substack.com", "why": "Great habits writing"}]


def test_sunday_digest_email(client, user_a, monkeypatch):
    from tests.test_app import _verify, _clock
    no_ai(monkeypatch)
    fake_book(monkeypatch)
    _verify(user_a)
    gid = user_a.post("/goals/chat", json={"title": "Read Atomic Habits"}).json()["goal"]["id"]
    user_a.post(f"/goals/{gid}/plan/quick")
    user_a.post(f"/goals/{gid}/plan/accept")
    monkeypatch.setattr(books, "similar_books", lambda b, **k: [
        {"title": "Deep Work", "author": "Cal Newport", "pages": 300, "url": "https://b.example/dw", "description": "Focus."}])
    from app import auth
    sunday = next_weekday(6)
    monkeypatch.setattr(jobs, "user_now", lambda u: auth.user_now(u).replace(
        year=sunday.year, month=sunday.month, day=sunday.day, hour=10))
    emailer.OUTBOX.clear()
    stats = client.post("/internal/cron/hourly", headers=H).json()
    assert stats["digests"] == 1
    digest = [m for m in emailer.OUTBOX if "ideas for your week" in m["subject"]][0]
    assert "Deep Work" in digest["html"] and "Atomic Habits" in digest["html"]
    user_a.patch("/auth/me", json={"email_weekly_digest": False})
    assert user_a.get("/auth/me").json()["email_weekly_digest"] is False


def test_suggestions_endpoint_is_cached(user_a, monkeypatch):
    no_ai(monkeypatch)
    fake_book(monkeypatch)
    calls = []
    monkeypatch.setattr(books, "similar_books", lambda b, **k: calls.append(1) or [{"title": "X", "author": "", "pages": 1, "url": "", "description": ""}])
    gid = user_a.post("/goals/chat", json={"title": "Read Atomic Habits"}).json()["goal"]["id"]
    user_a.post(f"/goals/{gid}/plan/quick")
    user_a.post(f"/goals/{gid}/plan/accept")
    assert user_a.get(f"/goals/{gid}/suggestions").json()["books"][0]["title"] == "X"
    user_a.get(f"/goals/{gid}/suggestions")
    assert len(calls) == 1


def test_day_phrases():
    from app.goal_chat import parse_days, parse_time, parse_split
    assert parse_days("Weekdays and Sunday") == [0, 1, 2, 3, 4, 6]
    assert parse_days("tuesdays and thursdays") == [1, 3]
    assert parse_days("3 days a week") == [0, 2, 4]
    assert parse_days("whenever") is None
    assert parse_time("6 pm") == "18:00" and parse_time("Yes, 7:30") == "07:30"
    assert parse_split("Mon legs; Thu back and biceps") == [{"title": "legs", "days": [0]},
                                                            {"title": "back and biceps", "days": [3]}]

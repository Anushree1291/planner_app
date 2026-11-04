import datetime as dt
import json

from app import ai_decompose, models, jobs, emailer
from app.database import SessionLocal
from tests.conftest import make_user


def fake_llm(title, deadline):
    return ([{"title": f"Step {i} of {title}", "estimated_minutes": 30, "priority": 3,
              "energy_required": "medium"} for i in range(1, 6)], "azure")


# ---------- auth ----------

def test_protected_routes_require_login(client):
    for method, path in [("get", "/goals"), ("get", "/plan"), ("post", "/plan/generate"),
                         ("get", "/tasks"), ("get", "/auth/me")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_signup_login_logout(client):
    r = client.post("/auth/signup", json={"email": "Me@Example.com", "password": "password123"})
    assert r.status_code == 200 and r.json()["email"] == "me@example.com"
    assert client.get("/auth/me").status_code == 200
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "me@example.com",
                                            "password": "wrong-pass"}).status_code == 401
    assert client.post("/auth/login", json={"email": "ME@example.com",
                                            "password": "password123"}).status_code == 200


def test_duplicate_email_and_short_password(client):
    client.post("/auth/signup", json={"email": "x@example.com", "password": "password123"})
    assert client.post("/auth/signup", json={"email": "x@example.com",
                                             "password": "password123"}).status_code == 409
    assert client.post("/auth/signup", json={"email": "y@example.com",
                                             "password": "short"}).status_code == 422


def test_password_is_hashed(client):
    client.post("/auth/signup", json={"email": "h@example.com", "password": "password123"})
    db = SessionLocal()
    u = db.query(models.User).filter_by(email="h@example.com").one()
    assert u.password_hash.startswith("$2") and "password123" not in u.password_hash
    db.close()


def test_login_rate_limit(client):
    for _ in range(10):
        client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 8})
    r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 8})
    assert r.status_code == 429


# ---------- data isolation ----------

def test_users_cannot_see_or_touch_each_others_data(user_a, user_b):
    goal_id = user_a.post("/goals", json={"title": "A's secret goal"}).json()["id"]
    task_id = user_a.post("/tasks", json={"title": "A's task", "goal_id": goal_id}).json()["id"]
    block_id = user_a.post("/calendar", json={"date": "2030-01-01", "start_time": "09:00",
                                              "end_time": "10:00", "title": "A"}).json()["id"]

    assert user_b.get("/goals").json() == []
    assert user_b.get("/tasks").json() == []
    assert user_b.patch(f"/tasks/{task_id}", json={"status": "done"}).status_code == 404
    assert user_b.delete(f"/tasks/{task_id}").status_code == 404
    assert user_b.delete(f"/goals/{goal_id}").status_code == 404
    assert user_b.delete(f"/calendar/{block_id}").status_code == 404
    # B can't attach a task to A's goal either
    assert user_b.post("/tasks", json={"title": "x", "goal_id": goal_id}).status_code == 404
    assert user_a.get("/tasks").json()[0]["status"] == "pending"


def test_delete_account_removes_everything(user_a):
    user_a.post("/goals", json={"title": "G"})
    user_a.post("/tasks", json={"title": "T"})
    assert user_a.delete("/auth/me").status_code == 200
    db = SessionLocal()
    assert db.query(models.User).count() == 0
    assert db.query(models.Task).count() == 0
    assert db.query(models.Goal).count() == 0
    db.close()


# ---------- AI limits ----------

def test_ai_per_user_limit_then_template_fallback(user_a, monkeypatch):
    monkeypatch.setattr(ai_decompose, "call_llm", fake_llm)
    r1 = user_a.post("/goals", json={"title": "G1", "auto_decompose": True}).json()
    r2 = user_a.post("/goals", json={"title": "G2", "auto_decompose": True}).json()
    r3 = user_a.post("/goals", json={"title": "G3", "auto_decompose": True}).json()
    assert r1["source"] == r2["source"] == "azure"
    assert r3["source"] == "template" and "used your 2" in r3["note"]
    assert r3["tasks_created"] > 0
    me = user_a.get("/auth/me").json()
    assert me["ai_calls_left_today"] == 0 and me["ai_providers"] == ["azure", "gemini"]


def test_ai_global_limit(monkeypatch):
    monkeypatch.setattr(ai_decompose, "call_llm", fake_llm)
    a, b = make_user("g1@example.com"), make_user("g2@example.com")
    assert a.post("/goals", json={"title": "1", "auto_decompose": True}).json()["source"] == "azure"
    assert a.post("/goals", json={"title": "2", "auto_decompose": True}).json()["source"] == "azure"
    assert b.post("/goals", json={"title": "3", "auto_decompose": True}).json()["source"] == "azure"
    r = b.post("/goals", json={"title": "4", "auto_decompose": True}).json()
    assert r["source"] == "template" and "daily AI budget" in r["note"]


def test_ai_failure_is_refunded(user_a, monkeypatch):
    def boom(*_):
        raise RuntimeError("API down")
    monkeypatch.setattr(ai_decompose, "call_llm", boom)
    r = user_a.post("/goals", json={"title": "G", "auto_decompose": True}).json()
    assert r["source"] == "template"
    assert user_a.get("/auth/me").json()["ai_calls_left_today"] == 2


def test_no_provider_configured(user_a, monkeypatch):
    monkeypatch.setattr(ai_decompose.config, "AI_PROVIDERS", ["none"])
    me = user_a.get("/auth/me").json()
    assert me["ai_daily_limit"] == 0 and me["ai_providers"] == []
    r = user_a.post("/goals", json={"title": "G", "auto_decompose": True}).json()
    assert r["source"] == "template" and "not configured" in r["note"]


def test_model_output_is_sanitised():
    out = ai_decompose._clean([{"title": "x" * 500, "estimated_minutes": 99999,
                                "priority": 42, "energy_required": "turbo"},
                               "garbage", {"no_title": 1}] + [{"title": "t"}] * 30)
    assert len(out) == ai_decompose.MAX_TASKS
    assert len(out[0]["title"]) == 200 and out[0]["estimated_minutes"] == 600
    assert out[0]["priority"] == 5 and out[0]["energy_required"] == "medium"


# ---------- AI providers (HTTP mocked) ----------

class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


TASKS_JSON = json.dumps([{"title": "Read docs", "estimated_minutes": 40, "priority": 4,
                          "energy_required": "high"}])


def test_azure_request_shape(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return FakeResp({"choices": [{"message": {"content": TASKS_JSON}}]})
    monkeypatch.setattr(ai_decompose.requests, "post", fake_post)
    tasks, provider = ai_decompose.call_llm("Learn Rust", None)
    url, headers, body = calls[0]
    assert provider == "azure" and tasks[0]["title"] == "Read docs"
    assert url == "https://test-resource.openai.azure.com/openai/v1/chat/completions"
    assert headers["api-key"] == "azure-test-key" and body["model"] == "gpt-4o-mini"


def test_gemini_request_shape_and_thought_parts(monkeypatch):
    monkeypatch.setattr(ai_decompose.config, "AI_PROVIDERS", ["gemini"])
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return FakeResp({"candidates": [{"content": {"parts": [
            {"text": "thinking about it...", "thought": True}, {"text": TASKS_JSON}]}}]})
    monkeypatch.setattr(ai_decompose.requests, "post", fake_post)
    tasks, provider = ai_decompose.call_llm("Learn Rust", "2030-01-01")
    url, headers, body = calls[0]
    assert provider == "gemini" and len(tasks) == 1
    assert url.endswith("/models/gemini-flash-latest:generateContent")
    assert headers["x-goog-api-key"] == "gemini-test-key"
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_falls_back_from_azure_to_gemini(monkeypatch):
    def fake_post(url, headers, json, timeout):
        if "azure" in url:
            return FakeResp({"error": "quota"}, status=429)
        return FakeResp({"candidates": [{"content": {"parts": [{"text": TASKS_JSON}]}}]})
    monkeypatch.setattr(ai_decompose.requests, "post", fake_post)
    tasks, provider = ai_decompose.call_llm("Learn Rust", None)
    assert provider == "gemini" and tasks[0]["title"] == "Read docs"


def test_azure_endpoint_formats():
    want = "https://r.openai.azure.com/openai/v1/chat/completions"
    for given in ["https://r.openai.azure.com/", "https://r.openai.azure.com",
                  "https://r.openai.azure.com/openai/v1/", "r.openai.azure.com",
                  "https://r.openai.azure.com/openai/deployments/x/chat/completions?api-version=1"]:
        got = ai_decompose.azure_chat_url(given)
        assert got in (want, "https://r.openai.azure.com/openai/deployments/x/chat/completions"), given
    assert ai_decompose.azure_chat_url("https://r.services.ai.azure.com/api/projects/p1") == \
        "https://r.services.ai.azure.com/openai/v1/chat/completions"


def test_parses_messy_model_replies():
    item = '{"title": "A", "estimated_minutes": 10}'
    for reply in [f"[{item}]", f"```json\n[{item}]\n```", f"Sure! Here you go:\n[{item}]\nGood luck",
                  f"<think>maybe [x]?</think>[{item}]", f'{{"tasks": [{item}]}}']:
        assert ai_decompose.parse_tasks(reply)[0]["title"] == "A", reply


# ---------- planner ----------

FUTURE = "2030-06-03"


def test_plan_respects_calendar_and_capacity(user_a):
    user_a.post("/calendar", json={"date": FUTURE, "start_time": "07:00", "end_time": "22:00",
                                   "title": "Busy all day"})
    for i in range(5):
        user_a.post("/tasks", json={"title": f"T{i}", "estimated_minutes": 30})
    assert user_a.get(f"/capacity?date={FUTURE}").json()["free_minutes"] == 60
    user_a.post(f"/plan/generate?date={FUTURE}")
    plan = user_a.get(f"/plan?date={FUTURE}").json()
    assert plan and all(p["start"] >= "22:00" for p in plan)


def test_regenerating_plan_does_not_shrink_estimates(user_a):
    user_a.post("/calendar", json={"date": FUTURE, "start_time": "07:45", "end_time": "23:00",
                                   "title": "busy"})
    user_a.post("/tasks", json={"title": "Big", "estimated_minutes": 120})
    for _ in range(3):
        user_a.post(f"/plan/generate?date={FUTURE}")
    assert user_a.get("/tasks").json()[0]["estimated_minutes"] == 120


def test_skip_bumps_priority_and_done_sticks(user_a):
    t1 = user_a.post("/tasks", json={"title": "T1"}).json()["id"]
    t2 = user_a.post("/tasks", json={"title": "T2"}).json()["id"]
    assert user_a.patch(f"/tasks/{t1}", json={"status": "skipped"}).json()["replanned"]
    tasks = {t["id"]: t for t in user_a.get("/tasks").json()}
    assert tasks[t1]["postponed_count"] == 1 and tasks[t1]["status"] == "pending"
    user_a.patch(f"/tasks/{t2}", json={"status": "done", "actual_minutes": 60})
    assert user_a.get("/tasks?status=done").json()[0]["id"] == t2


def test_dependencies_block_scheduling(user_a):
    first = user_a.post("/tasks", json={"title": "First"}).json()["id"]
    user_a.post("/tasks", json={"title": "Second", "depends_on_id": first})
    user_a.post(f"/plan/generate?date={FUTURE}")
    titles = [p["task"] for p in user_a.get(f"/plan?date={FUTURE}").json()]
    assert "First" in titles and "Second" not in titles


def test_input_validation(user_a):
    bad_block = {"date": FUTURE, "start_time": "10:00", "end_time": "09:00", "title": "x"}
    assert user_a.post("/calendar", json=bad_block).status_code == 422
    assert user_a.post("/tasks", json={"title": "x", "energy_required": "ultra"}).status_code == 422
    assert user_a.post("/tasks", json={"title": "x", "priority": 9}).status_code == 422


# ---------- demo + cron ----------

def test_demo_sandbox_has_sample_data(client):
    r = client.post("/auth/demo", json={"timezone": "Asia/Kolkata"})
    assert r.status_code == 200 and r.json()["is_guest"]
    assert len(client.get("/goals").json()) == 3
    assert client.get("/auth/me").json()["ai_daily_limit"] == 1


def test_cron_requires_secret(client):
    assert client.post("/internal/cron/hourly").status_code == 401
    assert client.post("/internal/cron/hourly",
                       headers={"X-Cron-Secret": "nope"}).status_code == 401
    assert client.post("/internal/cron/hourly",
                       headers={"X-Cron-Secret": "cron-test-secret"}).status_code == 200


def _clock(monkeypatch, hour):
    """Pretend it's `hour` o'clock today in every user's timezone."""
    from app import auth
    monkeypatch.setattr(jobs, "user_now", lambda u: auth.user_now(u).replace(hour=hour, minute=0))


def _verify(client, outbox_index=-1):
    """Follow the confirm link from the latest email, like a user clicking it."""
    import re
    link = re.search(r"http://testserver(/email/verify\?token=[\w\-\.]+)",
                     emailer.OUTBOX[outbox_index]["text"]).group(1)
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?verified=1"


def test_cron_is_idempotent_and_cleans_guests(client, user_a, monkeypatch):
    _clock(monkeypatch, 21)
    _verify(user_a)
    user_a.post("/tasks", json={"title": "T"})
    client.post("/auth/demo", json={})
    db = SessionLocal()
    guest = db.query(models.User).filter_by(is_guest=True).one()
    guest.created_at = models.utcnow() - dt.timedelta(hours=48)
    db.commit()
    db.close()
    emailer.OUTBOX.clear()

    h = {"X-Cron-Secret": "cron-test-secret"}
    first = client.post("/internal/cron/hourly", headers=h).json()
    second = client.post("/internal/cron/hourly", headers=h).json()
    assert first["morning_plans"] == 1 and first["guests_deleted"] == 1
    assert second["morning_plans"] == 0 and second["risk_checks"] == 0
    plans = [m for m in emailer.OUTBOX if "Your " in m["subject"]]
    assert len(plans) == 1 and plans[0]["to"] == "a@example.com"


# ---------- email ----------

def test_signup_sends_welcome_with_confirm_link(client):
    client.post("/auth/signup", json={"email": "new@example.com", "password": "password123",
                                      "name": "Anushree K"})
    assert len(emailer.OUTBOX) == 1
    mail = emailer.OUTBOX[0]
    assert mail["to"] == "new@example.com" and "Welcome, Anushree" in mail["subject"]
    assert client.get("/auth/me").json()["email_verified"] is False
    _verify(client)
    assert client.get("/auth/me").json()["email_verified"] is True


def test_unverified_users_get_no_scheduled_mail(client, user_a, monkeypatch):
    _clock(monkeypatch, 10)
    user_a.post("/tasks", json={"title": "T"})
    emailer.OUTBOX.clear()
    stats = client.post("/internal/cron/hourly", headers={"X-Cron-Secret": "cron-test-secret"}).json()
    assert stats["morning_plans"] == 1 and stats["emails_sent"] == 0 and emailer.OUTBOX == []
    assert user_a.post("/email/send-plan").status_code == 400


def test_daily_plan_email_is_personal_and_escaped(user_a):
    _verify(user_a)
    user_a.patch("/auth/me", json={"name": "Anushree"})
    user_a.post("/tasks", json={"title": "<script>alert(1)</script> Write essay",
                                "energy_required": "high"})
    user_a.post("/plan/generate")
    emailer.OUTBOX.clear()
    r = user_a.post("/email/send-plan")
    assert r.status_code == 200 and r.json()["to"] == "a@example.com"
    mail = emailer.OUTBOX[0]
    assert "Anushree" in mail["html"] and "&lt;script&gt;" in mail["html"]
    assert "<script>" not in mail["html"] and "/email/unsubscribe?token=" in mail["unsubscribe_url"]
    preview = user_a.get("/email/preview")
    assert preview.status_code == 200 and "Anushree" in preview.text


def test_send_plan_now_is_rate_limited(user_a):
    _verify(user_a)
    codes = [user_a.post("/email/send-plan").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_unsubscribe_needs_a_click_and_works_without_login(client, user_a):
    _verify(user_a)
    user_a.post("/email/send-plan")
    url = emailer.OUTBOX[-1]["unsubscribe_url"].replace("http://testserver", "")
    assert client.get(url).status_code == 200                 # page with a button
    assert user_a.get("/auth/me").json()["email_morning_plan"] is True
    assert client.post(url).status_code == 200                # the click
    me = user_a.get("/auth/me").json()
    assert me["email_morning_plan"] is False and me["email_risk_alerts"] is False
    assert client.get("/email/unsubscribe?token=forged").status_code == 400


def test_email_preferences_and_risk_alert(client, user_a, monkeypatch):
    _verify(user_a)
    user_a.patch("/auth/me", json={"email_morning_plan": False})
    gid = user_a.post("/goals", json={"title": "Thesis", "deadline":
                      (dt.date.today() + dt.timedelta(days=2)).isoformat()}).json()["id"]
    for i in range(6):
        user_a.post("/tasks", json={"title": f"Chapter {i}", "goal_id": gid, "estimated_minutes": 240})
    _clock(monkeypatch, 21)
    emailer.OUTBOX.clear()
    stats = client.post("/internal/cron/hourly", headers={"X-Cron-Secret": "cron-test-secret"}).json()
    assert stats["emails_sent"] == 1
    assert emailer.OUTBOX[0]["subject"].startswith("Heads-up: Thesis")


def test_email_budget_defers_extra_users(client, monkeypatch):
    """With an hourly limit of 2, the third person waits for a later run."""
    from app import config
    _clock(monkeypatch, 10)
    monkeypatch.setattr(config, "EMAIL_RATE_PER_HOUR", 2)
    users = [make_user(f"u{i}@example.com") for i in range(3)]
    for i, u in enumerate(users):
        _verify(u, outbox_index=i)
    db = SessionLocal()       # confirm emails count toward the limit; start the hour fresh
    db.query(models.EmailSend).delete()
    db.commit()
    db.close()
    emailer.OUTBOX.clear()
    h = {"X-Cron-Secret": "cron-test-secret"}
    first = client.post("/internal/cron/hourly", headers=h).json()
    assert first["emails_sent"] == 2 and first["deferred"] == 1
    second = client.post("/internal/cron/hourly", headers=h).json()
    assert second["emails_sent"] == 0 and second["deferred"] == 1     # still over the hour
    assert len(emailer.OUTBOX) == 2


def test_bulk_send_runs_in_parallel_batches(client, monkeypatch):
    from app import config
    _clock(monkeypatch, 10)
    monkeypatch.setattr(config, "EMAIL_RATE_PER_MINUTE", 1000)
    monkeypatch.setattr(config, "EMAIL_RATE_PER_HOUR", 1000)
    monkeypatch.setattr(config, "EMAIL_CONCURRENCY", 8)
    from app import auth
    users = []
    for i in range(12):
        auth.signup_limiter.reset()           # 12 sign-ups from one test IP
        users.append(make_user(f"bulk{i}@example.com"))
    for i, u in enumerate(users):
        _verify(u, outbox_index=i)
    emailer.OUTBOX.clear()
    stats = client.post("/internal/cron/hourly", headers={"X-Cron-Secret": "cron-test-secret"}).json()
    assert stats["emails_sent"] == 12 and stats["deferred"] == 0
    assert len({m["to"] for m in emailer.OUTBOX}) == 12


def test_throttled_sends_are_retried_next_run(client, user_a, monkeypatch):
    _clock(monkeypatch, 10)
    _verify(user_a)
    monkeypatch.setattr(jobs, "send_email", lambda *a, **k: "throttled")
    h = {"X-Cron-Secret": "cron-test-secret"}
    first = client.post("/internal/cron/hourly", headers=h).json()
    assert first["throttled"] and first["deferred"] == 1 and first["emails_sent"] == 0
    monkeypatch.setattr(jobs, "send_email", emailer.send_email)
    emailer.OUTBOX.clear()
    second = client.post("/internal/cron/hourly", headers=h).json()
    assert second["emails_sent"] == 1 and len(emailer.OUTBOX) == 1


def test_users_choose_their_morning_hour(client, user_a, monkeypatch):
    _verify(user_a)
    assert user_a.patch("/auth/me", json={"email_morning_hour": 8}).json()["email_morning_hour"] == 8
    assert user_a.patch("/auth/me", json={"email_morning_hour": 3}).status_code == 422
    h = {"X-Cron-Secret": "cron-test-secret"}
    emailer.OUTBOX.clear()
    _clock(monkeypatch, 7)
    assert client.post("/internal/cron/hourly", headers=h).json()["emails_sent"] == 0
    _clock(monkeypatch, 8)
    assert client.post("/internal/cron/hourly", headers=h).json()["emails_sent"] == 1


def test_bounce_reports_stop_scheduled_mail(client, user_a, monkeypatch):
    from app import mailing
    _verify(user_a)
    monkeypatch.setattr(mailing, "EMAIL_EVENTS_KEY", "events-key")
    assert client.post("/email/events?key=wrong", json=[]).status_code == 401
    r = client.post("/email/events?key=events-key", json=[{
        "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "abc"}}])
    assert r.json() == {"validationResponse": "abc"}
    r = client.post("/email/events?key=events-key", json=[{
        "eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
        "data": {"recipient": "A@example.com", "status": "Bounced"}}])
    assert r.json()["stopped"] == 1
    assert user_a.get("/auth/me").json()["email_bounced"] is True
    _clock(monkeypatch, 10)
    emailer.OUTBOX.clear()
    client.post("/internal/cron/hourly", headers={"X-Cron-Secret": "cron-test-secret"})
    assert emailer.OUTBOX == []
    user_a.post("/email/retry")
    assert user_a.get("/auth/me").json()["email_bounced"] is False


def test_demo_users_cannot_use_email(client):
    client.post("/auth/demo", json={})
    assert client.post("/email/send-plan").status_code == 400
    assert client.post("/email/resend-verification").status_code == 400


def test_adds_new_columns_to_an_old_database(tmp_path):
    """Upgrading a database created by an older version adds the email columns."""
    import sqlalchemy as sa
    from app import database
    old = sa.create_engine(f"sqlite:///{tmp_path}/old.db")
    with old.begin() as c:
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(254), ntfy_topic VARCHAR(64))"))
        c.execute(sa.text("INSERT INTO users (id, email) VALUES (1, 'old@example.com')"))
    real = database.engine
    database.engine = old
    try:
        database.add_missing_columns()
    finally:
        database.engine = real
    with old.connect() as c:
        row = c.execute(sa.text("SELECT email_verified, email_morning_plan FROM users")).one()
    assert tuple(bool(v) for v in row) == (False, True)


def test_healthz_and_dashboard(client):
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200 and "AI Execution Planner" in r.text


def test_smtp_message_is_built_correctly(monkeypatch):
    """The SMTP path builds a proper multipart message with unsubscribe headers."""
    from app import config
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent["host"] = (host, port)
        def starttls(self, context): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    for k, v in dict(EMAIL_PROVIDER="smtp", SMTP_HOST="smtp.gmail.com", SMTP_PORT=587,
                     SMTP_USERNAME="me@gmail.com", SMTP_PASSWORD="app-pass", EMAIL_FROM="").items():
        monkeypatch.setattr(config, k, v)
    assert emailer.send_email("you@example.com", "Hi", "<b>hi</b>", "hi", "https://x/unsub")
    msg = sent["msg"]
    assert sent["tls"] and sent["login"] == ("me@gmail.com", "app-pass")
    assert msg["To"] == "you@example.com" and "me@gmail.com" in msg["From"]
    assert msg["List-Unsubscribe"] == "<https://x/unsub>" and msg.is_multipart()


def test_azure_429_counts_as_throttled(monkeypatch):
    from azure.core.exceptions import HttpResponseError
    from app import config

    class Client:
        def begin_send(self, message):
            e = HttpResponseError("Too Many Requests")
            e.status_code = 429
            raise e
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "acs")
    monkeypatch.setattr(config, "ACS_CONNECTION_STRING", "endpoint=https://x.communication.azure.com/;accesskey=k")
    monkeypatch.setattr(config, "EMAIL_FROM", "DoNotReply@mail.example.com")
    monkeypatch.setattr(emailer, "_acs", lambda: Client())
    assert emailer.send_email("you@example.com", "s", "<p>h</p>", "t") == "throttled"


def test_overlapping_runs_are_refused(client):
    from app import main
    main._cron_lock.acquire()
    try:
        r = client.post("/internal/cron/hourly", headers={"X-Cron-Secret": "cron-test-secret"})
        assert r.json() == {"busy": True}
    finally:
        main._cron_lock.release()

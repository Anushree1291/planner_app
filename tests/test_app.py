import datetime as dt

from app import ai_decompose, models, jobs
from app.database import SessionLocal
from tests.conftest import make_user


def fake_claude(title, deadline):
    return [{"title": f"Step {i} of {title}", "estimated_minutes": 30, "priority": 3,
             "energy_required": "medium"} for i in range(1, 6)]


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
    monkeypatch.setattr(ai_decompose, "call_claude", fake_claude)
    r1 = user_a.post("/goals", json={"title": "G1", "auto_decompose": True}).json()
    r2 = user_a.post("/goals", json={"title": "G2", "auto_decompose": True}).json()
    r3 = user_a.post("/goals", json={"title": "G3", "auto_decompose": True}).json()
    assert r1["source"] == r2["source"] == "ai"
    assert r3["source"] == "template" and "used your 2" in r3["note"]
    assert r3["tasks_created"] > 0
    assert user_a.get("/auth/me").json()["ai_calls_left_today"] == 0


def test_ai_global_limit(monkeypatch):
    monkeypatch.setattr(ai_decompose, "call_claude", fake_claude)
    a, b = make_user("g1@example.com"), make_user("g2@example.com")
    assert a.post("/goals", json={"title": "1", "auto_decompose": True}).json()["source"] == "ai"
    assert a.post("/goals", json={"title": "2", "auto_decompose": True}).json()["source"] == "ai"
    assert b.post("/goals", json={"title": "3", "auto_decompose": True}).json()["source"] == "ai"
    r = b.post("/goals", json={"title": "4", "auto_decompose": True}).json()
    assert r["source"] == "template" and "daily AI budget" in r["note"]


def test_ai_failure_is_refunded(user_a, monkeypatch):
    def boom(*_):
        raise RuntimeError("API down")
    monkeypatch.setattr(ai_decompose, "call_claude", boom)
    r = user_a.post("/goals", json={"title": "G", "auto_decompose": True}).json()
    assert r["source"] == "template"
    assert user_a.get("/auth/me").json()["ai_calls_left_today"] == 2


def test_model_output_is_sanitised():
    out = ai_decompose._clean([{"title": "x" * 500, "estimated_minutes": 99999,
                                "priority": 42, "energy_required": "turbo"},
                               "garbage", {"no_title": 1}] + [{"title": "t"}] * 30)
    assert len(out) == ai_decompose.MAX_TASKS
    assert len(out[0]["title"]) == 200 and out[0]["estimated_minutes"] == 600
    assert out[0]["priority"] == 5 and out[0]["energy_required"] == "medium"


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
    assert user_a.post("/setup/notifications", json={"topic": "a b"}).status_code == 422


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


def test_cron_is_idempotent_and_cleans_guests(client, user_a, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs.notifications, "send_notification",
                        lambda topic, title, msg, **k: sent.append(title))
    monkeypatch.setattr(jobs, "MORNING_HOUR", 0)
    monkeypatch.setattr(jobs, "RISK_CHECK_HOUR", 0)
    user_a.post("/setup/notifications", json={"topic": "anu-planner-test-8f21"})
    user_a.post("/tasks", json={"title": "T"})
    client.post("/auth/demo", json={})
    db = SessionLocal()
    guest = db.query(models.User).filter_by(is_guest=True).one()
    guest.created_at = models.utcnow() - dt.timedelta(hours=48)
    db.commit()
    db.close()

    h = {"X-Cron-Secret": "cron-test-secret"}
    first = client.post("/internal/cron/hourly", headers=h).json()
    second = client.post("/internal/cron/hourly", headers=h).json()
    assert first["morning_plans"] == 1 and first["guests_deleted"] == 1
    assert second["morning_plans"] == 0 and second["risk_checks"] == 0
    assert sent.count("Today's plan is ready") == 1


def test_healthz_and_dashboard(client):
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200 and "AI Execution Planner" in r.text

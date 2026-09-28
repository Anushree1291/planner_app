import os
import sys
import tempfile
from pathlib import Path

# Isolated SQLite DB + deterministic settings, set before the app is imported.
_tmp = tempfile.mkdtemp()
# Tests DROP all tables, so they never read DATABASE_URL (it might point at production).
# To run them against Postgres, set TEST_DATABASE_URL explicitly.
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["CRON_SECRET"] = "cron-test-secret"
os.environ["ENABLE_LOCAL_SCHEDULER"] = "false"
os.environ["ANTHROPIC_API_KEY"] = "sk-test"          # AI "configured"; calls are mocked
os.environ["AI_DAILY_LIMIT_PER_USER"] = "2"
os.environ["AI_DAILY_LIMIT_PER_GUEST"] = "1"
os.environ["AI_DAILY_LIMIT_GLOBAL"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, engine
from app import auth


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    for lim in (auth.login_limiter, auth.signup_limiter, auth.demo_limiter):
        lim.reset()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_user(email="a@example.com", password="password123", tz="Asia/Kolkata"):
    c = TestClient(app)
    r = c.post("/auth/signup", json={"email": email, "password": password, "timezone": tz})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def user_a():
    return make_user("a@example.com")


@pytest.fixture
def user_b():
    return make_user("b@example.com")

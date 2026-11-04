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
# Both AI providers "configured" with fake values; real HTTP calls are always mocked.
os.environ["AI_PROVIDER"] = "azure,gemini"
os.environ["AZURE_AI_ENDPOINT"] = "https://test-resource.openai.azure.com/"
os.environ["AZURE_AI_API_KEY"] = "azure-test-key"
os.environ["AZURE_AI_DEPLOYMENT"] = "gpt-4o-mini"
os.environ["GEMINI_API_KEY"] = "gemini-test-key"
os.environ["GEMINI_MODEL"] = "gemini-flash-latest"
os.environ["EMAIL_PROVIDER"] = "console"   # emails land in emailer.OUTBOX
os.environ["PUBLIC_URL"] = "http://testserver"
os.environ["AI_DAILY_LIMIT_PER_USER"] = "2"
os.environ["AI_DAILY_LIMIT_PER_GUEST"] = "1"
os.environ["AI_DAILY_LIMIT_GLOBAL"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, engine
from app import auth, ai_decompose, emailer, mailing


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    for lim in (auth.login_limiter, auth.signup_limiter, auth.demo_limiter,
                mailing.send_now_limiter, mailing.resend_limiter):
        lim.reset()
    emailer.OUTBOX.clear()
    yield


@pytest.fixture(autouse=True)
def no_real_ai_calls(monkeypatch):
    """Safety net: any test that forgets to mock a provider fails instead of calling out."""
    def blocked(*a, **k):
        raise RuntimeError("real AI call attempted in tests")
    monkeypatch.setattr(ai_decompose.requests, "post", blocked)


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

"""All runtime configuration comes from environment variables (or a local .env file).

Locally you need none of them. In production (APP_ENV=production) SECRET_KEY and
CRON_SECRET are required so sessions and the scheduled-job endpoint are secure.
"""
import os
import secrets
import logging

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("planner")

APP_ENV = os.getenv("APP_ENV", "development")
IS_PROD = APP_ENV == "production"

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./planner.db")
# Some providers hand out postgres:// URLs; SQLAlchemy 2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SECRET_KEY = os.getenv("SECRET_KEY")
CRON_SECRET = os.getenv("CRON_SECRET")
if IS_PROD and (not SECRET_KEY or not CRON_SECRET):
    raise RuntimeError("SECRET_KEY and CRON_SECRET must be set when APP_ENV=production")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    log.warning("SECRET_KEY not set; using a random one (logins reset on restart).")

# Public URL of the app, used as the click-through link in notifications.
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

# ---- AI (Claude) ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
AI_DAILY_LIMIT_PER_USER = int(os.getenv("AI_DAILY_LIMIT_PER_USER", "3"))
AI_DAILY_LIMIT_PER_GUEST = int(os.getenv("AI_DAILY_LIMIT_PER_GUEST", "1"))
AI_DAILY_LIMIT_GLOBAL = int(os.getenv("AI_DAILY_LIMIT_GLOBAL", "100"))

# ---- Abuse limits (keep a free database from filling up) ----
MAX_GOALS_PER_USER = int(os.getenv("MAX_GOALS_PER_USER", "30"))
MAX_TASKS_PER_USER = int(os.getenv("MAX_TASKS_PER_USER", "400"))
MAX_BLOCKS_PER_USER = int(os.getenv("MAX_BLOCKS_PER_USER", "300"))
GUEST_TTL_HOURS = int(os.getenv("GUEST_TTL_HOURS", "24"))

# ---- Scheduled jobs ----
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "7"))   # local time per user
RISK_CHECK_HOUR = int(os.getenv("RISK_CHECK_HOUR", "20"))
# Run the hourly job inside the web process. Handy locally; in the cloud an
# Azure Function calls /internal/cron/hourly instead (free-tier apps sleep).
ENABLE_LOCAL_SCHEDULER = os.getenv(
    "ENABLE_LOCAL_SCHEDULER", "false" if IS_PROD else "true"
).lower() == "true"

NTFY_BASE = os.getenv("NTFY_BASE", "https://ntfy.sh")

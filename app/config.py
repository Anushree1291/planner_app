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

# Public URL of the app, used for links inside emails.
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

# ---- AI providers (all optional; without any, a template breakdown is used) ----
# Azure AI Foundry: any chat model you have deployed (gpt-4o-mini, gpt-5-mini,
# DeepSeek, Llama, Mistral, Phi, Grok, ...). Copy these from the Foundry portal.
AZURE_AI_ENDPOINT = os.getenv("AZURE_AI_ENDPOINT", "").strip()      # https://<resource>.openai.azure.com/
AZURE_AI_API_KEY = os.getenv("AZURE_AI_API_KEY", "").strip()
AZURE_AI_DEPLOYMENT = os.getenv("AZURE_AI_DEPLOYMENT", "").strip()  # your deployment name

# Google Gemini (free key from aistudio.google.com)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()

# Which providers to try, in order. If the first fails, the next is tried.
# "azure", "gemini", "azure,gemini", "gemini,azure" or "none".
AI_PROVIDERS = [p.strip().lower() for p in os.getenv("AI_PROVIDER", "azure,gemini").split(",")
                if p.strip()]

AI_DAILY_LIMIT_PER_USER = int(os.getenv("AI_DAILY_LIMIT_PER_USER", "3"))
AI_DAILY_LIMIT_PER_GUEST = int(os.getenv("AI_DAILY_LIMIT_PER_GUEST", "1"))
AI_DAILY_LIMIT_GLOBAL = int(os.getenv("AI_DAILY_LIMIT_GLOBAL", "100"))

# ---- Abuse limits (keep a free database from filling up) ----
MAX_GOALS_PER_USER = int(os.getenv("MAX_GOALS_PER_USER", "30"))
MAX_TASKS_PER_USER = int(os.getenv("MAX_TASKS_PER_USER", "400"))
MAX_BLOCKS_PER_USER = int(os.getenv("MAX_BLOCKS_PER_USER", "300"))
GUEST_TTL_HOURS = int(os.getenv("GUEST_TTL_HOURS", "24"))

# ---- Scheduled jobs ----
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "7"))   # default; each user can pick 6-9
MORNING_HOUR_CHOICES = (6, 7, 8, 9)
RISK_CHECK_HOUR = int(os.getenv("RISK_CHECK_HOUR", "20"))
# Run the hourly job inside the web process. Handy locally; in the cloud an
# Azure Function calls /internal/cron/hourly instead (free-tier apps sleep).
ENABLE_LOCAL_SCHEDULER = os.getenv(
    "ENABLE_LOCAL_SCHEDULER", "false" if IS_PROD else "true"
).lower() == "true"

# ---- Email (morning plan, risk alerts, verification) ----
# EMAIL_PROVIDER: "acs" (Azure Communication Services), "smtp" (Gmail, Brevo, Outlook, ...),
# "console" (log only, for local dev) or "none". Default: picked from what is configured.
ACS_CONNECTION_STRING = os.getenv("ACS_CONNECTION_STRING", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()          # e.g. DoNotReply@xxxx.azurecomm.net
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "AI Execution Planner").strip()
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "").strip().lower() or (
    "acs" if ACS_CONNECTION_STRING else "smtp" if SMTP_HOST else
    ("none" if IS_PROD else "console"))
# Sending limits. Set these to the limits Azure gives you (or your SMTP provider allows):
#   Azure-managed domain: 5/min, 10/hour (fixed)
#   custom domain:        30/min, 100/hour by default, raised on request
# The job never goes over them; anyone left over gets their email on the next run.
EMAIL_RATE_PER_MINUTE = int(os.getenv("EMAIL_RATE_PER_MINUTE", "30"))
EMAIL_RATE_PER_HOUR = int(os.getenv("EMAIL_RATE_PER_HOUR", "100"))
EMAIL_CONCURRENCY = int(os.getenv("EMAIL_CONCURRENCY", "4"))      # parallel sends
# One scheduled run stops after this many seconds (App Service cuts requests at 230 s);
# the timer runs every 5 minutes, so the next run carries on.
EMAIL_RUN_SECONDS = int(os.getenv("EMAIL_RUN_SECONDS", "200"))
# Shared secret for the Event Grid webhook that reports bounces (?key=...).
EMAIL_EVENTS_KEY = os.getenv("EMAIL_EVENTS_KEY", "").strip()

# AI Execution Planner — MVP

Implements Phase 1 + Phase 2 from the brainstorm:
- Goals → tasks (optionally AI-decomposed via Claude)
- Calendar-aware capacity calculation (`free_minutes today`)
- Energy-aware, urgency-sorted daily plan generation (won't jam 8 tasks into a 3-hour day)
- Automatic task splitting when a task doesn't fit a free slot
- "What should I do now?" endpoint
- Skip/postpone → automatic replan, with postponed tasks surfacing higher next time
- Learned time-estimation (actual vs estimated minutes adjust future scheduling)
- Goal risk detection (background job flags goals that need >3h/day to hit deadline)
- Push notifications to phone + laptop via ntfy.sh (no accounts/keys needed)

## 1. Run it locally

```bash
cd planner_app
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://localhost:8000** — a minimal dashboard to add goals, add calendar
blocks, generate today's plan, and try "what now".

Data is stored in a local `planner.db` SQLite file by default.

Optional: set `ANTHROPIC_API_KEY` as an environment variable to enable
AI goal decomposition (`auto_decompose: true` when creating a goal breaks
"Learn GenAI in 3 months" into ~10 concrete tasks automatically).

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Notifications on phone + laptop (no app store, no VAPID keys)

This uses **ntfy.sh**, a free open-source push notification service built
exactly for this kind of thing (self-hostable later if you want).

1. Pick a hard-to-guess topic name, e.g. `raj-planner-9f21ac` (treat it like a password — anyone who knows it can read your notifications).
2. **Phone:** install the "ntfy" app (iOS App Store / Google Play) → add subscription → paste your topic name.
3. **Laptop:** open `https://ntfy.sh/your-topic-name` in a browser and click "Enable notifications" (or install the ntfy desktop app for Mac/Windows/Linux).
4. Tell the planner about your topic:
   ```bash
   curl -X POST "http://localhost:8000/setup/notifications?topic=raj-planner-9f21ac"
   ```
   (or use the "Connect" box in the dashboard UI)
5. You'll immediately get a test notification on both devices.

From then on you'll get pushed:
- Your daily plan every morning at 7:00 AM (edit the hour in `app/scheduler.py`)
- A "time to focus" nudge whenever you call `/what-now`
- Goal risk warnings at 8:00 PM if a goal needs an unrealistic daily pace

If you'd rather not rely on the public ntfy.sh server long-term, you can
self-host ntfy in ~5 minutes (single Docker container) — see https://docs.ntfy.sh/install/.
An alternative that also works well: a Telegram bot (`sendMessage` via
`api.telegram.org`) if you'd rather use an app you already have installed.

## 3. Deploying to Azure

Recommended setup: **Azure Container Apps** (serverless containers, scales
to zero, cheap for a personal tool) + **Azure Database for PostgreSQL
Flexible Server** (so state survives restarts/redeploys — SQLite won't
persist reliably in a container).

### Step 1 — Create a Postgres database
```bash
az postgres flexible-server create \
  --resource-group my-planner-rg \
  --name my-planner-db \
  --location eastus \
  --admin-user plannerAdmin \
  --admin-password "<STRONG_PASSWORD>" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 15

az postgres flexible-server db create \
  --resource-group my-planner-rg \
  --server-name my-planner-db \
  --database-name plannerdb
```
Allow your Container App to reach it (or use the "Allow Azure services" firewall rule).

### Step 2 — Build & push the container image
```bash
az acr create --resource-group my-planner-rg --name myplanneracr --sku Basic
az acr build --registry myplanneracr --image planner:latest .
```

### Step 3 — Deploy to Container Apps
```bash
az containerapp env create \
  --name planner-env \
  --resource-group my-planner-rg \
  --location eastus

az containerapp create \
  --name planner-app \
  --resource-group my-planner-rg \
  --environment planner-env \
  --image myplanneracr.azurecr.io/planner:latest \
  --target-port 8000 \
  --ingress external \
  --registry-server myplanneracr.azurecr.io \
  --min-replicas 1 --max-replicas 1 \
  --env-vars \
    DATABASE_URL="postgresql://plannerAdmin:<PASSWORD>@my-planner-db.postgres.database.azure.com:5432/plannerdb?sslmode=require" \
    ANTHROPIC_API_KEY="<your key, optional>"
```

That gives you a public HTTPS URL. Open it from your phone's browser and
"Add to Home Screen" for an app-like icon — the built-in dashboard is a
normal web page so this works without any native app development.

**Cost note:** `min-replicas 1` keeps one instance always warm so the
background scheduler (morning plan + risk checks) actually fires. If you set
`min-replicas 0` to save money, the container sleeps and scheduled jobs won't
run until a request wakes it — in that case, trigger `/plan/generate` from
an **Azure Function on a Timer Trigger** instead (calls your app's URL every
morning), which is essentially free at this scale.

### Alternative: Azure App Service
If you'd rather not deal with containers at all:
```bash
az webapp up --resource-group my-planner-rg --name my-planner-app \
  --runtime "PYTHON:3.11" --sku B1
```
Then set `DATABASE_URL` and `ANTHROPIC_API_KEY` under
Configuration → Application settings in the Azure Portal. App Service
supports "Always On" (paid tiers) to keep the scheduler alive the same way
`min-replicas 1` does for Container Apps.

## 4. What's not built yet (roadmap, matching the original phases)

- Real Google/Outlook calendar sync (currently manual calendar blocks) — Phase 4
- Multi-goal conflict resolution, weekly review agent, focus-mode timer — Phase 2/3 extensions
- Multi-user auth (currently single hardcoded user) — needed before sharing this with anyone else
- Planner→Evaluator→Replan critique loop (currently replanning is a fixed heuristic, not an LLM critic)

The architecture (separate `planner.py` engine, `ai_decompose.py`, `notifications.py`)
is deliberately modular so each of these can be added without rewriting the core.

# Deploying to Azure (low-cost setup)

This guide puts the planner on a public HTTPS URL for about **$0/month**
(the free App Service tier) or about **$13/month** if you want the site to stay awake.

```
                ┌──────────────── Azure ────────────────┐
 Browser ──────►│ App Service (Linux, Python 3.11)      │──────► Neon Postgres (free)
 (users)        │ FastAPI + dashboard                   │
                │        ▲                              │──────► Claude API (AI breakdown)
                │        │ POST /internal/cron/hourly   │──────► ntfy.sh (push to phone/laptop)
                │ Function App (Flex Consumption)       │
                │ timer: minute 2 of every hour         │
                └───────────────────────────────────────┘
 GitHub ── push to main ──► GitHub Actions: tests (SQLite + Postgres) ──► deploy ──► smoke test
```

| Piece | Service | Cost |
|---|---|---|
| Web app | Azure App Service, **F1 Free** Linux plan | $0 (B1 ≈ $13/mo if you want it always awake) |
| Database | **Neon** serverless Postgres, free plan | $0 |
| Hourly jobs | Azure Functions **Flex Consumption** timer | ~$0 (pay per execution; a storage account costs a few cents) |
| CI/CD | GitHub Actions | $0 for public repos |
| AI | Anthropic API, Claude Haiku, rate-limited | a few cents per day, capped by `AI_DAILY_LIMIT_GLOBAL` |

> **Student?** *Azure for Students* gives you free credit without needing a credit card.
> Sign up with your college email at azure.microsoft.com/free/students.

**The trade-off with the free tier:** an F1 app goes to sleep after about 20 minutes idle, so the
first visitor waits roughly 20–40 s for it to wake. It also gets 60 CPU-minutes per day. That's fine
for a portfolio demo. If you want it snappier before interviews, run the one-line
upgrade to B1 in step 8, and downgrade again afterwards.

---

## 0. What you need

- A GitHub account.
- An Azure account (portal.azure.com).
- An Anthropic API key (console.anthropic.com). In Settings → Limits, set a **monthly spend limit**
  of something like $5. That is your hard safety net.
- All commands below run in **Azure Cloud Shell**, a Bash terminal in your browser with
  `az`, `git` and `zip` already installed. Open it from the `>_` icon at the top of the Azure portal and choose *Bash*.
  You don't need to install anything on your laptop.

## 1. Put the code on GitHub

On your laptop, in the `planner_app` folder:

```bash
git init
git add .
git commit -m "AI Execution Planner: multi-user, tested, Azure-ready"
git branch -M main
# create an empty PUBLIC repo named planner-app on github.com first, then:
git remote add origin https://github.com/<your-username>/planner-app.git
git push -u origin main
```

The first push runs the tests. The deploy step will fail until step 5 is done, and that's expected.

## 2. Create the free Postgres database (Neon)

1. Sign up at **neon.tech**, then choose *Create project*. Pick the region closest to India, such as
   *AWS Asia Pacific (Singapore)*, and name the database `plannerdb`.
2. Copy the **connection string**. It looks like
   `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/plannerdb?sslmode=require`

The app creates its tables automatically on first start.

## 3. Create the web app

In Cloud Shell, pick an app name. It must be unique across Azure, because it becomes `<name>.azurewebsites.net`.

```bash
RG=planner-rg
LOC=centralindia
APP=anushree-planner            # change me: this becomes your URL

# one-off for brand-new subscriptions (harmless if already registered)
az provider register --namespace Microsoft.Web --wait

az group create -n $RG -l $LOC
az appservice plan create -g $RG -n planner-plan --is-linux --sku F1
az webapp create -g $RG -p planner-plan -n $APP --runtime "PYTHON:3.11"
az webapp update -g $RG -n $APP --https-only true
az webapp config set -g $RG -n $APP --startup-file \
  "gunicorn -w 1 -k uvicorn.workers.UvicornWorker --timeout 120 --bind 0.0.0.0:8000 app.main:app"
```

If `F1` is rejected in your region ("free tier not available"), try `LOC=southindia` or `LOC=eastasia`.

## 4. Configure secrets on the web app

Generate two long random secrets and set the app settings. Paste your Neon URL and API key in place of the placeholders:

```bash
SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
CRON_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
echo "Save this CRON_SECRET, you need it in step 6: $CRON_SECRET"

az webapp config appsettings set -g $RG -n $APP --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  APP_ENV=production \
  PUBLIC_URL="https://$APP.azurewebsites.net" \
  DATABASE_URL='postgresql://USER:PASSWORD@ep-xxxx.aws.neon.tech/plannerdb?sslmode=require' \
  ANTHROPIC_API_KEY='sk-ant-...' \
  SECRET_KEY="$SECRET_KEY" \
  CRON_SECRET="$CRON_SECRET" \
  AI_DAILY_LIMIT_PER_USER=3 \
  AI_DAILY_LIMIT_PER_GUEST=1 \
  AI_DAILY_LIMIT_GLOBAL=100
```

Secrets live only in Azure, never in the repo. You can view or edit them later under
*Web App → Settings → Environment variables*.

## 5. Connect GitHub Actions (automatic deploys)

The workflow deploys with a *publish profile*. New App Service apps turn off the
basic-auth publishing it relies on, so turn it on for this app, then fetch the profile:

```bash
az resource update -g $RG --namespace Microsoft.Web --resource-type basicPublishingCredentialsPolicies \
  --parent sites/$APP --name scm --set properties.allow=true

az webapp deployment list-publishing-profiles -g $RG -n $APP --xml
```

In your GitHub repo, open **Settings → Secrets and variables → Actions**:

- **Secrets** tab: add a new secret named `AZURE_WEBAPP_PUBLISH_PROFILE`, with the entire XML output above as its value.
- **Variables** tab: add a new variable named `AZURE_WEBAPP_NAME`, with your app name (e.g. `anushree-planner`) as its value.

Then open **Actions → Test & deploy → Run workflow**, or just push a commit. When it's green, open
`https://<APP>.azurewebsites.net`. The workflow's last step waits until `/healthz` responds.

## 6. Hourly timer for morning plans and risk alerts

```bash
FUNC=$APP-cron
STORAGE=plannercron$RANDOM          # lowercase letters/digits, globally unique

az functionapp list-flexconsumption-locations -o table   # confirm your region is listed
az storage account create -g $RG -n $STORAGE -l $LOC --sku Standard_LRS \
  --allow-blob-public-access false
az functionapp create -g $RG -n $FUNC --storage-account $STORAGE \
  --flexconsumption-location $LOC --runtime python --runtime-version 3.11 --instance-memory 512

az functionapp config appsettings set -g $RG -n $FUNC --settings \
  PLANNER_URL="https://$APP.azurewebsites.net" CRON_SECRET="$CRON_SECRET"

git clone https://github.com/<your-username>/planner-app.git && cd planner-app/azure_function
zip -r ../cron.zip . && cd ..
az functionapp deployment source config-zip -g $RG -n $FUNC --src cron.zip --build-remote true
```

To check it works without waiting for the hour:

```bash
curl -X POST -H "X-Cron-Secret: $CRON_SECRET" https://$APP.azurewebsites.net/internal/cron/hourly
# → {"morning_plans":0,"risk_checks":0,"guests_deleted":0}
```

Each user gets their plan at 7 AM and risk alerts at 8 PM in **their own timezone**. Calling the
endpoint twice never double-sends, and demo sandboxes older than 24 h are deleted.

<details>
<summary>Fallback if your subscription can't create a Flex Consumption app</summary>

Some student or free subscriptions restrict plan types. You can get the same result with a free GitHub
Actions schedule instead. Add the secrets `PLANNER_URL` and `CRON_SECRET` to the repo, then create
`.github/workflows/hourly-cron.yml`:

```yaml
name: Hourly planner tick
on:
  schedule: [{ cron: "2 * * * *" }]
  workflow_dispatch:
jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS --max-time 180 -X POST -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" "${{ secrets.PLANNER_URL }}/internal/cron/hourly"
```

GitHub may delay scheduled runs by a few minutes. That's fine for a morning plan.
</details>

## 7. Try it

1. Open the site and click **Try the demo** (or sign up). Then click *Generate plan*.
2. Add a goal with "break down with AI" ticked. You should get 8–15 tasks. After the daily limit,
   it falls back to a template and tells you so.
3. Notifications: install the **ntfy** app on your phone, subscribe to a hard-to-guess topic,
   and enter the same topic in the dashboard. A test push arrives immediately.
4. On your phone, use the browser's *Add to Home Screen* for an app-like icon.

## 8. Useful commands

```bash
az webapp log tail -g $RG -n $APP                     # live server logs
az webapp restart -g $RG -n $APP
az appservice plan update -g $RG -n planner-plan --sku B1   # always-on, faster (~$13/mo)
az webapp config set -g $RG -n $APP --always-on true        # (B1 and up)
az appservice plan update -g $RG -n planner-plan --sku F1   # back to free
az group delete -n $RG                                 # tear EVERYTHING down
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Site shows "Application Error" | `az webapp log tail`. Usually a typo in `DATABASE_URL`, or a missing `SECRET_KEY`/`CRON_SECRET` (the app refuses to start in production without them). |
| `ModuleNotFoundError` in logs | `SCM_DO_BUILD_DURING_DEPLOYMENT=true` is missing, so the dependencies weren't installed. Set it and re-run the workflow. |
| Deploy step: *401 / publish profile invalid* | Re-run the `basicPublishingCredentialsPolicies` command in step 5, download a fresh profile and update the secret. |
| Site stopped with a "quota exceeded" page | The F1 60 CPU-min/day limit was hit. It resets daily. Upgrade to B1 for demos. |
| AI always says "used a template" | Check `ANTHROPIC_API_KEY` is set, and look in the logs for `AI decomposition failed`. |
| Logged out after every deploy | `SECRET_KEY` changed or isn't set. Keep it fixed. |

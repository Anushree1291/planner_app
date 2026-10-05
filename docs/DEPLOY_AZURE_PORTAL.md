# Deploying by hand: Azure portal + Azure DevOps Pipelines

This guide does everything by clicking through websites instead of typing commands. You end up with:

```
GitHub repo ──► Azure DevOps Pipeline ──► Azure App Service (your live site)
                 Test → Package → Deploy        │
                                                ├──► Neon Postgres (free database)
                                                ├──► Claude API
Azure Function (hourly timer) ─────────────────►┘   (morning plans + alerts)
```

The order matters: **database → web app → app settings → DevOps project → service
connection → pipeline → timer.**

---

## Step 1 — Put the code on GitHub

Create a **public** repo named `planner-app` on github.com and push the `planner_app` folder to it
(see *Step 1* in `DEPLOY_AZURE.md` for the git commands). Before pushing, edit
`azure-pipelines.yml` and change `webAppName` to the name you'll pick in Step 3.

## Step 2 — Create the database (Neon, free)

1. Go to **neon.tech** → sign up → **Create project**.
2. Project name `planner`, database name `plannerdb`, and the region closest to you (e.g. *AWS Asia Pacific – Singapore*).
3. On the dashboard, click **Connect** and copy the connection string:
   `postgresql://USER:PASSWORD@ep-xxxx.ap-southeast-1.aws.neon.tech/plannerdb?sslmode=require`
   Keep it somewhere safe. You'll paste it in Step 4.

## Step 3 — Create the App Service (Azure portal)

Go to **portal.azure.com** → **Create a resource** → search **Web App** → **Create**.

**Basics tab**

| Field | Value |
|---|---|
| Subscription | your subscription (e.g. *Azure for Students*) |
| Resource group | **Create new** → `planner-rg` |
| Name | e.g. `anushree-planner` (it must be unique, and it becomes your URL) |
| Secure unique default hostname | **Off**, if shown. Otherwise Azure adds a random suffix to your URL |
| Publish | **Code** |
| Runtime stack | **Python 3.11** |
| Operating system | **Linux** |
| Region | **Central India** (if the Free plan isn't offered, try South India or East Asia) |
| Linux plan | **Create new** → `planner-plan` |
| Pricing plan | Click *Explore pricing plans* → **Free F1** → *Select* |

**Deployment tab**
- Continuous deployment: **Disable**. Azure DevOps will handle deploys.
- Basic authentication: leave it **Disabled**. The DevOps connection doesn't need it.

**Networking tab:** Enable public access: **On**.

**Monitoring tab:** Application Insights: **No** (keeps it free; turn it on later if you want logs and metrics).

Click **Review + create** → **Create**. Wait about a minute, then click **Go to resource**. You'll see your
URL, `https://anushree-planner.azurewebsites.net`, which shows a default Azure page for now.

## Step 4 — Configure the web app

### 4a. Environment variables (secrets)

In your Web App: left menu **Settings → Environment variables** → **App settings** tab →
**+ Add** for each row below, then click **Apply** at the bottom and **Confirm** the restart.

| Name | Value |
|---|---|
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` ← **required**, so Azure installs `requirements.txt` |
| `APP_ENV` | `production` |
| `DATABASE_URL` | your Neon connection string from Step 2 |
| `SECRET_KEY` | a long random string (see below) |
| `CRON_SECRET` | a *different* long random string. Save it, because Step 7 needs it too |
| `PUBLIC_URL` | `https://anushree-planner.azurewebsites.net` |
| `ANTHROPIC_API_KEY` | your `sk-ant-...` key |
| `AI_DAILY_LIMIT_PER_USER` | `3` |
| `AI_DAILY_LIMIT_PER_GUEST` | `1` |
| `AI_DAILY_LIMIT_GLOBAL` | `100` |

To make a random string, open the **Cloud Shell** (`>_` icon at the top of the portal) and run
`python3 -c "import secrets;print(secrets.token_urlsafe(48))"`. Run it twice to get one for each secret.

> Also set a **monthly spend limit** (e.g. $5) on your Anthropic key at console.anthropic.com → Settings → Limits.

### 4b. Startup command and HTTPS

Left menu **Settings → Configuration** → **General settings** tab:

- **Startup Command:**
  `gunicorn -w 1 -k uvicorn.workers.UvicornWorker --timeout 120 --bind 0.0.0.0:8000 app.main:app`
- **HTTPS Only:** On

Click **Save**. The pipeline sets the startup command as well, so setting it here too is just a safety net.

## Step 5 — Create the Azure DevOps project

1. Go to **dev.azure.com** and sign in with the **same Microsoft account** as the Azure portal.
2. **Create new organization**, e.g. `anushree-dev`, with a region near you.
3. **+ New project** → name `planner-app`, visibility **Private** → *Create*.

### ⚠️ Free build minutes (do this now, because approval can take a few days)

New Azure DevOps organizations get **no free Microsoft-hosted build agents** until you request them.
Without a grant, your first run fails with *"No hosted parallelism has been purchased or granted"*.

- Request the free grant with the form at **https://aka.ms/azpipelines-parallelism-request**
  (choose *Private* project, and describe it as a personal learning project). It usually takes **2–3 business days**.
- Can't wait? Use your own laptop as a free **self-hosted agent** meanwhile: *Project settings → Agent pools →
  Default → New agent*, then follow the download-and-run steps for your OS. In `azure-pipelines.yml`, replace
  every `vmImage: $(vmImageName)` with `name: Default`. Your laptop must be on while the pipeline runs, and
  it needs Python 3.11 installed.

## Step 6 — Connect Azure DevOps to Azure (service connection)

This is the "connect them" step. It lets the pipeline deploy into your Azure subscription
without any password being stored.

1. In your DevOps project: bottom-left **Project settings** → **Pipelines → Service connections** →
   **Create service connection**.
2. Choose **Azure Resource Manager** → *Next*.
3. Identity type: **App registration (automatic)**. Credential: **Workload identity federation**.
4. Scope level: **Subscription**. Pick your subscription, then Resource group: **planner-rg**.
5. Service connection name: **`azure-planner-connection`** (it must match `azureServiceConnection` in `azure-pipelines.yml`).
6. Tick **Grant access permission to all pipelines** → **Save**.

If it errors with *insufficient privileges*, your account can't create app registrations (common on
some school tenants). In that case, choose **Managed identity** as the identity type, or ask your tenant admin.

## Step 7 — Create the pipeline

1. In the DevOps project: **Pipelines → Create Pipeline** (or *New pipeline*).
2. *Where is your code?* → **GitHub** → authorize Azure Pipelines → select your `planner-app` repo
   → approve the install of the Azure Pipelines GitHub app for that repo.
3. *Configure* → **Existing Azure Pipelines YAML file** → Branch `main`, Path `/azure-pipelines.yml` → *Continue*.
4. Check that `webAppName` and `azureServiceConnection` in the YAML match what you created → **Run**.

The run has three stages: **Run tests → Package app → Deploy to Azure**. The first time it reaches
Deploy, it may pause with *"This pipeline needs permission to access a resource"*. Click **View → Permit**.
When the smoke test prints `healthy`, your site is live.

From now on, every `git push` to `main` runs the pipeline automatically. The test results
appear under the run's **Tests** tab, which is worth a screenshot for your portfolio.

Optional: **Pipelines → Environments → production → Approvals and checks** lets you add yourself as an
approver, so deploys wait for a click.

## Step 8 — Hourly timer (morning plan + risk alerts)

**Create the Function App** in the portal: **Create a resource** → **Function App** → **Create** →
choose the **Flex Consumption** hosting option → *Select*.

| Field | Value |
|---|---|
| Resource group | `planner-rg` |
| Function App name | `anushree-planner-cron` |
| Region | same as the web app (if it isn't listed for Flex Consumption, pick the nearest one that is) |
| Runtime stack | **Python**, version **3.11** |
| Instance size | **512 MB** |
| Storage (tab) | let it create a new storage account |
| Monitoring | Application Insights optional |

**Review + create** → **Create**. Then, in the new Function App: **Settings → Environment variables** → add
`PLANNER_URL` = `https://anushree-planner.azurewebsites.net` and `CRON_SECRET` = the same value as in
Step 4 → **Apply**.

**Deploy the timer code** (the portal has no upload button for Python, so this is one short Cloud Shell step).
Open **Cloud Shell** (`>_`, *Bash*) and run:

```bash
git clone https://github.com/<your-username>/planner-app.git
cd planner-app/azure_function && zip -r ../cron.zip . && cd ..
az functionapp deployment source config-zip -g planner-rg -n anushree-planner-cron --src cron.zip --build-remote true
```

After about a minute, **Overview → Functions** lists `hourly_planner_tick`. It runs at minute 2 of every hour.

*If your subscription won't allow a Flex Consumption app*, skip Step 8 and use the free GitHub Actions
schedule from the "Fallback" section of `DEPLOY_AZURE.md`.

## Step 9 — Check it works

- Open `https://anushree-planner.azurewebsites.net` → **Try the demo**. The first load after idle takes 20–40 s on the Free plan.
- Open `https://anushree-planner.azurewebsites.net/healthz` → `{"ok":true}`
- **Web App → Monitoring → Log stream** shows live server logs if something's wrong.
- **Function App → hourly_planner_tick → Invocations** shows each hourly run.

## Common problems

| Symptom | Fix |
|---|---|
| Pipeline: *No hosted parallelism has been purchased or granted* | Step 5: wait for the grant, or use a self-hosted agent. |
| Pipeline: *Could not find service connection* | The name in the YAML must match Step 6 exactly. Also check **Grant access to all pipelines**. |
| Site: *Application Error* | **Log stream**. Usually a wrong `DATABASE_URL`, or missing `SECRET_KEY`/`CRON_SECRET`. |
| Log: `ModuleNotFoundError: fastapi` | `SCM_DO_BUILD_DURING_DEPLOYMENT=true` is missing (Step 4a). Add it and re-run the pipeline. |
| Site: *quota exceeded* page | The Free F1 plan's 60 CPU-min/day is used up; it resets daily. **App Service plan → Scale up → B1** (≈ $13/mo) for interview days. |
| AI always "used a template" | Check `ANTHROPIC_API_KEY`, and look in Log stream for `AI decomposition failed`. |

**Tear down everything:** portal → Resource groups → `planner-rg` → **Delete resource group**.

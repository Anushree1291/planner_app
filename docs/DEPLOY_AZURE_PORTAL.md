# Deploying by hand: Azure portal + Azure DevOps Pipelines

This guide does everything by clicking through websites instead of typing commands. You end up with:

```
Code repo (Azure Repos or GitHub) ──► Azure DevOps Pipeline ──► Azure App Service (your live site)
                                       Test → Package → Deploy        │
                                                                      ├──► Neon Postgres (free database)
                                                                      ├──► Azure AI Foundry and/or Gemini
                                                                      ├──► Azure Communication Services Email (no-reply)
Azure Function (5-minute timer) ──────────────────────────────────────►┘   (morning plan + alert emails)
```

The order matters: **database → web app → app settings → AI model → email → DevOps project → service
connection → pipeline → timer.**

---

## Step 1 — Put the code in a repo

Either works:

- **Azure Repos** (inside your Azure DevOps project, Step 5): *Repos → Files* shows the commands to push.
  When `git push` asks for a password, click **Clone → Generate Git Credentials** in Azure Repos and use that
  password, or run `git config --global credential.helper manager` so Git signs you in through the browser.
- **GitHub**: create a **public** repo named `planner-app` and push the `planner_app` folder to it
  (see *Step 1* in `DEPLOY_AZURE.md` for the git commands).

Before pushing, edit `azure-pipelines.yml`: set `webAppName` and `webAppUrl` to what you get in Step 3.

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
| Name | e.g. `ai-planner` (it must be unique) |
| Secure unique default hostname | **Off** if you want a short URL. If left on, Azure adds a random suffix such as `ai-planner-c9gre3….centralindia-01.azurewebsites.net`, which also works |
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

Click **Review + create** → **Create**. Wait about a minute, then click **Go to resource**. Copy the
**Default domain** from the Overview page. That's your site's address (below, `<your-url>`).

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
| `CRON_SECRET` | a *different* long random string. Save it, because Step 8 needs it too |
| `PUBLIC_URL` | `https://<your-url>` (no `/` at the end) |
| `AI_DAILY_LIMIT_PER_USER` | `3` |
| `AI_DAILY_LIMIT_PER_GUEST` | `1` |
| `AI_DAILY_LIMIT_GLOBAL` | `100` |

The AI settings come in Step 4c.

To make a random string, open the **Cloud Shell** (`>_` icon at the top of the portal) and run
`python3 -c "import secrets;print(secrets.token_urlsafe(48))"`. Run it twice to get one for each secret.

### 4b. Startup command and HTTPS

Left menu **Settings → Configuration** → **General settings** tab:

- **Startup Command:**
  `gunicorn -w 1 -k uvicorn.workers.UvicornWorker --timeout 120 --bind 0.0.0.0:8000 app.main:app`
- **HTTPS Only:** On

Click **Save**. The pipeline sets the startup command as well, so setting it here too is just a safety net.

## Step 4c — Set up the AI model (Azure AI Foundry and/or Gemini)

The "break down with AI" feature works with **either or both**. If both are set up, Azure is tried
first and Gemini takes over if Azure fails. With neither, goals get a simple 5-step template instead,
so you can skip this step and come back later.

### Option A: Azure AI Foundry (any model)

1. Go to **ai.azure.com** and sign in with the same account as the Azure portal.
2. **Create project**. Expand *Advanced options* and pick:
   - Resource group: **planner-rg**
   - Region: **East US 2** or **Sweden Central** (they offer the most models; Central India has fewer)
3. In your project: **Models + endpoints** (or *Model catalog*) → **Deploy model** → pick a chat model.
   Good cheap choices: **gpt-4o-mini**, **gpt-5-mini**, **DeepSeek-V3**, **Llama-3.3-70B-Instruct**, **Phi-4**.
   Deployment type **Global Standard** → **Deploy**.
4. Open the deployment. Copy three things:
   - **Endpoint / Target URI**: any of these formats works, e.g. `https://<name>.openai.azure.com/` or
     `https://<name>.services.ai.azure.com/...`. The app works out the rest.
   - **Key**
   - **Deployment name** (e.g. `gpt-4o-mini`), which is *your* name for it, not always the model name.
5. Back in the Web App → **Environment variables**, add:

| Name | Value |
|---|---|
| `AZURE_AI_ENDPOINT` | the endpoint you copied |
| `AZURE_AI_API_KEY` | the key |
| `AZURE_AI_DEPLOYMENT` | the deployment name |

> Azure for Students: some subscriptions can't create certain models. If a model won't deploy, try
> another one or another region, or use Gemini instead.
> Set a budget alert under **Cost Management → Budgets** (e.g. $5) as a safety net.

### Option B: Google Gemini (free tier)

1. Go to **aistudio.google.com/apikey** → sign in with a Google account → **Create API key**.
2. In the Web App → **Environment variables**, add `GEMINI_API_KEY` = that key.
   Optionally `GEMINI_MODEL` (default `gemini-flash-latest`, which always points at Google's current Flash model).

> The free tier has rate limits, and Google may use free-tier prompts to improve its products. That's fine for
> goal titles in a demo, but worth knowing.

### Which one is used?

`AI_PROVIDER` controls the order. The default `azure,gemini` means Azure first, Gemini as backup. Set it to
`gemini,azure` to prefer Gemini, or just `gemini` / `azure` to use one. Click **Apply** after any change.

To check what the live site sees: log in, then open `https://<your-url>/auth/me`. `ai_providers` lists the
providers that are configured.

## Step 4d — Set up email (Azure, from a no-reply address)

Each person gets a welcome email with a link to confirm their address. Once confirmed, they get
their plan at the morning time they pick in Settings (6:00–9:00, their timezone) and, at 20:00, a
heads-up only if a goal is slipping. Every email has a one-click "Stop these emails" link.

Email goes through **Azure Communication Services** from a no-reply address. The full click-by-click
setup is in **[`EMAIL_AZURE.md`](EMAIL_AZURE.md)**, in three stages:

1. **Test** with a free Azure address (`DoNotReply@xxxx.azurecomm.net`, 10 emails an hour). About 15 minutes.
2. **Your own domain** (`noreply@mail.yourdomain.com`, 100 an hour to start), plus bounce reporting.
3. **Ask Microsoft for a higher limit**, so everyone's plan goes out at 7:00 or 8:00.

The settings the web app needs:

| Name | Value |
|---|---|
| `ACS_CONNECTION_STRING` | from the Communication Services resource → Keys |
| `EMAIL_FROM` | your no-reply address |
| `EMAIL_RATE_PER_MINUTE` / `EMAIL_RATE_PER_HOUR` | your Azure sending limits (5 / 10 for the free Azure address) |
| `EMAIL_EVENTS_KEY` | a random key for the bounce webhook (stage 2) |

Until email is set up, the app works normally and simply sends nothing.

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
4. Scope level: **Subscription**. Pick your subscription, then Resource group: **planner-rg** (or leave it blank).
5. Service connection name: **`planner-azure-conn`** (it must match `azureServiceConnection` in `azure-pipelines.yml`).
6. Tick **Grant access permission to all pipelines** → **Save**.

If it errors with *insufficient privileges*, your account can't create app registrations (common on
some school tenants). In that case, choose **Managed identity** as the identity type, or ask your tenant admin.

## Step 7 — Create the pipeline

1. In the DevOps project: **Pipelines → Create Pipeline** (or *New pipeline*).
2. *Where is your code?* → **Azure Repos Git** if you pushed to Azure Repos, or **GitHub** (authorize Azure
   Pipelines and approve the GitHub app install for your repo).
3. Select your repo → *Configure* → **Existing Azure Pipelines YAML file** → Branch `main`,
   Path `/azure-pipelines.yml` → *Continue*.
4. Check that `webAppName`, `webAppUrl` and `azureServiceConnection` in the YAML match what you created → **Run**.

The run has three stages: **Run tests → Package app → Deploy to Azure**. The first time it reaches
Deploy, it may pause with *"This pipeline needs permission to access a resource"*. Click **View → Permit**.
When the smoke test prints `healthy`, your site is live.

From now on, every `git push` to `main` runs the pipeline automatically. The test results
appear under the run's **Tests** tab, which is worth a screenshot for your portfolio.

Optional: **Pipelines → Environments → production → Approvals and checks** lets you add yourself as an
approver, so deploys wait for a click.

## Step 8 — Timer for the morning plan and alerts (every 5 minutes)

**Create the Function App** in the portal: **Create a resource** → **Function App** → **Create** →
choose the **Flex Consumption** hosting option → *Select*.

| Field | Value |
|---|---|
| Resource group | `planner-rg` |
| Function App name | e.g. `ai-planner-cron` |
| Region | same as the web app (if it isn't listed for Flex Consumption, pick the nearest one that is) |
| Runtime stack | **Python**, version **3.11** |
| Instance size | **512 MB** |
| Storage (tab) | let it create a new storage account |
| Monitoring | Application Insights optional |

**Review + create** → **Create**. Then, in the new Function App: **Settings → Environment variables** → add
`PLANNER_URL` = `https://<your-url>` and `CRON_SECRET` = the same value as in Step 4 → **Apply**.

**Deploy the timer code** (the portal has no upload button for Python, so this is one short Cloud Shell step).
Open **Cloud Shell** (`>_`, *Bash*) and run (use your repo's clone URL; for Azure Repos it's under *Repos → Clone*):

```bash
git clone <your-repo-clone-url> planner-app
cd planner-app/azure_function && zip -r ../cron.zip . && cd ..
az functionapp deployment source config-zip -g planner-rg -n ai-planner-cron --src cron.zip --build-remote true
```

After about a minute, **Overview → Functions** lists `hourly_planner_tick`. It runs every 5 minutes; runs with nothing to send finish in milliseconds.

*If your subscription won't allow a Flex Consumption app*, skip Step 8 and use the free GitHub Actions
schedule from the "Fallback" section of `DEPLOY_AZURE.md`.

## Step 9 — Check it works

- Open `https://<your-url>` → **Try the demo**. The first load after idle takes 20–40 s on the Free plan.
- Open `https://<your-url>/healthz` → `{"ok":true}`
- Add a goal with "break down with AI" ticked. The message says which provider created the tasks.
- Sign up with your own email, confirm it, then **Settings → Email me today's plan**.
- **Web App → Monitoring → Log stream** shows live server logs if something's wrong.
- **Function App → hourly_planner_tick → Invocations** shows each run.

## Common problems

| Symptom | Fix |
|---|---|
| `git push` asks for a password | Azure Repos → **Clone → Generate Git Credentials**, or `git config --global credential.helper manager` to sign in through the browser. |
| No emails arrive | Log stream shows `sending email to … failed` with the reason. `EMAIL_FROM` must be the exact MailFrom address, and the domain must be connected to the Communication Services resource (see `EMAIL_AZURE.md`). Check spam too. Only **confirmed** addresses get the morning plan. |
| Pipeline: *No hosted parallelism has been purchased or granted* | Step 5: wait for the grant, or use a self-hosted agent. |
| Step 6: *Service connection with name … already exists. Only a user having Administrator/User role …* | An earlier attempt left a hidden or half-created connection with that name. Create the new one under a different name and change `azureServiceConnection` in `azure-pipelines.yml` to match. |
| Pipeline: *Could not find service connection* | The name in the YAML must match Step 6 exactly. Also check **Grant access to all pipelines**. |
| Pipeline: smoke test never becomes healthy | `webAppUrl` in the YAML must be the full **Default domain** from the Web App's Overview page. |
| Site: *Application Error* | **Log stream**. Usually a wrong `DATABASE_URL`, or missing `SECRET_KEY`/`CRON_SECRET`. |
| Log: `ModuleNotFoundError: fastapi` | `SCM_DO_BUILD_DURING_DEPLOYMENT=true` is missing (Step 4a). Add it and re-run the pipeline. |
| Site: *quota exceeded* page | The Free F1 plan's 60 CPU-min/day is used up; it resets daily. **App Service plan → Scale up → B1** (≈ $13/mo) for interview days. |
| AI always "used a template" | Log stream shows `AI provider azure failed: …` or `gemini failed: …` with the reason. **401**: wrong key. **404** (Azure): wrong `AZURE_AI_DEPLOYMENT` name. **404** (Gemini): wrong `GEMINI_MODEL`. **429**: rate limit or quota. |

**Tear down everything:** portal → Resource groups → `planner-rg` → **Delete resource group**.

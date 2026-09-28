"""Azure Function (Python v2 model): wakes the planner once an hour.

The web app runs on App Service's free tier, which sleeps when idle, so it can't
keep its own clock. This timer calls POST {PLANNER_URL}/internal/cron/hourly with
a shared secret; the app then sends each user's morning plan / evening risk check
at the right hour in their own timezone and cleans up expired demo accounts.

App settings required on the Function App:
  PLANNER_URL   e.g. https://my-planner-app.azurewebsites.net
  CRON_SECRET   same value as the web app's CRON_SECRET
"""
import logging
import os
import urllib.request

import azure.functions as func

app = func.FunctionApp()


# NCRONTAB (6 fields, UTC): second minute hour day month weekday -> minute 2 of every hour
@app.timer_trigger(schedule="0 2 * * * *", arg_name="timer", run_on_startup=False,
                   use_monitor=True)
def hourly_planner_tick(timer: func.TimerRequest) -> None:
    url = os.environ["PLANNER_URL"].rstrip("/") + "/internal/cron/hourly"
    req = urllib.request.Request(url, method="POST", data=b"",
                                 headers={"X-Cron-Secret": os.environ["CRON_SECRET"]})
    # generous timeout: a sleeping free-tier app can take ~30-60s to cold start
    with urllib.request.urlopen(req, timeout=180) as resp:
        logging.info("planner cron -> %s %s", resp.status, resp.read(500).decode(errors="ignore"))

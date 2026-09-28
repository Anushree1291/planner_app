import os
import requests

# ntfy.sh is a free, open-source pub/sub push service. You "subscribe" to a
# secret topic name on your phone (app) and laptop (browser or desktop app),
# and this server "publishes" to that same topic. No accounts, keys, or
# app-store distribution needed. See README for setup.
NTFY_BASE = os.getenv("NTFY_BASE", "https://ntfy.sh")


def send_notification(topic: str, title: str, message: str, priority: str = "default",
                       tags: list[str] | None = None, click_url: str | None = None):
    if not topic:
        return  # notifications not configured for this user yet
    headers = {
        "Title": title,
        "Priority": priority,  # min, low, default, high, urgent
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url
    try:
        requests.post(f"{NTFY_BASE}/{topic}", data=message.encode("utf-8"),
                       headers=headers, timeout=10)
    except requests.RequestException:
        pass  # never let a notification failure break the planning flow


def notify_daily_plan(topic: str, num_tasks: int, first_task: str | None):
    msg = f"{num_tasks} tasks scheduled today."
    if first_task:
        msg += f" First up: {first_task}"
    send_notification(topic, "Today's plan is ready", msg, tags=["clipboard"])


def notify_what_now(topic: str, task_title: str, minutes: int):
    send_notification(
        topic, "Time to focus", f"{task_title} (~{minutes} min)",
        priority="high", tags=["dart"]
    )


def notify_risk(topic: str, goal_title: str, detail: str):
    send_notification(topic, f"⚠ {goal_title} is at risk", detail,
                       priority="high", tags=["warning"])

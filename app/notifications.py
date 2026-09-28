import requests

from .config import NTFY_BASE, PUBLIC_URL

# ntfy.sh is a free, open-source pub/sub push service. You "subscribe" to a
# secret topic name on your phone (app) and laptop (browser or desktop app),
# and this server "publishes" to that same topic. No accounts, keys, or
# app-store distribution needed. See README for setup.


def _header_safe(text: str) -> str:
    # HTTP headers must be latin-1; ntfy accepts UTF-8 titles via RFC 2047-ish,
    # but the simplest robust approach is to drop what latin-1 can't encode.
    return text.encode("latin-1", "ignore").decode("latin-1")[:250]


def send_notification(topic: str | None, title: str, message: str, priority: str = "default",
                      tags: list[str] | None = None, click_url: str | None = PUBLIC_URL):
    if not topic:
        return  # notifications not configured for this user yet
    headers = {"Title": _header_safe(title), "Priority": priority}
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
    send_notification(topic, "Time to focus", f"{task_title} (~{minutes} min)",
                      priority="high", tags=["dart"])


def notify_risk(topic: str, goal_title: str, detail: str):
    send_notification(topic, f"Goal at risk: {goal_title}", detail,
                      priority="high", tags=["warning"])

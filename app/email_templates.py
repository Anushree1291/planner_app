"""HTML + plain-text emails. Table layout and inline styles so they render in Gmail,
Outlook and phone mail apps. Every piece of user content goes through esc()."""
import datetime as dt
from html import escape as esc

ENERGY = {  # same colour language as the dashboard: (stripe, tint, label, label ink)
    "high": ("#E8962E", "#FFF4E3", "High focus", "#8A520B"),
    "medium": ("#2B9C93", "#E3F5F3", "Steady", "#13625B"),
    "low": ("#6A5FC8", "#EEECFB", "Light", "#3F3592"),
}
INK, MUTED, LINE, PAGE = "#1E2235", "#5F6680", "#E3E6EF", "#F1F3F8"
FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"


def _first_name(user) -> str:
    return (user.name or (user.email or "there").split("@")[0]).split(" ")[0]


def _greeting(now: dt.datetime) -> str:
    return "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"


def _hm(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def _layout(preheader: str, body: str, app_url: str, unsubscribe_url: str | None,
            show_open_button: bool = True) -> str:
    open_button = (f'''<tr><td style="padding:8px 28px 28px;{FONT}">
  <a href="{esc(app_url)}" style="display:inline-block;background:{INK};color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;padding:12px 20px;border-radius:10px;">Open my planner</a>
</td></tr>''' if show_open_button else '<tr><td style="height:20px;font-size:0;">&nbsp;</td></tr>')
    footer = (f'<a href="{esc(unsubscribe_url)}" style="color:{MUTED};">Stop these emails</a> &nbsp;or&nbsp; '
              if unsubscribe_url else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
</head><body style="margin:0;padding:0;background:{PAGE};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;">
<tr><td style="height:6px;background:linear-gradient(90deg,#E8962E,#2B9C93,#6A5FC8);background-color:#2B9C93;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 28px 8px;{FONT}color:{INK};">{body}</td></tr>
{open_button}
</table>
<p style="{FONT}font-size:12px;color:{MUTED};margin:16px 0 0;">{footer}<a href="{esc(app_url)}" style="color:{MUTED};">change email settings</a></p>
</td></tr></table></body></html>"""


def _plan_rows(items) -> str:
    rows = []
    for start, end, title, energy in items:
        color, tint, label, ink = ENERGY.get(energy or "medium", ENERGY["medium"])
        rows.append(f"""<tr>
<td style="padding:10px 12px 10px 0;width:98px;vertical-align:top;{FONT}font-size:14px;color:{MUTED};white-space:nowrap;">{esc(start)}–{esc(end)}</td>
<td style="padding:10px 0;vertical-align:top;border-top:1px solid {LINE};">
  <div style="border-left:4px solid {color};background:{tint};border-radius:8px;padding:10px 12px;{FONT}">
    <div style="font-size:15px;font-weight:600;color:{INK};">{esc(title)}</div>
    <div style="font-size:12px;color:{ink};margin-top:2px;">{label}</div>
  </div></td></tr>""")
    return "".join(rows)


def _goal_rows(goals) -> str:
    rows = []
    for title, pct, at_risk, detail in goals:
        bar = "#D64545" if at_risk else "#2B9C93"
        note = (f'<div style="font-size:12px;color:#D64545;margin-top:4px;">{esc(detail)}</div>'
                if at_risk else "")
        rows.append(f"""<tr><td style="padding:8px 0;{FONT}">
<div style="font-size:14px;color:{INK};"><b>{esc(title)}</b> <span style="color:{MUTED};">{int(pct)}% done</span></div>
<div style="background:{LINE};border-radius:99px;height:6px;margin-top:6px;"><div style="width:{max(int(pct), 2)}%;background:{bar};height:6px;border-radius:99px;"></div></div>
{note}</td></tr>""")
    return "".join(rows)


# ---------- messages ----------

def verification(user, verify_url: str, app_url: str, welcome: bool = True) -> tuple[str, str, str]:
    name = _first_name(user)
    subject = "Confirm your email for AI Execution Planner" if not welcome else f"Welcome, {name}! Confirm your email"
    intro = ("Your planner is ready. It fits your goals around your calendar and your energy, "
             "and replans when life gets in the way."
             if welcome else "Here's a fresh link to confirm your email address.")
    body = f"""<h1 style="font-size:24px;line-height:1.25;margin:0 0 12px;">Hi {esc(name)},</h1>
<p style="font-size:15px;line-height:1.6;color:{INK};margin:0 0 16px;">{intro}</p>
<p style="font-size:15px;line-height:1.6;color:{INK};margin:0 0 20px;">Confirm this address and we'll send your plan every morning, plus a heads-up in the evening if a goal starts slipping.</p>
<p style="margin:0 0 20px;"><a href="{esc(verify_url)}" style="display:inline-block;background:#2B9C93;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;padding:12px 20px;border-radius:10px;">Confirm my email</a></p>
<p style="font-size:13px;color:{MUTED};line-height:1.5;margin:0;">If you didn't sign up, ignore this email and nothing will be sent again.</p>"""
    text = (f"Hi {name},\n\n{intro}\n\nConfirm your email to get your daily plan:\n{verify_url}\n\n"
            "If you didn't sign up, ignore this email.")
    return subject, _layout("Confirm your email to get your daily plan.", body, app_url, None,
                            show_open_button=False), text


def daily_plan(user, now: dt.datetime, items, free_minutes: int, goals,
               app_url: str, unsubscribe_url: str) -> tuple[str, str, str]:
    name = _first_name(user)
    day = now.strftime("%A, %d %B").replace(" 0", " ")
    if items:
        first = items[0]
        subject = f"Your {now.strftime('%A')}: {len(items)} blocks, first up {first[2]}"[:120]
        lead = (f"You have <b>{_hm(free_minutes)}</b> of free time today. I've planned "
                f"<b>{len(items)}</b> focused block{'s' if len(items) != 1 else ''}, hardest work first.")
        plan_html = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_plan_rows(items)}</table>'
        plan_text = "\n".join(f"  {s}-{e}  {t}  ({ENERGY.get(en or 'medium', ENERGY['medium'])[2]})"
                              for s, e, t, en in items)
    else:
        subject = f"Your {now.strftime('%A')} is open"
        lead = "Nothing is scheduled today. Add a goal and I'll break it into steps that fit your day."
        plan_html, plan_text = "", "  (nothing scheduled)"
    risky = [g for g in goals if g[2]]
    goal_html = (f'<h2 style="font-size:16px;margin:24px 0 4px;">Your goals</h2>'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_goal_rows(goals[:5])}</table>'
                 if goals else "")
    body = f"""<p style="font-size:13px;color:{MUTED};margin:0 0 6px;">{esc(day)}</p>
<h1 style="font-size:24px;line-height:1.25;margin:0 0 10px;">{_greeting(now)}, {esc(name)}</h1>
<p style="font-size:15px;line-height:1.6;margin:0 0 18px;">{lead}</p>
{plan_html}{goal_html}"""
    text = (f"{_greeting(now)}, {name}\n{day}\n\nFree time today: {_hm(free_minutes)}\n\nToday's plan:\n"
            f"{plan_text}\n\n" + ("\n".join(f"  ! {t}: {d}" for t, _, _, d in risky) + "\n\n" if risky else "")
            + f"Open your planner: {app_url}\nStop these emails: {unsubscribe_url}")
    preheader = f"{len(items)} blocks planned, {_hm(free_minutes)} free." if items else "Your day is open."
    return subject, _layout(preheader, body, app_url, unsubscribe_url), text


def risk_alert(user, goals_at_risk, app_url: str, unsubscribe_url: str) -> tuple[str, str, str]:
    name = _first_name(user)
    n = len(goals_at_risk)
    subject = (f"Heads-up: {goals_at_risk[0][0]} is slipping" if n == 1
               else f"Heads-up: {n} goals are slipping")[:120]
    body = f"""<h1 style="font-size:24px;line-height:1.25;margin:0 0 10px;">Hi {esc(name)}, a quick check-in</h1>
<p style="font-size:15px;line-height:1.6;margin:0 0 8px;">At the current pace, {'this goal needs' if n == 1 else 'these goals need'} more time per day than is realistic. Tomorrow's plan will put {'it' if n == 1 else 'them'} first, or you could move the deadline or trim some tasks.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_goal_rows(goals_at_risk)}</table>"""
    text = (f"Hi {name},\n\nThese goals need more time per day than is realistic:\n"
            + "\n".join(f"  - {t} ({int(p)}% done): {d}" for t, p, _, d in goals_at_risk)
            + f"\n\nOpen your planner: {app_url}\nStop these emails: {unsubscribe_url}")
    return subject, _layout(subject, body, app_url, unsubscribe_url), text


def _button(url: str, label: str, color: str) -> str:
    return (f'<a href="{esc(url)}" style="display:inline-block;background:{color};color:#ffffff;text-decoration:none;'
            f'font-weight:600;font-size:15px;padding:12px 16px;border-radius:10px;margin:0 8px 8px 0;">{esc(label)}</a>')


def checkin(user, now: dt.datetime, links: dict, waiting: list[str], app_url: str,
            unsubscribe_url: str) -> tuple[str, str, str]:
    """For people whose days vary: ask how today looks before planning it."""
    name = _first_name(user)
    day = now.strftime("%A")
    subject = f"How does your {day} look, {name}?"
    todo = "".join(f'<li style="margin:4px 0;">{esc(t)}</li>' for t in waiting[:3])
    body = f"""<p style="font-size:13px;color:{MUTED};margin:0 0 6px;">{esc(now.strftime('%A, %d %B').replace(' 0', ' '))}</p>
<h1 style="font-size:24px;line-height:1.25;margin:0 0 10px;">{_greeting(now)}, {esc(name)}</h1>
<p style="font-size:15px;line-height:1.6;margin:0 0 18px;">Your days aren't all the same, so I plan each one on the morning. One tap and I'll fit today around it.</p>
<div style="margin:0 0 12px;">{_button(links['usual'], 'A usual day', '#2B9C93')}{_button(links['light'], 'Busy, keep it light', '#E8962E')}{_button(links['off'], 'Day off or on leave', '#6A5FC8')}</div>
{f'<p style="font-size:14px;color:{MUTED};margin:12px 0 4px;">Waiting for you:</p><ul style="font-size:15px;color:{INK};margin:0;padding-left:20px;">{todo}</ul>' if todo else ''}"""
    text = (f"{_greeting(now)}, {name}\n\nHow does today look? Tap one and I'll plan it:\n"
            f"  A usual day: {links['usual']}\n  Busy, keep it light: {links['light']}\n"
            f"  Day off or on leave: {links['off']}\n\n"
            + ("Waiting for you:\n" + "\n".join(f"  - {t}" for t in waiting[:3]) + "\n\n" if waiting else "")
            + f"Stop these emails: {unsubscribe_url}")
    return subject, _layout(f"One tap and I'll plan your {day}.", body, app_url, unsubscribe_url,
                            show_open_button=False), text


def digest(user, sections: list[dict], app_url: str, unsubscribe_url: str) -> tuple[str, str, str]:
    """Weekly ideas: safe books and Substack newsletters related to the person's goals."""
    name = _first_name(user)
    subject = f"{name}, a few ideas for your week"
    parts, text_parts = [], []
    for s in sections:
        items = ""
        for b in s.get("books", []):
            by = f" by {esc(b['author'])}" if b.get("author") else ""
            items += (f'<tr><td style="padding:8px 0;border-top:1px solid {LINE};{FONT}">'
                      f'<a href="{esc(b.get("url") or app_url)}" style="font-size:15px;font-weight:600;color:{INK};text-decoration:none;">{esc(b["title"])}</a>'
                      f'<span style="font-size:14px;color:{MUTED};">{by}{", " + str(b["pages"]) + " pages" if b.get("pages") else ""}</span>'
                      f'{"<div style=" + chr(34) + "font-size:13px;color:" + MUTED + ";margin-top:2px;" + chr(34) + ">" + esc(b["description"]) + "</div>" if b.get("description") else ""}</td></tr>')
            text_parts.append(f"  Book: {b['title']}{' by ' + b['author'] if b.get('author') else ''} {b.get('url', '')}")
        for n in s.get("newsletters", []):
            items += (f'<tr><td style="padding:8px 0;border-top:1px solid {LINE};{FONT}">'
                      f'<a href="{esc(n["url"])}" style="font-size:15px;font-weight:600;color:{INK};text-decoration:none;">{esc(n["name"])}</a>'
                      f'<span style="font-size:13px;color:#13625B;"> Substack</span>'
                      f'<div style="font-size:13px;color:{MUTED};margin-top:2px;">{esc(n.get("why", ""))}</div></td></tr>')
            text_parts.append(f"  Newsletter: {n['name']} {n['url']}")
        if items:
            parts.append(f'<h2 style="font-size:16px;margin:22px 0 4px;">Because you\'re working on {esc(s["goal"])}</h2>'
                         f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{items}</table>')
    body = f"""<h1 style="font-size:24px;line-height:1.25;margin:0 0 10px;">Hi {esc(name)}, here are a few ideas</h1>
<p style="font-size:15px;line-height:1.6;margin:0;">Picked to go with your goals this week. Add one as a goal and I'll fit it into your days.</p>
{''.join(parts)}"""
    text = f"Hi {name}, a few ideas for your week:\n\n" + "\n".join(text_parts) + f"\n\nStop these emails: {unsubscribe_url}"
    return subject, _layout("Books and newsletters picked for your goals.", body, app_url, unsubscribe_url), text

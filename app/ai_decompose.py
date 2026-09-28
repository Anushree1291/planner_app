import os
import json
import requests

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def decompose_goal(title: str, deadline: str | None) -> list[dict]:
    """Feature #3: Goal Decomposer Agent. Uses Claude to break a goal into
    concrete tasks with effort/priority/energy estimates. Falls back to a
    single placeholder task if no API key is configured."""
    if not ANTHROPIC_API_KEY:
        return [{"title": f"Plan out: {title}", "estimated_minutes": 30,
                  "priority": 3, "energy_required": "medium"}]

    prompt = f"""Break the goal "{title}"{f' (deadline: {deadline})' if deadline else ''} into
8-15 concrete, actionable tasks a person could schedule on a calendar.

Return ONLY a JSON array, no prose, no markdown fences. Each item:
{{"title": str, "estimated_minutes": int, "priority": int (1-5), "energy_required": "high"|"medium"|"low"}}
"""
    resp = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)

"""Book facts and safe suggestions.

- lookup_book(): page count, author and genre from Google Books, with Open Library as a fallback.
  Both are free; GOOGLE_BOOKS_API_KEY is optional and only raises Google's rate limit.
- similar_books(): a few books in the same genre, filtered hard for explicit or sensual
  content (maturity rating, genre and keyword checks), because these go out by email.
- newsletters(): Substack suggestions from the AI, each checked to really exist and filtered
  the same way. Skipped when no AI is configured.
"""
import json
import logging
import os
import re

import requests

log = logging.getLogger("planner.books")
TIMEOUT = 10
GOOGLE = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY = "https://openlibrary.org"

# Anything matching these never gets suggested. Deliberately broad.
BLOCKED = re.compile(
    r"\b(erotic\w*|erotica|sensual\w*|steamy|smut\w*|sex\w*|explicit|nsfw|xxx|bdsm|kink\w*|"
    r"fetish\w*|lust\w*|seduc\w*|porn\w*|spicy|nude|naked|hentai|harem|"
    r"adult (?:fiction|content|romance|novel)|dark romance|mature content|18\+)\b", re.I)
BLOCKED_GENRES = ("romance", "erotica", "erotic", "lgbt erotica", "adult")


def is_safe_text(*parts) -> bool:
    return not BLOCKED.search(" ".join(p for p in parts if p))


def _google_params(q: str, n: int) -> dict:
    p = {"q": q, "maxResults": n, "printType": "books"}
    key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        p["key"] = key
    return p


def _from_google(item: dict) -> dict:
    v = item.get("volumeInfo", {})
    return {
        "title": v.get("title", "").strip(),
        "subtitle": v.get("subtitle", ""),
        "author": ", ".join(v.get("authors", [])[:2]),
        "pages": int(v.get("pageCount") or 0),
        "categories": v.get("categories", []),
        "description": (v.get("description") or "")[:600],
        "mature": v.get("maturityRating") == "MATURE",
        "url": v.get("infoLink") or v.get("canonicalVolumeLink") or "",
        "year": (v.get("publishedDate") or "")[:4],
        "source": "Google Books",
    }


def _safe_book(b: dict) -> bool:
    genres = " ".join(b.get("categories", [])).lower()
    return (not b.get("mature") and not any(g in genres for g in BLOCKED_GENRES)
            and is_safe_text(b.get("title"), b.get("subtitle"), genres, b.get("description")))


def lookup_book(query: str) -> dict | None:
    """Best match with a page count, or None."""
    query = re.sub(r"^\s*(read|finish|reading)\s+", "", query.strip(), flags=re.I)[:200]
    try:
        r = requests.get(GOOGLE, params=_google_params(query, 5), timeout=TIMEOUT)
        r.raise_for_status()
        for item in r.json().get("items", []):
            b = _from_google(item)
            if b["title"] and b["pages"] > 0:
                return b
    except Exception as e:
        log.warning("Google Books lookup failed: %s", e)
    try:
        r = requests.get(f"{OPEN_LIBRARY}/search.json", timeout=TIMEOUT,
                         params={"q": query, "limit": 5,
                                 "fields": "title,author_name,number_of_pages_median,subject,first_publish_year,key"})
        r.raise_for_status()
        for d in r.json().get("docs", []):
            if d.get("number_of_pages_median"):
                return {"title": d.get("title", ""), "subtitle": "",
                        "author": ", ".join((d.get("author_name") or [])[:2]),
                        "pages": int(d["number_of_pages_median"]),
                        "categories": (d.get("subject") or [])[:5], "description": "", "mature": False,
                        "url": f"{OPEN_LIBRARY}{d.get('key', '')}", "year": str(d.get("first_publish_year") or ""),
                        "source": "Open Library"}
    except Exception as e:
        log.warning("Open Library lookup failed: %s", e)
    return None


def similar_books(book: dict, limit: int = 3, exclude: set[str] | None = None) -> list[dict]:
    """Safe books in the same genre as `book`."""
    exclude = {t.lower() for t in (exclude or set())} | {book.get("title", "").lower()}
    genres = [g for g in book.get("categories", []) if not any(x in g.lower() for x in BLOCKED_GENRES)]
    if not genres:
        return []
    out = []
    try:
        r = requests.get(GOOGLE, timeout=TIMEOUT,
                         params=_google_params(f'subject:"{genres[0].split("/")[0].strip()}"', 20))
        r.raise_for_status()
        for item in r.json().get("items", []):
            b = _from_google(item)
            if b["title"] and b["title"].lower() not in exclude and _safe_book(b):
                exclude.add(b["title"].lower())
                out.append({k: b[k] for k in ("title", "author", "pages", "url", "description")})
            if len(out) >= limit:
                break
    except Exception as e:
        log.warning("similar books lookup failed: %s", e)
    for b in out:
        b["description"] = (b["description"][:180].rsplit(" ", 1)[0] + "…") if len(b["description"]) > 180 else b["description"]
    return out


SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def substack_exists(subdomain: str) -> bool:
    if not SUBDOMAIN.match(subdomain or ""):
        return False
    try:
        r = requests.get(f"https://{subdomain}.substack.com/", timeout=8, allow_redirects=False)
        return r.status_code == 200
    except Exception:
        return False


def newsletters(topic: str, limit: int = 2) -> list[dict]:
    """Substack newsletters about `topic`, suggested by the AI and verified to exist."""
    from .ai_decompose import ai_configured, complete
    if not ai_configured() or not is_safe_text(topic):
        return []
    prompt = f"""Suggest 4 well-known, active Substack newsletters for someone interested in: {topic[:200]}
Only suggest newsletters that are suitable for all ages: nothing explicit, sexual, sensual or adult.
Return ONLY a JSON array: [{{"name": str, "subdomain": str (the part before .substack.com), "why": str (max 15 words)}}]"""
    try:
        text, _ = complete(prompt)
        start, end = text.find("["), text.rfind("]")
        items = json.loads(text[start:end + 1]) if start != -1 else []
    except Exception as e:
        log.warning("newsletter suggestions failed: %s", e)
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name, sub, why = str(it.get("name", ""))[:80], str(it.get("subdomain", "")).lower().strip(), str(it.get("why", ""))[:120]
        if is_safe_text(name, why) and substack_exists(sub):
            out.append({"name": name, "url": f"https://{sub}.substack.com", "why": why})
        if len(out) >= limit:
            break
    return out

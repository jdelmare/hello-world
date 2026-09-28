"""Collect recent items from RSS/Atom feeds, Hacker News, Reddit, Bluesky and Hugging Face.

Every collector is best-effort: a failing source is logged and skipped so one
dead feed never blocks the daily run.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "ai-cyber-dashboard/1.0 (+https://github.com/jdelmare/modeltrends)"}
TIMEOUT = 20


def log(msg: str) -> None:
    print(f"[collect] {msg}", file=sys.stderr)


def _parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _strip(text: str | None, limit: int = 400) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_feed(src: dict, since: dt.datetime) -> list[dict]:
    r = requests.get(src["url"], headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields = {_local(c.tag): c for c in el}
        title = _strip(fields["title"].text if "title" in fields else "", 300)
        link = ""
        if "link" in fields:
            link = fields["link"].get("href") or (fields["link"].text or "")
        for c in el:  # Atom may have several <link>; prefer rel=alternate
            if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                link = c.get("href")
        date = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in fields:
                date = _parse_date(fields[k].text)
                if date:
                    break
        if date and date < since:
            continue
        summary = ""
        for k in ("description", "summary", "content", "encoded"):
            if k in fields and fields[k].text:
                summary = _strip(fields[k].text)
                break
        out.append({
            "title": title, "url": link.strip(), "summary": summary,
            "date": (date or dt.datetime.now(dt.timezone.utc)).date().isoformat(),
            "source": src["name"], "kind": src["kind"],
        })
    return out


def fetch_hn(query: str, since: dt.datetime) -> list[dict]:
    r = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": query, "tags": "story", "numericFilters": f"created_at_i>{int(since.timestamp())}"},
        headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [{
        "title": h.get("title") or "", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
        "summary": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments on Hacker News",
        "date": h["created_at"][:10], "source": "Hacker News", "kind": "social",
        "engagement": (h.get("points") or 0) + (h.get("num_comments") or 0),
    } for h in r.json().get("hits", [])]


def fetch_reddit(sub: str, query: str, since: dt.datetime) -> list[dict]:
    r = requests.get(
        f"https://www.reddit.com/r/{sub}/search.json",
        params={"q": query, "restrict_sr": 1, "sort": "new", "t": "week", "limit": 50},
        headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for c in r.json().get("data", {}).get("children", []):
        d = c["data"]
        created = dt.datetime.fromtimestamp(d["created_utc"], dt.timezone.utc)
        if created < since:
            continue
        out.append({
            "title": d.get("title", ""), "url": "https://www.reddit.com" + d.get("permalink", ""),
            "summary": _strip(d.get("selftext", "")), "date": created.date().isoformat(),
            "source": f"r/{sub}", "kind": "social", "engagement": d.get("score", 0) + d.get("num_comments", 0),
        })
    return out


def _bsky_token() -> str | None:
    handle, pw = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
    if not (handle and pw):
        return None
    r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                      json={"identifier": handle, "password": pw}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["accessJwt"]


def fetch_bluesky(query: str, since: dt.datetime, token: str) -> list[dict]:
    r = requests.get(
        "https://bsky.social/xrpc/app.bsky.feed.searchPosts",
        params={"q": query, "since": since.isoformat().replace("+00:00", "Z"), "limit": 50, "sort": "top"},
        headers={**UA, "Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for p in r.json().get("posts", []):
        rkey = p["uri"].rsplit("/", 1)[-1]
        out.append({
            "title": _strip(p["record"].get("text", ""), 280),
            "url": f"https://bsky.app/profile/{p['author']['handle']}/post/{rkey}",
            "summary": "", "date": p["record"].get("createdAt", "")[:10],
            "source": f"Bluesky @{p['author']['handle']}", "kind": "social",
            "engagement": p.get("likeCount", 0) + p.get("repostCount", 0),
        })
    return out


def fetch_hf_models(term: str) -> list[dict]:
    """Recently created open-weight models — used to spot new releases."""
    r = requests.get("https://huggingface.co/api/models",
                     params={"search": term, "sort": "createdAt", "direction": -1, "limit": 20},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "title": f"New on Hugging Face: {m['id']}", "url": f"https://huggingface.co/{m['id']}",
        "summary": f"{m.get('downloads', 0)} downloads, {m.get('likes', 0)} likes",
        "date": (m.get("createdAt") or "")[:10], "source": "Hugging Face", "kind": "official",
        "engagement": m.get("likes", 0),
    } for m in r.json()]


def collect_all(cfg: dict, since: dt.datetime) -> list[dict]:
    items: list[dict] = []

    def run(label, fn, *args):
        try:
            got = fn(*args)
            items.extend(got)
            log(f"{label}: {len(got)}")
        except Exception as e:  # noqa: BLE001 — every source is best-effort
            log(f"{label}: FAILED ({type(e).__name__}: {e})")

    for src in cfg.get("rss", []):
        run(src["name"], fetch_feed, src, since)
    for q in cfg.get("hn_queries", []):
        run(f"HN '{q}'", fetch_hn, q, since)
    for r in cfg.get("reddit", []):
        run(f"r/{r['sub']}", fetch_reddit, r["sub"], r["query"], since)
    try:
        token = _bsky_token()
    except Exception as e:  # noqa: BLE001
        log(f"Bluesky auth FAILED ({e})")
        token = None
    if token:
        for q in cfg.get("bluesky_queries", []):
            run(f"Bluesky '{q}'", fetch_bluesky, q, since, token)
    else:
        log("Bluesky: skipped (set BSKY_HANDLE and BSKY_APP_PASSWORD to enable)")
    for term in cfg.get("huggingface_search", []):
        run(f"HF '{term}'", fetch_hf_models, term)

    seen, unique = set(), []
    for it in items:
        key = it["url"] or it["title"]
        if it["title"] and key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


AI_CYBER_TERMS = re.compile(
    r"\b(ai|llm|model|agent|claude|gpt|gemini|grok|llama|deepseek|qwen|kimi|glm|mythos|fable|muse)\b", re.I)
CYBER_TERMS = re.compile(
    r"(cyber|exploit|vulnerab|zero[- ]day|0day|sandbox|breach|hack|malware|pentest|ctf|cve|jailbreak|red[- ]team|security)", re.I)


def is_relevant(item: dict) -> bool:
    text = f"{item['title']} {item.get('summary', '')}"
    return bool(AI_CYBER_TERMS.search(text) and CYBER_TERMS.search(text)) or item["kind"] == "paper"


def match_models(item: dict, registry: list[dict]) -> list[str]:
    text = f"{item['title']} {item.get('summary', '')}".lower()
    hits = []
    for m in registry:
        names = [m["name"], *m.get("aliases", [])]
        if any(re.search(r"(?<![\w.])" + re.escape(n.lower()) + r"(?![\w]|\.\d)", text) for n in names):
            hits.append(m["id"])
    return hits

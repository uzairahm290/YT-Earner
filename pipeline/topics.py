"""Stage 1: find documentary-worthy topics.

Two signals:
  - RSS headlines from US tech outlets (what's in the news)
  - Wikipedia articles with a traffic spike (what people are curious about right now)

The output is a shortlist for YOU to pick from. Picking the topic is the human step.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

from .config import ROOT, load_config, user_agent

WIKI_API = "https://en.wikipedia.org/w/api.php"
PAGEVIEWS_TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m}/{d}"
FIXTURES = ROOT / "tests" / "fixtures"

# Wikipedia's top list is full of noise that's never a tech documentary.
_SKIP = re.compile(r"^(Main_Page|Special:|Wikipedia:|File:|Portal:|Help:|Talk:|Deaths_in_|List_of_|\d{4}_in_)")


def fetch_rss(feeds: list[str], offline: bool = False) -> list[dict]:
    items = []
    sources = [str(FIXTURES / "sample_feed.xml")] if offline else feeds
    for url in sources:
        try:
            parsed = feedparser.parse(url, agent=user_agent())
        except Exception as e:  # one dead feed shouldn't kill the run
            print(f"  ! feed failed: {url} ({e})")
            continue
        outlet = parsed.feed.get("title", url)
        for entry in parsed.entries[:30]:
            items.append({
                "kind": "news",
                "title": entry.get("title", "").strip(),
                "summary": re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:300].strip(),
                "url": entry.get("link", ""),
                "source": outlet,
            })
    return items


def fetch_wikipedia_trending(keywords: list[str], offline: bool = False) -> list[dict]:
    if offline:
        import json
        top = json.loads((FIXTURES / "sample_pageviews.json").read_text())["items"][0]["articles"]
        descriptions = json.loads((FIXTURES / "sample_descriptions.json").read_text())
    else:
        # Pageview data lands a day late, so look at yesterday.
        day = datetime.now(timezone.utc) - timedelta(days=1)
        url = PAGEVIEWS_TOP.format(y=day.strftime("%Y"), m=day.strftime("%m"), d=day.strftime("%d"))
        resp = requests.get(url, headers={"User-Agent": user_agent()}, timeout=30)
        resp.raise_for_status()
        top = resp.json()["items"][0]["articles"]
        titles = [a["article"] for a in top if not _SKIP.match(a["article"])][:150]
        descriptions = _describe(titles)

    kw = [k.lower() for k in keywords]
    trending = []
    for art in top:
        title = art["article"]
        if _SKIP.match(title):
            continue
        desc = descriptions.get(title, "")
        if not any(k in desc.lower() or k in title.lower() for k in kw):
            continue
        trending.append({
            "kind": "wiki_trend",
            "title": title.replace("_", " "),
            "summary": desc,
            "url": f"https://en.wikipedia.org/wiki/{title}",
            "source": f"Wikipedia trending (#{art['rank']}, {art['views']:,} views)",
            "views": art["views"],
        })
    return trending


def _describe(titles: list[str]) -> dict[str, str]:
    """Short Wikidata descriptions ("American technology company"), 50 titles per request."""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        resp = requests.get(WIKI_API, params={
            "action": "query", "format": "json", "prop": "description",
            "titles": "|".join(t.replace("_", " ") for t in batch),
        }, headers={"User-Agent": user_agent()}, timeout=30)
        resp.raise_for_status()
        for page in resp.json().get("query", {}).get("pages", {}).values():
            out[page["title"].replace(" ", "_")] = page.get("description", "")
    return out


def find_topics(offline: bool = False) -> list[dict]:
    cfg = load_config()["topics"]
    candidates = []
    if cfg.get("wikipedia_trending"):
        try:
            candidates += fetch_wikipedia_trending(cfg["trend_keywords"], offline=offline)
        except requests.RequestException as e:
            print(f"  ! Wikipedia trends unavailable ({e})")
    candidates += fetch_rss(cfg["rss_feeds"], offline=offline)

    # Drop near-duplicate headlines (same story from several outlets).
    seen, unique = set(), []
    for c in candidates:
        key = re.sub(r"[^a-z0-9 ]", "", c["title"].lower())[:50]
        if key and key not in seen:
            seen.add(key)
            unique.append(c)
    return unique[: cfg.get("max_candidates", 25)]


def format_topics(topics: list[dict]) -> str:
    lines = ["# Topic candidates", "",
             "Pick one with a real STORY in it: conflict, a rise, a fall, a mystery.",
             "A headline is a hook; the documentary is usually the bigger story behind it.", ""]
    for i, t in enumerate(topics, 1):
        lines.append(f"{i}. **{t['title']}**  \n   _{t['source']}_ — {t['summary'][:160]}  \n   {t['url']}")
    return "\n".join(lines)

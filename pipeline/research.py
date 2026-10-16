"""Stage 2: research.

Pulls the Wikipedia article for the topic (plus any extra articles you name),
then asks the LLM to turn it into a fact sheet: timeline, people, numbers,
turning points. Every fact carries a source so you can check it.

The fact sheet is the ONLY material the script writer is allowed to use.
That's what stops the model from inventing dates and quotes.
"""
from __future__ import annotations

import json

import requests

from .config import ROOT, load_prompt, user_agent
from .episode import Episode
from .llm import chat, extract_json, max_input_chars

WIKI_API = "https://en.wikipedia.org/w/api.php"
FIXTURES = ROOT / "tests" / "fixtures"


def resolve_title(query: str) -> str:
    """Turn a loose topic ("nokia downfall") into the best-matching article title."""
    resp = requests.get(WIKI_API, params={
        "action": "query", "format": "json", "list": "search", "srsearch": query, "srlimit": 1,
    }, headers={"User-Agent": user_agent()}, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("query", {}).get("search", [])
    if not hits:
        raise ValueError(f"Wikipedia has nothing for '{query}'")
    return hits[0]["title"]


def fetch_article(title: str) -> dict:
    resp = requests.get(WIKI_API, params={
        "action": "query", "format": "json", "redirects": 1, "titles": title,
        "prop": "extracts|extlinks|info", "explaintext": 1, "ellimit": 100, "inprop": "url",
    }, headers={"User-Agent": user_agent()}, timeout=30)
    resp.raise_for_status()
    page = next(iter(resp.json()["query"]["pages"].values()))
    if "missing" in page:
        raise ValueError(f"No Wikipedia article titled '{title}'")
    return {
        "title": page["title"],
        "url": page.get("fullurl", f"https://en.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"),
        "extract": page.get("extract", ""),
        # Real citations from the article: news pieces, filings, company pages. Your fact-check list.
        "external_links": [l["*"] for l in page.get("extlinks", []) if l.get("*", "").startswith("http")],
    }


def gather(topic: str, extra: list[str] | None = None, offline: bool = False) -> list[dict]:
    if offline:
        return [json.loads((FIXTURES / "sample_article.json").read_text())]
    titles = [resolve_title(topic)] + [resolve_title(e) for e in (extra or [])]
    articles = []
    for t in dict.fromkeys(titles):  # dedupe, keep order
        print(f"  fetching: {t}")
        articles.append(fetch_article(t))
    return articles


def build_corpus(articles: list[dict], budget: int) -> str:
    """Concatenate articles, splitting the character budget so every article gets a fair share."""
    share = budget // max(1, len(articles))
    parts = []
    for i, a in enumerate(articles, 1):
        text = a["extract"]
        if len(text) > share:
            text = text[:share] + "\n[...trimmed...]"
        parts.append(f"### SOURCE S{i}: {a['title']} ({a['url']})\n{text}")
    return "\n\n".join(parts)


def run(topic: str, extra: list[str] | None = None, offline: bool = False) -> Episode:
    ep = Episode.create(topic)
    print(f"[research] {topic} -> episodes/{ep.slug}/")

    articles = gather(topic, extra, offline=offline)
    ep.write("research/sources.json", articles)

    corpus = build_corpus(articles, max_input_chars())
    sources_index = {f"S{i}": a["url"] for i, a in enumerate(articles, 1)}

    reply = chat(
        system=load_prompt("factsheet_system.md"),
        user=f"TOPIC: {topic}\n\nSOURCES:\n\n{corpus}",
        task="factsheet",
        temperature=0.2,  # facts, not creativity
    )
    factsheet = extract_json(reply)
    factsheet["sources_index"] = sources_index
    factsheet["external_links"] = sorted({l for a in articles for l in a["external_links"]})[:60]

    ep.write("factsheet.json", factsheet)
    ep.write("factsheet.md", render_factsheet(topic, factsheet))
    ep.set_status("researched")
    print(f"  ✓ fact sheet: {len(factsheet.get('timeline', []))} timeline events, "
          f"{len(factsheet.get('needs_verification', []))} items flagged to verify")
    return ep


def render_factsheet(topic: str, fs: dict) -> str:
    """Human-readable version for your fact-check pass."""
    out = [f"# Fact sheet: {topic}", "", fs.get("summary", ""), ""]

    if fs.get("story_angles"):
        out += ["## Possible story angles", *[f"- {a}" for a in fs["story_angles"]], ""]
    if fs.get("timeline"):
        out += ["## Timeline", *[f"- **{e['date']}** — {e['event']} `[{e.get('source', '?')}]`"
                                 for e in fs["timeline"]], ""]
    if fs.get("people"):
        out += ["## People", *[f"- **{p['name']}** — {p['role']}" for p in fs["people"]], ""]
    if fs.get("key_numbers"):
        out += ["## Key numbers", *[f"- **{n['figure']}** — {n['context']} `[{n.get('source', '?')}]`"
                                    for n in fs["key_numbers"]], ""]
    if fs.get("turning_points"):
        out += ["## Turning points", *[f"- {t}" for t in fs["turning_points"]], ""]
    if fs.get("needs_verification"):
        out += ["## ⚠️ Verify before scripting", *[f"- [ ] {v}" for v in fs["needs_verification"]], ""]

    out += ["## Sources", *[f"- {k}: {v}" for k, v in fs.get("sources_index", {}).items()], ""]
    if fs.get("external_links"):
        out += ["## Citations from the articles (use these to fact-check)",
                *[f"- {l}" for l in fs["external_links"]], ""]
    return "\n".join(out)

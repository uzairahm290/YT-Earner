"""Stage 5: pictures for every beat.

For each beat the script asked for a kind of shot. This stage finds real candidates for it:

  archive_photo -> Wikimedia Commons (real, freely licensed photos of people/products/places)
  broll         -> Pexels / Pixabay stock video (needs a key), then Commons photos
  map           -> Commons maps of the place, then a place card
  document      -> a clean text card, flagged so you add a screenshot of the real document
  chart         -> a big-number stat card built from the figure in the script
  title_card    -> a title card

Nothing is AI-generated and nothing is faked. Every candidate keeps its license and credit,
which end up in the video description. You pick, swap or upload in the dashboard.

Only small metadata is stored (URLs, credits, choices). Full-size files get downloaded at
render time, so the database stays small.
"""
from __future__ import annotations

import base64
import html
import os
import re
import time
from datetime import datetime, timezone

import requests

from .config import env, load_config, user_agent
from .episode import Episode

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PEXELS_VIDEOS = "https://api.pexels.com/videos/search"
PIXABAY_VIDEOS = "https://pixabay.com/api/videos/"

CARD_TYPES = {"document", "chart", "title_card"}


def cfg() -> dict:
    return load_config()["visuals"]


def _session() -> requests.Session:
    if os.getenv("DOCU_MOCK_VISUALS") == "1":
        return _DemoSession()
    s = requests.Session()
    s.headers["User-Agent"] = user_agent()
    return s


class _DemoSession:
    """Demo mode: answers from the recorded test fixtures, with local sample images."""

    def get(self, url, params=None, headers=None, timeout=None):
        import json
        from pathlib import Path
        fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
        name = "commons_search.json" if "wikimedia" in url else "pexels_videos.json" if "pexels" in url else None
        text = (fixtures / name).read_text() if name else '{"hits": []}'
        for i, host in enumerate(("https://upload.wikimedia.org", "https://images.pexels.com"), 1):
            text = re.sub(re.escape(host) + r'[^"]*\.(?:jpe?g|png)', f"/static/demo/sample{i}.jpg", text)

        class R:
            status_code = 200
            def json(self): return json.loads(text)
            def raise_for_status(self): pass
        return R()


# ──────────────────────────── query cleanup ────────────────────────────

_MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december)"
_FILLER = re.compile(
    r"\b(official|announcement|unveiling|early|concept|rendering|photo|image|picture|news|headline|"
    r"report|close up of|footage|slow motion|fast motion|animated|animation of|graphic|illustration|"
    r"hands-on|impression|showing|highlighting|of a|of the|with a|in a|alongside)\b", re.I)


def query_variants(query: str) -> list[str]:
    """Progressively broader searches: exact, minus dates and filler, then just the proper nouns."""
    q = re.sub(r"\s+", " ", query).strip()
    out = [q]
    no_dates = re.sub(rf"\b{_MONTHS}\b|\b(1[89]\d{{2}}|20\d{{2}})s?\b|\b\d{{1,2}},?\b", " ", q, flags=re.I)
    no_dates = re.sub(r"\s+", " ", no_dates).strip(" ,-")
    out.append(no_dates)
    lean = re.sub(r"\s+", " ", _FILLER.sub(" ", no_dates)).strip(" ,-")
    out.append(lean)
    # Runs of words with a capital anywhere (catches "iPhone Duo", "John Ternus", "LG Display").
    names = re.findall(r"\b\w*[A-Z][\w&.-]*(?:\s+\w*[A-Z][\w&.-]*)*", lean)
    names = [n for n in names if " " in n or len(n) >= 5]
    if names:
        out.append(max(names, key=len))
    seen, variants = set(), []
    for v in out:
        if v and len(v) > 2 and v.lower() not in seen:
            seen.add(v.lower())
            variants.append(v)
    return variants


# ──────────────────────────── providers ────────────────────────────

def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _license_ok(name: str) -> bool:
    low = (name or "").lower()
    return any(low.startswith(a) or a in low for a in cfg()["allowed_licenses"])


def search_commons(query: str, limit: int, session: requests.Session, maps: bool = False) -> list[dict]:
    resp = session.get(COMMONS_API, params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 12,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 640,
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|ObjectName|Restrictions",
    }, timeout=20)
    resp.raise_for_status()
    pages = sorted(resp.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        if not maps and info.get("width", 0) < cfg()["min_photo_width"]:
            continue
        if not _license_ok(lic):
            continue
        thumb = info.get("thumburl") or info.get("url")
        # Commons thumbnails can be requested at any width by editing the path.
        full = thumb.replace("/640px-", "/1920px-") if info.get("width", 0) > 1920 and "/640px-" in thumb else info.get("url")
        out.append({
            "source": "commons", "kind": "image",
            "thumb": thumb, "url": full,
            "width": info.get("width"), "height": info.get("height"),
            "title": _strip_html(meta.get("ObjectName", {}).get("value", "")) or page.get("title", "").removeprefix("File:"),
            "credit": _strip_html(meta.get("Artist", {}).get("value", ""))[:120] or "Wikimedia Commons",
            "license": lic, "license_url": meta.get("LicenseUrl", {}).get("value"),
            "page": info.get("descriptionurl"),
        })
        if len(out) >= limit:
            break
    return out


def search_pexels(query: str, limit: int, session: requests.Session) -> list[dict]:
    key = env("PEXELS_API_KEY")
    if not key:
        return []
    resp = session.get(PEXELS_VIDEOS, params={"query": query, "orientation": "landscape", "per_page": limit + 2},
                       headers={"Authorization": key}, timeout=20)
    resp.raise_for_status()
    out = []
    for v in resp.json().get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("width") and f.get("file_type") == "video/mp4"]
        hd = sorted(files, key=lambda f: abs(f["width"] - 1920))
        if not hd:
            continue
        best = hd[0]
        out.append({
            "source": "pexels", "kind": "video",
            "thumb": v.get("image"), "url": best["link"],
            "width": best["width"], "height": best.get("height"), "duration": v.get("duration"),
            "title": query, "credit": (v.get("user") or {}).get("name", "Pexels"),
            "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
            "page": v.get("url"),
        })
        if len(out) >= limit:
            break
    return out


def search_pixabay(query: str, limit: int, session: requests.Session) -> list[dict]:
    key = env("PIXABAY_API_KEY")
    if not key:
        return []
    resp = session.get(PIXABAY_VIDEOS, params={"key": key, "q": query[:100], "per_page": max(3, limit + 2),
                                               "safesearch": "true"}, timeout=20)
    resp.raise_for_status()
    out = []
    for v in resp.json().get("hits", []):
        vids = v.get("videos", {})
        best = vids.get("large") if (vids.get("large") or {}).get("url") else vids.get("medium", {})
        if not best.get("url"):
            continue
        out.append({
            "source": "pixabay", "kind": "video",
            "thumb": best.get("thumbnail") or (vids.get("medium") or {}).get("thumbnail"),
            "url": best["url"], "width": best.get("width"), "height": best.get("height"),
            "duration": v.get("duration"), "title": v.get("tags", query),
            "credit": v.get("user", "Pixabay"),
            "license": "Pixabay Content License", "license_url": "https://pixabay.com/service/license-summary/",
            "page": v.get("pageURL"),
        })
        if len(out) >= limit:
            break
    return out


# ──────────────────────────── cards ────────────────────────────

_FIGURE = re.compile(
    r"(\$?\d[\d,.]*\s*(?:%|percent|million|billion|trillion|x)?(?:\s*(?:-|to|–)\s*\$?\d[\d,.]*\s*(?:%|percent|million|billion|trillion)?)?)",
    re.I)


def make_card(beat: dict) -> dict:
    """A clean on-brand card. Honest by design: text only, no fake screenshots."""
    v = beat["visual"]
    t, q = v["type"], v["query"].strip()
    if t == "chart":
        # Prefer a figure with a unit ("60 percent", "6-8 million") over a bare year.
        figures = [m for m in _FIGURE.finditer(q) if re.search(r"\d", m.group(1))]
        with_unit = [m for m in figures if re.search(r"%|percent|million|billion|trillion|\$|x\b", m.group(1), re.I)]
        not_year = [m for m in figures if not re.fullmatch(r"(1[89]|20)\d{2}", m.group(1).strip())]
        m = (with_unit or not_year or [None])[0]
        if m:
            figure = m.group(1).replace("percent", "%").replace(" %", "%").strip()
            label = re.sub(r"\s+", " ", (q[:m.start()] + q[m.end():])).strip(" ,-")
            return {"source": "card", "kind": "card", "card": "stat", "big": figure, "text": label}
        return {"source": "card", "kind": "card", "card": "title", "text": q}
    if t == "document":
        return {"source": "card", "kind": "card", "card": "document",
                "text": beat.get("on_screen_text") or q, "sub": q, "needs_screenshot": True}
    if t == "map":
        return {"source": "card", "kind": "card", "card": "place", "text": q}
    return {"source": "card", "kind": "card", "card": "title",
            "text": beat.get("on_screen_text") or q}


# ──────────────────────────── per beat ────────────────────────────

def _stock(query: str, limit: int, session) -> list[dict]:
    found = []
    for name in cfg()["stock_providers"]:
        fn = {"pexels": search_pexels, "pixabay": search_pixabay}.get(name)
        if fn and len(found) < limit:
            found += fn(query, limit - len(found), session)
    return found


def _commons(query: str, limit: int, session, maps: bool = False) -> list[dict]:
    for variant in query_variants(query):
        found = search_commons(f"{variant} map" if maps else variant, limit, session, maps=maps)
        if found:
            return found
    return []


def find_for_beat(beat: dict, session: requests.Session | None = None) -> dict:
    session = session or _session()
    limit = cfg()["candidates_per_beat"]
    v = beat["visual"]
    t, q = v["type"], v["query"]
    candidates, errors = [], []

    def attempt(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except requests.RequestException as e:
            errors.append(f"{fn.__name__.replace('search_', '').replace('_', '')}: {str(e)[:120]}")
            return []

    if t == "archive_photo":
        candidates = attempt(_commons, q, limit, session)
    elif t == "broll":
        candidates = attempt(_stock, q, limit, session)
        if len(candidates) < limit:
            candidates += attempt(_commons, q, limit - len(candidates), session)
    elif t == "map":
        candidates = attempt(_commons, q, limit, session, maps=True)
    elif t == "document":
        candidates = attempt(_commons, q, limit - 1, session)  # related real photos as alternates

    card = make_card(beat)
    # Cards lead for shots that are meant to be text; otherwise they're the last resort.
    if t in CARD_TYPES:
        candidates = [card] + candidates
    else:
        candidates = candidates + [card]

    return {
        "id": beat["id"], "type": t, "query": q,
        "candidates": candidates, "chosen": 0,
        "missing": t in ("archive_photo",) and candidates[0]["source"] == "card",
        "errors": errors,
    }


# ──────────────────────────── run ────────────────────────────

def run(slug: str, progress=None) -> Episode:
    ep = Episode.load(slug)
    script = ep.read("script.json")
    if not script:
        raise ValueError("There's no script to find pictures for yet.")
    old = {b["id"]: b for b in (ep.read("visuals.json") or {}).get("beats", [])}
    beats = [b for ch in script["chapters"] for b in ch["beats"]]
    session = _session()
    started = time.time()
    print(f"[visuals] {ep.read('meta.json')['topic']}: {len(beats)} beats")

    results = []
    for i, beat in enumerate(beats, 1):
        prev = old.get(beat["id"])
        if prev and prev["query"] == beat["visual"]["query"] and prev["type"] == beat["visual"]["type"]:
            results.append(prev)  # unchanged beat: keep its candidates and your pick
        else:
            results.append(find_for_beat(beat, session))
            time.sleep(0.15)  # be polite to Commons
        if progress:
            progress(i, len(beats))

    manifest = {
        "beats": results,
        "providers": {"commons": True, "pexels": bool(env("PEXELS_API_KEY")),
                      "pixabay": bool(env("PIXABAY_API_KEY"))},
        "found_at": datetime.now(timezone.utc).isoformat(),
    }
    ep.write("visuals.json", manifest)
    s = summary(manifest)
    print(f"  ✓ {s['real']} real pictures, {s['cards']} cards, {s['needs_you']} need you "
          f"({time.time() - started:.0f}s)")
    return ep


def summary(manifest: dict) -> dict:
    beats = manifest.get("beats", [])
    chosen = [b["candidates"][b["chosen"]] for b in beats if b["candidates"]]
    return {
        "beats": len(beats),
        "real": sum(c["source"] != "card" for c in chosen),
        "cards": sum(c["source"] == "card" for c in chosen),
        "needs_you": sum(1 for b, c in zip(beats, chosen) if c.get("needs_screenshot") or b.get("missing")),
    }


# ──────────────────────────── edits from the dashboard ────────────────────────────

def _beat(manifest: dict, beat_id: str) -> dict:
    for b in manifest.get("beats", []):
        if b["id"] == beat_id:
            return b
    raise KeyError(beat_id)


def choose(slug: str, beat_id: str, index: int) -> dict:
    ep = Episode.load(slug)
    m = ep.read("visuals.json")
    b = _beat(m, beat_id)
    if not 0 <= index < len(b["candidates"]):
        raise ValueError("That option doesn't exist.")
    b["chosen"] = index
    b["missing"] = False
    ep.write("visuals.json", m)
    return b


def research_beat(slug: str, beat_id: str, query: str) -> dict:
    """Search again for one beat with your own words. Keeps any upload you made."""
    ep = Episode.load(slug)
    m = ep.read("visuals.json")
    b = _beat(m, beat_id)
    uploads = [c for c in b["candidates"] if c["source"] == "upload"]
    fresh = find_for_beat({"id": beat_id, "visual": {"type": b["type"], "query": query},
                           "on_screen_text": None})
    b.update(candidates=uploads + fresh["candidates"], chosen=0, query=query,
             errors=fresh["errors"], missing=fresh["missing"] and not uploads)
    ep.write("visuals.json", m)
    return b


MAX_UPLOAD = 3_000_000  # bytes, after the browser resizes it


def add_upload(slug: str, beat_id: str, data_url: str, name: str) -> dict:
    """Your own screenshot or photo. Stored with the episode and chosen right away."""
    match = re.match(r"data:(image/(?:jpeg|png|webp));base64,(.+)", data_url or "", re.S)
    if not match:
        raise ValueError("Upload a JPG, PNG or WebP image.")
    raw = base64.b64decode(match.group(2))
    if len(raw) > MAX_UPLOAD:
        raise ValueError("That image is over 3 MB even after resizing.")
    ep = Episode.load(slug)
    m = ep.read("visuals.json")
    b = _beat(m, beat_id)
    n = sum(c["source"] == "upload" for c in b["candidates"]) + 1
    file = f"visuals/uploads/{beat_id}-{n}.b64"
    ep.write(file, f"{match.group(1)};{match.group(2)}")
    b["candidates"].insert(0, {
        "source": "upload", "kind": "image", "file": file,
        "title": (name or "Your upload")[:80], "credit": "Your upload", "license": "Yours",
    })
    b["chosen"] = 0
    b["missing"] = False
    ep.write("visuals.json", m)
    return b


def load_upload(ep: Episode, file: str) -> tuple[str, bytes] | None:
    data = ep.read(file)
    if not data:
        return None
    mime, b64 = data.split(";", 1)
    return mime, base64.b64decode(b64)


def credits(manifest: dict) -> list[str]:
    """One line per distinct source actually used, ready for the YouTube description."""
    lines, seen = [], set()
    for b in manifest.get("beats", []):
        if not b["candidates"]:
            continue
        c = b["candidates"][b["chosen"]]
        if c["source"] in ("card", "upload") or c.get("page") in seen:
            continue
        seen.add(c.get("page"))
        where = {"commons": "Wikimedia Commons", "pexels": "Pexels", "pixabay": "Pixabay"}[c["source"]]
        lines.append(f"{c.get('title') or 'Untitled'}: {c.get('credit')} / {where} ({c.get('license')}) {c.get('page') or ''}".strip())
    return lines

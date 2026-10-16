#!/usr/bin/env python3
"""Production desk: the web dashboard for the pipeline.

  python app.py            # real mode, uses your .env keys
  python app.py --demo     # offline fixtures + canned LLM replies, no keys needed

Then open http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from pipeline import research, script, topics
from pipeline.config import EPISODES_DIR, ROOT, env, load_config
from pipeline.episode import Episode, slugify

DEMO = os.getenv("DOCU_MOCK_LLM") == "1"

# The full flow, in order. `built: False` stages show in the UI as coming next.
STAGES = [
    {"key": "research", "label": "Research", "status": "researched", "built": True},
    {"key": "verify", "label": "Verify facts", "status": "verified", "built": True},
    {"key": "script", "label": "Script", "status": "scripted", "built": True},
    {"key": "approve", "label": "Approve script", "status": "approved", "built": True},
    {"key": "voice", "label": "Voice", "status": "voiced", "built": False},
    {"key": "visuals", "label": "Visuals", "status": "visuals_ready", "built": False},
    {"key": "render", "label": "Render", "status": "rendered", "built": False},
    {"key": "shorts", "label": "Shorts", "status": "shorts_ready", "built": False},
    {"key": "upload", "label": "Upload", "status": "uploaded", "built": False},
]
STATUS_ORDER = ["new"] + [s["status"] for s in STAGES]

app = FastAPI(title="docu-pipeline")


# ──────────────────────────── background jobs ────────────────────────────
# LLM calls take 20-90s on free tiers, so they run in the background and the UI polls.

_pool = ThreadPoolExecutor(max_workers=2)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def start_job(kind: str, label: str, fn, *, slug: str | None = None) -> dict:
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "label": label, "slug": slug,
           "status": "running", "started": time.time(), "finished": None, "error": None, "result": None}
    with _lock:
        _jobs[job["id"]] = job

    def run():
        try:
            job["result"] = fn()
            job["status"] = "done"
        except Exception as e:  # surfaced in the UI, full trace in the terminal
            traceback.print_exc()
            job["status"] = "error"
            job["error"] = str(e) or e.__class__.__name__
        finally:
            job["finished"] = time.time()

    _pool.submit(run)
    return job


@app.get("/api/jobs")
def list_jobs():
    cutoff = time.time() - 600
    return [j for j in _jobs.values() if j["status"] == "running" or (j["finished"] or 0) > cutoff]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "No such job")
    return _jobs[job_id]


# ──────────────────────────── setup / status ────────────────────────────

@app.get("/api/status")
def status():
    cfg = load_config()
    llm = cfg["llm"]
    prov = llm["providers"][llm["provider"]]
    return {
        "demo": DEMO,
        "channel": cfg["channel"]["name"],
        "provider": llm["provider"],
        "model": prov["model"],
        "key_present": bool(env(prov["api_key_env"])) or DEMO,
        "key_env": prov["api_key_env"],
        "wiki_agent_set": bool(env("WIKI_USER_AGENT")) or DEMO,
        "stages": STAGES,
        "script_cfg": cfg["script"],
        "banned_phrases": cfg["banned_phrases"],
    }


# ──────────────────────────── topics ────────────────────────────

TOPICS_FILE = EPISODES_DIR / "_topics-latest.json"


@app.get("/api/topics")
def get_topics():
    if not TOPICS_FILE.exists():
        return {"fetched": None, "items": []}
    return json.loads(TOPICS_FILE.read_text())


@app.post("/api/topics/refresh")
def refresh_topics():
    def work():
        items = topics.find_topics(offline=DEMO)
        EPISODES_DIR.mkdir(exist_ok=True)
        data = {"fetched": date.today().isoformat(), "items": items}
        TOPICS_FILE.write_text(json.dumps(data, indent=2))
        return {"count": len(items)}
    return start_job("topics", "Finding topics", work)


# ──────────────────────────── episodes ────────────────────────────

def summarize(ep: Episode) -> dict:
    meta = ep.read("meta.json") or {}
    lint = ep.read("lint.json")
    fs = ep.read("factsheet.json") or {}
    verify = fs.get("needs_verification", [])
    return {
        "slug": ep.slug,
        "topic": meta.get("topic", ep.slug),
        "status": meta.get("status", "new"),
        "created_at": meta.get("created_at"),
        "verify_total": len(verify),
        "verify_done": len(meta.get("verify_checked", [])),
        "est_minutes": lint.get("est_minutes") if lint else None,
        "running": any(j["slug"] == ep.slug and j["status"] == "running" for j in _jobs.values()),
    }


@app.get("/api/episodes")
def list_episodes():
    if not EPISODES_DIR.exists():
        return []
    eps = [Episode(p.name) for p in EPISODES_DIR.iterdir() if p.is_dir()]
    out = [summarize(e) for e in eps]
    return sorted(out, key=lambda e: e["created_at"] or "", reverse=True)


@app.get("/api/episodes/{slug}")
def get_episode(slug: str):
    ep = _load(slug)
    return {**summarize(ep),
            "meta": ep.read("meta.json"),
            "factsheet": ep.read("factsheet.json"),
            "script": ep.read("script.json"),
            "lint": ep.read("lint.json")}


class NewEpisode(BaseModel):
    topic: str
    extra: list[str] = []


@app.post("/api/episodes")
def create_episode(body: NewEpisode):
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(422, "Give the story a topic first.")
    slug = slugify(topic)
    Episode.create(topic)  # folder exists right away so the UI can open it
    extra = [e.strip() for e in body.extra if e.strip()]

    def work():
        ep = research.run(topic, extra=extra, offline=DEMO)
        meta = ep.read("meta.json")
        meta["verify_checked"] = []
        ep.write("meta.json", meta)
        return {"slug": ep.slug}
    job = start_job("research", f"Researching {topic}", work, slug=slug)
    return {"slug": slug, "job": job}


@app.post("/api/episodes/{slug}/research")
def rerun_research(slug: str, body: NewEpisode):
    ep = _load(slug)
    topic = ep.read("meta.json")["topic"]
    extra = [e.strip() for e in body.extra if e.strip()]

    def work():
        research.run(topic, extra=extra, offline=DEMO)
        meta = ep.read("meta.json")
        meta["verify_checked"] = []
        ep.write("meta.json", meta)
        return {"slug": slug}
    return start_job("research", f"Re-researching {topic}", work, slug=slug)


@app.put("/api/episodes/{slug}/factsheet")
def save_factsheet(slug: str, factsheet: dict):
    _load(slug)
    research.save_factsheet(slug, factsheet)
    return get_episode(slug)


class VerifyBody(BaseModel):
    checked: list[int]


@app.put("/api/episodes/{slug}/verify")
def set_verify(slug: str, body: VerifyBody):
    ep = _load(slug)
    meta = ep.read("meta.json")
    total = len((ep.read("factsheet.json") or {}).get("needs_verification", []))
    meta["verify_checked"] = sorted({i for i in body.checked if 0 <= i < total})
    ep.write("meta.json", meta)
    all_done = len(meta["verify_checked"]) == total
    if all_done and _rank(meta["status"]) < _rank("verified"):
        ep.set_status("verified")
    elif not all_done and meta["status"] == "verified":
        ep.set_status("researched")
    return summarize(ep)


class ScriptRequest(BaseModel):
    angle: str | None = None
    notes: str | None = None


@app.post("/api/episodes/{slug}/script")
def generate_script(slug: str, body: ScriptRequest):
    ep = _load(slug)
    if not ep.read("factsheet.json"):
        raise HTTPException(409, "Research this story before writing the script.")
    meta = ep.read("meta.json")
    meta["angle"], meta["notes"] = body.angle, body.notes
    ep.write("meta.json", meta)
    return start_job("script", f"Writing script: {meta['topic']}",
                     lambda: (script.run(slug, angle=body.angle, notes=body.notes), {"slug": slug})[1],
                     slug=slug)


@app.put("/api/episodes/{slug}/script")
def save_script(slug: str, data: dict):
    ep = _load(slug)
    try:
        script.save(slug, data)
    except ValidationError as e:
        first = e.errors()[0]
        where = " → ".join(str(p) for p in first["loc"])
        raise HTTPException(422, f"{where}: {first['msg']}")
    # Any edit after approval sends it back for another look.
    if ep.read("meta.json")["status"] == "approved":
        ep.set_status("scripted")
    return get_episode(slug)


@app.post("/api/episodes/{slug}/approve")
def approve(slug: str):
    ep = _load(slug)
    lint = ep.read("lint.json")
    if not lint:
        raise HTTPException(409, "There's no script to approve yet.")
    if not lint["ok"]:
        raise HTTPException(409, "Fix the flagged phrases and unbacked numbers before approving.")
    ep.set_status("approved")
    return get_episode(slug)


@app.post("/api/episodes/{slug}/unapprove")
def unapprove(slug: str):
    ep = _load(slug)
    if ep.read("meta.json")["status"] == "approved":
        ep.set_status("scripted")
    return get_episode(slug)


@app.delete("/api/episodes/{slug}")
def delete_episode(slug: str):
    ep = _load(slug)
    shutil.rmtree(ep.dir)
    return {"deleted": slug}


def _load(slug: str) -> Episode:
    try:
        return Episode.load(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"No episode called '{slug}'")


def _rank(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else 0


# ──────────────────────────── frontend ────────────────────────────

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="offline fixtures + canned LLM, no keys needed")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if a.demo:
        os.environ["DOCU_MOCK_LLM"] = "1"
    import uvicorn
    print(f"\n  Production desk → http://localhost:{a.port}  {'(demo mode)' if a.demo else ''}\n")
    uvicorn.run("app:app", host="127.0.0.1", port=a.port, reload=False)

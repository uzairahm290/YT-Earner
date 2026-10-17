#!/usr/bin/env python3
"""Production desk: the web dashboard for the pipeline.

  python app.py            # real mode, uses your .env keys
  python app.py --demo     # offline fixtures + canned LLM replies, no keys needed

Then open http://localhost:8000

Deployed (Render): DESK_PASSWORD turns on the login, DATABASE_URL points storage at Supabase.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from pipeline import research, script, topics, visuals, voice
from pipeline.config import ROOT, env, load_config
from pipeline.episode import Episode, slugify
from pipeline.store import get_store

DEMO = os.getenv("DOCU_MOCK_LLM") == "1"
# Heavy stages normally run on the GitHub Actions worker. Locally (or in demo mode) the
# dashboard can run them itself: set INLINE_WORKER=1, with requirements-worker.txt installed.
INLINE_WORKER = DEMO or os.getenv("INLINE_WORKER") == "1"
if DEMO:
    os.environ.setdefault("DOCU_MOCK_TTS", "1")
    os.environ.setdefault("DOCU_MOCK_VISUALS", "1")

# The full flow, in order. `built: False` stages show in the UI as coming next.
STAGES = [
    {"key": "research", "label": "Research", "status": "researched", "built": True},
    {"key": "verify", "label": "Verify facts", "status": "verified", "built": True},
    {"key": "script", "label": "Script", "status": "scripted", "built": True},
    {"key": "approve", "label": "Approve script", "status": "approved", "built": True},
    {"key": "voice", "label": "Voice", "status": "voiced", "built": True},
    {"key": "visuals", "label": "Visuals", "status": "visuals_ready", "built": True},
    {"key": "render", "label": "Render", "status": "rendered", "built": True},
    {"key": "shorts", "label": "Shorts", "status": "shorts_ready", "built": False},
    {"key": "upload", "label": "Upload", "status": "uploaded", "built": False},
]
STATUS_ORDER = ["new"] + [s["status"] for s in STAGES]

app = FastAPI(title="docu-pipeline")


# ──────────────────────────── login ────────────────────────────
# One user, one password. The session cookie is an HMAC keyed by the password itself,
# so changing DESK_PASSWORD signs every device out.

PASSWORD = env("DESK_PASSWORD")
COOKIE = "desk_session"
OPEN_PATHS = ("/login", "/api/login", "/healthz", "/static/")

if os.getenv("RENDER") and not PASSWORD:
    raise SystemExit("DESK_PASSWORD is not set. Refusing to run on a public URL without a login.")


def _session_token() -> str:
    return hmac.new(PASSWORD.encode(), b"production-desk-session-v1", hashlib.sha256).hexdigest()


def _signed_in(request: Request) -> bool:
    return not PASSWORD or hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_token())


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if _signed_in(request) or path.startswith(OPEN_PATHS):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Signed out. Reload to sign in again."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


_failed: dict[str, list[float]] = {}


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    if not PASSWORD:
        return {"ok": True}
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    recent = [t for t in _failed.get(ip, []) if t > time.time() - 900]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many wrong passwords. Try again in 15 minutes.")
    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        _failed[ip] = recent + [time.time()]
        time.sleep(1)  # slows guessing
        raise HTTPException(401, "That password isn't right.")
    _failed.pop(ip, None)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _session_token(), max_age=60 * 60 * 24 * 30, httponly=True,
                    secure=https, samesite="lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "web" / "login.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


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
        "auth": bool(PASSWORD),
        "storage": get_store().kind,
        "channel": cfg["channel"]["name"],
        "provider": llm["provider"],
        "model": prov["model"],
        "key_present": bool(env(prov["api_key_env"])) or DEMO,
        "key_env": prov["api_key_env"],
        "wiki_agent_set": bool(env("WIKI_USER_AGENT")) or DEMO,
        "stages": STAGES,
        "script_cfg": cfg["script"],
        "banned_phrases": cfg["banned_phrases"],
        "voices": voice.voice_choices(),
        "voice_default": cfg["voice"]["default"],
        "stock_video": [p for p in ("pexels", "pixabay") if env(f"{p.upper()}_API_KEY")],
        "preview_seconds": cfg["render"]["preview_seconds"],
        "worker": "inline" if INLINE_WORKER else ("instant" if _dispatch_ready() else "every 10 minutes"),
    }


# ──────────────────────────── topics ────────────────────────────

@app.get("/api/topics")
def get_topics():
    return topics.load_latest() or {"fetched": None, "items": []}


@app.post("/api/topics/refresh")
def refresh_topics():
    def work():
        items = topics.find_topics(offline=DEMO)
        topics.save_latest(items)
        return {"count": len(items)}
    return start_job("topics", "Finding topics", work)


# ──────────────────────────── episodes ────────────────────────────

SUMMARY_FILES = ["meta.json", "lint.json", "factsheet.json"]


def summarize(ep: Episode, files: dict | None = None) -> dict:
    files = files if files is not None else {n: ep.read(n) for n in SUMMARY_FILES}
    meta = files.get("meta.json") or {}
    lint = files.get("lint.json")
    fs = files.get("factsheet.json") or {}
    verify = fs.get("needs_verification", [])
    return {
        "slug": ep.slug,
        "topic": meta.get("topic", ep.slug),
        "status": meta.get("status", "new"),
        "created_at": meta.get("created_at"),
        "verify_total": len(verify),
        "verify_done": len(meta.get("verify_checked", [])),
        "est_minutes": lint.get("est_minutes") if lint else None,
        "voice_state": (meta.get("voice") or {}).get("state"),
        "render_state": (meta.get("render") or {}).get("state"),
        "running": any(j["slug"] == ep.slug and j["status"] == "running" for j in _jobs.values()),
    }


@app.get("/api/episodes")
def list_episodes():
    out = [summarize(Episode(slug), files) for slug, files in Episode.read_many(SUMMARY_FILES).items()]
    return sorted(out, key=lambda e: e["created_at"] or "", reverse=True)


@app.get("/api/episodes/{slug}")
def get_episode(slug: str):
    ep = _load(slug)
    sc = ep.read("script.json")
    timings = ep.read("voice/timings.json")
    if timings and sc:
        # The narration was recorded from an older version of the script.
        timings["stale"] = timings.get("script_hash") != voice.script_hash(sc)
    vis = ep.read("visuals.json")
    if vis:
        vis["summary"] = visuals.summary(vis)
        vis["credits"] = visuals.credits(vis)
    return {**summarize(ep),
            "meta": ep.read("meta.json"),
            "factsheet": ep.read("factsheet.json"),
            "script": sc,
            "lint": ep.read("lint.json"),
            "voice": timings,
            "visuals": vis}


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
    # Any edit after approval sends it back for another look (and a new narration).
    if ep.read("meta.json")["status"] in ("approved", "voiced", "visuals_ready", "rendered"):
        ep.set_status("scripted")
    return get_episode(slug)


class ApproveBody(BaseModel):
    voice: str | None = None
    narrate: bool = True


@app.post("/api/episodes/{slug}/approve")
def approve(slug: str, body: ApproveBody | None = None):
    body = body or ApproveBody()
    ep = _load(slug)
    lint = ep.read("lint.json")
    if not lint:
        raise HTTPException(409, "There's no script to approve yet.")
    if not lint["ok"]:
        raise HTTPException(409, "Fix the flagged phrases and unbacked numbers before approving.")
    ep.set_status("approved")
    if body.narrate:
        _queue_voice(ep, body.voice)
    return get_episode(slug)


@app.post("/api/episodes/{slug}/unapprove")
def unapprove(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if (meta.get("voice") or {}).get("state") in ("queued",):
        meta["voice"]["state"] = "cancelled"
        ep.write("meta.json", meta)
    if meta["status"] in ("approved", "voiced", "visuals_ready", "rendered"):
        ep.set_status("scripted")
    return get_episode(slug)


# ──────────────────────────── voice ────────────────────────────

class VoiceBody(BaseModel):
    voice: str | None = None


@app.post("/api/episodes/{slug}/voice")
def request_voice(slug: str, body: VoiceBody):
    ep = _load(slug)
    if ep.read("meta.json")["status"] not in ("approved", "voiced", "visuals_ready", "rendered"):
        raise HTTPException(409, "Approve the script first. Narration is recorded from the approved version.")
    if ep.read("meta.json")["status"] in ("voiced", "visuals_ready", "rendered"):
        ep.set_status("approved")
    _queue_voice(ep, body.voice)
    return get_episode(slug)


@app.post("/api/episodes/{slug}/voice/cancel")
def cancel_voice(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    job = meta.get("voice") or {}
    if job.get("state") == "running":
        raise HTTPException(409, "The narrator already started. It'll finish in a few minutes.")
    if job.get("state") in ("queued", "error"):
        job["state"] = "cancelled"
        ep.write("meta.json", meta)
    return get_episode(slug)


@app.get("/api/episodes/{slug}/audio")
def narration_audio(slug: str):
    ep = _load(slug)
    mp3 = voice.load_mp3(ep)
    if not mp3:
        raise HTTPException(404, "No narration recorded yet.")
    return Response(mp3, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=300"})


# ──────────────────────────── visuals ────────────────────────────

@app.post("/api/episodes/{slug}/visuals")
def find_visuals(slug: str):
    ep = _load(slug)
    if ep.read("meta.json")["status"] not in ("approved", "voiced", "visuals_ready", "rendered"):
        raise HTTPException(409, "Approve the script first. Pictures are found for the approved beats.")
    topic = ep.read("meta.json")["topic"]
    return start_job("visuals", f"Finding pictures: {topic}", lambda: (visuals.run(slug), {"slug": slug})[1],
                     slug=slug)


class VisualEdit(BaseModel):
    choice: int | None = None
    query: str | None = None


@app.put("/api/episodes/{slug}/visuals/{beat_id}")
def edit_visual(slug: str, beat_id: str, body: VisualEdit):
    ep = _load(slug)
    try:
        if body.query is not None and body.query.strip():
            visuals.research_beat(slug, beat_id, body.query.strip())
        elif body.choice is not None:
            visuals.choose(slug, beat_id, body.choice)
    except KeyError:
        raise HTTPException(404, "That beat isn't in the picture list. Find pictures again.")
    except ValueError as e:
        raise HTTPException(422, str(e))
    _unapprove_visuals(ep)
    return get_episode(slug)


class UploadBody(BaseModel):
    data_url: str
    name: str = ""


@app.post("/api/episodes/{slug}/visuals/{beat_id}/upload")
def upload_visual(slug: str, beat_id: str, body: UploadBody):
    ep = _load(slug)
    try:
        visuals.add_upload(slug, beat_id, body.data_url, body.name)
    except KeyError:
        raise HTTPException(404, "That beat isn't in the picture list.")
    except ValueError as e:
        raise HTTPException(422, str(e))
    _unapprove_visuals(ep)
    return get_episode(slug)


@app.get("/api/episodes/{slug}/uploads/{name}")
def get_upload(slug: str, name: str):
    ep = _load(slug)
    if not re.fullmatch(r"c\d+b\d+-\d+\.b64", name):
        raise HTTPException(404, "No such upload")
    found = visuals.load_upload(ep, f"visuals/uploads/{name}")
    if not found:
        raise HTTPException(404, "No such upload")
    mime, data = found
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/episodes/{slug}/visuals/approve")
def approve_visuals(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if not ep.read("visuals.json"):
        raise HTTPException(409, "Find pictures first.")
    if meta["status"] != "voiced":
        raise HTTPException(409, "Record the narration first. The cut is timed to the voice.")
    ep.set_status("visuals_ready")
    return get_episode(slug)


def _unapprove_visuals(ep: Episode) -> None:
    if ep.read("meta.json")["status"] in ("visuals_ready", "rendered"):
        ep.set_status("voiced")


# ──────────────────────────── render ────────────────────────────

@app.post("/api/episodes/{slug}/render")
def request_render(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if meta["status"] not in ("visuals_ready", "rendered"):
        raise HTTPException(409, "Approve the narration and pictures first.")
    if (meta.get("render") or {}).get("state") in ("queued", "running"):
        raise HTTPException(409, "This video is already in the render queue.")
    meta["render"] = {"state": "queued", "requested_at": datetime.now(timezone.utc).isoformat()}
    ep.write("meta.json", meta)
    if meta["status"] == "rendered":
        ep.set_status("visuals_ready")
    if INLINE_WORKER:
        from pipeline import worker
        start_job("render", f"Rendering {meta['topic']}", lambda: worker.run_jobs("render"), slug=slug)
    else:
        _dispatch_worker()
    return get_episode(slug)


@app.post("/api/episodes/{slug}/render/cancel")
def cancel_render(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    job = meta.get("render") or {}
    if job.get("state") == "running":
        raise HTTPException(409, "The render already started. It can't be stopped from here.")
    if job.get("state") in ("queued", "error"):
        job["state"] = "cancelled"
        ep.write("meta.json", meta)
    return get_episode(slug)


@app.get("/api/episodes/{slug}/preview")
def render_preview(slug: str):
    from pipeline import render
    data = render.load_preview(_load(slug))
    if not data:
        raise HTTPException(404, "No preview yet.")
    return Response(data, media_type="video/mp4", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/episodes/{slug}/captions.srt")
def render_captions(slug: str):
    srt = _load(slug).read("render/captions.srt")
    if not srt:
        raise HTTPException(404, "No captions yet.")
    return Response(srt, media_type="application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.srt"'})


def _queue_voice(ep: Episode, voice_id: str | None) -> None:
    voice_id = voice_id or load_config()["voice"]["default"]
    if voice_id not in voice.voice_choices():
        raise HTTPException(422, f"Unknown voice '{voice_id}'.")
    meta = ep.read("meta.json")
    if (meta.get("voice") or {}).get("state") == "running":
        raise HTTPException(409, "A narration is already being recorded for this story.")
    meta["voice"] = {"state": "queued", "voice": voice_id,
                     "requested_at": datetime.now(timezone.utc).isoformat()}
    ep.write("meta.json", meta)
    if INLINE_WORKER:
        from pipeline import worker
        start_job("voice", f"Narrating {meta['topic']}", lambda: worker.run_voice_jobs(), slug=ep.slug)
    else:
        _dispatch_worker()


def _dispatch_ready() -> bool:
    return bool(env("GITHUB_DISPATCH_TOKEN") and env("GITHUB_REPO"))


def _dispatch_worker() -> None:
    """Wake the GitHub Actions worker now instead of waiting for its 10-minute schedule."""
    if not _dispatch_ready():
        return

    def go():
        try:
            r = requests.post(
                f"https://api.github.com/repos/{env('GITHUB_REPO')}/actions/workflows/produce.yml/dispatches",
                headers={"Authorization": f"Bearer {env('GITHUB_DISPATCH_TOKEN')}",
                         "Accept": "application/vnd.github+json"},
                json={"ref": "main"}, timeout=15)
            if r.status_code >= 300:
                print(f"  ! couldn't wake the worker ({r.status_code}): {r.text[:200]}")
        except requests.RequestException as e:
            print(f"  ! couldn't wake the worker: {e}")
    threading.Thread(target=go, daemon=True).start()


@app.delete("/api/episodes/{slug}")
def delete_episode(slug: str):
    ep = _load(slug)
    ep.delete()
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

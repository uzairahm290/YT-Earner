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

from pipeline import audio, illustrate, narrator_gen, persona, research, script, shorts, story, thumbnail, topics, visuals, voice, voiceclone, voicelab, youtube
from pipeline.config import ROOT, env, load_config
from pipeline.episode import Episode, slugify
from pipeline.store import get_store

DEMO = os.getenv("DOCU_MOCK_LLM") == "1"
# Heavy stages normally run on the GitHub Actions worker. Locally (or in demo mode) the
# dashboard can run them itself: set INLINE_WORKER=1, with requirements-worker.txt installed.
INLINE_WORKER = DEMO or os.getenv("INLINE_WORKER") == "1"
if DEMO:
    os.environ.setdefault("DOCU_MOCK_TTS", "1")
    os.environ.setdefault("DOCU_MOCK_ILLUSTRATE", "1")
    os.environ.setdefault("DOCU_MOCK_VISUALS", "1")
    os.environ.setdefault("DOCU_MOCK_YOUTUBE", "1")

# The full flow, in order. `built: False` stages show in the UI as coming next.
STAGES = [
    {"key": "research", "label": "Research", "status": "researched", "built": True},
    {"key": "verify", "label": "Verify facts", "status": "verified", "built": True},
    {"key": "script", "label": "Script", "status": "scripted", "built": True},
    {"key": "approve", "label": "Approve script", "status": "approved", "built": True},
    {"key": "voice", "label": "Voice", "status": "voiced", "built": True},
    {"key": "visuals", "label": "Visuals", "status": "visuals_ready", "built": True},
    {"key": "render", "label": "Render", "status": "rendered", "built": True},
    # Shorts run beside the long video rather than after it; the UI marks this stage done
    # once any Short is made (meta.shorts.made), not by status.
    {"key": "shorts", "label": "Shorts", "status": "shorts_ready", "built": True, "parallel": True},
    {"key": "upload", "label": "Upload", "status": "uploaded", "built": True, "parallel": True},
]
STATUS_ORDER = ["new"] + [s["status"] for s in STAGES if not s.get("parallel")]

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
        "youtube": youtube.public_account(),
        "music": audio.library(),
        "music_default": audio.cfg()["default"],
        "persona": persona.info(),
        "voiceref": voiceclone.reference_info(),
        "clone_voice": voiceclone.VOICE_ID,
        "story_minutes": story.cfg()["minutes"],
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
        "illustrate_state": (meta.get("illustrate") or {}).get("state"),
        "format": meta.get("format", "doc"),
        "shorts_state": (meta.get("shorts") or {}).get("state"),
        "shorts_made": len((meta.get("shorts") or {}).get("made") or {}),
        "youtube_state": (meta.get("youtube") or {}).get("state"),
        "uploaded": sum(1 for u in (meta.get("upload") or {}).values() if u.get("state") == "done"),
        "running": any(j["slug"] == ep.slug and j["status"] == "running" for j in _jobs.values()),
    }


_last_collect_wake = [0.0]


def _wake_for_waiting(metas: list[dict]) -> None:
    """Kaggle notebooks still drawing get collected by the worker. GitHub's schedule is unreliable,
    so while the dashboard is open, wake the worker every 10 minutes until they're in."""
    if INLINE_WORKER or not any((m.get(k) or {}).get("state") == "waiting" for m in metas for k in ("illustrate", "voice")):
        return
    if time.time() - _last_collect_wake[0] > 600:
        _last_collect_wake[0] = time.time()
        _dispatch_worker()


@app.get("/api/episodes")
def list_episodes():
    files_by_slug = Episode.read_many(SUMMARY_FILES)
    _wake_for_waiting([f.get("meta.json") or {} for f in files_by_slug.values()])
    out = [summarize(Episode(slug), files) for slug, files in files_by_slug.items()]
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
        vis["illustratable"] = illustrate.candidates_for(vis)
    return {**summarize(ep),
            "meta": ep.read("meta.json"),
            "factsheet": ep.read("factsheet.json"),
            "script": sc,
            "lint": ep.read("lint.json"),
            "voice": timings,
            "visuals": vis,
            "shorts": _shorts_view(ep),
            "youtube": _youtube_view(ep)}


def _youtube_view(ep: Episode) -> dict | None:
    meta = ep.read("meta.json") or {}
    if (meta.get("render") or {}).get("state") != "done" and not (meta.get("shorts") or {}).get("made"):
        return None
    return youtube.package(ep)


def _shorts_view(ep: Episode) -> dict | None:
    data = shorts.load(ep)
    if not data:
        return None
    for it in data["items"]:
        it["seconds"] = shorts.estimate_seconds(it)
    data["problems"] = shorts.check(data)
    data["saved"] = ep.read("shorts.json") is not None
    return data


class NewEpisode(BaseModel):
    topic: str
    extra: list[str] = []
    format: str = "doc"          # "doc" (researched documentary) or "story" (narrated fiction)
    premise: str | None = None
    minutes: int | None = None


@app.post("/api/episodes")
def create_episode(body: NewEpisode):
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(422, "Give the story a topic first.")
    if body.format == "story":
        return _create_story(body)
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


def _create_story(body: NewEpisode) -> dict:
    premise = (body.premise or body.topic).strip()
    minutes = max(5, min(40, body.minutes or story.cfg()["minutes"]))
    score, near = story.closest(premise)
    ep = Episode.create(body.topic.strip())
    meta = ep.read("meta.json")
    meta.update(format="story", premise=premise, minutes=minutes, verify_checked=[],
                music={"track": story.cfg()["music"]})
    ep.write("meta.json", meta)
    ep.set_status("verified")  # no research stage: straight to writing
    job = start_job("script", f"Writing story: {body.topic.strip()}",
                    lambda: (story.run(ep.slug, premise=premise, minutes=minutes), {"slug": ep.slug})[1], slug=ep.slug)
    warning = (f"Close to an earlier story ({score:.0%}): \"{near.get('title')}\"" if near and score >= story.cfg()["too_similar"] else None)
    return {"slug": ep.slug, "job": job, "warning": warning}


@app.post("/api/stories/premises")
def story_premises():
    try:
        return {"premises": story.suggest()}
    except Exception as e:
        raise HTTPException(502, f"Couldn't get ideas from the model: {str(e)[:200]}")


# ──────────────────────────── narrator (story videos) ────────────────────────────

class PersonaUpload(BaseModel):
    data_url: str
    name: str = "Narrator"
    ai_generated: bool = False


class VoiceRef(BaseModel):
    data_url: str
    name: str = "My voice"
    consent: bool = False


@app.post("/api/voiceref")
def voiceref_upload(body: VoiceRef):
    """A 30-60 s sample of your own voice, used to make narration sound like you."""
    import base64 as b64
    if not body.consent:
        raise HTTPException(422, "Confirm this is your own voice (or you have the speaker's permission).")
    m = re.match(r"data:(audio/[a-z0-9.+-]+|video/mp4);base64,(.+)", body.data_url or "", re.S)
    if not m:
        raise HTTPException(422, "Upload an audio file (M4A, MP3 or WAV).")
    try:
        meta = voiceclone.save_reference(b64.b64decode(m.group(2)), m.group(1), body.name)
    except voiceclone.CloneError as e:
        raise HTTPException(422, str(e))
    # Clean it on Kaggle right away: background voices and noise out, only your voice left.
    if INLINE_WORKER:
        start_job("voiceref", "Cleaning your voice sample", voiceclone.clean_reference)
    else:
        _dispatch_worker()
    return meta


# ──────────────────────────── voice studio ────────────────────────────

class LabRequest(BaseModel):
    text: str
    voice: str
    speed: float = 1.0


def _lab_view() -> dict:
    return {"takes": list(reversed(voicelab.takes())), "voiceref": voiceclone.reference_info(),
            "clone_voice": voiceclone.VOICE_ID, "voices": voice.voice_choices(),
            "max_chars": voicelab.MAX_CHARS, "enhance_voice": voicelab.ENHANCE,
            "enhance_max_mb": voicelab.enhance.MAX_BYTES // 1048576}


@app.get("/api/voicelab")
def voicelab_list():
    if any(t.get("state") == "waiting" for t in voicelab.takes()) and not INLINE_WORKER:
        if time.time() - _last_collect_wake[0] > 600:  # Kaggle still converting: keep the worker coming back
            _last_collect_wake[0] = time.time()
            _dispatch_worker()
    return _lab_view()


@app.post("/api/voicelab")
def voicelab_create(body: LabRequest):
    try:
        voicelab.create(body.text, body.voice, body.speed)
    except voicelab.LabError as e:
        raise HTTPException(422, str(e))
    if INLINE_WORKER:
        start_job("voicelab", "Voice studio", voicelab.run_pending)
    else:
        _dispatch_worker()
    return _lab_view()


class EnhanceRequest(BaseModel):
    data_url: str
    name: str = "Recording"
    mute_others: bool = True


@app.post("/api/voicelab/enhance")
def voicelab_enhance(body: EnhanceRequest):
    """A recording of your own voice to make clearer: noise and other voices out, same voice."""
    import base64 as b64
    m = re.match(r"data:(audio/[a-z0-9.+-]+|video/[a-z0-9.+-]+);base64,(.+)", body.data_url or "", re.S)
    if not m:
        raise HTTPException(422, "Upload an audio file (M4A, MP3, WAV) or a phone video.")
    try:
        voicelab.create_enhance(b64.b64decode(m.group(2)), m.group(1), body.name, body.mute_others)
    except voicelab.LabError as e:
        raise HTTPException(422, str(e))
    if INLINE_WORKER:
        start_job("voicelab", "Voice studio", voicelab.run_pending)
    else:
        _dispatch_worker()
    return _lab_view()


@app.get("/api/voicelab/{take_id}/original")
def voicelab_original(take_id: str):
    found = voicelab.original(take_id)
    if not found:
        raise HTTPException(404, "Not there.")
    mime, data = found
    return Response(data, media_type="audio/mp4" if mime.startswith("video/") else mime,
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/voicelab/{take_id}.mp3")
def voicelab_audio(take_id: str, download: int = 0):
    data = voicelab.audio(take_id)
    if not data:
        raise HTTPException(404, "Not ready.")
    headers = {"Cache-Control": "private, max-age=3600"}
    if download:
        take = voicelab.get(take_id) or {}
        base = re.sub(r"[^A-Za-z0-9 _-]+", "", take.get("name", "")).strip().replace(" ", "-")[:50]
        fname = f"{base}-enhanced.mp3" if take.get("voice") == voicelab.ENHANCE and base else f"voice-{take_id}.mp3"
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return Response(data, media_type="audio/mpeg", headers=headers)


@app.get("/api/voicelab/{take_id}/text")
def voicelab_text(take_id: str):
    return {"text": voicelab.text_of(take_id)}


@app.delete("/api/voicelab/{take_id}")
def voicelab_delete(take_id: str):
    voicelab.delete(take_id)
    return _lab_view()


@app.get("/api/voiceref/audio")
def voiceref_audio(which: str = "original"):
    """Your sample as uploaded, or after the cleanup, to compare by ear."""
    import base64 as b64
    raw = get_store().read(voiceclone.SETTINGS, "voiceref/clean.b64" if which == "clean" else "voiceref/audio.b64")
    if not raw:
        raise HTTPException(404, "Not there yet.")
    mime, _, data = raw.partition(";")
    return Response(b64.b64decode(data), media_type="audio/mp4" if mime == "video/mp4" else mime,
                    headers={"Cache-Control": "private, max-age=60"})


@app.delete("/api/voiceref")
def voiceref_remove():
    voiceclone.remove_reference()
    return {}


@app.get("/api/persona")
def persona_info():
    return persona.info() or {}


class NarratorGen(BaseModel):
    description: str = ""
    name: str = "Narrator"


def _narrator_view() -> dict:
    job = narrator_gen.info()
    if job.get("state") == "waiting" and not INLINE_WORKER and time.time() - _last_collect_wake[0] > 600:
        _last_collect_wake[0] = time.time()
        _dispatch_worker()
    return {**job, "default": narrator_gen.DEFAULT, "persona": persona.info()}


@app.get("/api/persona/generate")
def narrator_gen_status():
    return _narrator_view()


@app.post("/api/persona/generate")
def narrator_gen_start(body: NarratorGen):
    """Draw a made-up narrator on Kaggle: a few candidates, each cut out, to pick from."""
    try:
        narrator_gen.request(body.description, body.name)
    except narrator_gen.NarratorGenError as e:
        raise HTTPException(422, str(e))
    if INLINE_WORKER:
        start_job("narrator", "Drawing a narrator", narrator_gen.run)
    else:
        _dispatch_worker()
    return _narrator_view()


@app.get("/api/persona/generate/{k}.{kind}")
def narrator_gen_picture(k: int, kind: str):
    if kind not in ("jpg", "png"):
        raise HTTPException(404, "Not there.")
    data = narrator_gen.candidate(k, kind)
    if not data:
        raise HTTPException(404, "Not there.")
    return Response(data, media_type="image/jpeg" if kind == "jpg" else "image/png",
                    headers={"Cache-Control": "private, max-age=300"})


class NarratorPick(BaseModel):
    k: int
    name: str = ""


@app.post("/api/persona/generate/use")
def narrator_gen_use(body: NarratorPick):
    try:
        narrator_gen.use(body.k, body.name or None)
    except (narrator_gen.NarratorGenError, persona.PersonaError) as e:
        raise HTTPException(422, str(e))
    return _narrator_view()


@app.get("/api/persona/cutout.png")
def persona_image():
    data = persona.cutout_png()
    if not data:
        raise HTTPException(404, "No narrator yet.")
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@app.post("/api/persona")
def persona_upload(body: PersonaUpload):
    import base64 as b64
    m = re.match(r"data:image/[a-z+]+;base64,(.+)", body.data_url or "", re.S)
    if not m:
        raise HTTPException(422, "Upload a PNG or JPG image.")
    try:
        return persona.save(b64.b64decode(m.group(1)), body.name, body.ai_generated)
    except persona.PersonaError as e:
        raise HTTPException(422, str(e))


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
    premise: str | None = None   # stories
    minutes: int | None = None


@app.post("/api/episodes/{slug}/script")
def generate_script(slug: str, body: ScriptRequest):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if meta.get("format") == "story":
        premise = (body.premise or meta.get("premise") or meta["topic"]).strip()
        minutes = max(5, min(40, body.minutes or meta.get("minutes") or story.cfg()["minutes"]))
        return start_job("script", f"Writing story: {meta['topic']}",
                         lambda: (story.run(slug, premise=premise, minutes=minutes, notes=body.notes), {"slug": slug})[1],
                         slug=slug)
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


@app.get("/api/episodes/{slug}/{folder}/{name}")
def get_upload(slug: str, folder: str, name: str):
    ep = _load(slug)
    if folder not in ("uploads", "ai") or not re.fullmatch(r"c\d+b\d+-\d+\.b64", name):
        raise HTTPException(404, "No such upload")
    found = visuals.load_upload(ep, f"visuals/{folder}/{name}")
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


class IllustrateBody(BaseModel):
    beats: list[str] | None = None
    prompts: dict[str, str] | None = None  # {beat_id: your wording}, for drawing one picture at a time
    styles: dict[str, str] | None = None   # {beat_id: "ink" | "photo"}; ink when left out


@app.post("/api/episodes/{slug}/visuals/illustrate")
def request_illustrations(slug: str, body: IllustrateBody):
    ep = _load(slug)
    manifest = ep.read("visuals.json")
    if not manifest:
        raise HTTPException(409, "Find pictures first.")
    meta = ep.read("meta.json")
    job = meta.get("illustrate") or {}
    if job.get("state") in ("running", "waiting"):
        raise HTTPException(409, "Kaggle is drawing this story's batch right now. Try again when it finishes.")

    beats = {b["id"]: b for b in manifest["beats"]}
    prompts = {k: v.strip() for k, v in (body.prompts or {}).items()}
    styles = {k: "photo" for k, v in (body.styles or {}).items() if v == "photo" and k in prompts}
    wanted = list(dict.fromkeys((body.beats or []) + list(prompts)))
    if not wanted and job.get("state") == "queued":  # "Illustrate all" again just nudges the worker
        _nudge_worker("illustrate", slug)
        return get_episode(slug)
    wanted = wanted or illustrate.candidates_for(manifest)
    for bid in wanted:
        b = beats.get(bid)
        if not b:
            raise HTTPException(404, f"No beat {bid}.")
        if bid in styles:
            ok, why = illustrate.check_photo_prompt(prompts[bid])
        elif bid in prompts:
            ok, why = illustrate.check_prompt(prompts[bid])
        else:
            ok, why = illustrate.eligible(b["type"], b["query"])
        if not ok:
            raise HTTPException(422, why)
    if not wanted:
        raise HTTPException(409, "No beats here can be illustrated: they all show real people, products or documents.")

    if job.get("state") == "queued":  # add to the batch that's waiting instead of replacing it
        wanted = list(dict.fromkeys((job.get("beats") or []) + wanted))
        prompts = {**(job.get("prompts") or {}), **prompts}
        styles = {**{k: v for k, v in (job.get("styles") or {}).items() if k not in prompts or k in styles}, **styles}
    meta["illustrate"] = {"state": "queued", "beats": wanted, "prompts": prompts, "styles": styles,
                          "requested_at": job.get("requested_at") if job.get("state") == "queued"
                          else datetime.now(timezone.utc).isoformat()}
    ep.write("meta.json", meta)
    _nudge_worker("illustrate", slug)
    return get_episode(slug)


# ──────────────────────────── shorts ────────────────────────────

class ShortLine(BaseModel):
    beat: str
    text: str


class ShortItem(BaseModel):
    id: str
    include: bool = True
    hook: str
    lines: list[ShortLine]
    outro: str = ""
    why: str = ""


class ShortsBody(BaseModel):
    label: str
    items: list[ShortItem]


@app.post("/api/episodes/{slug}/shorts")
def save_shorts(slug: str, body: ShortsBody):
    ep = _load(slug)
    manifest = ep.read("visuals.json") or {}
    known = {b["id"] for b in manifest.get("beats", [])} | {b["id"] for ch in (ep.read("script.json") or {}).get("chapters", []) for b in ch["beats"]}
    for it in body.items:
        for ln in it.lines:
            if ln.beat not in known:
                raise HTTPException(422, f"{it.id}: no beat {ln.beat}.")
    data = {"label": body.label.strip()[:60] or ep.read("meta.json")["topic"],
            "items": [it.model_dump() for it in body.items]}
    ep.write("shorts.json", data)
    return get_episode(slug)


@app.post("/api/episodes/{slug}/shorts/reset")
def reset_shorts(slug: str):
    """Throw away your edits and start again from the script's flagged moments."""
    ep = _load(slug)
    script_ = ep.read("script.json")
    if not script_:
        raise HTTPException(409, "There's no script yet.")
    ep.write("shorts.json", shorts.build(script_, ep.read("meta.json")["topic"]))
    return get_episode(slug)


class MakeShortsBody(BaseModel):
    ids: list[str] | None = None


@app.post("/api/episodes/{slug}/shorts/make")
def make_shorts(slug: str, body: MakeShortsBody):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if meta["status"] not in ("visuals_ready", "rendered"):
        raise HTTPException(409, "Approve the pictures first. Shorts reuse the long video's pictures.")
    job = meta.get("shorts") or {}
    if job.get("state") == "running":
        raise HTTPException(409, "Shorts are being made right now.")
    if job.get("state") == "queued":
        _nudge_worker("shorts", slug)
        return get_episode(slug)
    data = shorts.load(ep)
    if not data or not data["items"]:
        raise HTTPException(409, "The script didn't flag any Shorts.")
    problems = shorts.check(data)
    if problems:
        raise HTTPException(422, " ".join(problems))
    ids = [it["id"] for it in data["items"] if it.get("include", True) and (not body.ids or it["id"] in body.ids)]
    if not ids:
        raise HTTPException(409, "Tick at least one Short to make.")
    if not ep.read("shorts.json"):
        ep.write("shorts.json", data)  # freeze what gets made
    meta["shorts"] = {**job, "state": "queued", "ids": ids, "error": None, "progress": 0,
                      "requested_at": datetime.now(timezone.utc).isoformat()}
    ep.write("meta.json", meta)
    _nudge_worker("shorts", slug)
    return get_episode(slug)


@app.post("/api/episodes/{slug}/shorts/cancel")
def cancel_shorts(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    job = meta.get("shorts") or {}
    if job.get("state") == "running":
        raise HTTPException(409, "They're already being made. It can't be stopped from here.")
    if job.get("state") in ("queued", "error"):
        job["state"] = "done" if job.get("made") else "cancelled"
        job["error"] = None
        ep.write("meta.json", meta)
    return get_episode(slug)


@app.get("/api/episodes/{slug}/shorts/{short_id}/preview")
def short_preview(slug: str, short_id: str):
    data = shorts.load_preview(_load(slug), short_id)
    if not data:
        raise HTTPException(404, "No preview yet.")
    return Response(data, media_type="video/mp4", headers={"Cache-Control": "private, max-age=60"})


# ──────────────────────────── thumbnails ────────────────────────────

class ThumbBody(BaseModel):
    text: str | None = None


@app.post("/api/episodes/{slug}/thumbnails")
def make_thumbnails(slug: str, body: ThumbBody):
    _load(slug)
    try:
        thumbnail.make(slug, body.text)
    except thumbnail.ThumbnailError as e:
        raise HTTPException(409, str(e))
    return get_episode(slug)


class ThumbChoice(BaseModel):
    id: str


@app.post("/api/episodes/{slug}/thumbnails/choose")
def choose_thumbnail(slug: str, body: ThumbChoice):
    ep = _load(slug)
    meta = ep.read("meta.json")
    t = meta.get("thumbnail") or {}
    if body.id not in {o["id"] for o in t.get("options", [])}:
        raise HTTPException(404, "No such thumbnail.")
    t["chosen"] = body.id
    ep.write("meta.json", meta)
    return get_episode(slug)


@app.get("/api/thumbnails/{slug}/{oid}.jpg")
def thumbnail_image(slug: str, oid: str, download: int = 0):
    data = thumbnail.load(_load(slug), oid)
    if not data:
        raise HTTPException(404, "No thumbnail.")
    headers = {"Cache-Control": "private, max-age=60"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{slug}-thumbnail.jpg"'
    return Response(data, media_type="image/jpeg", headers=headers)


# ──────────────────────────── music ────────────────────────────

class MusicUpload(BaseModel):
    data_url: str
    name: str
    credit: str = ""


@app.get("/api/music")
def music_list():
    return audio.library()


@app.post("/api/music")
def music_add(body: MusicUpload):
    m = re.match(r"data:audio/(?:mpeg|mp3);base64,(.+)", body.data_url or "", re.S)
    if not m:
        raise HTTPException(422, "Upload an MP3 file.")
    import base64 as b64
    try:
        return audio.add_track(body.name, body.credit, b64.b64decode(m.group(1)))
    except audio.AudioError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/music/{track_id}")
def music_remove(track_id: str):
    audio.remove_track(track_id)
    return audio.library()


@app.get("/api/music/{track_id}/preview")
def music_preview(track_id: str):
    """20 seconds to listen to before picking."""
    import base64 as b64
    import io
    import wave as wav
    kind, _, key = track_id.partition(":")
    if kind == "upload":
        data = get_store().read(audio.SETTINGS, f"music/{key}.mp3.b64")
        if not data:
            raise HTTPException(404, "No such track.")
        return Response(b64.b64decode(data), media_type="audio/mpeg")
    if kind != "builtin" or key not in audio.PROGRESSIONS:
        raise HTTPException(404, "No such track.")
    import numpy as np
    x = audio.synth_loop(key)[:, : 20 * audio.SR].mean(axis=0)[::2]  # mono, 24 kHz, 20 s
    x *= np.minimum(1, np.linspace(20, 0, len(x)))  # fade the last second
    buf = io.BytesIO()
    with wav.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.SR // 2)
        w.writeframes((np.clip(x * 2.5, -1, 1) * 32767).astype("<i2").tobytes())
    return Response(buf.getvalue(), media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


class MusicPick(BaseModel):
    track: str


@app.post("/api/episodes/{slug}/music")
def music_pick(slug: str, body: MusicPick):
    ep = _load(slug)
    if body.track != "none" and body.track not in {t["id"] for t in audio.library()}:
        raise HTTPException(422, "Unknown music track.")
    meta = ep.read("meta.json")
    meta["music"] = {"track": body.track}
    ep.write("meta.json", meta)
    return get_episode(slug)


# ──────────────────────────── YouTube ────────────────────────────

def _public_base(request: Request) -> str:
    # Render sets RENDER_EXTERNAL_URL; behind its proxy request.base_url would say http://.
    return (env("RENDER_EXTERNAL_URL") or str(request.base_url)).rstrip("/")


def _oauth_state() -> str:
    ts = str(int(time.time()))
    sig = hmac.new((PASSWORD or "dev").encode(), f"youtube-connect:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def _oauth_state_ok(state: str) -> bool:
    ts, _, sig = state.partition(".")
    if not ts.isdigit() or time.time() - int(ts) > 900:
        return False
    good = hmac.new((PASSWORD or "dev").encode(), f"youtube-connect:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, good)


@app.get("/api/youtube")
def youtube_status(request: Request):
    return {**youtube.public_account(), "redirect_uri": f"{_public_base(request)}/api/youtube/callback"}


@app.get("/api/youtube/connect")
def youtube_connect(request: Request):
    if not youtube.configured():
        raise HTTPException(409, "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on Render first (see DEPLOY.md, step 7).")
    redirect = f"{_public_base(request)}/api/youtube/callback"
    if youtube.mock():
        return RedirectResponse(f"/api/youtube/callback?code=mock&state={_oauth_state()}", status_code=303)
    return RedirectResponse(youtube.auth_url(redirect, _oauth_state()), status_code=303)


@app.get("/api/youtube/callback")
def youtube_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/#/?youtube={error}", status_code=303)
    if not _oauth_state_ok(state):
        raise HTTPException(400, "That sign-in link is stale or wasn't started here. Connect again from the dashboard.")
    try:
        youtube.connect(code, f"{_public_base(request)}/api/youtube/callback")
    except youtube.YouTubeError as e:
        return RedirectResponse(f"/#/?youtube=failed&why={requests.utils.quote(str(e)[:200])}", status_code=303)
    return RedirectResponse("/#/?youtube=connected", status_code=303)


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    youtube.disconnect()
    return youtube.public_account()


@app.get("/api/episodes/{slug}/youtube")
def youtube_package(slug: str):
    return youtube.package(_load(slug))


class UploadTarget(BaseModel):
    what: str                      # "long" or a Short id like "s1"
    privacy: str = "private"
    publish_at: str | None = None  # ISO time; YouTube publishes it then


class UploadBody(BaseModel):
    targets: list[UploadTarget]


@app.post("/api/episodes/{slug}/youtube/upload")
def youtube_upload(slug: str, body: UploadBody):
    ep = _load(slug)
    if not youtube.public_account()["connected"]:
        raise HTTPException(409, "Connect your YouTube channel first.")
    meta = ep.read("meta.json")
    job = meta.get("youtube") or {}
    if job.get("state") == "running":
        raise HTTPException(409, "An upload for this story is running right now.")
    pack = youtube.package(ep)
    targets = []
    for t in body.targets:
        info = pack["long"] if t.what == "long" else pack["shorts"].get(t.what)
        if not info or not info["ready"]:
            raise HTTPException(409, f"{t.what}: render it before uploading.")
        if t.privacy not in ("private", "unlisted", "public"):
            raise HTTPException(422, "Privacy is private, unlisted or public.")
        if t.publish_at:
            try:
                when = datetime.fromisoformat(t.publish_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, "That schedule time isn't a date.")
            if when.tzinfo is None or when <= datetime.now(timezone.utc):
                raise HTTPException(422, "Pick a schedule time in the future.")
        targets.append(t.model_dump())
    if not targets:
        raise HTTPException(422, "Nothing to upload.")
    if job.get("state") == "queued":  # add to the waiting batch
        have = {t["what"] for t in job.get("targets") or []}
        targets = (job.get("targets") or []) + [t for t in targets if t["what"] not in have]
    meta["youtube"] = {"state": "queued", "targets": targets, "error": None, "progress": 0,
                       "requested_at": datetime.now(timezone.utc).isoformat()}
    up = meta.get("upload") or {}
    for t in targets:
        up[t["what"]] = {**(up.get(t["what"]) or {}), "state": "queued", "error": None}
    meta["upload"] = up
    ep.write("meta.json", meta)
    _nudge_worker("youtube", slug)
    return get_episode(slug)


# ──────────────────────────── render ────────────────────────────

@app.post("/api/episodes/{slug}/render")
def request_render(slug: str):
    ep = _load(slug)
    meta = ep.read("meta.json")
    if meta["status"] not in ("visuals_ready", "rendered"):
        raise HTTPException(409, "Approve the narration and pictures first.")
    state = (meta.get("render") or {}).get("state")
    if state == "running":
        raise HTTPException(409, "This video is already rendering.")
    if state == "queued":
        _nudge_worker("render", slug)
        return get_episode(slug)
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
    if voice_id == voiceclone.VOICE_ID and not voiceclone.reference_info():
        raise HTTPException(409, "Upload a sample of your voice first.")
    if voice_id != voiceclone.VOICE_ID and voice_id not in voice.voice_choices():
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


def _nudge_worker(kind: str, slug: str | None = None) -> None:
    if INLINE_WORKER:
        from pipeline import worker
        label = {"illustrate": "Drawing illustrations", "render": "Rendering", "shorts": "Making Shorts",
                 "youtube": "Uploading to YouTube"}.get(kind, kind)
        start_job(kind, label, lambda: worker.run_jobs(kind), slug=slug)
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

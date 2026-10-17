"""Stage 8: YouTube.

Two ways out, both from the dashboard's YouTube tab:

  Upload kit   title, description (with YouTube chapters and photo credits), tags and captions
               ready to copy, plus the video files. Works today, nothing to set up.
  Upload       one click per video. You connect your channel once (Google sign-in, from the
               dashboard); the worker downloads the file from its GitHub render run and sends it
               with the official YouTube Data API, then adds the caption track.

Free quota: 10,000 units a day. An upload costs 1,600 and a caption track 400, so about five
long videos, or six Shorts, a day.

Heads-up, enforced by YouTube rather than us: videos uploaded through an API project Google
hasn't reviewed are locked to private, and a locked video can't be made public later. The review
("audit") is free. Until it passes, use the upload kit for anything that has to go public.

Set DOCU_MOCK_YOUTUBE=1 to fake the API (tests, demo mode).
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import env
from .episode import Episode
from .store import get_store
from . import render, shorts, visuals

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
CHUNK = 16 * 1024 * 1024  # resumable upload chunk; must be a multiple of 256 KB
SETTINGS = "_settings"     # store "slug" for desk-wide settings (hidden from the story list)

SCIENCE_AND_TECH = "28"


class YouTubeError(RuntimeError):
    pass


def mock() -> bool:
    return os.getenv("DOCU_MOCK_YOUTUBE") == "1"


# ──────────────────────────── account ────────────────────────────

def configured() -> bool:
    return mock() or bool(env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET"))


def account() -> dict | None:
    raw = get_store().read(SETTINGS, "youtube.json")
    return json.loads(raw) if raw else None


def public_account() -> dict:
    a = account() or {}
    return {"configured": configured(), "connected": bool(a.get("refresh_token")),
            "channel": a.get("channel_title"), "channel_id": a.get("channel_id"),
            "connected_at": a.get("connected_at")}


def disconnect() -> None:
    get_store().write(SETTINGS, "youtube.json", json.dumps({}))


def auth_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
        "include_granted_scopes": "true",
    })


def connect(code: str, redirect_uri: str) -> dict:
    """Finish the Google sign-in: swap the code for a refresh token, remember which channel."""
    if mock():
        tokens = {"refresh_token": "mock-refresh", "access_token": "mock"}
    else:
        r = requests.post(TOKEN_URL, data={
            "code": code, "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
        if r.status_code != 200:
            raise YouTubeError(f"Google refused the sign-in: {r.text[:300]}")
        tokens = r.json()
    if not tokens.get("refresh_token"):
        raise YouTubeError("Google didn't hand over a refresh token. Remove the app's access at "
                           "myaccount.google.com/permissions and connect again.")
    channel = _my_channel(tokens["access_token"])
    data = {"refresh_token": tokens["refresh_token"], "channel_id": channel["id"],
            "channel_title": channel["title"], "connected_at": datetime.now(timezone.utc).isoformat()}
    get_store().write(SETTINGS, "youtube.json", json.dumps(data))
    return data


def access_token() -> str:
    if mock():
        return "mock"
    a = account() or {}
    if not a.get("refresh_token"):
        raise YouTubeError("YouTube isn't connected. Connect your channel on the YouTube tab first.")
    if not configured():
        raise YouTubeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET aren't set on this machine "
                           "(they're needed on Render and as GitHub secrets).")
    r = requests.post(TOKEN_URL, data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": a["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        if "invalid_grant" in r.text:
            raise YouTubeError("Google withdrew the channel connection (it expires after 7 days while the "
                               "Google app is in Testing). Publish the app, then connect again on the YouTube tab.")
        raise YouTubeError(f"Couldn't refresh the YouTube login: {r.text[:300]}")
    return r.json()["access_token"]


def _my_channel(token: str) -> dict:
    if mock():
        return {"id": "UCmock", "title": "Mock Channel"}
    r = requests.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    items = r.json().get("items") if r.status_code == 200 else None
    if not items:
        raise YouTubeError("That Google account has no YouTube channel. Sign in with the channel's account.")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


# ──────────────────────────── what goes in the boxes ────────────────────────────

def _clock(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def chapters(script: dict, timings: dict | None) -> list[str]:
    """YouTube chapters from the narration: '0:00 Intro' lines. YouTube needs the first at 0:00,
    at least three, each at least 10 seconds long; otherwise none at all."""
    if not timings:
        return []
    starts = {b["id"]: b["start"] for b in timings.get("beats", [])}
    marks = []
    for i, ch in enumerate(script["chapters"]):
        t = 0.0 if i == 0 else starts.get(ch["beats"][0]["id"])
        if t is None:
            return []
        marks.append((t, ch["title"]))
    ends = [m[0] for m in marks[1:]] + [timings.get("duration", marks[-1][0] + 60)]
    if len(marks) < 3 or any(e - s < 10 for (s, _), e in zip(marks, ends)):
        return []
    return [f"{_clock(t)} {title}" for t, title in marks]


def _tags(tags: list[str], extra: list[str] = ()) -> list[str]:
    out, total = [], 0
    for t in [*tags, *extra]:
        t = re.sub(r"[<>\"]", "", t).strip()
        if t and t.lower() not in {x.lower() for x in out} and total + len(t) + 1 <= 480:
            out.append(t)
            total += len(t) + 1
    return out


def package(ep: Episode) -> dict:
    """Everything YouTube asks for, for the long video and each Short."""
    script = ep.read("script.json") or {}
    meta = ep.read("meta.json") or {}
    manifest = ep.read("visuals.json") or {}
    title = (script.get("chosen_title") or (script.get("title_options") or [meta.get("topic", "")])[0])[:100]
    cred = visuals.credits(manifest) if manifest else []
    picks = {b["id"]: (b["candidates"][b["chosen"]] if b.get("candidates") else {}) for b in manifest.get("beats", [])}
    ai_used = any(p.get("source") == "ai" for p in picks.values())
    photo_used = any(p.get("source") == "ai" and p.get("style") == "photo" for p in picks.values())
    marks = chapters(script, ep.read("voice/timings.json")) if script else []
    uploads = meta.get("upload") or {}
    long_url = (uploads.get("long") or {}).get("url")

    parts = [script.get("description", "").strip()]
    if marks:
        parts.append("Chapters\n" + "\n".join(marks))
    if cred:
        parts.append("Pictures\n" + "\n".join(cred))
    notes = ["Narrated with a synthetic voice (Kokoro)."]
    if photo_used:
        notes.append("Some general scenes (places and objects, never people or real events) are AI-generated images; "
                     "every picture of a real person, product or event is real.")
    elif ai_used:
        notes.append("Some abstract illustrations were drawn with AI (Stable Diffusion XL); every photo of a real person, product or place is real.")
    parts.append(" ".join(notes))
    long = {"title": title, "description": "\n\n".join(p for p in parts if p)[:4900],
            "tags": _tags(script.get("tags", [])), "category": SCIENCE_AND_TECH,
            "file": f"{ep.slug}.mp4", "ready": (meta.get("render") or {}).get("state") == "done",
            # YouTube's "altered or synthetic content" box: required for realistic AI images.
            "synthetic": photo_used}

    out_shorts = {}
    data = shorts.load(ep) or {"items": []}
    made = (meta.get("shorts") or {}).get("made") or {}
    for it in data["items"]:
        if it["id"] not in made:
            continue
        hook = it["hook"].strip().rstrip(".")
        s_title = (hook if len(hook) <= 90 else hook[:87].rsplit(" ", 1)[0] + "…") + " #Shorts"
        lines = [it["hook"].strip(), " ".join(ln["text"] for ln in it["lines"])[:600]]
        lines.append(f"Full story: {long_url}" if long_url else f"Full story: “{title}” on this channel.")
        short_photo = any(picks.get(ln["beat"], {}).get("source") == "ai" and picks.get(ln["beat"], {}).get("style") == "photo"
                          for ln in it["lines"])
        if short_photo:
            lines.append("Includes AI-generated scenes (places and objects only).")
        out_shorts[it["id"]] = {"title": s_title, "description": "\n\n".join(lines), "synthetic": short_photo,
                                "tags": _tags(script.get("tags", []), ["shorts"]), "category": SCIENCE_AND_TECH,
                                "file": made[it["id"]]["file"], "ready": True}
    return {"long": long, "shorts": out_shorts}


# ──────────────────────────── getting the file ────────────────────────────

def fetch_file(name: str, run_id: str | None, repo: str | None, dest: Path) -> Path:
    """The MP4 from this machine's output folder, or from the GitHub run that made it."""
    local = render.OUTPUT / name
    if local.exists():
        return local
    if not (run_id and repo):
        raise YouTubeError(f"{name} isn't here and there's no GitHub run to fetch it from. Render it again.")
    token = env("GITHUB_TOKEN")
    if not token:
        raise YouTubeError("GITHUB_TOKEN isn't available to fetch the video from its GitHub run.")
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts", headers=h, timeout=30)
    r.raise_for_status()
    arts = [a for a in r.json().get("artifacts", []) if not a.get("expired")]
    if not arts:
        raise YouTubeError("The video's download from GitHub has expired (they're kept 30 days). Render it again.")
    print(f"  downloading {name} from GitHub run {run_id}")
    with requests.get(arts[0]["archive_download_url"], headers=h, stream=True, timeout=600) as z:
        z.raise_for_status()
        buf = io.BytesIO(z.content)
    with zipfile.ZipFile(buf) as zf:
        member = next((m for m in zf.namelist() if Path(m).name == name), None)
        if not member:
            raise YouTubeError(f"{name} isn't in that GitHub run's files. Render it again.")
        dest.write_bytes(zf.read(member))
    return dest


# ──────────────────────────── upload ────────────────────────────

def upload_video(path: Path, info: dict, privacy: str, publish_at: str | None, token: str, progress=None) -> str:
    """Resumable upload. Returns the video id."""
    if mock():
        return "mock" + re.sub(r"\W", "", path.stem)[-6:]
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True,
              "containsSyntheticMedia": bool(info.get("synthetic"))}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)  # YouTube publishes it at that time
    body = {"snippet": {"title": info["title"], "description": info["description"], "tags": info["tags"],
                        "categoryId": info["category"], "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    size = path.stat().st_size
    start = requests.post(f"{UPLOAD_API}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                   "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                          data=json.dumps(body), timeout=60)
    if start.status_code != 200:
        raise YouTubeError(_explain(start))
    where = start.headers["Location"]
    sent = 0
    with open(path, "rb") as f:
        while sent < size:
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            for attempt in range(5):
                try:
                    r = requests.put(where, data=chunk, timeout=300, headers={
                        "Authorization": f"Bearer {token}", "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}"})
                    break
                except requests.RequestException as e:
                    if attempt == 4:
                        raise YouTubeError(f"Upload kept dropping: {e}")
                    time.sleep(5 * (attempt + 1))
            if r.status_code in (200, 201):
                return r.json()["id"]
            if r.status_code != 308:
                raise YouTubeError(_explain(r))
            rng = r.headers.get("Range")  # what YouTube actually has
            sent = int(rng.split("-")[1]) + 1 if rng else 0
            f.seek(sent)
            if progress:
                progress(sent, size)
    raise YouTubeError("YouTube didn't confirm the upload.")


def upload_captions(video_id: str, srt: str, token: str) -> None:
    if mock():
        return
    boundary = "docupipeline"
    meta = json.dumps({"snippet": {"videoId": video_id, "language": "en", "name": "English", "isDraft": False}})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n{srt}\r\n--{boundary}--").encode()
    r = requests.post(f"{UPLOAD_API}/captions", params={"uploadType": "multipart", "part": "snippet"},
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": f"multipart/related; boundary={boundary}"}, data=body, timeout=120)
    if r.status_code not in (200, 201):
        raise YouTubeError("Video uploaded, but the captions weren't: " + _explain(r))


def _explain(r: requests.Response) -> str:
    try:
        err = r.json().get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", r.text[:200])
    except ValueError:
        reason, msg = "", r.text[:200]
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        return "YouTube's free daily quota is used up (about 6 uploads a day). It resets at midnight Pacific time."
    if reason == "uploadLimitExceeded":
        return "Your channel hit YouTube's own daily upload limit. Try tomorrow."
    if r.status_code == 401:
        return "YouTube login expired. Connect the channel again on the YouTube tab."
    return f"YouTube said {r.status_code} {reason}: {msg}"


def run(slug: str, targets: list[dict], progress=None) -> dict:
    """targets: [{"what": "long" | "s1"..., "privacy": "private", "publish_at": None}]"""
    import tempfile
    ep = Episode.load(slug)
    pack = package(ep)
    meta = ep.read("meta.json")
    try:
        token = access_token()
    except YouTubeError as e:
        up = meta.get("upload") or {}
        for t in targets:
            up[t["what"]] = {**(up.get(t["what"]) or {}), "state": "error", "error": str(e)}
        meta["upload"] = up
        ep.write("meta.json", meta)
        raise
    results = {}
    for t in targets:
        what = t["what"]
        info = pack["long"] if what == "long" else pack["shorts"].get(what)
        if not info or not info["ready"]:
            results[what] = {"state": "error", "error": "Not rendered yet."}
            continue
        src = meta.get("render") if what == "long" else ((meta.get("shorts") or {}).get("made") or {}).get(what, {})
        print(f"[upload] {meta['topic']} · {what}: “{info['title']}” as {t.get('privacy', 'private')}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = fetch_file(info["file"], (src or {}).get("run_id"), (src or {}).get("repo"), Path(tmp) / info["file"])
                vid = upload_video(path, info, t.get("privacy", "private"), t.get("publish_at"), token, progress)
            captions = None
            if what == "long" and ep.read("render/captions.srt"):
                try:
                    upload_captions(vid, ep.read("render/captions.srt"), token)
                    captions = True
                except YouTubeError as e:
                    captions = str(e)
            results[what] = {"state": "done", "video_id": vid, "url": f"https://youtu.be/{vid}",
                             "privacy": t.get("privacy", "private"), "publish_at": t.get("publish_at"),
                             "captions": captions, "title": info["title"],
                             "uploaded_at": datetime.now(timezone.utc).isoformat()}
            print(f"  ✓ https://youtu.be/{vid}")
            # Shorts uploaded after the long video link to it: refresh the package for the next one.
            if what == "long":
                m = ep.read("meta.json")
                m.setdefault("upload", {})["long"] = results[what]
                ep.write("meta.json", m)
                pack = package(ep)
        except Exception as e:
            results[what] = {"state": "error", "error": str(e)[:400]}
            print(f"  ✗ {what}: {e}")
    meta = ep.read("meta.json")
    up = meta.get("upload") or {}
    for what, res in results.items():
        up[what] = {**(up.get(what) or {}), **res}
    job = meta.get("youtube") or {}
    job.update(state="done" if any(r["state"] == "done" for r in results.values()) else "error",
               finished_at=datetime.now(timezone.utc).isoformat(),
               error=None if any(r["state"] == "done" for r in results.values())
               else "; ".join(f"{k}: {v.get('error')}" for k, v in results.items()))
    meta["upload"], meta["youtube"] = up, job
    ep.write("meta.json", meta)
    if job["state"] == "error":
        raise YouTubeError(job["error"])
    return up

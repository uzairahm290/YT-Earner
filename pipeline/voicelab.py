"""Voice studio: turn any text into narration, in your own voice (American accent) or a built-in one.

Not tied to a story. You paste text in the dashboard's Voice page; each request is a "take":

    queued  → the GitHub worker reads it with Kokoro (paragraph by paragraph)
            → built-in voice: mastered and stored, done
            → your voice: sent to Kaggle where OpenVoice swaps the timbre for yours (same notebook as
              the story narration), collected by this or a later worker run, mastered, stored

Takes live desk-wide in the database (_settings/voicelab/...). Only one take in your voice is on
Kaggle at a time, so a slow queue never gets two notebooks overwriting each other.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .store import get_store
from . import voice, voiceclone

SETTINGS = "_settings"
INDEX = "voicelab/index.json"
MAX_CHARS = 12000  # about 13 minutes of speech
KERNEL_NAME = "docu-voice-lab"


class LabError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def takes() -> list[dict]:
    raw = get_store().read(SETTINGS, INDEX)
    return json.loads(raw) if raw else []


def _save_all(items: list[dict]) -> None:
    get_store().write(SETTINGS, INDEX, json.dumps(items[-60:]))


def _update(take_id: str, **fields) -> dict:
    items = takes()
    for t in items:
        if t["id"] == take_id:
            t.update(fields)
            _save_all(items)
            return t
    raise LabError("That take was deleted.")


def get(take_id: str) -> dict | None:
    return next((t for t in takes() if t["id"] == take_id), None)


def create(text: str, voice_id: str, speed: float) -> dict:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if len(text) < 2:
        raise LabError("Write something to read first.")
    if len(text) > MAX_CHARS:
        raise LabError(f"That's {len(text):,} characters; the studio takes up to {MAX_CHARS:,} (about 13 minutes). "
                       "Split it into parts.")
    if voice_id == voiceclone.VOICE_ID:
        if not voiceclone.reference_info():
            raise LabError("Upload a sample of your voice first.")
    elif voice_id not in voice.voice_choices():
        raise LabError("Unknown voice.")
    speed = max(0.8, min(1.2, float(speed or 1.0)))
    take = {"id": uuid.uuid4().hex[:10], "voice": voice_id, "speed": round(speed, 2), "chars": len(text),
            "preview": text[:160], "state": "queued", "created_at": _now()}
    get_store().write(SETTINGS, f"voicelab/{take['id']}.txt", text)
    items = takes()
    items.append(take)
    _save_all(items)
    return take


def delete(take_id: str) -> None:
    store = get_store()
    _save_all([t for t in takes() if t["id"] != take_id])
    for name in (f"voicelab/{take_id}.txt", f"voicelab/{take_id}.mp3.b64"):
        store.write(SETTINGS, name, "")


def audio(take_id: str) -> bytes | None:
    data = get_store().read(SETTINGS, f"voicelab/{take_id}.mp3.b64")
    return base64.b64decode(data) if data else None


def text_of(take_id: str) -> str:
    return get_store().read(SETTINGS, f"voicelab/{take_id}.txt") or ""


def pending() -> list[str]:
    return [t["id"] for t in takes() if t.get("state") in ("queued", "waiting")]


# ──────────────────────────── the work (GitHub worker) ────────────────────────────

def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for p in paras:  # very long paragraphs read better (and convert safer) in pieces
        if len(p) <= 900:
            out.append(p)
            continue
        cur = ""
        for sentence in re.split(r"(?<=[.!?])\s+", p):
            if cur and len(cur) + len(sentence) > 700:
                out.append(cur)
                cur = sentence
            else:
                cur = f"{cur} {sentence}".strip()
        if cur:
            out.append(cur)
    return out or [text]


def _read(take: dict):
    """Kokoro reads the text: returns (track, beat timings)."""
    import numpy as np
    base = voiceclone.cfg()["base_voice"] if take["voice"] == voiceclone.VOICE_ID else take["voice"]
    narrator = voice.Narrator(base)
    sr = voice.SAMPLE_RATE
    parts, beats, cursor = [np.zeros(int(0.3 * sr), np.float32)], [], 0.3
    for p in _paragraphs(text_of(take["id"])):
        clip = narrator.speak(p, speed=take["speed"])
        beats.append({"start": round(cursor, 3), "end": round(cursor + len(clip) / sr, 3)})
        parts += [clip, np.zeros(int(0.5 * sr), np.float32)]
        cursor += len(clip) / sr + 0.5
    parts.append(np.zeros(int(0.6 * sr), np.float32))
    return np.concatenate(parts), beats


def _store(take_id: str, track, started: float) -> dict:
    mp3 = voice.master_to_mp3(track)
    get_store().write(SETTINGS, f"voicelab/{take_id}.mp3.b64", base64.b64encode(mp3).decode())
    return _update(take_id, state="done", finished_at=_now(), duration=round(len(track) / voice.SAMPLE_RATE, 1),
                   size_kb=len(mp3) // 1024, seconds_taken=round(time.time() - started), error=None)


def run_one(take_id: str) -> dict:
    import os
    take = get(take_id)
    if not take:
        raise LabError("That take was deleted.")
    started = time.time()
    _update(take_id, state="running", started_at=_now(), error=None, stage=None)
    print(f"[voice studio] take {take_id}: {take['chars']} characters, {take['voice']}")
    track, beats = _read(take)
    if take["voice"] != voiceclone.VOICE_ID or os.getenv("DOCU_MOCK_VOICECLONE") == "1":
        return _store(take_id, track, started)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        ref = voiceclone.reference_wav(d)
        raw = voiceclone._write_wav(d / "raw.wav", track)
        job = {"chunks": voiceclone.chunks(beats, len(track), voice.SAMPLE_RATE), "tau": voiceclone.cfg()["tau"]}
        kernel_id = voiceclone._push_kaggle(KERNEL_NAME, {"raw.wav": raw, "ref.wav": ref},
                                            voiceclone.KERNEL.replace("__JOB__", json.dumps(job)))
    _update(take_id, kernel=kernel_id, kaggle_status="queued", pushed_at=_now(), length=len(track))
    return _wait_and_finish(take_id, voiceclone.KAGGLE_WAIT, started)


def _wait_and_finish(take_id: str, seconds: float, started: float) -> dict:
    from .illustrate import wait
    take = get(take_id)
    state, _ = wait(take["kernel"], seconds, on_status=lambda s: _update(take_id, kaggle_status=s))
    if state not in ("complete", "error"):
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(take["pushed_at"])).total_seconds() / 3600
        if age_h > 6:
            raise LabError("Kaggle never got to it (6 hours in its queue). Generate it again later.")
        return _update(take_id, state="waiting", kaggle_status=state)
    with tempfile.TemporaryDirectory() as d:
        wav = voiceclone.collect(take["kernel"], Path(d))
        converted = voiceclone._read_wav(wav)
    return _store(take_id, converted, started)


def run_pending(which: str = "all") -> tuple[int, int]:
    """Worker entry: finish takes waiting on Kaggle, then start queued ones. Only one take in your
    voice is on Kaggle at a time (they share one notebook).
    which: "builtin" (quick, CPU only), "clone" (your voice, via Kaggle) or "all"."""
    def wanted(t):
        mine = t["voice"] == voiceclone.VOICE_ID
        return which == "all" or (which == "clone") == mine
    done = failed = 0
    for take in [t for t in takes() if t.get("state") == "waiting" and wanted(t)]:
        try:
            r = _wait_and_finish(take["id"], 15 * 60, time.time())
            done += r["state"] == "done"
        except Exception as e:
            _update(take["id"], state="error", error=str(e)[:400])
            failed += 1
    for take in [t for t in takes() if t.get("state") == "queued" and wanted(t)]:
        if take["voice"] == voiceclone.VOICE_ID and voiceclone.clean_info().get("state") in ("queued", "running", "waiting"):
            _update(take["id"], stage="Waiting for your voice sample to finish cleaning")
            continue  # the cleanup is still on Kaggle; this take uses the clean sample once it's back
        if take["voice"] == voiceclone.VOICE_ID and any(
                t.get("state") == "waiting" and t["voice"] == voiceclone.VOICE_ID for t in takes()):
            continue  # its turn comes when the one on Kaggle is collected
        try:
            r = run_one(take["id"])
            done += r["state"] in ("done", "waiting")
        except Exception as e:
            _update(take["id"], state="error", error=str(e)[:400])
            failed += 1
    return done, failed


def estimate_minutes(chars: int) -> float:
    return round(chars / 5.5 / load_config()["story"]["words_per_minute"], 1)

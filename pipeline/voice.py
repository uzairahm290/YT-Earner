"""Stage 4: narration.

Approved script -> one mastered MP3 for the whole video + start/end times for every beat.

The single file is what you hear in the dashboard (each beat's play button seeks into it)
and what the render stage cuts pictures against later. Kokoro-82M runs on CPU, so this
works on a free GitHub Actions runner without a GPU.

Set DOCU_MOCK_TTS=1 to swap the model for tones of the right length (tests, demo mode).
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import load_config
from .episode import Episode

SAMPLE_RATE = 24000
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")

# Broadcast-style voice chain: cut rumble, a touch of presence, gentle compression,
# then loudness to YouTube's reference (-14 LUFS integrated, peaks under -1.5 dBTP).
MASTER_CHAIN = ",".join([
    "highpass=f=75",
    "equalizer=f=180:t=q:w=1.0:g=1.5",     # a little chest
    "equalizer=f=3500:t=q:w=1.2:g=2",      # clarity
    "acompressor=threshold=-20dB:ratio=3:attack=8:release=120:makeup=2",
    "loudnorm=I=-14:TP=-1.5:LRA=9",
    "alimiter=limit=0.7:level=false",      # single-pass loudnorm + MP3 encoding overshoot; keeps true peaks near -2 dB
])


class VoiceError(RuntimeError):
    pass


def cfg() -> dict:
    return load_config()["voice"]


def voice_choices() -> dict[str, str]:
    return dict(cfg()["choices"])


# ──────────────────────────── text cleanup ────────────────────────────

_SPOKEN = [
    (re.compile(r"\$(\d[\d,.]*)\s*(billion|million|trillion)", re.I), r"\1 \2 dollars"),
    (re.compile(r"\$(\d[\d,.]*)"), r"\1 dollars"),
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"\s+—\s+|\s+–\s+|—"), ", "),       # em dashes read as a pause, not "dash"
    (re.compile(r"\b(\d{4})s\b"), r"\1s"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
    (re.compile(r"\bCEO\b"), "C.E.O."),
    (re.compile(r"\s{2,}"), " "),
]


def speakable(text: str) -> str:
    """Rewrite symbols the model tends to read badly."""
    out = text.strip()
    for pattern, repl in _SPOKEN:
        out = pattern.sub(repl, out)
    return out


# ──────────────────────────── model ────────────────────────────

def model_dir() -> Path:
    return Path(os.path.expanduser(cfg()["model_dir"]))


def ensure_model() -> Path:
    """Download the model once (~350 MB). GitHub Actions caches the folder between runs."""
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        path = d / name
        if path.exists() and path.stat().st_size > 1_000_000:
            continue
        print(f"  downloading {name}...")
        tmp = path.with_suffix(".part")
        with requests.get(f"{cfg()['model_url']}/{name}", stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.rename(path)
    return d


class Narrator:
    def __init__(self, voice: str):
        if voice not in voice_choices():
            raise VoiceError(f"Unknown voice '{voice}'. Pick one of: {', '.join(voice_choices())}")
        self.voice = voice
        self.mock = os.getenv("DOCU_MOCK_TTS") == "1"
        if not self.mock:
            from kokoro_onnx import Kokoro  # heavy import, only on the worker
            d = ensure_model()
            self.model = Kokoro(str(d / MODEL_FILES[0]), str(d / MODEL_FILES[1]))

    def speak(self, text: str, speed: float | None = None) -> np.ndarray:
        import numpy as np
        text = speakable(text)
        speed = speed or cfg()["speed"]
        if not text:
            return np.zeros(0, dtype=np.float32)
        if self.mock:
            # A quiet tone as long as the line would take to read: keeps timings realistic.
            seconds = max(0.6, len(text.split()) / (2.4 * speed / 0.95))
            t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
            return (0.08 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        samples, sr = self.model.create(text, voice=self.voice, speed=speed, lang="en-us")
        if sr != SAMPLE_RATE:
            raise VoiceError(f"Model returned {sr} Hz, expected {SAMPLE_RATE}")
        return np.asarray(samples, dtype=np.float32)


# ──────────────────────────── assembly ────────────────────────────

def _silence(seconds: float) -> np.ndarray:
    import numpy as np
    return np.zeros(int(max(0.0, seconds) * SAMPLE_RATE), dtype=np.float32)


def narrate(script: dict, voice: str, progress=None) -> tuple[np.ndarray, list[dict]]:
    """Speak every beat in order. Returns the raw track and each beat's start/end in seconds."""
    import numpy as np
    c = cfg()
    narrator = Narrator(voice)
    beats = [b for ch in script["chapters"] for b in ch["beats"]]
    parts = [_silence(c["lead_in"])]
    cursor = c["lead_in"]
    timings = []
    for i, beat in enumerate(beats, 1):
        audio = narrator.speak(beat["narration"])
        length = len(audio) / SAMPLE_RATE
        timings.append({"id": beat["id"], "start": round(cursor, 3), "end": round(cursor + length, 3)})
        pause = _silence(float(beat.get("pause_after", 0.3)))
        parts += [audio, pause]
        cursor += length + len(pause) / SAMPLE_RATE
        if progress:
            progress(i, len(beats))
    parts.append(_silence(c["tail"]))
    return np.concatenate(parts), timings


def master_to_mp3(track: np.ndarray) -> bytes:
    """Write the raw track, run the mastering chain, return MP3 bytes."""
    import numpy as np
    if not shutil.which("ffmpeg"):
        raise VoiceError("ffmpeg isn't installed (needed to master the narration)")
    with tempfile.TemporaryDirectory() as tmp:
        raw, out = Path(tmp) / "raw.wav", Path(tmp) / "voice.mp3"
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(track, -1, 1) * 32767).astype("<i2").tobytes())
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
               "-af", MASTER_CHAIN, "-ar", str(SAMPLE_RATE), "-ac", "1",
               "-c:a", "libmp3lame", "-b:a", cfg()["mp3_bitrate"], str(out)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise VoiceError(f"ffmpeg failed: {result.stderr[-400:]}")
        return out.read_bytes()


# ──────────────────────────── run ────────────────────────────

def run(slug: str, voice: str | None = None) -> Episode:
    ep = Episode.load(slug)
    script = ep.read("script.json")
    if not script:
        raise VoiceError("There's no script to narrate yet.")
    meta = ep.read("meta.json")
    voice = voice or (meta.get("voice") or {}).get("voice") or cfg()["default"]

    started = time.time()
    print(f"[voice] {meta['topic']} with {voice}")
    track, timings = narrate(script, voice, progress=lambda i, n: print(f"  beat {i}/{n}") if i % 10 == 0 or i == n else None)
    mp3 = master_to_mp3(track)

    # Stored as text so it lives next to everything else (Supabase row or local file).
    ep.write("voice/narration.mp3.b64", base64.b64encode(mp3).decode())
    ep.write("voice/timings.json", {
        "voice": voice,
        "duration": round(len(track) / SAMPLE_RATE, 2),
        "beats": timings,
        "script_hash": script_hash(script),
    })
    meta = ep.read("meta.json")
    meta["voice"] = {
        "state": "done", "voice": voice,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds_taken": round(time.time() - started),
        "duration": round(len(track) / SAMPLE_RATE, 2),
        "size_kb": len(mp3) // 1024,
    }
    ep.write("meta.json", meta)
    ep.set_status("voiced")
    print(f"  ✓ {len(track) / SAMPLE_RATE / 60:.1f} min of narration, {len(mp3) // 1024} KB, "
          f"took {time.time() - started:.0f}s")
    return ep


def script_hash(script: dict) -> str:
    """Fingerprint of what gets spoken, so the dashboard can tell when the voice is out of date."""
    import hashlib
    spoken = "\n".join(f"{b['narration']}|{b.get('pause_after', 0.3)}"
                       for ch in script["chapters"] for b in ch["beats"])
    return hashlib.sha256(spoken.encode()).hexdigest()[:16]


def load_mp3(ep: Episode) -> bytes | None:
    data = ep.read("voice/narration.mp3.b64")
    return base64.b64decode(data) if data else None

"""Music and sound for the cut.

Music beds are synthesized here, so there is never a license question:

    calm      slow minor-key pads, a low root, a gentle shimmer: the default documentary bed
    tension   darker voicings and a soft low pulse, for stories about trouble
    uplift    major progression with quiet plucked arpeggios, for build and success stories

Each bed is a seamless 64-second loop (eight 8-second chords) rendered once per video and looped
to length. Your own tracks (e.g. from YouTube's Audio Library) can be uploaded in the dashboard
instead; their credit line goes into the description.

The mix keeps the narration on top: the music sits low and dips further whenever the narrator
speaks (side-chain ducking), fades in and out, and a few quiet effects mark structure: a whoosh
into each chapter, a soft hit when a big number lands.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import wave
from pathlib import Path

from .config import load_config
from .store import get_store

SR = 48000
MOODS = {"calm": "Calm (built in)", "tension": "Tension (built in)", "uplift": "Uplifting (built in)"}
SETTINGS = "_settings"


class AudioError(RuntimeError):
    pass


def cfg() -> dict:
    return load_config()["audio"]


# ──────────────────────────── synthesis ────────────────────────────

_NOTE = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def _hz(note: str) -> float:
    m = re.fullmatch(r"([A-G]#?)(\d)", note)
    semis = _NOTE[m.group(1)] + 12 * (int(m.group(2)) + 1) - 69  # A4 = 69 in MIDI
    return 440.0 * 2 ** (semis / 12)


# Chord voicings (root in the bass, pad notes above), 8 chords = one loop.
PROGRESSIONS = {
    "calm": [("A2", ["A3", "C4", "E4", "B4"]), ("F2", ["F3", "A3", "C4", "E4"]),
             ("C3", ["G3", "C4", "E4", "B4"]), ("G2", ["G3", "B3", "D4", "A4"]),
             ("A2", ["A3", "C4", "E4", "G4"]), ("F2", ["A3", "C4", "F4", "G4"]),
             ("D2", ["A3", "D4", "F4", "C5"]), ("E2", ["G#3", "B3", "E4", "B4"])],
    "tension": [("D2", ["A3", "D4", "F4", "E4"]), ("A#1", ["F3", "A#3", "D4", "C5"]),
                ("D2", ["A3", "D4", "F4", "G4"]), ("C2", ["G3", "C4", "D#4", "G4"]),
                ("D2", ["A3", "D4", "F4", "E4"]), ("A#1", ["F3", "A#3", "D4", "A4"]),
                ("G1", ["G3", "A#3", "D4", "F4"]), ("A1", ["E3", "A3", "C#4", "E4"])],
    "uplift": [("C3", ["G3", "C4", "E4", "D5"]), ("G2", ["G3", "B3", "D4", "A4"]),
               ("A2", ["A3", "C4", "E4", "B4"]), ("F2", ["A3", "C4", "F4", "G4"]),
               ("C3", ["G3", "C4", "E4", "G4"]), ("G2", ["B3", "D4", "G4", "A4"]),
               ("F2", ["A3", "C4", "F4", "C5"]), ("G2", ["G3", "B3", "D4", "F4"])],
}
CHORD_SECONDS = 8.0


def _pad_voice(np, freq: float, t, seed: int, width: float):
    """A soft pad note: three detuned partials with a slow shimmer; returns (left, right)."""
    rng = np.random.default_rng(seed)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for cents, pan in ((-6, 0.8), (0, 0.5), (6, 0.2)):
        f = freq * 2 ** (cents / 1200)
        phase = rng.uniform(0, 2 * np.pi)
        tone = np.sin(2 * np.pi * f * t + phase) + 0.18 * np.sin(4 * np.pi * f * t + phase)
        left += tone * (1 - pan * width)
        right += tone * (1 - (1 - pan) * width)
    shimmer = 1 + 0.15 * np.sin(2 * np.pi * rng.uniform(0.05, 0.12) * t + rng.uniform(0, 6.28))
    return left * shimmer / 3, right * shimmer / 3


def synth_loop(mood: str):
    """One seamless loop for a mood, stereo float32 at 48 kHz, peak about -12 dBFS."""
    import numpy as np
    prog = PROGRESSIONS[mood]
    n = int(CHORD_SECONDS * SR)
    over = int(2.0 * SR)  # chords overlap by 2 s, wrapping around at the loop seam
    total = n * len(prog)
    out = np.zeros((2, total), dtype=np.float64)
    seg_len = n + over
    t = np.arange(seg_len) / SR
    # Raised-cosine in and out, each `over` long: overlapping neighbours sum to a steady level.
    env = np.ones(seg_len)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(over) / over)
    env[:over] = ramp
    env[-over:] = ramp[::-1]
    for ci, (root, notes) in enumerate(prog):
        left = np.zeros(seg_len)
        right = np.zeros(seg_len)
        bass = np.sin(2 * np.pi * _hz(root) * t) * 0.55 + np.sin(2 * np.pi * _hz(root) * 2 * t) * 0.12
        left += bass
        right += bass
        for ni, note in enumerate(notes):
            l, r = _pad_voice(np, _hz(note), t, seed=ci * 31 + ni, width=0.6)
            gain = 0.32 if ni < len(notes) - 1 else 0.18  # the top note a touch quieter
            left += l * gain
            right += r * gain
        if mood == "uplift":  # gentle plucked arpeggio, eighth notes at 90 bpm
            step = 60 / 90 / 2
            for k in range(int(CHORD_SECONDS / step)):
                start = int(k * step * SR)
                note = notes[[0, 1, 2, 3, 2, 1][k % 6]]
                f = _hz(note) * 2
                dur = int(0.9 * SR)
                tt = np.arange(min(dur, seg_len - start)) / SR
                pluck = np.sin(2 * np.pi * f * tt) * np.exp(-tt * 5.5) * 0.10
                pan = 0.35 + 0.3 * ((k % 3) / 2)
                left[start:start + len(tt)] += pluck * (1 - pan)
                right[start:start + len(tt)] += pluck * pan
        if mood == "tension":  # a soft low pulse, like a slow heartbeat
            for k in range(int(CHORD_SECONDS)):
                for off, amp in ((0.0, 0.35), (0.28, 0.2)):
                    start = int((k + off) * SR)
                    tt = np.arange(int(0.35 * SR)) / SR
                    thump = np.sin(2 * np.pi * 52 * tt) * np.exp(-tt * 14) * amp
                    end = min(seg_len, start + len(tt))
                    left[start:end] += thump[: end - start]
                    right[start:end] += thump[: end - start]
        seg = np.vstack([left, right]) * env
        pos = ci * n
        idx = (np.arange(seg_len) + pos) % total  # wrap the tail of the last chord onto the start
        np.add.at(out[0], idx, seg[0])
        np.add.at(out[1], idx, seg[1])
    out *= 0.25 / max(1e-9, np.abs(out).max())  # -12 dBFS peak; the mix sets the real level
    return out.astype(np.float32)


def write_wav(path: Path, stereo) -> Path:
    import numpy as np
    pcm = (np.clip(stereo.T, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def whoosh(seconds: float = 0.9):
    """Filtered noise sweeping up then away: marks a new chapter."""
    import numpy as np
    n = int(seconds * SR)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    t = np.linspace(0, 1, n)
    out = np.zeros(n)
    # A cheap swept band-pass: a one-pole low-pass minus a lower one, cutoff rising then falling.
    lo = hi = 0.0
    for i in range(n):
        fc = 300 + 2600 * np.sin(np.pi * t[i]) ** 2
        a_hi = 1 - np.exp(-2 * np.pi * fc / SR)
        a_lo = 1 - np.exp(-2 * np.pi * fc * 0.35 / SR)
        hi += a_hi * (noise[i] - hi)
        lo += a_lo * (noise[i] - lo)
        out[i] = hi - lo
    env = np.sin(np.pi * t) ** 1.6
    out = out * env
    out *= 0.5 / max(1e-9, np.abs(out).max())
    pan = np.linspace(0.25, 0.75, n)  # drifts left to right
    return np.vstack([out * (1 - pan), out * pan]).astype(np.float32)


def hit(seconds: float = 1.4):
    """A soft low impact with a little air on top: a number landing on screen."""
    import numpy as np
    n = int(seconds * SR)
    t = np.arange(n) / SR
    body = np.sin(2 * np.pi * (70 - 25 * t) * t) * np.exp(-t * 4.5)
    air = np.random.default_rng(3).standard_normal(n) * np.exp(-t * 22) * 0.15
    out = body + air
    out *= 0.5 / max(1e-9, np.abs(out).max())
    return np.vstack([out, out]).astype(np.float32)


# ──────────────────────────── music library ────────────────────────────

def library() -> list[dict]:
    """Built-in moods plus your uploads."""
    out = [{"id": f"builtin:{k}", "name": v, "credit": ""} for k, v in MOODS.items()]
    raw = get_store().read(SETTINGS, "music/index.json")
    for t in json.loads(raw) if raw else []:
        out.append({"id": f"upload:{t['id']}", "name": t["name"], "credit": t.get("credit", "")})
    return out


def add_track(name: str, credit: str, data: bytes) -> dict:
    if len(data) > 12 * 1024 * 1024:
        raise AudioError("That file is over 12 MB. Use an MP3 under 12 MB.")
    if not (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise AudioError("That isn't an MP3 file.")
    store = get_store()
    raw = store.read(SETTINGS, "music/index.json")
    index = json.loads(raw) if raw else []
    tid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "track"
    while any(t["id"] == tid for t in index):
        tid += "-2"
    store.write(SETTINGS, f"music/{tid}.mp3.b64", base64.b64encode(data).decode())
    index.append({"id": tid, "name": name.strip()[:60] or tid, "credit": credit.strip()[:300]})
    store.write(SETTINGS, "music/index.json", json.dumps(index))
    return {"id": f"upload:{tid}", "name": index[-1]["name"], "credit": index[-1]["credit"]}


def remove_track(track_id: str) -> None:
    tid = track_id.removeprefix("upload:")
    store = get_store()
    raw = store.read(SETTINGS, "music/index.json")
    index = [t for t in (json.loads(raw) if raw else []) if t["id"] != tid]
    store.write(SETTINGS, "music/index.json", json.dumps(index))
    store.write(SETTINGS, f"music/{tid}.mp3.b64", "")


def credit_for(track_id: str | None) -> str:
    return next((t["credit"] for t in library() if t["id"] == track_id), "")


def choice(meta: dict) -> str:
    """The story's music: what you picked, else the configured default."""
    picked = (meta.get("music") or {}).get("track")
    return picked or cfg()["default"]


def bed_file(track_id: str, folder: Path) -> Path | None:
    """A file ffmpeg can loop under the narration, or None for no music."""
    if not track_id or track_id == "none":
        return None
    kind, _, key = track_id.partition(":")
    if kind == "builtin":
        if key not in PROGRESSIONS:
            raise AudioError(f"No built-in music called {key}.")
        return write_wav(folder / f"bed-{key}.wav", synth_loop(key))
    if kind == "upload":
        data = get_store().read(SETTINGS, f"music/{key}.mp3.b64")
        if not data:
            raise AudioError("That music track was deleted. Pick another on the story page.")
        out = folder / f"bed-{key}.mp3"
        out.write_bytes(base64.b64decode(data))
        return out
    raise AudioError(f"Unknown music choice {track_id}.")


# ──────────────────────────── the mix ────────────────────────────

def mix(narration: Path, duration: float, out: Path, bed: Path | None,
        whooshes: list[float] = (), hits: list[float] = (), music_db: float | None = None) -> Path:
    """Narration on top, music ducked under it, effects at their times. Writes AAC (.m4a)."""
    c = cfg()
    music_db = c["music_db"] if music_db is None else music_db
    folder = out.parent
    inputs = ["-i", str(narration)]
    chains, mixins = [], []
    chains.append("[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[voice][key]")
    mixins.append("[voice]")
    k = 1
    if bed is not None:
        inputs += ["-stream_loop", "-1", "-i", str(bed)]
        fade_out = max(0.0, duration - 3.5)
        chains.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{duration:.3f},"
                      f"volume={music_db}dB,afade=t=in:d=2.5,afade=t=out:st={fade_out:.3f}:d=3.5[bed]")
        # Duck: when the narrator talks the music dips (~22 dB under the voice) and comes back up in pauses.
        chains.append("[bed][key]sidechaincompress=threshold=0.02:ratio=3:attack=40:release=650:makeup=1[music]")
        mixins.append("[music]")
        k += 1
    else:
        chains.append("[key]anullsink")
    for name, times, gen, db in (("whoosh", whooshes, whoosh, c["whoosh_db"]), ("hit", hits, hit, c["hit_db"])):
        if not times:
            continue
        path = write_wav(folder / f"{name}.wav", gen())
        for i, at in enumerate(times):
            inputs += ["-i", str(path)]
            ms = max(0, int(at * 1000))
            chains.append(f"[{k}:a]volume={db}dB,adelay={ms}|{ms}[{name}{i}]")
            mixins.append(f"[{name}{i}]")
            k += 1
    chains.append(f"{''.join(mixins)}amix=inputs={len(mixins)}:normalize=0:duration=first,"
                  f"alimiter=limit=0.89:level=false[out]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", ";".join(chains), "-map", "[out]", "-t", f"{duration:.3f}",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioError(f"ffmpeg failed mixing the sound: {r.stderr.strip()[-400:]}")
    return out

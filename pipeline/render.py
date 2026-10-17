"""Stage 6: the cut.

narration MP3 + beat timings + chosen pictures -> a 1080p documentary MP4, captions and a preview.

Per beat:
  photo  -> slow Ken Burns move (direction alternates so it never feels mechanical)
  video  -> scaled/cropped to 16:9, trimmed (or looped) to the beat
  card   -> drawn with Pillow in the channel's type, with a gentle push-in
  + lower third (name/date) when the script has one
  + one grade for everything: a touch of contrast, slightly muted color, vignette, fine grain

Each beat lasts from its first word to the next beat's first word, so pictures change exactly
when the narrator moves on. Segments are encoded identically, joined without re-encoding, and
the narration is laid on top.

Runs on the GitHub Actions worker (needs ffmpeg + Pillow). The MP4 is too big for the database,
so the workflow keeps it as a downloadable artifact; a 60-second preview and the captions are
stored with the episode for the dashboard.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import ROOT, load_config, user_agent
from .episode import Episode
from . import visuals, voice

FONTS = ROOT / "assets" / "fonts"
OUTPUT = ROOT / "output"

INK = (16, 22, 28)
PAPER = (246, 243, 234)
MARKER = (243, 210, 74)
SOFT = (184, 193, 200)


class RenderError(RuntimeError):
    pass


def cfg() -> dict:
    return load_config()["render"]


def _ff(*args: str, what: str) -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(f"ffmpeg failed on {what}: {result.stderr.strip()[-500:]}")


# ──────────────────────────── drawing (cards, lower thirds) ────────────────────────────

def _font(size: int, weight: int = 800, width: int = 70):
    from PIL import ImageFont
    f = ImageFont.truetype(str(FONTS / "Archivo.ttf"), size)
    f.set_variation_by_axes([weight, width])  # axes: Weight, Width
    return f


def _mono(size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONTS / "CourierPrime-Regular.ttf"), size)


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _fit(draw, text: str, max_width: int, max_lines: int, start: int, weight=800, width=70):
    """Largest font size where the text fits in max_lines."""
    size = start
    while size > 28:
        font = _font(size, weight, width)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
        size -= 6
    font = _font(28, weight, width)
    return font, _wrap(draw, text, font, max_width)[:max_lines]


def draw_card(card: dict, path: Path) -> Path:
    from PIL import Image, ImageDraw
    W, H = 1920, 1080  # designed at 1080p, scaled to the output size at the end
    kind = card.get("card", "title")
    bg = PAPER if kind == "document" else INK
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    left, maxw = 160, W - 320

    if kind == "stat":
        big = _font(260, 850, 62)
        d.text((left, H * 0.30), card["big"], font=big, fill=MARKER)
        label_font, lines = _fit(d, card.get("text", ""), maxw, 3, 64, 600, 90)
        y = H * 0.30 + 300
        for line in lines:
            d.text((left, y), line, font=label_font, fill=SOFT)
            y += label_font.size * 1.25
    elif kind == "document":
        d.rectangle([110, 160, 126, H - 160], fill=MARKER)  # inside the vignette, where it stays visible
        d.text((left, 190), "DOCUMENT", font=_font(34, 700, 100), fill=(138, 101, 51))
        title_font, lines = _fit(d, card.get("text", ""), maxw, 3, 150)
        y = 260
        for line in lines:
            d.text((left, y), line, font=title_font, fill=INK)
            y += title_font.size * 1.08
        sub = _mono(40)
        for line in textwrap.wrap(card.get("sub", ""), 64)[:3]:
            d.text((left, y + 40), line, font=sub, fill=(92, 102, 112))
            y += 54
    else:
        if kind == "place":  # quiet map-paper grid behind a place name
            for x in range(0, W, 64):
                d.line([(x, 0), (x, H)], fill=(24, 36, 46), width=1)
            for y0 in range(0, H, 64):
                d.line([(0, y0), (W, y0)], fill=(24, 36, 46), width=1)
        title_font, lines = _fit(d, card.get("text", ""), maxw, 3, 170)
        total = len(lines) * title_font.size * 1.08
        y = (H - total) / 2
        for line in lines:
            d.text((left, y), line, font=title_font, fill=(243, 245, 246))
            y += title_font.size * 1.08

    out_size = (cfg()["width"], cfg()["height"])
    if out_size != (W, H):
        img = img.resize(out_size, Image.LANCZOS)
    img.save(path, quality=95)
    return path


def draw_lower_third(text: str, path: Path) -> Path:
    """Name/date strip, bottom left. Transparent PNG laid over the shot."""
    from PIL import Image, ImageDraw
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # "Stephen Elop — CEO, Nokia" -> name "Stephen Elop", role "CEO, Nokia"
    for sep in (" — ", " – ", " - ", ", "):
        if sep in text:
            name, _, role = text.partition(sep)
            break
    else:
        name, role = text, ""
    big, small = _font(52, 750, 80), _font(34, 500, 100)
    pad, x, y = 26, 110, H - 230
    w1 = d.textlength(name.strip(), font=big)
    w2 = d.textlength(role.strip(), font=small) if role.strip() else 0
    box_w = max(w1, w2) + pad * 2
    box_h = 52 + (34 + 14 if role.strip() else 0) + pad * 2
    d.rectangle([x, y, x + 8, y + box_h], fill=MARKER + (255,))
    d.rectangle([x + 8, y, x + 8 + box_w, y + box_h], fill=INK + (215,))
    d.text((x + 8 + pad, y + pad - 6), name.strip(), font=big, fill=(255, 255, 255, 255))
    if role.strip():
        d.text((x + 8 + pad, y + pad + 60), role.strip(), font=small, fill=SOFT + (255,))
    out_size = (cfg()["width"], cfg()["height"])
    if out_size != (W, H):
        img = img.resize(out_size, Image.LANCZOS)
    img.save(path)
    return path


# ──────────────────────────── assets ────────────────────────────

def fetch(candidate: dict, ep: Episode, dest: Path, session: requests.Session) -> Path | None:
    """Download (or decode) the chosen picture. None = fall back to a card."""
    if candidate["source"] == "card":
        return draw_card(candidate, dest.with_suffix(".jpg"))
    if candidate["source"] in ("upload", "ai"):
        found = visuals.load_upload(ep, candidate["file"])
        if not found:
            return None
        mime, data = found
        out = dest.with_suffix(".png" if mime.endswith("png") else ".jpg")
        out.write_bytes(data)
        return out
    url = candidate.get("url")
    if not url:
        return None
    if url.startswith("/static/"):  # demo mode's sample images live in web/
        local = ROOT / "web" / url.removeprefix("/static/")
        if local.exists():
            out = dest.with_suffix(local.suffix)
            shutil.copy(local, out)
            return out
    suffix = ".mp4" if candidate.get("kind") == "video" else Path(url.split("?")[0]).suffix or ".jpg"
    out = dest.with_suffix(suffix)
    for attempt in range(3):
        try:
            with session.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(out, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            return out
        except requests.RequestException as e:
            print(f"    download failed ({e}), retrying")
            time.sleep(2 * (attempt + 1))
    return None


# ──────────────────────────── segments ────────────────────────────

# Camera moves for stills, cycled per beat: (zoom at start, zoom at end, x anchor, y anchor).
# Zoom is in units of ken_burns_zoom (0 = full frame, 1 = fully zoomed); anchors are 0..1 across
# the spare room. "pan" anchors slide from one side to the other over the beat.
_MOVES = [
    (0, 1, 0.5, 0.5),       # push in, centered
    (1, 0, 0.5, 0.45),      # pull out
    (1, 1, "pan", 0.5),     # pan left to right
    (0, 1, 0.3, 0.35),      # push in toward the upper left third
    (1, 1, "pan-back", 0.5),  # pan right to left
]


def _grade() -> str:
    g = cfg()["grain"]
    chain = ["eq=contrast=1.05:saturation=0.9:gamma=0.98", "vignette=angle=PI/5"]
    if g:
        chain.append(f"noise=alls={g}:allf=t")
    return ",".join(chain)


def still_filter(index: int, frames: int) -> str:
    c = cfg()
    W, H, fps, k = c["width"], c["height"], c["fps"], c["ken_burns_zoom"]
    a, b, ax, ay = _MOVES[index % len(_MOVES)]
    z0, z1 = 1 + a * k, 1 + b * k
    p = f"(on/{max(1, frames - 1)})"
    zoom = f"({z0}+({z1}-{z0})*{p})"
    if ax in ("pan", "pan-back"):
        s0, s1 = (0.0, 1.0) if ax == "pan" else (1.0, 0.0)
        xexpr = f"(iw-iw/zoom)*({s0}+({s1}-{s0})*{p})"
    else:
        xexpr = f"(iw-iw/zoom)*{ax}"
    yexpr = f"(ih-ih/zoom)*{ay}"
    # 1.5x oversample keeps zoompan's integer stepping from looking jittery
    return (f"scale={int(W * 1.5)}:{int(H * 1.5)}:force_original_aspect_ratio=increase,"
            f"crop={int(W * 1.5)}:{int(H * 1.5)},setsar=1,"
            f"zoompan=z='{zoom}':x='{xexpr}':y='{yexpr}':d={frames}:s={W}x{H}:fps={fps}")


def video_filter() -> str:
    c = cfg()
    return (f"scale={c['width']}:{c['height']}:force_original_aspect_ratio=increase,"
            f"crop={c['width']}:{c['height']},fps={c['fps']},setsar=1")


def make_segment(index: int, asset: Path, kind: str, seconds: float, lower_third: Path | None,
                 out: Path) -> Path:
    c = cfg()
    frames = max(1, round(seconds * c["fps"]))
    enc = ["-c:v", "libx264", "-preset", c["preset"], "-crf", str(c["crf"]),
           "-maxrate", c["max_bitrate"], "-bufsize", c["max_bitrate"].replace("M", "") + "0M",
           "-pix_fmt", "yuv420p", "-r", str(c["fps"]), "-an", "-frames:v", str(frames)]

    if kind == "video":
        inputs = ["-stream_loop", "-1", "-i", str(asset)]
        base = f"[0:v]{video_filter()},{_grade()}[v]"
    else:
        inputs = ["-i", str(asset)]
        base = f"[0:v]{still_filter(index, frames)},{_grade()}[v]"

    if lower_third:
        hold = min(cfg()["lower_third_seconds"], max(1.5, seconds - 0.5))
        inputs += ["-loop", "1", "-i", str(lower_third)]
        graph = (f"{base};[1:v]format=rgba,fade=in:st=0.3:d=0.4:alpha=1,"
                 f"fade=out:st={hold:.2f}:d=0.4:alpha=1[lt];[v][lt]overlay=0:0:shortest=0[out]")
        _ff(*inputs, "-filter_complex", graph, "-map", "[out]", *enc, str(out), what=f"segment {index}")
    else:
        _ff(*inputs, "-filter_complex", base, "-map", "[v]", *enc, str(out), what=f"segment {index}")
    return out


# ──────────────────────────── captions ────────────────────────────

def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def captions(script: dict, timings: dict, words_per_caption: int = 9) -> str:
    """SRT from beat timings: each beat's words spread evenly over its spoken time.
    Uploaded to YouTube as a caption track (not burned in), which also helps search."""
    spoken = {b["id"]: b for b in timings["beats"]}
    cues, n = [], 1
    for ch in script["chapters"]:
        for beat in ch["beats"]:
            t = spoken.get(beat["id"])
            if not t:
                continue
            words = beat["narration"].split()
            chunks = [words[i:i + words_per_caption] for i in range(0, len(words), words_per_caption)]
            span = (t["end"] - t["start"]) / max(1, len(words))
            pos = t["start"]
            for chunk in chunks:
                end = pos + span * len(chunk)
                cues.append(f"{n}\n{_ts(pos)} --> {_ts(end)}\n{' '.join(chunk)}\n")
                n += 1
                pos = end
    return "\n".join(cues)


# ──────────────────────────── run ────────────────────────────

def plan(script: dict, timings: dict) -> list[dict]:
    """Beat order with on-screen durations: first word of a beat to first word of the next."""
    beats = [b for ch in script["chapters"] for b in ch["beats"]]
    spoken = {b["id"]: b for b in timings["beats"]}
    missing = [b["id"] for b in beats if b["id"] not in spoken]
    if missing:
        raise RenderError("The narration doesn't match the script any more. Re-record it first "
                          f"(beats without audio: {', '.join(missing[:5])}).")
    out = []
    for i, b in enumerate(beats):
        start = 0.0 if i == 0 else spoken[b["id"]]["start"]
        end = spoken[beats[i + 1]["id"]]["start"] if i + 1 < len(beats) else timings["duration"]
        out.append({"beat": b, "start": start, "seconds": round(end - start, 3)})
    return out


def run(slug: str, progress=None) -> dict:
    if not shutil.which("ffmpeg"):
        raise RenderError("ffmpeg isn't installed.")
    ep = Episode.load(slug)
    script, timings = ep.read("script.json"), ep.read("voice/timings.json")
    manifest = ep.read("visuals.json")
    mp3 = voice.load_mp3(ep)
    if not (script and timings and mp3):
        raise RenderError("Narration missing. Record it before rendering.")
    if timings.get("script_hash") != voice.script_hash(script):
        raise RenderError("The script changed after the narration was recorded. Re-record it first.")
    if not manifest:
        raise RenderError("Pick the pictures before rendering.")
    picks = {b["id"]: b["candidates"][b["chosen"]] for b in manifest["beats"] if b["candidates"]}

    started = time.time()
    topic = ep.read("meta.json")["topic"]
    segments = plan(script, timings)
    print(f"[render] {topic}: {len(segments)} shots, {timings['duration'] / 60:.1f} min")

    OUTPUT.mkdir(exist_ok=True)
    final = OUTPUT / f"{slug}.mp4"
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "narration.mp3").write_bytes(mp3)
        parts, fallbacks = [], 0
        for i, seg in enumerate(segments):
            beat = seg["beat"]
            pick = picks.get(beat["id"]) or visuals.make_card(beat)
            asset = fetch(pick, ep, tmp / f"asset{i:03}", session)
            kind = pick.get("kind", "image")
            if asset is None:  # dead link: an honest card beats a black hole
                fallbacks += 1
                asset, kind = draw_card(visuals.make_card(beat), tmp / f"asset{i:03}.jpg"), "image"
            l3 = (beat.get("on_screen_text") or "").strip()
            if pick.get("source") == "card" and l3 and l3.lower() in " ".join(
                    str(pick.get(k, "")) for k in ("text", "big", "sub")).lower():
                l3 = ""  # the card already says it in big type
            lt = draw_lower_third(l3, tmp / f"lt{i:03}.png") if l3 else None
            try:
                make_segment(i, asset, "video" if kind == "video" else "image", seg["seconds"], lt,
                             tmp / f"seg{i:03}.mp4")
            except RenderError:
                # A file ffmpeg can't read (odd codec, broken download): fall back to the card.
                fallbacks += 1
                card = draw_card(visuals.make_card(beat), tmp / f"card{i:03}.jpg")
                make_segment(i, card, "image", seg["seconds"], lt, tmp / f"seg{i:03}.mp4")
            parts.append(tmp / f"seg{i:03}.mp4")
            if progress:
                progress(i + 1, len(segments))
            (tmp / f"asset{i:03}.mp4").unlink(missing_ok=True)  # stock clips are big; free space as we go

        (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts))
        _ff("-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-c", "copy", str(tmp / "picture.mp4"),
            what="joining shots")
        _ff("-i", str(tmp / "picture.mp4"), "-i", str(tmp / "narration.mp3"),
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart", str(final), what="adding narration")

        # Small preview for the dashboard: first minute, 480p.
        preview = tmp / "preview.mp4"
        _ff("-i", str(final), "-t", str(cfg()["preview_seconds"]), "-vf", "scale=854:-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(preview), what="preview")
        ep.write("render/preview.mp4.b64", base64.b64encode(preview.read_bytes()).decode())

    srt = captions(script, timings)
    ep.write("render/captions.srt", srt)
    (OUTPUT / f"{slug}.srt").write_text(srt, encoding="utf-8")

    info = {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds_taken": round(time.time() - started),
        "duration": timings["duration"],
        "size_mb": round(final.stat().st_size / 1e6, 1),
        "shots": len(segments),
        "fallbacks": fallbacks,
        "file": final.name,
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "repo": os.getenv("GITHUB_REPOSITORY"),
    }
    meta = ep.read("meta.json")
    meta["render"] = {**(meta.get("render") or {}), **info}
    ep.write("meta.json", meta)
    ep.set_status("rendered")
    print(f"  ✓ {final.name}: {info['size_mb']} MB, {len(segments)} shots, "
          f"{fallbacks} fell back to cards, took {info['seconds_taken']}s")
    return info


def load_preview(ep: Episode) -> bytes | None:
    data = ep.read("render/preview.mp4.b64")
    return base64.b64decode(data) if data else None

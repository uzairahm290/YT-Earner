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
import math
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import ROOT, load_config, user_agent
from .episode import Episode
from . import audio, motion, visuals, voice

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
        if kind == "place":  # a map pin above the name; `pin` animates it dropping in (0 → 1)
            drop = card.get("pin", 1.0)
            tip_y = (y - 40) * drop
            if tip_y > 0:
                px = left + 52
                d.ellipse([px - 34, tip_y - 112, px + 34, tip_y - 44], fill=MARKER)
                d.polygon([(px - 29, tip_y - 66), (px + 29, tip_y - 66), (px, tip_y)], fill=MARKER)
                d.ellipse([px - 13, tip_y - 91, px + 13, tip_y - 65], fill=INK)
                if drop >= 0.98:
                    d.ellipse([px - 26, tip_y - 6, px + 26, tip_y + 8], outline=(70, 82, 92), width=3)
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


def draw_chapter_title(number: int, title: str, path: Path, bottom: bool = False) -> Path:
    """'CHAPTER 2' over the chapter's name on a soft dark band, top left. Over cards, whose own
    text fills the frame, a compact one-line strip along the bottom edge instead. Transparent PNG."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bottom:
        label, big = _font(28, 700, 100), _font(44, 760, 80)
        tag = f"CHAPTER {number}"
        tw = d.textlength(tag, font=label)
        x, y = 110, H - 96
        d.rectangle([0, H - 132, W, H], fill=INK + (235,))
        d.rectangle([0, H - 132, W, H - 128], fill=MARKER + (255,))
        d.text((x, y), tag, font=label, fill=MARKER + (255,))
        d.text((x + tw + 28, y - 10), title, font=big, fill=(255, 255, 255, 255))
    else:
        label, big = _font(34, 700, 100), _font(84, 820, 72)
        lines = _wrap(d, title, big, 1100)[:2]
        box_w = max(d.textlength(ln, font=big) for ln in lines) + 80
        box_h = 70 + len(lines) * 92 + 30
        x, y = 110, 110
        glow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(glow).rounded_rectangle([x - 40, y - 40, x + box_w, y + box_h], radius=40, fill=170)
        shade = Image.new("RGBA", (W, H), INK + (0,))
        shade.putalpha(glow.filter(ImageFilter.GaussianBlur(40)))
        img = Image.alpha_composite(img, shade)
        d = ImageDraw.Draw(img)
        d.text((x, y), f"CHAPTER {number}", font=label, fill=MARKER + (255,))
        d.rectangle([x, y + 52, x + 64, y + 57], fill=MARKER + (255,))
        for i, ln in enumerate(lines):
            d.text((x, y + 76 + i * 92), ln, font=big, fill=(255, 255, 255, 255))
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
    """Per-shot colour: a touch of contrast, slightly muted, vignette. Grain is added once, in the
    final pass, so the in-between files stay clean and fast to encode."""
    return "eq=contrast=1.05:saturation=0.9:gamma=0.98,vignette=angle=PI/5"


def _grain() -> str:
    g = cfg()["grain"]
    return f"noise=alls={g}:allf=t" if g else "null"


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


def _intermediate() -> list[str]:
    """Shots are encoded fast and nearly lossless; the final pass does the real compression."""
    c = cfg()
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(c["fps"]), "-an"]


def _overlay_chain(base: str, overlays: list[tuple[Path, float, float]], first_input: int) -> tuple[list[str], str, str]:
    """ffmpeg inputs + filter graph laying transparent PNGs over [v], each fading in and out."""
    inputs, graph, last = [], base, "v"
    for k, (png, start, hold) in enumerate(overlays):
        idx = first_input + k
        inputs += ["-loop", "1", "-i", str(png)]
        graph += (f";[{idx}:v]format=rgba,fade=in:st={start:.2f}:d=0.4:alpha=1,"
                  f"fade=out:st={start + hold:.2f}:d=0.4:alpha=1[o{k}];[{last}][o{k}]overlay=0:0:shortest=0[v{k}]")
        last = f"v{k}"
    return inputs, graph, last


def make_segment(index: int, asset: Path, kind: str, seconds: float, overlays: list[tuple[Path, float, float]],
                 out: Path, frames_dir: list[Path] | None = None) -> Path:
    """One shot. kind: video | image | card. frames_dir: an animated card's entrance frames."""
    c = cfg()
    frames = max(1, round(seconds * c["fps"]))
    enc = [*_intermediate(), "-frames:v", str(frames)]

    if kind == "image" and c.get("parallax"):
        moving = motion.parallax_frames(asset, frames, index, (c["width"], c["height"]))
        if moving is not None:
            return motion.encode_frames(moving, out, seconds, c["fps"], _grade(), (c["width"], c["height"]), enc, overlays)

    if kind == "video":
        inputs = ["-stream_loop", "-1", "-i", str(asset)]
        base = f"[0:v]{video_filter()},{_grade()}[v]"
    elif frames_dir:
        # The entrance frames, then the last one held for the rest of the shot.
        hold = max(0.0, seconds - len(frames_dir) / c["fps"])
        inputs = ["-framerate", str(c["fps"]), "-i", str(frames_dir[0].parent / "f%03d.jpg")]
        base = (f"[0:v]scale={c['width']}:{c['height']},setsar=1,tpad=stop_mode=clone:stop_duration={hold + 1:.2f},"
                f"{_grade()}[v]")
    else:
        inputs = ["-i", str(asset)]
        base = f"[0:v]{still_filter(index, frames)},{_grade()}[v]"
    more, graph, last = _overlay_chain(base, overlays, 1)
    _ff(*inputs, *more, "-filter_complex", graph, "-map", f"[{last}]", *enc, str(out), what=f"shot {index}")
    return out


def crossfade(parts: list[Path], seconds: list[float], out: Path, fade: float) -> Path:
    """Join one chapter's shots with soft dissolves. Each part except the last was cut `fade`
    seconds long so the dissolve doesn't steal time from the narration."""
    if len(parts) == 1:
        shutil.copy(parts[0], out)
        return out
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]
    graph, last, offset = [], "0:v", 0.0
    for k in range(1, len(parts)):
        offset += seconds[k - 1]
        graph.append(f"[{last}][{k}:v]xfade=transition=fade:duration={fade}:offset={offset:.3f}[x{k}]")
        last = f"x{k}"
    _ff(*inputs, "-filter_complex", ";".join(graph), "-map", f"[{last}]", *_intermediate(), str(out),
        what="dissolves")
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


def usable_alternates(beat_manifest: dict | None, chosen: dict) -> list[dict]:
    """The picked picture first, then the beat's other real pictures. AI images and cards only
    appear if they're the one you picked."""
    if not beat_manifest:
        return [chosen]
    others = [c for c in beat_manifest.get("candidates", []) if c is not chosen
              and c["source"] not in ("card", "ai") and not c.get("needs_screenshot")]
    return [chosen] + others


def shots(script: dict, timings: dict, picks: dict, manifest: dict) -> list[dict]:
    """Beats -> shots. A beat that stays on screen longer than max_shot_seconds is split, moving
    through its other real pictures (or new camera moves on the same one)."""
    c = cfg()
    by_id = {b["id"]: b for b in manifest.get("beats", [])}
    chapter_of = {b["id"]: ci for ci, ch in enumerate(script["chapters"]) for b in ch["beats"]}
    out, seen_chapters = [], set()
    for seg in plan(script, timings):
        beat = seg["beat"]
        pick = picks.get(beat["id"]) or visuals.make_card(beat)
        options = [pick] if pick["source"] == "card" else usable_alternates(by_id.get(beat["id"]), pick)
        n = 1 if pick["source"] == "card" else max(1, math.ceil(seg["seconds"] / c["max_shot_seconds"] - 0.15))
        ci = chapter_of[beat["id"]]
        for k in range(n):
            out.append({"beat": beat, "chapter": ci, "pick": options[k % len(options)],
                        "start": seg["start"] + seg["seconds"] * k / n, "seconds": seg["seconds"] / n,
                        "first_of_beat": k == 0, "first_of_chapter": k == 0 and ci not in seen_chapters})
        seen_chapters.add(ci)
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
    meta = ep.read("meta.json")
    c = cfg()
    fade = c["crossfade"]

    started = time.time()
    topic = meta["topic"]
    cut = shots(script, timings, picks, manifest)
    print(f"[render] {topic}: {len(cut)} shots from {len(picks)} beats, {timings['duration'] / 60:.1f} min")

    OUTPUT.mkdir(exist_ok=True)
    final = OUTPUT / f"{slug}.mp4"
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    fetched: dict[str, Path | None] = {}
    whooshes, hits = [], []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "narration.mp3").write_bytes(mp3)
        parts, lengths, fallbacks, jobs = [], [], 0, []
        groups: list[list[int]] = []
        prev_joins = False
        for i, shot in enumerate(cut):
            beat, pick = shot["beat"], shot["pick"]
            # Dissolve into the next shot when it's in the same chapter and both are long enough.
            nxt = cut[i + 1] if i + 1 < len(cut) else None
            joins = bool(fade) and nxt is not None and nxt["chapter"] == shot["chapter"] \
                and shot["seconds"] > 1.0 and nxt["seconds"] > 1.0
            if not groups or not prev_joins:
                groups.append([])
            groups[-1].append(i)
            prev_joins = joins
            length = shot["seconds"] + (fade if joins else 0)

            key = pick.get("file") or pick.get("url") or f"card-{beat['id']}"
            if key not in fetched:
                fetched[key] = fetch(pick, ep, tmp / f"asset{len(fetched):03}", session)
            asset, kind = fetched[key], pick.get("kind", "image")
            frames_dir = None
            if asset is None:  # dead link: an honest card beats a black hole
                fallbacks += 1
                asset, kind = draw_card(visuals.make_card(beat), tmp / f"card{i:03}.jpg"), "image"
            elif pick["source"] == "card":
                fdir = tmp / f"anim{i:03}"
                fdir.mkdir()
                frames_dir = motion.card_frames(pick, draw_card, fdir, c["fps"])
                kind = "card"
                if frames_dir and pick.get("card") == "stat":
                    hits.append(shot["start"] + 1.15)  # lands with the number

            overlays = []
            l3 = (beat.get("on_screen_text") or "").strip() if shot["first_of_beat"] else ""
            if pick.get("source") == "card" and l3 and l3.lower() in " ".join(
                    str(pick.get(k, "")) for k in ("text", "big", "sub")).lower():
                l3 = ""  # the card already says it in big type
            if l3:
                hold = min(c["lower_third_seconds"], max(1.5, shot["seconds"] - 0.5))
                overlays.append((draw_lower_third(l3, tmp / f"lt{i:03}.png"), 0.3, hold))
            if shot["first_of_chapter"] and shot["chapter"] > 0:
                ch = script["chapters"][shot["chapter"]]
                overlays.append((draw_chapter_title(shot["chapter"], ch["title"], tmp / f"ch{i:03}.png",
                                                    bottom=pick["source"] == "card"),
                                 0.4, min(3.2, max(1.5, shot["seconds"] - 0.6))))
                whooshes.append(max(0.0, shot["start"] - 0.35))

            jobs.append({"i": i, "asset": asset, "kind": kind, "length": length, "overlays": overlays,
                         "frames_dir": frames_dir, "beat": beat, "out": tmp / f"seg{i:03}.mp4"})
            parts.append(tmp / f"seg{i:03}.mp4")
            lengths.append(shot["seconds"])

        # Shots are independent, so encode several at once (ffmpeg and the parallax maths both
        # release Python's lock); a free GitHub runner has 4 cores.
        done_count = [0]
        lock = threading.Lock()

        def make(job):
            try:
                make_segment(job["i"], job["asset"], job["kind"], job["length"], job["overlays"], job["out"],
                             job["frames_dir"])
                fell = 0
            except (RenderError, RuntimeError) as e:
                # A file ffmpeg can't read (odd codec, broken download): fall back to the card.
                print(f"    shot {job['i']}: {str(e)[:120]}; using a card")
                card = draw_card(visuals.make_card(job["beat"]), job["out"].with_suffix(".fb.jpg"))
                make_segment(job["i"], card, "image", job["length"], job["overlays"], job["out"])
                fell = 1
            with lock:
                done_count[0] += 1
                if progress:
                    progress(done_count[0], len(jobs))
            return fell

        print(f"  prepared {len(jobs)} shots ({time.time() - started:.0f}s)")
        workers = max(1, min(4, (os.cpu_count() or 2)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fallbacks += sum(pool.map(make, jobs))

        print(f"  shots encoded ({time.time() - started:.0f}s)")
        joined = []
        for gi, g in enumerate(groups):
            joined.append(crossfade([parts[i] for i in g], [lengths[i] for i in g], tmp / f"group{gi:03}.mp4", fade))
            for i in g:
                parts[i].unlink(missing_ok=True)
        (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in joined))

        print(f"  dissolves joined ({time.time() - started:.0f}s)")
        track = audio.choice(meta)
        bed = audio.bed_file(track, tmp)
        audio.mix(tmp / "narration.mp3", timings["duration"], tmp / "sound.m4a", bed, whooshes, hits)
        print(f"  sound mixed ({time.time() - started:.0f}s)")
        _ff("-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-i", str(tmp / "sound.m4a"),
            "-map", "0:v", "-map", "1:a", "-vf", _grain(), "-c:v", "libx264", "-preset", c["preset"], "-crf", str(c["crf"]),
            "-maxrate", c["max_bitrate"], "-bufsize", c["max_bitrate"].replace("M", "") + "0M",
            "-pix_fmt", "yuv420p", "-r", str(c["fps"]), "-c:a", "copy",
            "-t", f"{timings['duration']:.3f}", "-movflags", "+faststart", str(final), what="final cut")

        # Small preview for the dashboard: first minute, 480p.
        preview = tmp / "preview.mp4"
        _ff("-i", str(final), "-t", str(c["preview_seconds"]), "-vf", "scale=854:-2",
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
        "shots": len(cut),
        "fallbacks": fallbacks,
        "music": track,
        "parallax": bool(c.get("parallax")) and not motion._depth_failed,
        "file": final.name,
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "repo": os.getenv("GITHUB_REPOSITORY"),
    }
    meta = ep.read("meta.json")
    meta["render"] = {**(meta.get("render") or {}), **info}
    ep.write("meta.json", meta)
    ep.set_status("rendered")
    print(f"  ✓ {final.name}: {info['size_mb']} MB, {len(cut)} shots, "
          f"{fallbacks} fell back to cards, took {info['seconds_taken']}s")
    return info


def load_preview(ep: Episode) -> bytes | None:
    data = ep.read("render/preview.mp4.b64")
    return base64.b64decode(data) if data else None

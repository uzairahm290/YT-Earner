"""The story-video cut: stock footage under a dark overlay, the narrator cut out on the left,
big yellow captions on a dark band, an avatar badge and a subscribe button.

Layers, back to front:
    1. the clips (a new one at least every ~11 s, soft dissolves inside a chapter)
    2. a black overlay (~40%) so everything on top reads, and the footage feels like backdrop
    3. the narrator cutout, bottom left, about 90% of the frame's height
    4. a round avatar of the narrator, top right, with a yellow ring
    5. a subscribe button, top left: in for a few seconds at the start, then once every 5 minutes
    6. captions: a dark band across the lower third, two lines of yellow capitals, timed to the
       narration word by word with Whisper (falls back to word length when Whisper isn't there)
Music: the story's bed, ducked under the voice.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import load_config, user_agent
from .episode import Episode
from . import audio, persona, render, shorts, visuals, voice

YELLOW = (246, 232, 60)


class StoryRenderError(RuntimeError):
    pass


def cfg() -> dict:
    return load_config()["story"]


# ──────────────────────────── drawn layers ────────────────────────────

def _place(frame, path: Path) -> tuple[Path, int, int]:
    """Crop a full-frame transparent layer to what's drawn on it: overlaying a small image at its
    spot costs ffmpeg a fraction of blending a whole transparent frame, 30 times a second."""
    box = frame.getbbox() or (0, 0, 2, 2)
    frame.crop(box).save(path)
    return path, box[0], box[1]


def draw_persona(png: bytes, folder: Path, W: int, H: int) -> tuple[tuple, tuple]:
    """((cutout, x, y), (round avatar, x, y)) as transparent PNGs placed for the frame."""
    import io
    import numpy as np
    from PIL import Image, ImageDraw
    cut = Image.open(io.BytesIO(png)).convert("RGBA")
    target_h = int(H * 0.92)
    scale = target_h / cut.height
    if cut.width * scale > W * 0.42:  # very wide photos: cap the width instead
        scale = W * 0.42 / cut.width
    body = cut.resize((round(cut.width * scale), round(cut.height * scale)), Image.LANCZOS)
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    frame.alpha_composite(body, (int(W * 0.02), H - body.height))
    body_layer = _place(frame, folder / "persona.png")

    # Avatar: a square around the head (the widest opaque run near the top), in a ring.
    a = np.asarray(cut)[..., 3] > 128
    rows = np.where(a.any(axis=1))[0]
    top = rows[0] if len(rows) else 0
    head_h = int(cut.height * 0.26)
    cols = np.where(a[top:top + head_h].any(axis=0))[0]
    cx = int(cols.mean()) if len(cols) else cut.width // 2
    side = int(head_h * 1.15)
    box = (max(0, cx - side // 2), max(0, top - side // 12), max(0, cx - side // 2) + side, max(0, top - side // 12) + side)
    face = Image.new("RGBA", (side, side), (40, 46, 54, 255))
    face.alpha_composite(cut.crop(box))
    D = int(H * 0.17)
    face = face.resize((D, D), Image.LANCZOS)
    mask = Image.new("L", (D, D), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, D - 1, D - 1], fill=255)
    ring = int(D * 0.045)
    badge = Image.new("RGBA", (D + 2 * ring, D + 2 * ring), (0, 0, 0, 0))
    ImageDraw.Draw(badge).ellipse([0, 0, D + 2 * ring - 1, D + 2 * ring - 1], fill=YELLOW + (255,))
    badge.paste(face, (ring, ring), mask)
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    frame.alpha_composite(badge, (W - badge.width - int(W * 0.025), int(H * 0.035)))
    return body_layer, _place(frame, folder / "avatar.png")


def draw_subscribe(folder: Path, W: int, H: int) -> tuple:
    """A red pill with a bell and SUBSCRIBE. Generic on purpose: no YouTube logo."""
    from PIL import Image, ImageDraw
    s = H / 1080
    font = render._font(int(54 * s), 820, 90)
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(frame)
    tw = d.textlength("SUBSCRIBE", font=font)
    x, y, h = int(40 * s), int(36 * s), int(96 * s)
    w = int(tw + 150 * s)
    d.rounded_rectangle([x + 4, y + 6, x + w + 4, y + h + 6], radius=h // 2, fill=(0, 0, 0, 110))  # shadow
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(214, 28, 36, 255))
    # bell
    bx, by, r = x + int(52 * s), y + h // 2, int(22 * s)
    d.pieslice([bx - r, by - r - int(6 * s), bx + r, by + r], 180, 360, fill=(255, 255, 255, 255))
    d.rectangle([bx - r, by - int(6 * s), bx + r, by + int(10 * s)], fill=(255, 255, 255, 255))
    d.rectangle([bx - r - int(5 * s), by + int(10 * s), bx + r + int(5 * s), by + int(15 * s)], fill=(255, 255, 255, 255))
    d.ellipse([bx - int(6 * s), by + int(15 * s), bx + int(6 * s), by + int(25 * s)], fill=(255, 255, 255, 255))
    d.text((x + int(96 * s), y + (h - font.size) / 2 - int(6 * s)), "SUBSCRIBE", font=font, fill=(255, 255, 255, 255))
    return _place(frame, folder / "subscribe.png")


def _caption_lines(d, words: list[str], font, max_w: int) -> list[str]:
    lines, line = [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if d.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def draw_caption(words: list[str], path: Path, W: int) -> Path:
    """Two lines of yellow capitals on a dark band, full width. The band is drawn per page so it
    only shows while someone is talking."""
    from PIL import Image, ImageDraw
    s = W / 1920
    band_h = int(250 * s)
    img = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = int(70 * s)
    text = [w.upper() for w in words]
    while True:
        font = render._font(size, 900, 82)
        lines = _caption_lines(d, text, font, int(W * 0.78))
        if len(lines) <= 2 or size <= int(44 * s):
            break
        size -= int(4 * s) or 1
    d.rectangle([0, 0, W, band_h], fill=(10, 12, 14, 165))
    lh = int(size * 1.18)
    y = (band_h - lh * len(lines[:2])) / 2
    for ln in lines[:2]:
        tw = d.textlength(ln, font=font)
        d.text(((W - tw) / 2, y), ln, font=font, fill=YELLOW + (255,), stroke_width=max(2, int(4 * s)),
               stroke_fill=(0, 0, 0, 255))
        y += lh
    img.save(path)
    return path


# ──────────────────────────── word timings ────────────────────────────

def _pcm16k(mp3: Path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def caption_pages(script: dict, timings: dict, mp3: Path) -> list[dict]:
    """[{start, end, words}] covering the narration, a page at a time."""
    per = cfg()["caption_words"]
    listener = shorts.Listener()
    pcm = _pcm16k(mp3) if listener.model is not None else None
    spoken = {b["id"]: b for b in timings["beats"]}
    beats = [b for ch in script["chapters"] for b in ch["beats"]]
    pages = []
    for i, beat in enumerate(beats):
        t = spoken.get(beat["id"])
        if not t:
            continue
        words = beat["narration"].split()
        heard = []
        if pcm is not None:
            clip = pcm[int(t["start"] * 16000): int(t["end"] * 16000)]
            heard = [(w, t["start"] + a, t["start"] + b) for w, a, b in listener.words(clip, 16000)]
        times = shorts.align(words, heard, t["start"], t["end"])
        next_start = spoken[beats[i + 1]["id"]]["start"] if i + 1 < len(beats) and beats[i + 1]["id"] in spoken \
            else timings["duration"]
        groups = shorts.pages(words, per=per)
        for gi, g in enumerate(groups):
            start = times[g[0]][0]
            end = times[groups[gi + 1][0]][0] if gi + 1 < len(groups) else min(next_start, times[g[-1]][1] + 0.6)
            pages.append({"start": round(start, 3), "end": round(max(end, start + 0.3), 3),
                          "words": [words[k] for k in g]})
    return pages


def srt(pages: list[dict]) -> str:
    return "\n".join(f"{n}\n{render._ts(p['start'])} --> {render._ts(p['end'])}\n{' '.join(p['words'])}\n"
                     for n, p in enumerate(pages, 1))


# ──────────────────────────── the cut ────────────────────────────

def run(slug: str, progress=None) -> dict:
    if not shutil.which("ffmpeg"):
        raise StoryRenderError("ffmpeg isn't installed.")
    ep = Episode.load(slug)
    script, timings = ep.read("script.json"), ep.read("voice/timings.json")
    manifest = ep.read("visuals.json")
    mp3 = voice.load_mp3(ep)
    if not (script and timings and mp3):
        raise StoryRenderError("Narration missing. Record it before rendering.")
    if timings.get("script_hash") != voice.script_hash(script):
        raise StoryRenderError("The story changed after the narration was recorded. Re-record it first.")
    if not manifest:
        raise StoryRenderError("Find the clips before rendering.")
    face = persona.cutout_png()
    if not face:
        raise StoryRenderError("Add the narrator photo first (Pictures tab → Narrator).")
    picks = {b["id"]: b["candidates"][b["chosen"]] for b in manifest["beats"] if b["candidates"]}
    meta = ep.read("meta.json")
    rc, sc = render.cfg(), cfg()
    W, H, fps = rc["width"], rc["height"], rc["fps"]
    fade = rc["crossfade"]

    started = time.time()
    cut = render.shots(script, timings, picks, manifest, max_shot=sc["max_shot_seconds"])
    print(f"[story render] {meta['topic']}: {len(cut)} shots, {timings['duration'] / 60:.1f} min")
    render.OUTPUT.mkdir(exist_ok=True)
    final = render.OUTPUT / f"{slug}.mp4"
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    fetched: dict[str, Path | None] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "narration.mp3").write_bytes(mp3)

        # 1. Background shots, grouped by chapter so dissolves stay inside one.
        jobs, groups, parts, lengths, fallbacks = [], [], [], [], 0
        prev_joins = False
        for i, shot in enumerate(cut):
            beat, pick = shot["beat"], shot["pick"]
            nxt = cut[i + 1] if i + 1 < len(cut) else None
            joins = bool(fade) and nxt is not None and nxt["chapter"] == shot["chapter"] \
                and shot["seconds"] > 1.0 and nxt["seconds"] > 1.0
            if not groups or not prev_joins:
                groups.append([])
            groups[-1].append(i)
            prev_joins = joins
            key = pick.get("file") or pick.get("url") or f"card-{beat['id']}"
            if key not in fetched:
                fetched[key] = render.fetch(pick, ep, tmp / f"asset{len(fetched):03}", session)
            asset, kind = fetched[key], ("video" if pick.get("kind") == "video" else "image")
            if asset is None:
                fallbacks += 1
                asset, kind = render.draw_card(visuals.make_card(beat), tmp / f"card{i:03}.jpg"), "image"
            jobs.append({"i": i, "asset": asset, "kind": kind, "beat": beat, "out": tmp / f"seg{i:03}.mp4",
                         "length": shot["seconds"] + (fade if joins else 0)})
            parts.append(tmp / f"seg{i:03}.mp4")
            lengths.append(shot["seconds"])

        done, lock = [0], threading.Lock()

        def make(job):
            fell = 0
            try:
                # Plain camera moves: depth parallax is lost under the dark overlay and costs time.
                render.make_segment(job["i"], job["asset"], job["kind"], job["length"], [], job["out"], parallax=False)
            except (render.RenderError, RuntimeError) as e:
                print(f"    shot {job['i']}: {str(e)[:120]}; using a card")
                card = render.draw_card(visuals.make_card(job["beat"]), job["out"].with_suffix(".fb.jpg"))
                render.make_segment(job["i"], card, "image", job["length"], [], job["out"], parallax=False)
                fell = 1
            with lock:
                done[0] += 1
                if progress:
                    progress(int(done[0] * 0.8), len(jobs))  # the last 20% is the overlay pass
            return fell

        with ThreadPoolExecutor(max_workers=max(1, min(4, os.cpu_count() or 2))) as pool:
            fallbacks += sum(pool.map(make, jobs))
        print(f"  shots encoded ({time.time() - started:.0f}s)")
        joined = []
        for gi, g in enumerate(groups):
            joined.append(render.crossfade([parts[i] for i in g], [lengths[i] for i in g], tmp / f"group{gi:03}.mp4", fade))
            for i in g:
                parts[i].unlink(missing_ok=True)
        (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in joined))

        # 2. Captions, timed to the voice.
        pages = caption_pages(script, timings, tmp / "narration.mp3")
        caps = tmp / "caps"
        caps.mkdir()
        from PIL import Image
        blank = caps / "blank.png"
        Image.new("RGBA", (W, int(250 * W / 1920)), (0, 0, 0, 0)).save(blank)
        # Each page from its first word; a blank when there's a real pause before the next page.
        timeline = [(blank, 0.0)]
        for n, p in enumerate(pages):
            timeline.append((draw_caption(p["words"], caps / f"p{n:04}.png", W), p["start"]))
            nxt = pages[n + 1]["start"] if n + 1 < len(pages) else timings["duration"]
            if nxt - p["end"] > 0.05:
                timeline.append((blank, p["end"]))
        timeline.sort(key=lambda x: x[1])
        lines = ["ffconcat version 1.0"]
        for k, (img, start) in enumerate(timeline):
            nxt = timeline[k + 1][1] if k + 1 < len(timeline) else timings["duration"]
            lines += [f"file '{img.name}'", f"duration {max(0.02, nxt - start):.3f}"]
        lines.append(f"file '{timeline[-1][0].name}'")
        (caps / "caps.ffconcat").write_text("\n".join(lines) + "\n")
        print(f"  {len(pages)} caption pages ({time.time() - started:.0f}s)")

        # 3. Narrator, avatar, subscribe button.
        body, avatar = draw_persona(face, tmp, W, H)
        sub = draw_subscribe(tmp, W, H)
        dur = timings["duration"]
        windows = [(4.0, 11.0)] + [(t, t + 6.0) for t in range(300, int(dur) - 20, 300)]
        sub_on = "+".join(f"between(t,{a:.1f},{b:.1f})" for a, b in windows)

        # 4. Sound.
        track = (meta.get("music") or {}).get("track") or sc["music"]
        bed = audio.bed_file(track, tmp)
        audio.mix(tmp / "narration.mp3", dur, tmp / "sound.m4a", bed)

        # 5. Everything together.
        cap_y = int(H * 0.70) - int(125 * W / 1920)
        # No film grain here: under a 40% overlay nobody sees it, and it doubles the file size.
        grain = f",noise=alls={sc['grain']}:allf=t" if sc.get("grain") else ""
        (body, bx, by), (avatar, ax, ay), (sub, ux, uy) = body, avatar, sub
        graph = (f"[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=black@{sc['overlay']}:t=fill[bg];"
                 f"[bg][1:v]overlay={bx}:{by}[p];[p][2:v]overlay={ax}:{ay}[a];"
                 f"[a][3:v]overlay={ux}:{uy}:enable='{sub_on}'[s];"
                 f"[4:v]format=rgba,setpts=PTS-STARTPTS[c];[s][c]overlay=0:{cap_y}:eof_action=repeat{grain}[v]")
        render._ff("-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                   "-loop", "1", "-i", str(body), "-loop", "1", "-i", str(avatar), "-loop", "1", "-i", str(sub),
                   "-f", "concat", "-safe", "0", "-i", str(caps / "caps.ffconcat"),
                   "-i", str(tmp / "sound.m4a"),
                   "-filter_complex", graph, "-map", "[v]", "-map", "5:a",
                   "-c:v", "libx264", "-preset", rc["preset"], "-crf", str(rc["crf"]),
                   "-maxrate", rc["max_bitrate"], "-bufsize", rc["max_bitrate"].replace("M", "") + "0M",
                   "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "copy",
                   "-t", f"{dur:.3f}", "-movflags", "+faststart", str(final), what="story final cut")
        if progress:
            progress(len(jobs), len(jobs))

        preview = tmp / "preview.mp4"
        render._ff("-i", str(final), "-t", str(rc["preview_seconds"]), "-vf", "scale=854:-2",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k",
                   "-movflags", "+faststart", str(preview), what="preview")
        ep.write("render/preview.mp4.b64", base64.b64encode(preview.read_bytes()).decode())

    captions = srt(pages)
    ep.write("render/captions.srt", captions)
    (render.OUTPUT / f"{slug}.srt").write_text(captions, encoding="utf-8")
    info = {
        "state": "done", "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds_taken": round(time.time() - started), "duration": timings["duration"],
        "size_mb": round(final.stat().st_size / 1e6, 1), "shots": len(cut), "fallbacks": fallbacks,
        "music": track, "layout": "story", "file": final.name,
        "run_id": os.getenv("GITHUB_RUN_ID"), "repo": os.getenv("GITHUB_REPOSITORY"),
    }
    meta = ep.read("meta.json")
    meta["render"] = {**(meta.get("render") or {}), **info}
    ep.write("meta.json", meta)
    ep.set_status("rendered")
    print(f"  ✓ {final.name}: {info['size_mb']} MB, {len(cut)} shots, took {info['seconds_taken']}s")
    return info

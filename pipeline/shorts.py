"""Stage 7: vertical Shorts.

The script already flags 30–55 second moments that stand on their own. Each Short is:

    hook line (new, written to stop a scroll)
    the flagged beats, word for word (already fact-checked, pictures already picked)
    one closing line pointing at the full video

You can edit every line in the dashboard before making them (shorts.json). The worker then:

  1. records the lines with Kokoro, a little quicker than the long video
  2. times every word with Whisper (tiny, CPU) listening to our own narration, so the caption
     highlight sits exactly on the word being said
  3. cuts 9:16: a new picture or a new move at least every 3 seconds, wide photos slide
     across the tall frame, cards redrawn for vertical
  4. burns in the captions: a short page of words, the spoken word on a marker-yellow chip,
     a soft dark halo behind the text so it reads on any picture; the story name at the top

Output: output/<slug>-<id>.mp4 (kept as a GitHub artifact) and a small preview per Short in the
database for the dashboard.
"""
from __future__ import annotations

import base64
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import load_config, user_agent
from .episode import Episode
from . import audio, render, visuals, voice

INK, MARKER, WHITE = render.INK, render.MARKER, (255, 255, 255)


class ShortsError(RuntimeError):
    pass


def cfg() -> dict:
    return load_config()["shorts"]


# ──────────────────────────── the plan (what each Short says) ────────────────────────────

def build(script: dict, topic: str) -> dict:
    """Default shorts.json from the script's flagged moments."""
    items = []
    for n, pick in enumerate(script.get("shorts") or [], 1):
        chapters = script["chapters"]
        if not 0 <= pick["chapter"] < len(chapters):
            continue
        beats = chapters[pick["chapter"]]["beats"][pick["beats"][0] - 1: pick["beats"][1]]
        if not beats:
            continue
        items.append({
            "id": f"s{n}", "include": True,
            "hook": pick["hook"],
            "lines": [{"beat": b["id"], "text": b["narration"]} for b in beats],
            "outro": cfg()["outro"],
            "why": pick.get("why", ""),
        })
    return {"label": short_label(topic), "items": items}


def short_label(topic: str) -> str:
    """The small title at the top of every Short: the subject, not a sentence."""
    t = re.sub(r"^(all the news about|the story of|how|why|what)\s+", "", topic.strip(), flags=re.I)
    t = re.sub(r"^(the|a|an)\s+", "", t, flags=re.I)
    return t[:40].strip(" .,:;-").title() if t.islower() else t[:40].strip(" .,:;-")


def load(ep: Episode) -> dict | None:
    saved = ep.read("shorts.json")
    if saved:
        return saved
    script = ep.read("script.json")
    if not script:
        return None
    return build(script, ep.read("meta.json")["topic"])


def units(item: dict) -> list[dict]:
    """Everything spoken in one Short, in order, with the beat whose picture goes under it."""
    lines = [ln for ln in item["lines"] if ln["text"].strip()]
    first = lines[0]["beat"] if lines else None
    out = [{"role": "hook", "text": item["hook"].strip(), "beat": first}] if item["hook"].strip() else []
    out += [{"role": "line", "text": ln["text"].strip(), "beat": ln["beat"]} for ln in lines]
    if item.get("outro", "").strip():
        out.append({"role": "outro", "text": item["outro"].strip(), "beat": None})
    return out


def estimate_seconds(item: dict) -> float:
    words = sum(len(u["text"].split()) for u in units(item))
    return round(words / (2.55 * cfg()["speed"]) + 0.3 * len(units(item)), 1)


def check(data: dict) -> list[str]:
    problems = []
    for it in data.get("items", []):
        if not it.get("include", True):
            continue
        if not any(ln["text"].strip() for ln in it.get("lines", [])):
            problems.append(f"{it['id']}: no lines left to say.")
        if estimate_seconds(it) > cfg()["max_seconds"]:
            problems.append(f"{it['id']}: about {estimate_seconds(it):.0f}s, over {cfg()['max_seconds']}s. Trim a line.")
    return problems


# ──────────────────────────── word timings ────────────────────────────

def _norm(w: str) -> str:
    w = w.lower().replace("%", " percent").replace("$", "")
    return re.sub(r"[^a-z0-9]", "", w)


def align(words: list[str], heard: list[tuple[str, float, float]], start: float, end: float) -> list[tuple[float, float]]:
    """Give every caption word a (start, end): take Whisper's time where the word matches what
    it heard, spread the rest evenly between their matched neighbours."""
    import difflib
    n = len(words)
    times: list[tuple[float, float] | None] = [None] * n
    if heard:
        a = [_norm(w) for w in words]
        b = [_norm(w) for w, _, _ in heard]
        for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
            for k in range(block.size):
                _, s, e = heard[block.b + k]
                times[block.a + k] = (s, e)
    # Fill the gaps proportionally to word length between known anchors.
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else start
        right = times[j][0] if j < n else end
        right = max(right, left + 0.05 * (j - i))
        weights = [max(1, len(words[k])) for k in range(i, j)]
        total, cursor = sum(weights), left
        for k, wgt in zip(range(i, j), weights):
            span = (right - left) * wgt / total
            times[k] = (cursor, cursor + span)
            cursor += span
        i = j
    # Monotonic and inside the line.
    out, last = [], start
    for s, e in times:
        s = min(max(s, last), end)
        e = min(max(e, s + 0.04), end)
        out.append((round(s, 3), round(e, 3)))
        last = s
    return out


class Listener:
    """Whisper on CPU. None of this runs in tests or demo mode (DOCU_MOCK_TTS)."""

    def __init__(self):
        self.model = None
        if os.getenv("DOCU_MOCK_TTS") == "1":
            return
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(cfg()["whisper_model"], device="cpu", compute_type="int8")
        except Exception as e:  # captions still work, just timed by word length
            print(f"  ! Whisper unavailable ({e}); timing words by length instead")

    def words(self, audio, sample_rate: int) -> list[tuple[str, float, float]]:
        if self.model is None or not len(audio):
            return []
        import numpy as np
        # Whisper wants 16 kHz mono float32.
        n = int(len(audio) * 16000 / sample_rate)
        pcm = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        segments, _ = self.model.transcribe(pcm, language="en", word_timestamps=True, beam_size=1,
                                            vad_filter=False, condition_on_previous_text=False)
        return [(w.word.strip(), float(w.start), float(w.end)) for seg in segments for w in (seg.words or [])]


# ──────────────────────────── drawing ────────────────────────────

def _band_size() -> tuple[int, int]:
    return cfg()["width"], 560


_WEAK = {"a", "an", "the", "of", "in", "on", "to", "and", "or", "but", "for", "with", "at", "by", "from",
         "its", "it's", "his", "her", "their", "this", "that", "is", "was", "as", "into", "than", "so"}


def pages(words: list[str], per: int | None = None) -> list[list[int]]:
    """Group word indexes into caption pages: up to N words, broken after punctuation when
    possible, never ending a page on "the", "of", "in"... (the reader's eye stalls there)."""
    per = per or cfg()["words_per_page"]
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        ends_sentence = w.endswith((".", "?", "!")) and not re.fullmatch(r"(?:[A-Z]\.)+", w)
        ends_clause = w.endswith((",", ":", ";"))
        if (ends_sentence and len(cur) >= 2) or (ends_clause and len(cur) >= 3):
            out.append(cur)
            cur = []
        elif len(cur) >= per:
            carry = []
            while len(cur) > 3 and re.sub(r"\W", "", words[cur[-1]].lower()) in _WEAK:
                carry.insert(0, cur.pop())
            out.append(cur)
            cur = carry
    if cur:
        if out and len(cur) <= 2 and len(out[-1]) + len(cur) <= per + 2:
            out[-1] += cur  # don't leave a lonely word on its own page
        else:
            out.append(cur)
    return out


def _layout(draw, words: list[str], font, max_w: int) -> list[list[tuple[int, str, float]]]:
    """Lines of (word index, word, width) that fit max_w."""
    space = draw.textlength(" ", font=font) * 1.6
    lines, line, width = [], [], 0.0
    for i, w in words:
        wl = draw.textlength(w, font=font)
        if line and width + space + wl > max_w:
            lines.append(line)
            line, width = [], 0.0
        line.append((i, w, wl))
        width += (space if len(line) > 1 else 0) + wl
    if line:
        lines.append(line)
    return lines


def draw_caption(words: list[str], page: list[int], current: int | None, path: Path, scale: float = 1.0) -> Path:
    """One caption state: the page's words, `current` on the yellow chip. The hook uses scale > 1."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = _band_size()
    size = int(cfg()["caption_size"] * scale)
    font = render._font(size, 820, 88)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = _layout(d, [(i, words[i]) for i in page], font, W - 150)
    while len(lines) > 3 and size > 44:  # very long words: shrink rather than spill
        size -= 6
        font = render._font(size, 820, 88)
        lines = _layout(d, [(i, words[i]) for i in page], font, W - 150)
    space = d.textlength(" ", font=font) * 1.6
    lh = int(size * 1.22)
    top = (H - lh * len(lines)) // 2

    # A soft dark halo behind the words so they read on white sails and blue sky alike.
    halo = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(halo)
    for li, line in enumerate(lines):
        lw = sum(w for _, _, w in line) + space * (len(line) - 1)
        x0 = (W - lw) / 2
        y0 = top + li * lh
        hd.rounded_rectangle([x0 - 22, y0 - 4, x0 + lw + 22, y0 + lh + 4], radius=24, fill=150)
    halo = halo.filter(ImageFilter.GaussianBlur(26))
    shade = Image.new("RGBA", (W, H), INK + (0,))
    shade.putalpha(halo)
    img = Image.alpha_composite(img, shade)
    d = ImageDraw.Draw(img)

    for li, line in enumerate(lines):
        lw = sum(w for _, _, w in line) + space * (len(line) - 1)
        x = (W - lw) / 2
        y = top + li * lh
        for i, w, wl in line:
            if i == current:
                pad_x, pad_y = 10, 4
                d.rounded_rectangle([x - pad_x, y + pad_y, x + wl + pad_x, y + lh - pad_y],
                                    radius=10, fill=MARKER + (255,))
                d.text((x, y + (lh - size) / 2 - size * 0.12), w, font=font, fill=INK + (255,))
            else:
                d.text((x, y + (lh - size) / 2 - size * 0.12), w, font=font, fill=WHITE + (255,),
                       stroke_width=3, stroke_fill=INK + (200,))
            x += wl + space
    img.save(path)
    return path


def draw_title(label: str, path: Path) -> Path:
    """The story's name at the top, with a short marker underline."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = cfg()["width"], 260
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font, lines = render._fit(d, label, W - 200, 1, 54, 760, 85)
    text = lines[0] if lines else label
    tw = d.textlength(text, font=font)
    x, y = (W - tw) / 2, 150
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).rounded_rectangle([x - 30, y - 10, x + tw + 30, y + font.size + 30], radius=30, fill=120)
    shade = Image.new("RGBA", (W, H), INK + (0,))
    shade.putalpha(glow.filter(ImageFilter.GaussianBlur(22)))
    img = Image.alpha_composite(img, shade)
    d = ImageDraw.Draw(img)
    d.text((x, y), text, font=font, fill=WHITE + (255,))
    d.rectangle([W / 2 - 34, y + font.size + 16, W / 2 + 34, y + font.size + 21], fill=MARKER + (255,))
    img.save(path)
    return path


def draw_card(card: dict, path: Path, end_title: str | None = None) -> Path:
    """Cards redrawn for the tall frame. Text sits in the upper half; captions own the lower half."""
    from PIL import Image, ImageDraw
    W, H = cfg()["width"], cfg()["height"]
    img = Image.new("RGB", (W, H), render.INK)
    d = ImageDraw.Draw(img)
    left, maxw = 96, W - 192
    if end_title is not None:
        label_font = render._font(40, 700, 100)
        d.text(((W - d.textlength("FULL STORY", font=label_font)) / 2, 470), "FULL STORY", font=label_font, fill=MARKER)
        font, lines = render._fit(d, end_title, maxw, 4, 104)
        y = 540
        for line in lines:
            d.text(((W - d.textlength(line, font=font)) / 2, y), line, font=font, fill=(243, 245, 246))
            y += font.size * 1.1
    elif card.get("card") == "stat":
        big = render._font(220, 850, 62)
        d.text(((W - d.textlength(card.get("big", ""), font=big)) / 2, 420), card.get("big", ""), font=big, fill=MARKER)
        font, lines = render._fit(d, card.get("text", ""), maxw, 3, 60, 600, 90)
        y = 690
        for line in lines:
            d.text(((W - d.textlength(line, font=font)) / 2, y), line, font=font, fill=render.SOFT)
            y += font.size * 1.25
    else:
        if card.get("card") == "place":
            for x in range(0, W, 64):
                d.line([(x, 0), (x, H)], fill=(24, 36, 46), width=1)
            for y0 in range(0, H, 64):
                d.line([(0, y0), (W, y0)], fill=(24, 36, 46), width=1)
        if card.get("card") == "document":
            d.rectangle([60, 400, 72, 1000], fill=MARKER)
            d.text((left, 400), "DOCUMENT", font=render._font(34, 700, 100), fill=MARKER)
        font, lines = render._fit(d, card.get("text", ""), maxw, 4, 120)
        y = 460
        for line in lines:
            d.text(((W - d.textlength(line, font=font)) / 2, y), line, font=font, fill=(243, 245, 246))
            y += font.size * 1.08
    img.save(path, quality=95)
    return path


# ──────────────────────────── shots ────────────────────────────

def _usable(beat: dict) -> list[dict]:
    """A beat's real pictures, chosen one first; cards only if there's nothing else."""
    cands = beat.get("candidates") or []
    if not cands:
        return []
    chosen = cands[beat.get("chosen", 0)] if beat.get("chosen", 0) < len(cands) else cands[0]
    # Alternates are real pictures only: an AI image goes into a Short only if you picked it.
    real = [c for c in cands if c["source"] not in ("card", "ai") and not c.get("needs_screenshot")]
    order = [chosen] + [c for c in real if c is not chosen]
    return order


def plan_shots(spoken: list[dict], beats: dict) -> list[dict]:
    """Split each spoken unit into shots no longer than max_shot_seconds, rotating through
    the beat's pictures so a 7-second line shows two or three images instead of one."""
    max_s = cfg()["max_shot_seconds"]
    used: dict[str, int] = {}
    shots = []
    for u in spoken:
        n = max(1, math.ceil((u["shot_end"] - u["shot_start"]) / max_s - 0.15))
        step = (u["shot_end"] - u["shot_start"]) / n
        for k in range(n):
            pick = None
            if u["beat"] and u["beat"] in beats:
                options = _usable(beats[u["beat"]])
                if options:
                    idx = used.get(u["beat"], 0)
                    pick = options[idx % len(options)]
                    used[u["beat"]] = idx + 1
            shots.append({"start": u["shot_start"] + k * step, "end": u["shot_start"] + (k + 1) * step,
                          "pick": pick, "beat": u["beat"], "role": u["role"]})
    return shots


def _grade() -> str:
    # The long video's grade minus the film grain: grain looks nice at 16:9 but triples the
    # file size, and phone screens hide it anyway.
    return "eq=contrast=1.05:saturation=0.92:gamma=0.98,vignette=angle=PI/6"


def still_filter(index: int, frames: int, size: tuple[int, int], landscape: bool) -> str:
    """Wide photos slide across the tall frame; tall ones (and cards) get a slow push."""
    c = cfg()
    W, H, fps = c["width"], c["height"], c["fps"]
    if landscape:
        # Cover the height with a little room, then travel a limited distance so it never whooshes.
        sh = int(H * 1.06)
        sw = int(sh * size[0] / size[1])
        spare = max(0, sw - W)
        travel = min(spare, int(c["pan_speed"] * frames / fps))
        x0 = (spare - travel) // 2
        direction = 1 if index % 2 == 0 else -1
        start = x0 if direction == 1 else x0 + travel
        return (f"scale={sw}:{sh},setsar=1,"
                f"crop={W}:{H}:x='{start}+{direction}*{travel}*min(1,n/{max(1, frames - 1)})':y='{(sh - H) // 2}',"
                f"fps={fps}")
    z0, z1 = (1.0, 1.10) if index % 2 == 0 else (1.10, 1.0)
    p = f"(on/{max(1, frames - 1)})"
    return (f"scale={int(W * 1.5)}:{int(H * 1.5)}:force_original_aspect_ratio=increase,"
            f"crop={int(W * 1.5)}:{int(H * 1.5)},setsar=1,"
            f"zoompan=z='{z0}+({z1}-{z0})*{p}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.4':d={frames}:s={W}x{H}:fps={fps}")


def punch_filter(frames: int) -> str:
    """The hook's first shot: starts 20% closer and settles back in 0.6 s, then drifts in."""
    c = cfg()
    W, H, fps = c["width"], c["height"], c["fps"]
    k = max(2, int(0.6 * fps))
    z = f"if(lt(on,{k}),1.2-0.15*(1-pow(1-on/{k},3)),1.05+0.04*(on-{k})/{max(1, frames - k)})"
    return (f"scale={int(W * 1.5)}:{int(H * 1.5)}:force_original_aspect_ratio=increase,"
            f"crop={int(W * 1.5)}:{int(H * 1.5)},setsar=1,"
            f"zoompan=z='{z}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.45':d={frames}:s={W}x{H}:fps={fps}")


def make_shot(index: int, asset: Path, kind: str, frames: int, out: Path, punch: bool = False) -> Path:
    c = cfg()
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
           "-r", str(c["fps"]), "-an", "-frames:v", str(frames)]
    if kind == "video":
        vf = (f"scale={c['width']}:{c['height']}:force_original_aspect_ratio=increase,"
              f"crop={c['width']}:{c['height']},fps={c['fps']},setsar=1,{_grade()}")
        render._ff("-stream_loop", "-1", "-i", str(asset), "-vf", vf, *enc, str(out), what=f"short shot {index}")
        return out
    from PIL import Image
    with Image.open(asset) as im:
        size = im.size
    landscape = size[0] / size[1] > c["width"] / c["height"] * 1.25
    move = punch_filter(frames) if punch else still_filter(index, frames, size, landscape)
    vf = f"{move},{_grade()}"
    render._ff("-loop", "1", "-i", str(asset), "-vf", vf, *enc, str(out), what=f"short shot {index}")
    return out


# ──────────────────────────── one Short ────────────────────────────

def make_one(ep: Episode, item: dict, label: str, end_title: str, beats: dict, narrator, listener,
             tmp: Path, session: requests.Session) -> dict:
    import numpy as np
    c = cfg()
    sr = voice.SAMPLE_RATE
    spoken = units(item)
    if not spoken:
        raise ShortsError(f"{item['id']} has nothing to say.")

    # 1. Voice, line by line, with word timings.
    lead, gap, tail = 0.25, c["pause"], 0.7
    parts = [np.zeros(int(lead * sr), dtype=np.float32)]
    cursor = lead
    for u in spoken:
        clip = narrator.speak(u["text"], speed=c["speed"])
        length = len(clip) / sr
        u["start"], u["end"] = cursor, cursor + length
        heard = [(w, cursor + s, cursor + e) for w, s, e in listener.words(clip, sr)]
        u["words"] = u["text"].split()
        u["times"] = align(u["words"], heard, u["start"], u["end"])
        parts += [clip, np.zeros(int(gap * sr), dtype=np.float32)]
        cursor += length + gap
    parts.append(np.zeros(int(tail * sr), dtype=np.float32))
    track = np.concatenate(parts)
    duration = len(track) / sr
    (tmp / "voice.mp3").write_bytes(voice.master_to_mp3(track))
    music = audio.choice(ep.read("meta.json") or {})
    bed = audio.bed_file(music, tmp)
    # A whoosh as the hook hands over to the story; music a touch louder than in the long video.
    hand_over = [max(0.0, spoken[1]["start"] - 0.3)] if len(spoken) > 1 else []
    audio.mix(tmp / "voice.mp3", duration, tmp / "sound.m4a", bed, whooshes=hand_over,
              music_db=audio.cfg()["music_db"] + 2)

    # Pictures change on the first word of each line (the hook starts at 0).
    for i, u in enumerate(spoken):
        u["shot_start"] = 0.0 if i == 0 else u["start"]
        u["shot_end"] = spoken[i + 1]["start"] if i + 1 < len(spoken) else duration

    # 2. Shots.
    shots = plan_shots(spoken, beats)
    fps = c["fps"]
    parts_v, fallbacks = [], 0
    for i, shot in enumerate(shots):
        f0, f1 = round(shot["start"] * fps), round(shot["end"] * fps)
        frames = max(1, f1 - f0)
        asset, kind = None, "image"
        if shot["role"] == "outro":
            asset = draw_card({}, tmp / f"end{i:02}.jpg", end_title=end_title)
        elif shot["pick"] and shot["pick"]["source"] != "card":
            asset = render.fetch(shot["pick"], ep, tmp / f"a{i:02}", session)
            kind = "video" if shot["pick"].get("kind") == "video" else "image"
        if asset is None:
            card = (shot["pick"] if shot["pick"] and shot["pick"]["source"] == "card"
                    else visuals.make_card(_beat_stub(shot["beat"], beats)))
            asset, kind = draw_card(card, tmp / f"card{i:02}.jpg"), "image"
            fallbacks += shot["pick"] is not None and shot["pick"]["source"] != "card"
        out = tmp / f"shot{i:02}.mp4"
        try:
            make_shot(i, asset, kind, frames, out, punch=(i == 0 and kind == "image"))
        except render.RenderError:
            fallbacks += 1
            make_shot(i, draw_card(visuals.make_card(_beat_stub(shot["beat"], beats)), tmp / f"fb{i:02}.jpg"),
                      "image", frames, out)
        parts_v.append(out)
        (tmp / f"a{i:02}.mp4").unlink(missing_ok=True)

    (tmp / "shots.txt").write_text("".join(f"file '{p.name}'\n" for p in parts_v))
    render._ff("-f", "concat", "-safe", "0", "-i", str(tmp / "shots.txt"), "-c", "copy", str(tmp / "picture.mp4"),
               what="joining shots")

    # 3. Captions: one image per spoken word, shown from that word until the next one.
    caps = tmp / "caps"
    caps.mkdir(exist_ok=True)
    blank = caps / "blank.png"
    from PIL import Image
    Image.new("RGBA", _band_size(), (0, 0, 0, 0)).save(blank)
    timeline: list[tuple[Path, float]] = []  # (image, start time)
    timeline.append((blank, 0.0))
    for ui, u in enumerate(spoken):
        hook = u["role"] == "hook"  # the hook lands 2-3 big words at a time
        for pi, page in enumerate(pages(u["words"], per=3 if hook else None)):
            for wi in page:
                img = draw_caption(u["words"], page, wi, caps / f"u{ui:02}p{pi:02}w{wi:03}.png",
                                   scale=1.4 if hook else 1.0)
                timeline.append((img, u["times"][wi][0]))
    timeline.append((blank, duration - 0.05))
    lines = ["ffconcat version 1.0"]
    for k, (img, start) in enumerate(timeline):
        nxt = timeline[k + 1][1] if k + 1 < len(timeline) else duration
        lines += [f"file '{img.name}'", f"duration {max(0.02, nxt - start):.3f}"]
    lines.append(f"file '{timeline[-1][0].name}'")
    (caps / "caps.ffconcat").write_text("\n".join(lines) + "\n")
    title = draw_title(label, tmp / "title.png")

    # 4. Everything together.
    final = render.OUTPUT / f"{ep.slug}-{item['id']}.mp4"
    cap_y = int(c["height"] * c["caption_y"] - _band_size()[1] / 2)
    graph = (f"[0:v][1:v]overlay=0:0[t];"
             f"[2:v]format=rgba,setpts=PTS-STARTPTS[c];[t][c]overlay=0:{cap_y}:eof_action=repeat[v]")
    render._ff("-i", str(tmp / "picture.mp4"), "-loop", "1", "-i", str(title),
               "-f", "concat", "-safe", "0", "-i", str(caps / "caps.ffconcat"),
               "-i", str(tmp / "sound.m4a"),
               "-filter_complex", graph, "-map", "[v]", "-map", "3:a",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", str(c["crf"]), "-pix_fmt", "yuv420p",
               "-maxrate", "8M", "-bufsize", "16M", "-r", str(fps), "-c:a", "copy",
               "-t", f"{duration:.3f}", "-movflags", "+faststart", str(final), what=f"{item['id']} final cut")

    preview = tmp / f"{item['id']}-preview.mp4"
    render._ff("-i", str(final), "-vf", f"scale=-2:{c['preview_height']}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "30", "-c:a", "aac", "-b:a", "80k", "-movflags", "+faststart", str(preview),
               what=f"{item['id']} preview")
    ep.write(f"shorts/{item['id']}.preview.mp4.b64", base64.b64encode(preview.read_bytes()).decode())
    return {"duration": round(duration, 1), "shots": len(shots), "fallbacks": fallbacks,
            "size_mb": round(final.stat().st_size / 1e6, 1), "file": final.name,
            "words": sum(len(u["words"]) for u in spoken),
            "timed_by": "whisper" if listener.model is not None else "word length",
            "music": music,
            "run_id": os.getenv("GITHUB_RUN_ID"), "repo": os.getenv("GITHUB_REPOSITORY"),
            "made_at": datetime.now(timezone.utc).isoformat()}


def _beat_stub(beat_id: str | None, beats: dict) -> dict:
    """make_card wants a script beat; the visuals manifest has what it needs."""
    b = beats.get(beat_id or "", {})
    return {"id": beat_id or "end", "narration": "", "on_screen_text": None,
            "visual": {"type": b.get("type", "title_card"), "query": b.get("query", "")}}


# ──────────────────────────── run (worker) ────────────────────────────

def run(slug: str, ids: list[str] | None = None, progress=None) -> dict:
    if not shutil.which("ffmpeg"):
        raise ShortsError("ffmpeg isn't installed.")
    ep = Episode.load(slug)
    data = load(ep)
    manifest = ep.read("visuals.json")
    script = ep.read("script.json") or {}
    if not data or not data["items"]:
        raise ShortsError("This story has no Shorts flagged.")
    if not manifest:
        raise ShortsError("Pick the pictures before making Shorts.")
    items = [it for it in data["items"] if it.get("include", True) and (not ids or it["id"] in ids)]
    if not items:
        raise ShortsError("No Shorts selected.")
    beats = {b["id"]: b for b in manifest["beats"]}
    meta = ep.read("meta.json")
    narrator = voice.Narrator((meta.get("voice") or {}).get("voice") or voice.cfg()["default"])
    listener = Listener()
    end_title = script.get("chosen_title") or (script.get("title_options") or [meta["topic"]])[0]

    started = time.time()
    print(f"[shorts] {meta['topic']}: {len(items)} Shorts")
    render.OUTPUT.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    made, failed = {}, {}
    for k, item in enumerate(items, 1):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                made[item["id"]] = make_one(ep, item, data.get("label") or meta["topic"], end_title, beats,
                                            narrator, listener, Path(tmp), session)
                print(f"  ✓ {item['id']}: {made[item['id']]['duration']}s, {made[item['id']]['shots']} shots")
            except Exception as e:  # one broken Short shouldn't sink the others
                failed[item["id"]] = str(e)[:300]
                print(f"  ✗ {item['id']}: {e}")
        if progress:
            progress(k, len(items))
    if not made:
        raise ShortsError("Every Short failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items()))

    meta = ep.read("meta.json")
    prev = (meta.get("shorts") or {}).get("made") or {}
    meta["shorts"] = {**(meta.get("shorts") or {}), "state": "done",
                      "finished_at": datetime.now(timezone.utc).isoformat(),
                      "seconds_taken": round(time.time() - started),
                      "made": {**prev, **made}, "failed": failed,
                      "run_id": os.getenv("GITHUB_RUN_ID"), "repo": os.getenv("GITHUB_REPOSITORY")}
    ep.write("meta.json", meta)
    print(f"  ✓ {len(made)} Shorts in {time.time() - started:.0f}s" + (f", {len(failed)} failed" if failed else ""))
    return meta["shorts"]


def load_preview(ep: Episode, short_id: str) -> bytes | None:
    data = ep.read(f"shorts/{short_id}.preview.mp4.b64")
    return base64.b64decode(data) if data else None

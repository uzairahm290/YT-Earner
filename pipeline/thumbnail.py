"""Thumbnails: three options from the story's strongest real pictures and the script's thumbnail text.

Click-through decides whether a video is seen at all, so these follow what works on documentary
channels: one strong image, 2–4 huge words, one of them on a marker-yellow chip, high contrast that
still reads at phone size. Three layouts, each on a different picture:

    left   text stacked on the left over a dark fade, picture fills the frame
    bottom picture fills the frame, text on an ink band along the bottom
    split  ink panel on the left with the text, picture on the right two-thirds

Made on the dashboard (Pillow), stored with the story, sent to YouTube with the upload.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import user_agent
from .episode import Episode
from . import render, visuals

W, H = 1280, 720
LAYOUTS = ("left", "bottom", "split")
# Beat types most likely to carry a face, a product or a place people recognise.
TYPE_RANK = {"archive_photo": 0, "broll": 1, "map": 3, "document": 4, "chart": 5, "title_card": 6}


class ThumbnailError(RuntimeError):
    pass


def pictures(ep: Episode, limit: int = 3) -> list[dict]:
    """The story's best real pictures: picked ones first, people/products before B-roll,
    earlier beats before later (the opening usually holds the story's face)."""
    m = ep.read("visuals.json") or {}
    scored = []
    for order, b in enumerate(m.get("beats", [])):
        if not b.get("candidates"):
            continue
        c = b["candidates"][b["chosen"]]
        if c["source"] == "card" or c.get("needs_screenshot"):
            continue
        if c.get("kind") == "video" and not c.get("thumb"):
            continue
        scored.append((TYPE_RANK.get(b["type"], 2), order, b["id"], c))
    scored.sort(key=lambda x: (x[0], x[1]))
    seen, out = set(), []
    for _, _, bid, c in scored:
        key = c.get("file") or c.get("url") or c.get("thumb")
        if key in seen:
            continue
        seen.add(key)
        out.append({"beat": bid, "candidate": c})
        if len(out) == limit:
            break
    return out


def _load(ep: Episode, c: dict, session: requests.Session):
    from PIL import Image
    if c["source"] in ("upload", "ai"):
        found = visuals.load_upload(ep, c["file"])
        if not found:
            return None
        return Image.open(io.BytesIO(found[1])).convert("RGB")
    url = c.get("thumb") if c.get("kind") == "video" else c.get("url")
    if not url:
        return None
    if url.startswith("/static/"):
        return Image.open(render.ROOT / "web" / url.removeprefix("/static/")).convert("RGB")
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except (requests.RequestException, OSError):
        return None


def _cover(img, w: int, h: int, focus_y: float = 0.4):
    from PIL import Image
    scale = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left = (img.width - w) // 2
    top = int((img.height - h) * focus_y)
    return img.crop((left, top, left + w, top + h))


def _pop(img):
    """Thumbnail grade: more contrast and colour than the video itself; phones shrink everything."""
    from PIL import ImageEnhance
    img = ImageEnhance.Contrast(img).enhance(1.18)
    img = ImageEnhance.Color(img).enhance(1.15)
    return ImageEnhance.Sharpness(img).enhance(1.3)


def _words(text: str) -> tuple[list[str], int]:
    """Words in capitals, and which one goes on the yellow chip: a number if there is one,
    otherwise the last word (thumbnail text usually ends on its punch)."""
    words = text.upper().split()[:5]
    hi = next((i for i, w in enumerate(words) if any(ch.isdigit() for ch in w)), len(words) - 1)
    return words, hi


def _set_text(d, words: list[str], hi: int, x: int, y: int, max_w: int, max_h: int, align: str = "left"):
    """Stack the words in as few lines as fit, as big as fit, the chosen word on a chip."""
    from PIL import ImageDraw  # noqa: F401
    size = 190
    while size > 60:
        font = render._font(size, 900, 64)
        space = d.textlength(" ", font=font) * 0.9
        lines, line, width = [], [], 0.0
        for i, w in enumerate(words):
            wl = d.textlength(w, font=font)
            if line and width + space + wl > max_w:
                lines.append(line)
                line, width = [], 0.0
            line.append((i, w, wl))
            width += (space if len(line) > 1 else 0) + wl
        if line:
            lines.append(line)
        lh = size * 1.02
        if len(lines) <= 3 and lh * len(lines) <= max_h:
            break
        size -= 8
    top = y + (max_h - lh * len(lines)) / 2
    for li, line in enumerate(lines):
        lw = sum(w for _, _, w in line) + space * (len(line) - 1)
        cx = x if align == "left" else x + (max_w - lw) / 2
        cy = top + li * lh
        for i, w, wl in line:
            if i == hi:
                d.rectangle([cx - 14, cy + size * 0.08, cx + wl + 14, cy + size * 1.02], fill=render.MARKER)
                d.text((cx, cy), w, font=font, fill=render.INK)
            else:
                d.text((cx, cy), w, font=font, fill=(255, 255, 255), stroke_width=6, stroke_fill=render.INK)
            cx += wl + space


def draw(photo, text: str, layout: str):
    from PIL import Image, ImageDraw
    words, hi = _words(text)
    if layout == "split":
        canvas = Image.new("RGB", (W, H), render.INK)
        canvas.paste(_pop(_cover(photo, 840, H)), (W - 840, 0))
        fade = Image.linear_gradient("L").rotate(-90).resize((160, H))  # ink on the left edge, photo by 160 px in
        canvas.paste(Image.new("RGB", (160, H), render.INK), (W - 840, 0), fade)
        d = ImageDraw.Draw(canvas)
        _set_text(d, words, hi, 56, 60, 560, H - 120)
        d.rectangle([56, H - 70, 136, H - 62], fill=render.MARKER)
        return canvas
    canvas = _pop(_cover(photo, W, H))
    shade = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shade)
    if layout == "left":
        for x in range(W):  # dark on the left, clear by two-thirds across
            sd.line([(x, 0), (x, H)], fill=int(215 * max(0.0, 1 - x / (W * 0.68)) ** 1.3))
    else:
        for y in range(H):
            sd.line([(0, y), (W, y)], fill=int(225 * max(0.0, (y - H * 0.35) / (H * 0.55)) ** 1.1))
    canvas = Image.composite(Image.new("RGB", (W, H), render.INK), canvas, shade)
    d = ImageDraw.Draw(canvas)
    if layout == "left":
        _set_text(d, words, hi, 56, 50, 700, H - 100)
    else:
        _set_text(d, words, hi, 60, int(H * 0.46), W - 120, int(H * 0.50), align="left")
    return canvas


def make(slug: str, text: str | None = None) -> dict:
    """Three options. Returns the thumbnail record stored in meta.json."""
    ep = Episode.load(slug)
    script = ep.read("script.json") or {}
    text = (text or script.get("chosen_thumbnail") or (script.get("thumbnail_text_options") or [""])[0]).strip()
    if not text:
        raise ThumbnailError("Write the thumbnail text first (Title and thumbnail tab).")
    pics = pictures(ep)
    if not pics:
        raise ThumbnailError("No real pictures picked yet. Find and pick pictures first.")
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    loaded = [(p, _load(ep, p["candidate"], session)) for p in pics]
    loaded = [(p, img) for p, img in loaded if img is not None]
    if not loaded:
        raise ThumbnailError("Couldn't download any of the pictures. Try again in a minute.")
    options = []
    for i, layout in enumerate(LAYOUTS):
        p, img = loaded[i % len(loaded)]
        out = draw(img, text, layout)
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=90, optimize=True)
        oid = f"t{i + 1}"
        ep.write(f"thumbnails/{oid}.jpg.b64", base64.b64encode(buf.getvalue()).decode())
        options.append({"id": oid, "layout": layout, "beat": p["beat"], "kb": len(buf.getvalue()) // 1024})
    meta = ep.read("meta.json")
    prev = meta.get("thumbnail") or {}
    meta["thumbnail"] = {"text": text, "options": options,
                         "chosen": prev.get("chosen") if prev.get("chosen") in {o["id"] for o in options} else "t1",
                         "made_at": datetime.now(timezone.utc).isoformat()}
    ep.write("meta.json", meta)
    return meta["thumbnail"]


def load(ep: Episode, oid: str) -> bytes | None:
    data = ep.read(f"thumbnails/{oid}.jpg.b64")
    return base64.b64decode(data) if data else None


def chosen_bytes(ep: Episode) -> bytes | None:
    t = (ep.read("meta.json") or {}).get("thumbnail") or {}
    return load(ep, t["chosen"]) if t.get("chosen") else None

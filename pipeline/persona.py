"""The narrator shown on screen in story videos: one photo, cut out, reused in every video.

Accepted uploads:
  - a PNG that's already transparent (remove.bg, Photoshop...): used as is
  - an image with a fake "transparent" checkerboard baked into the pixels (common when an image is
    saved from a stock or AI site): the checker is detected and removed. It matches the exact
    repeating grid of two greys, so a white shirt or grey suit inside the outline is kept.
  - an ordinary photo: refused with a pointer to a free background remover, because a bad
    automatic cutout looks worse on screen than no narrator at all

Stored desk-wide in the database (_settings/persona/...), with the original kept alongside.
"""
from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timezone

from .store import get_store

SETTINGS = "_settings"


class PersonaError(ValueError):
    pass


def info() -> dict | None:
    raw = get_store().read(SETTINGS, "persona/meta.json")
    return json.loads(raw) if raw else None


def cutout_png() -> bytes | None:
    data = get_store().read(SETTINGS, "persona/cutout.png.b64")
    return base64.b64decode(data) if data else None


def remove() -> None:
    store = get_store()
    for name in ("persona/meta.json", "persona/cutout.png.b64", "persona/original.b64"):
        store.write(SETTINGS, name, "")


# ──────────────────────────── cutting out ────────────────────────────

def _real_alpha(im) -> bool:
    if im.mode not in ("RGBA", "LA") and not (im.mode == "P" and "transparency" in im.info):
        return False
    import numpy as np
    a = np.asarray(im.convert("RGBA"))[..., 3]
    return (a < 250).mean() > 0.05  # at least some of the picture is actually see-through


def _find_checker(rgb):
    """(square size, x offset, y offset, dark colour, light colour) of a baked-in checkerboard
    along the image's top edge, or None."""
    import numpy as np
    row = rgb[2, :, :].astype(int)
    runs, start = [], 0
    for x in range(1, row.shape[0] + 1):
        if x == row.shape[0] or np.abs(row[x] - row[start]).max() > 6:
            runs.append((start, x - start, tuple(row[start])))
            start = x
    inner = [r for r in runs[1:-1] if r[1] > 4]
    if len(inner) < 4:
        return None
    sizes = [r[1] for r in inner]
    size = max(set(sizes), key=sizes.count)
    if sizes.count(size) < len(inner) * 0.7 or not 6 <= size <= 64:
        return None
    colours = sorted({r[2] for r in inner}, key=sum)
    dark, light = np.array(colours[0], float), np.array(colours[-1], float)
    if dark.min() < 180 or np.abs(light - dark).max() < 8:
        return None
    first_end = runs[0][1]
    offset = (size - first_end) % size
    first_dark = np.abs(np.array(runs[0][2], float) - dark).max() < 6
    return size, offset, first_dark, dark, light


def remove_checker(im):
    """RGBA cutout from an image with a checkerboard baked in."""
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy import ndimage
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    found = _find_checker(rgb)
    if not found:
        raise PersonaError("Couldn't find a clean background to remove.")
    size, off, first_dark, dark, light = found
    H, W, _ = rgb.shape
    ys, xs = np.mgrid[0:H, 0:W]
    # The top-left square's colour is known; the rest alternate from there.
    parity = ((xs + off) // size + (ys + off) // size) % 2
    is_dark = parity == 0 if first_dark else parity == 1
    expect = np.where(is_dark[..., None], dark, light)
    other = np.where(is_dark[..., None], light, dark)
    diff = np.abs(rgb - expect).max(-1)
    edge = (((xs + off) % size) <= 1) | (((xs + off) % size) >= size - 1) | \
           (((ys + off) % size) <= 1) | (((ys + off) % size) >= size - 1)
    match = (diff <= 6) | (edge & (np.abs(rgb - other).max(-1) <= 6))
    lab, n = ndimage.label(match)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    sizes = ndimage.sum(match, lab, range(1, n + 1))
    # Background: connected to the picture's edge, or an enclosed patch big enough to be a real gap
    # (between an arm and the body). Small enclosed matches are shading on a white shirt: kept.
    gap = max(2500, int(H * W * 0.003))
    keep = [i for i in range(1, n + 1) if i in border or sizes[i - 1] >= gap]
    fg = ~ndimage.binary_closing(np.isin(lab, keep), iterations=1)
    fl, fn = ndimage.label(fg)
    if not fn:
        raise PersonaError("Nothing left after removing the background.")
    fg = fl == (1 + int(np.argmax(ndimage.sum(fg, fl, range(1, fn + 1)))))  # the person, not stray bits
    fg = ndimage.binary_erosion(fg, iterations=1)  # no light fringe on dark footage
    band = fg & ~ndimage.binary_erosion(fg, iterations=2)
    alpha = fg.astype(np.float32)
    alpha[band] = np.clip((diff[band] - 6) / 45, 0, 1)
    alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7)),
                       dtype=np.float32) / 255
    alpha[ndimage.binary_erosion(fg, iterations=3)] = 1.0
    al = np.clip(alpha, 1e-3, 1)[..., None]
    out_rgb = np.where(alpha[..., None] > 0.98, rgb, np.clip((rgb - (1 - al) * expect) / al, 0, 255))
    out = Image.fromarray(np.dstack([out_rgb, alpha * 255]).astype(np.uint8), "RGBA")
    return out.crop(out.getbbox())


def cut(data: bytes):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise PersonaError("That isn't an image file.")
    if _real_alpha(im):
        out = im.convert("RGBA")
        return out.crop(out.getbbox()), "transparent"
    try:
        return remove_checker(im), "checkerboard removed"
    except PersonaError:
        raise PersonaError("This photo has a normal background. Remove it first with a free tool "
                           "(remove.bg or Photoshop Express), save as PNG, and upload that.")


def save(data: bytes, name: str, ai_generated: bool) -> dict:
    if len(data) > 15 * 1024 * 1024:
        raise PersonaError("That image is over 15 MB.")
    out, how = cut(data)
    from PIL import Image
    if out.height > 1600:  # plenty for a 1080p frame; keeps the database row small
        out = out.resize((round(out.width * 1600 / out.height), 1600), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    store = get_store()
    store.write(SETTINGS, "persona/original.b64", base64.b64encode(data).decode())
    store.write(SETTINGS, "persona/cutout.png.b64", base64.b64encode(buf.getvalue()).decode())
    meta = {"name": name.strip()[:40] or "Narrator", "ai_generated": bool(ai_generated), "how": how,
            "size": list(out.size), "updated_at": datetime.now(timezone.utc).isoformat()}
    store.write(SETTINGS, "persona/meta.json", json.dumps(meta))
    return meta

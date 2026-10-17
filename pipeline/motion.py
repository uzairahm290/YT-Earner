"""Moving pictures for the cut: 2.5D parallax on photos, and cards that animate in.

Parallax: a small depth model (Depth Anything V2 Small, Apache 2.0, CPU) guesses how far each
part of a photo is from the camera. As the camera drifts, near things slide a little more than far
things, so a flat photo gets depth, the way documentary editors do it by hand in After Effects.
Kept subtle on purpose (about 1.5% of the frame) so edges don't tear. If the model can't load, the
cut falls back to the plain Ken Burns move.

Cards: a big number counts up to its value and lands; a place name gets a map pin dropping onto it.
"""
from __future__ import annotations

import math
import os
import re
import threading
import subprocess
from pathlib import Path

from .config import load_config

_depth_pipe = None
_depth_failed = False
_depth_lock = threading.Lock()  # shots render in parallel; the model runs one image at a time


def cfg() -> dict:
    return load_config()["render"]


# ──────────────────────────── depth ────────────────────────────

def depth_map(img):
    """Depth for a PIL image, 0..1 with 1 = nearest, at the image's size. None when the model
    isn't available (no torch, offline): the caller falls back to the plain camera move."""
    global _depth_pipe, _depth_failed
    import numpy as np
    from PIL import Image, ImageFilter
    w, h = img.size
    if os.getenv("DOCU_MOCK_DEPTH") == "1":
        # Tests: near at the bottom, far at the top, like most landscape photos.
        return np.tile(np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None], (1, w))
    if _depth_failed or not cfg().get("parallax"):
        return None
    with _depth_lock:
        return _depth(img, w, h)


def _depth(img, w, h):
    global _depth_pipe, _depth_failed
    import numpy as np
    from PIL import Image, ImageFilter
    if _depth_failed:
        return None
    if _depth_pipe is None:
        try:
            from transformers import pipeline
            _depth_pipe = pipeline("depth-estimation", model=cfg()["depth_model"], device=-1)
        except Exception as e:
            print(f"  ! depth model unavailable ({e}); photos get the plain camera move")
            _depth_failed = True
            return None
    small = img.copy()
    small.thumbnail((640, 640))
    pred = _depth_pipe(small)["depth"]  # PIL, brighter = nearer
    d = pred.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(radius=max(2, w // 160)))
    arr = np.asarray(d, dtype=np.float32)
    lo, hi = np.percentile(arr, 2), np.percentile(arr, 98)
    return np.clip((arr - lo) / max(1e-6, hi - lo), 0, 1)


# Camera paths for parallax: (zoom start, zoom end, drift x, drift y), drift in fractions of width.
_PATHS = [(1.00, 1.07, 0.012, 0.0), (1.07, 1.00, -0.012, 0.0), (1.03, 1.06, 0.0, -0.010),
          (1.06, 1.02, 0.015, 0.004), (1.02, 1.07, -0.010, -0.006)]


def parallax_frames(image_path: Path, frames: int, index: int, size: tuple[int, int]):
    """Yield RGB frames (H×W×3 uint8) of a parallax camera move, or None if there's no depth."""
    import numpy as np
    from PIL import Image
    W, H = size
    # Work on a cover-cropped copy with a little margin so drifting never shows an edge.
    margin = 1.12
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        scale = max(W * margin / im.width, H * margin / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        left, top = (im.width - round(W * margin)) // 2, (im.height - round(H * margin)) // 2
        im = im.crop((left, top, left + round(W * margin), top + round(H * margin)))
        src = np.asarray(im)
        depth = depth_map(im)
    if depth is None:
        return None
    import cv2
    sh, sw = src.shape[:2]
    z0, z1, dx, dy = _PATHS[index % len(_PATHS)]
    amount = cfg().get("parallax_amount", 1.0)
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    # Output pixel -> normalised coords centred on the frame.
    nx, ny = (xs - W / 2) / W, (ys - H / 2) / H
    near = cv2.resize(depth, (W, H)) - 0.5  # -0.5 far … +0.5 near

    def gen():
        for f in range(frames):
            p = f / max(1, frames - 1)
            p = p * p * (3 - 2 * p)  # ease in and out
            zoom = z0 + (z1 - z0) * p
            ox = dx * (p - 0.5) * amount
            oy = dy * (p - 0.5) * amount
            # Near pixels move further than far ones: that difference is the depth.
            px = ox * (1 + 1.6 * near)
            py = oy * (1 + 1.6 * near)
            map_x = (nx / zoom - px) * sw / margin + sw / 2
            map_y = (ny / zoom - py) * sh / margin + sh / 2
            yield cv2.remap(src, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return gen()


# ──────────────────────────── animated cards ────────────────────────────

_NUM = re.compile(r"^(\D*?)(\d[\d,]*(?:\.\d+)?)(.*)$")


def count_up_values(big: str, frames: int) -> list[str] | None:
    """'60%' -> ['0%', ..., '60%'] with an ease-out; None if it isn't a plain number."""
    m = _NUM.match(big.strip())
    if not m:
        return None
    prefix, num, suffix = m.groups()
    if re.fullmatch(r"(1[89]|20)\d{2}", num) and not prefix and not suffix.strip():
        return None  # a bare year doesn't count up
    decimals = len(num.split(".")[1]) if "." in num else 0
    value = float(num.replace(",", ""))
    commas = "," in num
    out = []
    for f in range(frames):
        p = f / max(1, frames - 1)
        p = 1 - (1 - p) ** 3
        v = value * p
        s = f"{v:,.{decimals}f}" if commas else f"{v:.{decimals}f}"
        out.append(f"{prefix}{s}{suffix}")
    return out


def card_frames(card: dict, draw, folder: Path, fps: int) -> list[Path] | None:
    """PNG frames for the card's entrance (about 1.2 s), or None for a still card.
    `draw(card, path)` is render.draw_card; the last frame equals the still card."""
    n = int(1.2 * fps)
    paths = []
    if card.get("card") == "stat":
        values = count_up_values(card.get("big", ""), n)
        if not values:
            return None
        for i, v in enumerate(values):
            paths.append(draw({**card, "big": v}, folder / f"f{i:03}.jpg"))
        return paths
    if card.get("card") == "place":
        for i in range(n):
            p = i / max(1, n - 1)
            # drop, overshoot, settle
            if p < 0.55:
                drop = (p / 0.55) ** 2
            else:
                q = (p - 0.55) / 0.45
                drop = 1 + 0.08 * (1 - q) * math.sin(q * math.pi * 2)
            paths.append(draw({**card, "pin": drop}, folder / f"f{i:03}.jpg"))
        return paths
    return None


def encode_frames(frames, out: Path, seconds: float, fps: int, grade: str, size: tuple[int, int],
                  enc: list[str], overlays: list[tuple[Path, float, float]] = ()) -> Path:
    """Pipe raw frames (an iterator of H×W×3 arrays) into one graded segment."""
    W, H = size
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    graph = f"[0:v]{grade},setsar=1[v0]"
    last = "v0"
    for k, (png, start, hold) in enumerate(overlays, 1):
        cmd += ["-loop", "1", "-i", str(png)]
        graph += (f";[{k}:v]format=rgba,fade=in:st={start:.2f}:d=0.4:alpha=1,"
                  f"fade=out:st={start + hold:.2f}:d=0.4:alpha=1[o{k}];[{last}][o{k}]overlay=0:0:shortest=0[v{k}]")
        last = f"v{k}"
    cmd += ["-filter_complex", graph, "-map", f"[{last}]", *enc, str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed encoding frames: {err.strip()[-400:]}")
    return out

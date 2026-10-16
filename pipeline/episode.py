"""Every video lives in its own folder under episodes/<slug>/ so each stage can pick up where the last stopped."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .config import EPISODES_DIR


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60] or "untitled"


class Episode:
    def __init__(self, slug: str):
        self.slug = slug
        self.dir = EPISODES_DIR / slug

    @classmethod
    def create(cls, topic: str) -> "Episode":
        ep = cls(slugify(topic))
        ep.dir.mkdir(parents=True, exist_ok=True)
        meta = ep.read("meta.json") or {}
        meta.setdefault("topic", topic)
        meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        meta.setdefault("status", "new")
        ep.write("meta.json", meta)
        return ep

    @classmethod
    def load(cls, slug: str) -> "Episode":
        ep = cls(slug)
        if not ep.dir.exists():
            raise FileNotFoundError(f"No episode '{slug}' in {EPISODES_DIR}")
        return ep

    def path(self, name: str) -> Path:
        return self.dir / name

    def read(self, name: str):
        p = self.path(name)
        if not p.exists():
            return None
        if p.suffix == ".json":
            return json.loads(p.read_text(encoding="utf-8"))
        return p.read_text(encoding="utf-8")

    def write(self, name: str, data) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            p.write_text(data, encoding="utf-8")
        return p

    def set_status(self, status: str) -> None:
        meta = self.read("meta.json") or {}
        meta["status"] = status
        meta[f"{status}_at"] = datetime.now(timezone.utc).isoformat()
        self.write("meta.json", meta)

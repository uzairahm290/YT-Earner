"""Every video is an episode: a named set of files (meta.json, factsheet.json, script.json, ...)
kept in the store, so each stage can pick up where the last stopped."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone

from .store import get_store


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60] or "untitled"


def _decode(name: str, text: str | None):
    if text is None:
        return None
    return json.loads(text) if name.endswith(".json") else text


class Episode:
    def __init__(self, slug: str):
        self.slug = slug
        self.store = get_store()

    @classmethod
    def create(cls, topic: str) -> "Episode":
        ep = cls(slugify(topic))
        meta = ep.read("meta.json") or {}
        meta.setdefault("topic", topic)
        meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        meta.setdefault("status", "new")
        ep.write("meta.json", meta)
        return ep

    @classmethod
    def load(cls, slug: str) -> "Episode":
        ep = cls(slug)
        if not ep.store.exists(slug):
            raise FileNotFoundError(f"No episode '{slug}'")
        return ep

    @staticmethod
    def all_slugs() -> list[str]:
        return get_store().slugs()

    @staticmethod
    def read_many(names: list[str]) -> dict[str, dict]:
        """{slug: {name: parsed}} for every episode, in one go."""
        raw = get_store().read_many(names)
        return {slug: {n: _decode(n, t) for n, t in files.items()} for slug, files in raw.items()}

    def read(self, name: str):
        return _decode(name, self.store.read(self.slug, name))

    def write(self, name: str, data) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else data
        self.store.write(self.slug, name, text)

    def delete(self) -> None:
        self.store.delete(self.slug)

    def set_status(self, status: str) -> None:
        meta = self.read("meta.json") or {}
        meta["status"] = status
        meta[f"{status}_at"] = datetime.now(timezone.utc).isoformat()
        self.write("meta.json", meta)

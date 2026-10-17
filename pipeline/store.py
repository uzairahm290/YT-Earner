"""Where episode files live.

Locally: plain files under episodes/<slug>/ (easy to open and diff).
Deployed: Postgres (Supabase), because free hosts wipe their disk on every restart.

Set DATABASE_URL to switch to Postgres. Both stores hold the same thing: a set of
named text files per episode (meta.json, factsheet.json, script.json, ...).
"""
from __future__ import annotations

import os
import shutil
import threading
from functools import lru_cache
from pathlib import Path

from .config import EPISODES_DIR, env


class FileStore:
    kind = "files"

    def __init__(self, root: Path = EPISODES_DIR):
        self.root = root

    def read(self, slug: str, name: str) -> str | None:
        p = self.root / slug / name
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write(self, slug: str, name: str, text: str) -> None:
        p = self.root / slug / name
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a reader on another thread never sees a half-written file.
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    def exists(self, slug: str) -> bool:
        return (self.root / slug).is_dir()

    def slugs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("_"))

    def read_many(self, names: list[str]) -> dict[str, dict[str, str]]:
        out = {}
        for slug in self.slugs():
            out[slug] = {n: t for n in names if (t := self.read(slug, n)) is not None}
        return out

    def files(self, slug: str) -> dict[str, str]:
        base = self.root / slug
        return {str(p.relative_to(base)): p.read_text(encoding="utf-8")
                for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp")} if base.exists() else {}

    def delete(self, slug: str) -> None:
        shutil.rmtree(self.root / slug, ignore_errors=True)


class PgStore:
    kind = "postgres"

    def __init__(self, dsn: str):
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None: Supabase's poolers don't like server-side prepared statements.
        self.pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True, timeout=20,
                                   kwargs={"prepare_threshold": None, "autocommit": True})
        # Own schema, not `public`: Supabase exposes `public` through its REST API,
        # so a table there would be readable with the project's anon key.
        with self.pool.connection() as c:
            c.execute("create schema if not exists desk")
            c.execute("""
                create table if not exists desk.episode_files (
                    slug       text not null,
                    name       text not null,
                    content    text not null,
                    updated_at timestamptz not null default now(),
                    primary key (slug, name)
                )""")

    def read(self, slug: str, name: str) -> str | None:
        with self.pool.connection() as c:
            row = c.execute("select content from desk.episode_files where slug=%s and name=%s",
                            (slug, name)).fetchone()
        return row[0] if row else None

    def write(self, slug: str, name: str, text: str) -> None:
        with self.pool.connection() as c:
            c.execute("""insert into desk.episode_files (slug, name, content) values (%s, %s, %s)
                         on conflict (slug, name) do update set content=excluded.content, updated_at=now()""",
                      (slug, name, text))

    def exists(self, slug: str) -> bool:
        with self.pool.connection() as c:
            return c.execute("select 1 from desk.episode_files where slug=%s limit 1", (slug,)).fetchone() is not None

    def slugs(self) -> list[str]:
        with self.pool.connection() as c:
            rows = c.execute("select distinct slug from desk.episode_files where slug not like '\\_%%' order by slug").fetchall()
        return [r[0] for r in rows]

    def read_many(self, names: list[str]) -> dict[str, dict[str, str]]:
        # One round trip for the episode list instead of three per episode.
        with self.pool.connection() as c:
            rows = c.execute("select slug, name, content from desk.episode_files "
                             "where name = any(%s) and slug not like '\\_%%'", (names,)).fetchall()
        out: dict[str, dict[str, str]] = {}
        for slug, name, content in rows:
            out.setdefault(slug, {})[name] = content
        return out

    def files(self, slug: str) -> dict[str, str]:
        with self.pool.connection() as c:
            rows = c.execute("select name, content from desk.episode_files where slug=%s", (slug,)).fetchall()
        return dict(rows)

    def delete(self, slug: str) -> None:
        with self.pool.connection() as c:
            c.execute("delete from desk.episode_files where slug=%s", (slug,))


_store_lock = threading.Lock()


@lru_cache(maxsize=1)
def _build_store():
    dsn = env("DATABASE_URL")
    return PgStore(dsn) if dsn else FileStore()


def get_store():
    # The dashboard fires several requests at once on page load. Without the lock each one
    # can miss the cache at the same time and open its own connection pool.
    with _store_lock:
        return _build_store()


get_store.cache_clear = _build_store.cache_clear  # tests reset the store between runs

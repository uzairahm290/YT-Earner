"""The production worker: picks up jobs the dashboard queued and runs them.

The dashboard (on Render's tiny free instance) can't run a voice model, draw with SDXL or encode
video, so heavy
stages work like a mailbox. Clicking "Record narration" or "Render video" marks the episode as
queued in the database; this worker, running on a free GitHub Actions machine, finds queued
episodes and does the work.

    python cli.py worker            # process everything queued, then exit
    python cli.py worker --check    # exit 0 if there's work, 3 if not (lets Actions skip setup)
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta, timezone

from .episode import Episode

# Job kinds, in the order they run. Each lives in meta.json under its own key.
KINDS = ("voice", "illustrate", "render", "shorts")

# A job marked running longer than this was abandoned (runner died, timed out, cancelled).
STALE_AFTER = {"voice": timedelta(minutes=45), "illustrate": timedelta(minutes=75),
               "render": timedelta(minutes=130), "shorts": timedelta(minutes=90)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def pending(kind: str = "voice") -> list[str]:
    """Slugs with a queued job of this kind, oldest request first."""
    metas = Episode.read_many(["meta.json"])
    queued = [((m.get("meta.json") or {}).get(kind, {}).get("requested_at", ""), slug)
              for slug, m in metas.items()
              if (m.get("meta.json") or {}).get(kind, {}).get("state") == "queued"]
    return [slug for _, slug in sorted(queued)]


def release_stale() -> list[str]:
    """Flip abandoned 'running' jobs to an error the dashboard can show, so nothing hangs forever."""
    released = []
    for slug, files in Episode.read_many(["meta.json"]).items():
        meta = files.get("meta.json") or {}
        changed = False
        for kind in KINDS:
            job = meta.get(kind) or {}
            if job.get("state") != "running" or not job.get("started_at"):
                continue
            if _now() - datetime.fromisoformat(job["started_at"]) > STALE_AFTER[kind]:
                job.update(state="error", error="The worker stopped before finishing. Try again.")
                changed = True
        if changed:
            Episode(slug).write("meta.json", meta)
            released.append(slug)
    return released


def _claim(slug: str, kind: str) -> dict | None:
    ep = Episode(slug)
    meta = ep.read("meta.json") or {}
    job = meta.get(kind) or {}
    if job.get("state") != "queued":
        return None  # cancelled or picked up in the meantime
    job.update(state="running", started_at=_now().isoformat(),
               runner=os.getenv("GITHUB_RUN_ID", "local"), error=None)
    meta[kind] = job
    ep.write("meta.json", meta)
    return job


def _fail(slug: str, kind: str, e: Exception) -> None:
    traceback.print_exc()
    ep = Episode(slug)
    meta = ep.read("meta.json") or {}
    meta.setdefault(kind, {}).update(state="error", error=str(e)[:500] or e.__class__.__name__)
    ep.write("meta.json", meta)


def _progress(slug: str, kind: str):
    """Write progress to the episode every ~10% so the dashboard can show it."""
    last = {"pct": -10}

    def report(done: int, total: int):
        pct = int(done * 100 / max(1, total))
        if pct - last["pct"] >= 10 or done == total:
            last["pct"] = pct
            print(f"  {kind}: {done}/{total}")
            ep = Episode(slug)
            meta = ep.read("meta.json") or {}
            if (meta.get(kind) or {}).get("state") == "running":
                meta[kind]["progress"] = pct
                ep.write("meta.json", meta)
    return report


def run_jobs(kind: str) -> tuple[int, int]:
    if kind == "voice":
        from . import voice as stage  # pulls in the model only when there's work
        work = lambda slug, job: stage.run(slug, job.get("voice"))  # noqa: E731
    elif kind == "illustrate":
        from . import illustrate as stage
        work = lambda slug, job: stage.run(slug, job.get("beats") or [], job.get("prompts"))  # noqa: E731
    elif kind == "shorts":
        from . import shorts as stage
        work = lambda slug, job: stage.run(slug, job.get("ids"), progress=_progress(slug, "shorts"))  # noqa: E731
    else:
        from . import render as stage
        work = lambda slug, job: stage.run(slug, progress=_progress(slug, "render"))  # noqa: E731

    done = failed = 0
    for slug in pending(kind):
        job = _claim(slug, kind)
        if not job:
            continue
        try:
            work(slug, job)
            done += 1
        except Exception as e:  # one bad episode shouldn't block the rest of the queue
            _fail(slug, kind, e)
            failed += 1
    return done, failed


def run_voice_jobs() -> tuple[int, int]:
    return run_jobs("voice")


def main(check_only: bool = False) -> int:
    for slug in release_stale():
        print(f"  released abandoned job: {slug}")
    from . import illustrate
    queued = {kind: pending(kind) for kind in KINDS}
    queued["kaggle (collect)"] = illustrate.waiting()  # notebooks still drawing from an earlier run
    total = sum(len(v) for v in queued.values())
    if check_only:
        print(f"{total} queued: " + (", ".join(f"{k} {s}" for k, v in queued.items() for s in v) or "nothing"))
        return 0 if total else 3
    if not total:
        print("Nothing queued.")
        return 0
    done = failed = 0
    if queued["kaggle (collect)"]:
        done, failed = illustrate.collect_waiting()
    for kind in KINDS:
        d, f = run_jobs(kind)
        done, failed = done + d, failed + f
    print(f"Finished: {done} done, {failed} failed.")
    return 1 if failed and not done else 0

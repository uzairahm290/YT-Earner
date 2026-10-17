"""The production worker: picks up jobs the dashboard queued and runs them.

The dashboard (on Render's tiny free instance) can't run a voice model, so heavy stages work
like a mailbox. Clicking "Generate voice" marks the episode as queued in the database; this
worker, running on a free GitHub Actions machine, finds queued episodes and does the work.

    python cli.py worker            # process everything queued, then exit
    python cli.py worker --check    # exit 0 if there's work, 3 if not (lets Actions skip setup)
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta, timezone

from .episode import Episode

# A job marked running longer than this was abandoned (runner died, timed out, cancelled).
STALE_AFTER = timedelta(minutes=45)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def pending() -> list[str]:
    """Slugs with a queued voice job, oldest request first."""
    metas = Episode.read_many(["meta.json"])
    queued = [(m["meta.json"].get("voice", {}).get("requested_at", ""), slug)
              for slug, m in metas.items()
              if (m.get("meta.json") or {}).get("voice", {}).get("state") == "queued"]
    return [slug for _, slug in sorted(queued)]


def release_stale() -> list[str]:
    """Flip abandoned 'running' jobs to an error the dashboard can show, so nothing hangs forever."""
    released = []
    for slug, files in Episode.read_many(["meta.json"]).items():
        meta = files.get("meta.json") or {}
        job = meta.get("voice") or {}
        if job.get("state") != "running" or not job.get("started_at"):
            continue
        if _now() - datetime.fromisoformat(job["started_at"]) > STALE_AFTER:
            job.update(state="error", error="The worker stopped before finishing. Try again.")
            Episode(slug).write("meta.json", meta)
            released.append(slug)
    return released


def _claim(slug: str) -> dict | None:
    ep = Episode(slug)
    meta = ep.read("meta.json") or {}
    job = meta.get("voice") or {}
    if job.get("state") != "queued":
        return None  # cancelled or picked up in the meantime
    job.update(state="running", started_at=_now().isoformat(),
               runner=os.getenv("GITHUB_RUN_ID", "local"), error=None)
    meta["voice"] = job
    ep.write("meta.json", meta)
    return job


def run_voice_jobs() -> tuple[int, int]:
    from . import voice  # pulls in the model only when there's work

    done = failed = 0
    for slug in pending():
        job = _claim(slug)
        if not job:
            continue
        try:
            voice.run(slug, job.get("voice"))
            done += 1
        except Exception as e:  # one bad episode shouldn't block the rest of the queue
            traceback.print_exc()
            ep = Episode(slug)
            meta = ep.read("meta.json") or {}
            meta.setdefault("voice", {}).update(state="error", error=str(e)[:500] or e.__class__.__name__)
            ep.write("meta.json", meta)
            failed += 1
    return done, failed


def main(check_only: bool = False) -> int:
    for slug in release_stale():
        print(f"  released abandoned job: {slug}")
    jobs = pending()
    if check_only:
        print(f"{len(jobs)} queued: {', '.join(jobs) or 'nothing'}")
        return 0 if jobs else 3
    if not jobs:
        print("Nothing queued.")
        return 0
    done, failed = run_voice_jobs()
    print(f"Finished: {done} narrated, {failed} failed.")
    return 1 if failed and not done else 0

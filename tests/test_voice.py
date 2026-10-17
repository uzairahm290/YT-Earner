"""Narration stage + worker queue. Uses DOCU_MOCK_TTS (tones), so no model download is needed.
Needs ffmpeg on PATH; skipped otherwise."""
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_TTS"] = "1"

from pipeline import research, script, voice, worker  # noqa: E402
from pipeline.config import EPISODES_DIR  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

SLUG = "test-voice"


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class Narration(unittest.TestCase):
    def setUp(self):
        research.run("Test Voice", offline=True)
        script.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)

    def queue(self, voice_id="am_onyx", **extra):
        meta = self.ep.read("meta.json")
        meta["voice"] = {"state": "queued", "voice": voice_id,
                         "requested_at": datetime.now(timezone.utc).isoformat(), **extra}
        self.ep.write("meta.json", meta)

    def test_speakable_rewrites_symbols(self):
        self.assertEqual(voice.speakable("It cost $7.2 billion — about 40%"),
                         "It cost 7.2 billion dollars, about 40 percent")

    def test_narration_timings_cover_every_beat_in_order(self):
        voice.run(SLUG, "am_michael")
        t = self.ep.read("voice/timings.json")
        ids = [b["id"] for ch in self.ep.read("script.json")["chapters"] for b in ch["beats"]]
        self.assertEqual([b["id"] for b in t["beats"]], ids)
        starts = [b["start"] for b in t["beats"]]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(b["end"] > b["start"] for b in t["beats"]))
        self.assertGreater(t["duration"], t["beats"][-1]["end"])
        self.assertGreater(len(voice.load_mp3(self.ep)), 10_000)
        self.assertEqual(self.ep.read("meta.json")["status"], "voiced")
        self.assertEqual(t["script_hash"], voice.script_hash(self.ep.read("script.json")))

    def test_editing_the_script_changes_the_hash(self):
        sc = self.ep.read("script.json")
        before = voice.script_hash(sc)
        sc["chapters"][0]["beats"][0]["narration"] += " Changed."
        self.assertNotEqual(before, voice.script_hash(sc))

    def test_worker_runs_queue_and_reports_empty(self):
        self.assertEqual(worker.main(check_only=True), 3)
        self.queue()
        self.assertEqual(worker.main(check_only=True), 0)
        self.assertEqual(worker.main(), 0)
        meta = self.ep.read("meta.json")
        self.assertEqual(meta["voice"]["state"], "done")
        self.assertEqual(self.ep.read("voice/timings.json")["voice"], "am_onyx")

    def test_worker_records_failures(self):
        self.queue("not_a_voice")
        self.assertEqual(worker.main(), 1)
        job = self.ep.read("meta.json")["voice"]
        self.assertEqual(job["state"], "error")
        self.assertIn("Unknown voice", job["error"])

    def test_abandoned_jobs_are_released(self):
        meta = self.ep.read("meta.json")
        meta["voice"] = {"state": "running",
                         "started_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        self.ep.write("meta.json", meta)
        self.assertEqual(worker.release_stale(), [SLUG])
        self.assertEqual(self.ep.read("meta.json")["voice"]["state"], "error")


if __name__ == "__main__":
    unittest.main()

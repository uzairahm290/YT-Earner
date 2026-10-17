"""Shorts: what they say, word timing, caption paging, the API, and a small real render."""
import importlib
import importlib.util
import os
import shutil
import unittest
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_TTS"] = "1"
os.environ["DOCU_MOCK_VISUALS"] = "1"

from pipeline import research, script, shorts, visuals, voice, worker  # noqa: E402
from pipeline.config import EPISODES_DIR, load_config  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

SLUG = "test-shorts"
SMALL = {**load_config()["shorts"], "width": 360, "height": 640, "fps": 10, "caption_size": 30,
         "preview_height": 320, "pan_speed": 60}
HAVE_TOOLS = bool(shutil.which("ffmpeg")) and importlib.util.find_spec("PIL") is not None


class Timing(unittest.TestCase):
    def test_heard_words_keep_their_times(self):
        words = "Nokia owned forty percent.".split()
        heard = [("Nokia", 1.0, 1.4), ("owned", 1.4, 1.7), ("forty", 1.7, 2.0), ("percent.", 2.0, 2.5)]
        self.assertEqual(shorts.align(words, heard, 1.0, 2.6), [(1.0, 1.4), (1.4, 1.7), (1.7, 2.0), (2.0, 2.5)])

    def test_words_whisper_spelled_differently_are_filled_in_between(self):
        # We print "C.E.O." and "$5"; Whisper hears "CEO" and "five dollars".
        words = "The new C.E.O. paid $5 billion.".split()
        heard = [("The", 0.0, 0.2), ("new", 0.2, 0.4), ("CEO", 0.4, 0.9), ("paid", 0.9, 1.2),
                 ("five", 1.2, 1.5), ("billion", 1.5, 1.9), ("dollars", 1.9, 2.3)]
        t = shorts.align(words, heard, 0.0, 2.4)
        self.assertEqual(len(t), len(words))
        self.assertEqual(t[2], (0.4, 0.9))                     # C.E.O. matched CEO
        self.assertTrue(1.2 <= t[4][0] < t[5][0])              # $5 lands between "paid" and "billion"
        self.assertEqual([s for s, _ in t], sorted(s for s, _ in t))

    def test_without_whisper_words_are_spread_by_length(self):
        t = shorts.align(["a", "longword"], [], 0.0, 1.0)
        self.assertLess(t[0][1] - t[0][0], t[1][1] - t[1][0])
        self.assertAlmostEqual(t[1][1], 1.0, places=2)


class Pages(unittest.TestCase):
    def test_every_word_once_in_order_and_no_page_ends_on_a_weak_word(self):
        text = ("Nokia was still the biggest phone maker on Earth and it was selling more handsets "
                "than anyone in the history of the industry, so how did it get here?")
        words = text.split()
        pages = shorts.pages(words)
        self.assertEqual([i for p in pages for i in p], list(range(len(words))))
        for p in pages[:-1]:
            self.assertNotIn(words[p[-1]].lower().strip(",."), shorts._WEAK, [words[i] for i in p])
        self.assertTrue(all(len(p) <= shorts.cfg()["words_per_page"] + 2 for p in pages))

    def test_sentences_start_new_pages(self):
        words = "February 2011. A memo lands.".split()
        self.assertEqual(shorts.pages(words), [[0, 1], [2, 3, 4]])


class Plan(unittest.TestCase):
    def setUp(self):
        research.run("Test Shorts", offline=True)
        script.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)
        for f in shorts.render.OUTPUT.glob(f"{SLUG}-*.mp4"):
            f.unlink()

    def test_built_from_the_scripts_flagged_moments(self):
        sc = self.ep.read("script.json")
        data = shorts.load(self.ep)
        self.assertEqual(len(data["items"]), len(sc["shorts"]))
        first, pick = data["items"][0], sc["shorts"][0]
        self.assertEqual(first["hook"], pick["hook"])
        beats = sc["chapters"][pick["chapter"]]["beats"][pick["beats"][0] - 1: pick["beats"][1]]
        self.assertEqual([ln["text"] for ln in first["lines"]], [b["narration"] for b in beats])
        roles = [u["role"] for u in shorts.units(first)]
        self.assertEqual(roles[0], "hook")
        self.assertEqual(roles[-1], "outro")

    def test_too_long_is_flagged(self):
        data = shorts.load(self.ep)
        data["items"][0]["lines"] = [{"beat": "c0b01", "text": "word " * 200}]
        self.assertTrue(any("Trim" in p for p in shorts.check(data)))

    def test_long_lines_are_cut_into_several_shots_rotating_pictures(self):
        beats = {"c0b01": {"id": "c0b01", "chosen": 0, "candidates": [
            {"source": "commons", "url": "a"}, {"source": "commons", "url": "b"}, {"source": "card"}]}}
        spoken = [{"role": "line", "beat": "c0b01", "shot_start": 0.0, "shot_end": 7.0}]
        shots = shorts.plan_shots(spoken, beats)
        self.assertEqual(len(shots), 3)
        self.assertEqual([s["pick"]["url"] for s in shots], ["a", "b", "a"])
        self.assertLessEqual(max(s["end"] - s["start"] for s in shots), shorts.cfg()["max_shot_seconds"])

    @unittest.skipUnless(HAVE_TOOLS, "ffmpeg or Pillow not installed")
    def test_made_through_the_worker_queue(self):
        voice.run(SLUG, "am_michael")
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)
        self.ep.set_status("visuals_ready")
        meta = self.ep.read("meta.json")
        meta["shorts"] = {"state": "queued", "ids": ["s1"], "requested_at": "2026-01-01T00:00:00+00:00"}
        self.ep.write("meta.json", meta)
        with mock.patch("pipeline.shorts.cfg", return_value=SMALL):
            self.assertEqual(worker.main(), 0)
        job = self.ep.read("meta.json")["shorts"]
        self.assertEqual(job["state"], "done", job)
        made = job["made"]["s1"]
        out = shorts.render.OUTPUT / made["file"]
        self.assertTrue(out.exists())
        import subprocess
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
        self.assertIn("video,360,640", probe)
        self.assertIn("audio", probe)
        self.assertAlmostEqual(made["duration"], float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True).stdout), delta=0.3)
        self.assertTrue(shorts.load_preview(self.ep, "s1"))


class Api(unittest.TestCase):
    def setUp(self):
        os.environ["DESK_PASSWORD"] = "pw"
        import app
        self.app = importlib.reload(app)
        from fastapi.testclient import TestClient
        self.c = TestClient(self.app.app)
        self.c.post("/api/login", json={"password": "pw"})
        research.run("Test Shorts", offline=True)
        script.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        del os.environ["DESK_PASSWORD"]
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)

    def test_edits_save_and_making_waits_for_approved_pictures(self):
        data = self.c.get(f"/api/episodes/{SLUG}").json()["shorts"]
        data["items"][0]["hook"] = "A sharper hook."
        r = self.c.post(f"/api/episodes/{SLUG}/shorts", json={"label": "Nokia", "items": data["items"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["shorts"]["items"][0]["hook"], "A sharper hook.")
        self.assertEqual(self.c.post(f"/api/episodes/{SLUG}/shorts/make", json={}).status_code, 409)

        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)
        self.ep.set_status("visuals_ready")
        with mock.patch.object(self.app, "_nudge_worker") as nudge:
            r = self.c.post(f"/api/episodes/{SLUG}/shorts/make", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["meta"]["shorts"]["state"], "queued")
        nudge.assert_called_once()

    def test_unknown_beats_are_refused(self):
        data = self.c.get(f"/api/episodes/{SLUG}").json()["shorts"]
        data["items"][0]["lines"][0]["beat"] = "c9b99"
        r = self.c.post(f"/api/episodes/{SLUG}/shorts", json={"label": "x", "items": data["items"]})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()

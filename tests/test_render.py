"""Render stage at thumbnail size, so it runs in seconds. Needs ffmpeg and Pillow; skipped otherwise."""
import importlib.util
import os
import shutil
import unittest
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_TTS"] = "1"
os.environ["DOCU_MOCK_VISUALS"] = "1"
os.environ["DOCU_MOCK_DEPTH"] = "1"  # parallax with a stand-in depth map

from pipeline import render, research, script, visuals, voice, worker  # noqa: E402
from pipeline.config import EPISODES_DIR, load_config  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

SLUG = "test-render"
TINY = {**load_config()["render"], "width": 320, "height": 180, "fps": 10, "preview_seconds": 3}
HAVE_TOOLS = bool(shutil.which("ffmpeg")) and importlib.util.find_spec("PIL") is not None


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg or Pillow not installed")
class Render(unittest.TestCase):
    def setUp(self):
        self.cfg = mock.patch("pipeline.render.cfg", return_value=TINY)
        self.cfg.start()
        research.run("Test Render", offline=True)
        script.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        self.cfg.stop()
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)
        for ext in ("mp4", "srt"):
            (render.OUTPUT / f"{SLUG}.{ext}").unlink(missing_ok=True)

    def ready(self):
        voice.run(SLUG, "am_michael")
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)

    def test_plan_covers_the_whole_narration_without_gaps(self):
        self.ready()
        timings = self.ep.read("voice/timings.json")
        segs = render.plan(self.ep.read("script.json"), timings)
        self.assertEqual(segs[0]["start"], 0.0)
        total = sum(s["seconds"] for s in segs)
        self.assertAlmostEqual(total, timings["duration"], places=2)
        self.assertTrue(all(s["seconds"] > 0 for s in segs))

    def test_captions_follow_the_voice(self):
        self.ready()
        srt = render.captions(self.ep.read("script.json"), self.ep.read("voice/timings.json"))
        self.assertTrue(srt.startswith("1\n00:00:00,600 --> "))
        self.assertIn("February 2011.", srt)

    def test_full_render_through_the_worker_queue(self):
        self.ready()
        meta = self.ep.read("meta.json")
        meta["render"] = {"state": "queued", "requested_at": "2026-01-01T00:00:00+00:00"}
        self.ep.write("meta.json", meta)
        self.assertEqual(worker.main(), 0)

        job = self.ep.read("meta.json")["render"]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(self.ep.read("meta.json")["status"], "rendered")
        out = render.OUTPUT / f"{SLUG}.mp4"
        self.assertTrue(out.exists())
        self.assertTrue(render.load_preview(self.ep).startswith(b"\x00\x00\x00") or render.load_preview(self.ep)[4:8] == b"ftyp")
        self.assertTrue(self.ep.read("render/captions.srt"))
        # Dissolves and split shots must not drift the picture away from the narration.
        import subprocess
        dur = lambda kind: float(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "stream=duration", "-of", "csv=p=0",
             str(out)], capture_output=True, text=True).stdout.strip())
        timings = self.ep.read("voice/timings.json")
        self.assertAlmostEqual(dur("v:0"), timings["duration"], delta=0.15)
        self.assertAlmostEqual(dur("a:0"), timings["duration"], delta=0.15)
        self.assertGreaterEqual(job["shots"], len(timings["beats"]))
        self.assertEqual(job["music"], "builtin:calm")

    def test_long_beats_are_split_across_real_pictures_only(self):
        self.ready()
        manifest = self.ep.read("visuals.json")
        sc, timings = self.ep.read("script.json"), self.ep.read("voice/timings.json")
        picks = {b["id"]: b["candidates"][b["chosen"]] for b in manifest["beats"]}
        with mock.patch("pipeline.render.cfg", return_value={**TINY, "max_shot_seconds": 2.0}):
            cut = render.shots(sc, timings, picks, manifest)
        self.assertGreater(len(cut), len(picks))
        self.assertTrue(all(s["seconds"] <= 2.0 * 1.2 for s in cut if s["pick"]["source"] != "card"))
        self.assertFalse(any(s["pick"]["source"] == "ai" and s["pick"] is not picks[s["beat"]["id"]] for s in cut))
        total = sum(s["seconds"] for s in cut)
        self.assertAlmostEqual(total, timings["duration"], places=2)

    def test_refuses_when_the_script_changed_after_recording(self):
        self.ready()
        sc = self.ep.read("script.json")
        sc["chapters"][0]["beats"][0]["narration"] += " Edited."
        self.ep.write("script.json", sc)
        with self.assertRaises(render.RenderError):
            render.run(SLUG)

    def test_cards_and_lower_thirds_draw(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            for kind in ({"card": "stat", "big": "60%", "text": "yield"}, {"card": "document", "text": "June 2026", "sub": "report"},
                         {"card": "place", "text": "Espoo, Finland"}, {"card": "title", "text": "Chapter one"}):
                p = render.draw_card(kind, Path(d) / "c.jpg")
                self.assertEqual(Image.open(p).size, (320, 180))
            lt = render.draw_lower_third("Stephen Elop — CEO, Nokia", Path(d) / "lt.png")
            self.assertEqual(Image.open(lt).mode, "RGBA")


if __name__ == "__main__":
    unittest.main()

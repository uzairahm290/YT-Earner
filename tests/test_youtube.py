"""YouTube: the upload package, chapters, and uploads through the worker (API faked)."""
import importlib
import os
import shutil
import unittest
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_TTS"] = "1"
os.environ["DOCU_MOCK_VISUALS"] = "1"
os.environ["DOCU_MOCK_YOUTUBE"] = "1"

from pipeline import research, script, visuals, voice, worker, youtube  # noqa: E402
from pipeline.config import EPISODES_DIR  # noqa: E402
from pipeline.episode import Episode  # noqa: E402
from pipeline.store import get_store  # noqa: E402

SLUG = "test-youtube"


class Chapters(unittest.TestCase):
    SCRIPT = {"chapters": [{"title": t, "beats": [{"id": f"c{i}b01"}]} for i, t in enumerate(["Open", "Rise", "Fall"])]}

    def test_first_at_zero_then_each_chapter_start(self):
        timings = {"duration": 200, "beats": [{"id": "c0b01", "start": 0.6}, {"id": "c1b01", "start": 65},
                                              {"id": "c2b01", "start": 3725}]}
        timings["duration"] = 3800
        self.assertEqual(youtube.chapters(self.SCRIPT, timings), ["0:00 Open", "1:05 Rise", "1:02:05 Fall"])

    def test_none_when_youtube_would_reject_them(self):
        short = {"duration": 30, "beats": [{"id": "c0b01", "start": 0}, {"id": "c1b01", "start": 5},
                                           {"id": "c2b01", "start": 20}]}
        self.assertEqual(youtube.chapters(self.SCRIPT, short), [])  # a chapter under 10 seconds
        two = {"chapters": self.SCRIPT["chapters"][:2]}
        self.assertEqual(youtube.chapters(two, {"duration": 99, "beats": [{"id": "c1b01", "start": 40}]}), [])


class Flow(unittest.TestCase):
    def setUp(self):
        research.run("Test YouTube", offline=True)
        script.run(SLUG)
        voice.run(SLUG, "am_michael")
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)
        self.ep = Episode.load(SLUG)
        meta = self.ep.read("meta.json")
        meta["render"] = {"state": "done", "file": f"{SLUG}.mp4"}
        meta["shorts"] = {"state": "done", "made": {"s1": {"file": f"{SLUG}-s1.mp4"}}}
        self.ep.write("meta.json", meta)
        self.ep.write("render/captions.srt", "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        youtube.render.OUTPUT.mkdir(exist_ok=True)
        for name in (f"{SLUG}.mp4", f"{SLUG}-s1.mp4"):
            (youtube.render.OUTPUT / name).write_bytes(b"\0" * 1000)
        youtube.connect("code", "http://x/cb")

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)
        youtube.disconnect()
        for name in (f"{SLUG}.mp4", f"{SLUG}-s1.mp4"):
            (youtube.render.OUTPUT / name).unlink(missing_ok=True)

    def test_package_is_ready_to_paste(self):
        p = youtube.package(self.ep)
        self.assertLessEqual(len(p["long"]["title"]), 100)
        self.assertIn("Narrated with a synthetic voice", p["long"]["description"])
        self.assertIn("s1", p["shorts"])
        self.assertTrue(p["shorts"]["s1"]["title"].endswith("#Shorts"))
        self.assertLessEqual(sum(len(t) + 1 for t in p["long"]["tags"]), 480)

    def test_realistic_ai_marks_the_video_synthetic(self):
        self.assertFalse(youtube.package(self.ep)["long"]["synthetic"])
        m = self.ep.read("visuals.json")
        b = m["beats"][0]
        b["candidates"].insert(0, {"source": "ai", "style": "photo", "kind": "image", "file": "visuals/ai/x.b64"})
        b["chosen"] = 0
        self.ep.write("visuals.json", m)
        p = youtube.package(self.ep)
        self.assertTrue(p["long"]["synthetic"])
        self.assertIn("AI-generated images", p["long"]["description"])

    def test_worker_uploads_long_then_shorts_link_to_it(self):
        meta = self.ep.read("meta.json")
        meta["youtube"] = {"state": "queued", "requested_at": "2026-01-01T00:00:00+00:00",
                           "targets": [{"what": "long", "privacy": "unlisted"}, {"what": "s1", "privacy": "private"}]}
        self.ep.write("meta.json", meta)
        self.assertEqual(worker.main(), 0)
        meta = self.ep.read("meta.json")
        self.assertEqual(meta["youtube"]["state"], "done")
        self.assertEqual(meta["upload"]["long"]["privacy"], "unlisted")
        self.assertTrue(meta["upload"]["long"]["captions"])
        self.assertTrue(meta["upload"]["s1"]["url"].startswith("https://youtu.be/"))
        self.assertIn(meta["upload"]["long"]["url"], youtube.package(self.ep)["shorts"]["s1"]["description"])

    def test_missing_login_marks_the_targets_failed(self):
        youtube.disconnect()
        with mock.patch.dict(os.environ, {"DOCU_MOCK_YOUTUBE": ""}):
            with self.assertRaises(youtube.YouTubeError):
                youtube.run(SLUG, [{"what": "long"}])
        self.assertEqual(self.ep.read("meta.json")["upload"]["long"]["state"], "error")


class Api(unittest.TestCase):
    def setUp(self):
        os.environ["DESK_PASSWORD"] = "pw"
        import app
        self.app = importlib.reload(app)
        from fastapi.testclient import TestClient
        self.c = TestClient(self.app.app)
        self.c.post("/api/login", json={"password": "pw"})

    def tearDown(self):
        del os.environ["DESK_PASSWORD"]
        youtube.disconnect()

    def test_connect_round_trip_checks_the_state(self):
        r = self.c.get("/api/youtube/callback?code=x&state=123.forged", follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        r = self.c.get("/api/youtube/connect", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/youtube").json()["channel"], "Mock Channel")
        self.assertIsNotNone(get_store().read("_settings", "youtube.json"))
        self.assertNotIn("_settings", [e["slug"] for e in self.c.get("/api/episodes").json()])


if __name__ == "__main__":
    unittest.main()


class Thumbnails(unittest.TestCase):
    def setUp(self):
        research.run("Test Thumbs", offline=True)
        script.run("test-thumbs")
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run("test-thumbs")
        self.ep = Episode.load("test-thumbs")

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / "test-thumbs", ignore_errors=True)

    def test_three_options_from_real_pictures_with_the_scripts_text(self):
        from pipeline import thumbnail
        t = thumbnail.make("test-thumbs")
        self.assertEqual([o["layout"] for o in t["options"]], ["left", "bottom", "split"])
        self.assertEqual(t["text"], self.ep.read("script.json")["thumbnail_text_options"][0])
        jpg = thumbnail.chosen_bytes(self.ep)
        from PIL import Image
        import io
        self.assertEqual(Image.open(io.BytesIO(jpg)).size, (1280, 720))
        self.assertLess(len(jpg), 2 * 1024 * 1024)  # YouTube's limit

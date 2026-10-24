"""Story format: writing, repeat detection, the narrator cutout, and the overlay render."""
import importlib
import importlib.util
import io
import os
import shutil
import subprocess
import unittest
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_TTS"] = "1"
os.environ["DOCU_MOCK_VISUALS"] = "1"
os.environ["DOCU_MOCK_YOUTUBE"] = "1"

from pipeline import persona, render, story, visuals, voice, worker, youtube  # noqa: E402
from pipeline.config import EPISODES_DIR, load_config  # noqa: E402
from pipeline.episode import Episode  # noqa: E402
from pipeline.store import get_store  # noqa: E402

SLUG = "test-story"
HAVE = bool(shutil.which("ffmpeg")) and importlib.util.find_spec("PIL") is not None
TINY = {**load_config()["render"], "width": 320, "height": 180, "fps": 10, "preview_seconds": 3}


def checker_person(w=240, h=320, size=24, off=6):
    """A fake 'transparent' PNG: checkerboard baked in, with a grey suit, a white shirt (same white
    as the light squares) and a gap between arm and body that should come out as background."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (230, 232, 233) if ((x + off) // size + (y + off) // size) % 2 == 0 else (247, 249, 250)
    d = ImageDraw.Draw(im)
    d.ellipse([90, 20, 150, 90], fill=(200, 160, 130))               # head
    d.rectangle([60, 90, 180, h - 1], fill=(110, 112, 116))          # suit
    d.rectangle([105, 95, 135, 200], fill=(247, 249, 250))           # white shirt, checker-white
    d.rectangle([62, 150, 85, 240], fill=(230, 232, 233))            # shirt shading, checker-grey
    d.rectangle([186, 120, 236, h - 1], fill=(110, 112, 116))        # arm, with a gap at x 181-185
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@unittest.skipUnless(HAVE, "ffmpeg or Pillow not installed")
class Narrator(unittest.TestCase):
    def tearDown(self):
        persona.remove()

    def test_checkerboard_is_removed_but_the_white_shirt_stays(self):
        import numpy as np
        out, how = persona.cut(checker_person())
        self.assertEqual(how, "checkerboard removed")
        a = np.asarray(out)[..., 3]
        # coordinates move with the crop; find the shirt by colour
        rgb = np.asarray(out)[..., :3].astype(int)
        shirt = (np.abs(rgb - [247, 249, 250]).max(-1) <= 3) & (a > 250)
        self.assertGreater(shirt.sum(), 1500, "the white shirt was cut out with the background")

    def test_plain_photo_is_refused_with_advice(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), (90, 120, 160)).save(buf, "JPEG")
        with self.assertRaises(persona.PersonaError) as e:
            persona.cut(buf.getvalue())
        self.assertIn("remove.bg", str(e.exception))

    def test_transparent_png_is_used_as_is(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([20, 20, 80, 80], fill=(200, 100, 50, 255))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        meta = persona.save(buf.getvalue(), "Ryan", True)
        self.assertEqual((meta["how"], meta["name"], meta["ai_generated"]), ("transparent", "Ryan", True))
        self.assertTrue(persona.cutout_png())


class Writing(unittest.TestCase):
    def setUp(self):
        get_store().write("_settings", "stories.json", "[]")
        self.ep = Episode.create("Test Story")
        meta = self.ep.read("meta.json")
        meta.update(format="story", premise="A licensed engineer is cut as non-essential before an audit.")
        self.ep.write("meta.json", meta)
        self.ep.set_status("verified")

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)
        get_store().write("_settings", "stories.json", "[]")

    def test_story_becomes_an_ordinary_script_with_scenes_and_a_disclaimer(self):
        story.run(SLUG, minutes=4)
        sc = self.ep.read("script.json")
        outline_chapters = 4
        self.assertEqual(len(sc["chapters"]), outline_chapters)
        beats = [b for ch in sc["chapters"] for b in ch["beats"]]
        self.assertTrue(all(b["visual"]["type"] == "broll" and b["visual"]["query"] for b in beats))
        self.assertIn(story.DISCLAIMER, sc["description"])
        lint = self.ep.read("lint.json")
        self.assertFalse(any("Numbers not found" in i for i in lint["issues"]))  # no fact sheet to check
        self.assertEqual(self.ep.read("meta.json")["status"], "scripted")

    def test_a_repeat_premise_is_flagged(self):
        story.run(SLUG, minutes=4)
        other = Episode.create("Test Story Again")
        try:
            meta = other.read("meta.json")
            meta.update(format="story")
            other.write("meta.json", meta)
            other.set_status("verified")
            story.run(other.slug, premise="A licensed engineer is cut as non-essential right before the audit.", minutes=4)
            issues = other.read("lint.json")["issues"]
            self.assertTrue(any("Close to an earlier story" in i for i in issues), issues)
        finally:
            shutil.rmtree(EPISODES_DIR / other.slug, ignore_errors=True)


@unittest.skipUnless(HAVE, "ffmpeg or Pillow not installed")
class Render(unittest.TestCase):
    def setUp(self):
        get_store().write("_settings", "stories.json", "[]")
        self.ep = Episode.create("Test Story")
        meta = self.ep.read("meta.json")
        meta.update(format="story", premise="A licensed engineer is cut as non-essential before an audit.")
        self.ep.write("meta.json", meta)
        self.ep.set_status("verified")
        story.run(SLUG, minutes=4)
        voice.run(SLUG, "am_michael")
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)
        self.ep.set_status("visuals_ready")

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)
        persona.remove()
        get_store().write("_settings", "stories.json", "[]")
        for ext in ("mp4", "srt"):
            (render.OUTPUT / f"{SLUG}.{ext}").unlink(missing_ok=True)

    def test_needs_a_narrator(self):
        with mock.patch("pipeline.render.cfg", return_value=TINY):
            with self.assertRaises(Exception) as e:
                render.run(SLUG)
        self.assertIn("narrator", str(e.exception).lower())

    def test_full_story_render_through_the_worker(self):
        persona.save(checker_person(), "Ryan", True)
        meta = self.ep.read("meta.json")
        meta["render"] = {"state": "queued", "requested_at": "2026-01-01T00:00:00+00:00"}
        self.ep.write("meta.json", meta)
        with mock.patch("pipeline.render.cfg", return_value=TINY):
            self.assertEqual(worker.main(), 0)
        job = self.ep.read("meta.json")["render"]
        self.assertEqual((job["state"], job["layout"]), ("done", "story"), job)
        out = render.OUTPUT / f"{SLUG}.mp4"
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, self.ep.read("voice/timings.json")["duration"], delta=0.2)
        srt = self.ep.read("render/captions.srt")
        self.assertIn("Derek", srt)
        # AI narrator photo -> the upload is marked synthetic and says so
        pack = youtube.package(self.ep)
        self.assertTrue(pack["long"]["synthetic"])
        self.assertIn("AI-generated", pack["long"]["description"])
        self.assertIn("fiction", pack["long"]["description"])


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

    def test_premise_ideas(self):
        r = self.c.post("/api/stories/premises")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()["premises"]), 3)


if __name__ == "__main__":
    unittest.main()


class OwnVoice(unittest.TestCase):
    """Your voice via OpenVoice on Kaggle; Kaggle itself is replaced by a pass-through."""
    def setUp(self):
        from pipeline import research, script
        research.run("Test Clone", offline=True)
        script.run("test-clone")
        self.ep = Episode.load("test-clone")

    def tearDown(self):
        from pipeline import voiceclone
        shutil.rmtree(EPISODES_DIR / "test-clone", ignore_errors=True)
        voiceclone.remove_reference()

    def test_chunks_cut_between_beats_and_cover_everything(self):
        from pipeline import voiceclone
        beats = [{"start": i * 10.0, "end": i * 10.0 + 9.0} for i in range(12)]  # 120 s
        sr = 24000
        ch = voiceclone.chunks(beats, 120 * sr, sr, max_seconds=45)
        self.assertEqual(ch[0][0], 0)
        self.assertEqual(ch[-1][1], 120 * sr)
        self.assertTrue(all(a == prev[1] for prev, (a, _) in zip(ch, ch[1:])))
        for a, _ in ch[1:]:
            t = a / sr
            self.assertTrue(any(b["end"] <= t <= nb["start"] for b, nb in zip(beats, beats[1:])), t)  # in a pause

    def test_needs_a_sample_and_consent(self):
        os.environ["DESK_PASSWORD"] = "pw"
        try:
            import app
            a = importlib.reload(app)
            from fastapi.testclient import TestClient
            c = TestClient(a.app)
            c.post("/api/login", json={"password": "pw"})
            self.ep.set_status("approved")
            self.assertEqual(c.post(f"/api/episodes/test-clone/voice", json={"voice": "clone:me"}).status_code, 409)
            import base64
            url = "data:audio/mp4;base64," + base64.b64encode(b"\0" * 40000).decode()
            self.assertEqual(c.post("/api/voiceref", json={"data_url": url, "consent": False}).status_code, 422)
            self.assertEqual(c.post("/api/voiceref", json={"data_url": url, "consent": True}).status_code, 200)
        finally:
            del os.environ["DESK_PASSWORD"]

    def test_narration_in_your_voice_keeps_the_timings_and_is_marked_ai(self):
        from pipeline import voiceclone
        with mock.patch.dict(os.environ, {"DOCU_MOCK_VOICECLONE": "1"}):
            voice.run("test-clone", voiceclone.VOICE_ID)
        t = self.ep.read("voice/timings.json")
        self.assertEqual(t["voice"], voiceclone.VOICE_ID)
        self.assertEqual(t["script_hash"], voice.script_hash(self.ep.read("script.json")))
        self.assertEqual(self.ep.read("meta.json")["status"], "voiced")
        self.assertTrue(voice.load_mp3(self.ep))
        meta = self.ep.read("meta.json")
        meta["render"] = {"state": "done"}
        self.ep.write("meta.json", meta)
        pack = youtube.package(self.ep)["long"]
        self.assertTrue(pack["synthetic"])
        self.assertIn("creator's own voice", pack["description"])

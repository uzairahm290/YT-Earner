"""AI illustrations: the eligibility rule, the Kaggle notebook we generate, and storing results.
Kaggle itself is replaced by stand-in images (DOCU_MOCK_ILLUSTRATE)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"
os.environ["DOCU_MOCK_VISUALS"] = "1"
os.environ["DOCU_MOCK_ILLUSTRATE"] = "1"

from pipeline import illustrate, research, script, visuals, worker  # noqa: E402
from pipeline.config import EPISODES_DIR  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

SLUG = "test-illustrate"


class Rule(unittest.TestCase):
    def test_only_abstract_broll(self):
        ok = ["gears grinding to a halt", "two paths diverging", "microscopic view of glass material",
              "close up of old keypad phone", "empty factory floor at night"]
        never = [("broll", "Foxconn factory floor"), ("broll", "iPhone Duo water resistance test"),
                 ("broll", "timeline graphic with patents, Cook, Foxconn"), ("broll", "launch event 2026"),
                 ("archive_photo", "gears turning"), ("document", "press release"), ("chart", "growth 40%"),
                 ("map", "factory locations"), ("title_card", "chapter one")]
        for q in ok:
            self.assertTrue(illustrate.eligible("broll", q)[0], q)
        for t, q in never:
            self.assertFalse(illustrate.eligible(t, q)[0], (t, q))

    def test_prompt_uses_house_style_and_drops_camera_words(self):
        p = illustrate.prompt_for("close up of circuit board assembly slow motion")
        self.assertTrue(p.startswith("circuit board assembly, editorial ink illustration"))

    def test_generated_kaggle_script_is_valid_python_with_the_job_inside(self):
        with tempfile.TemporaryDirectory() as d:
            items = [{"id": "c0b01", "prompt": 'a "quoted" idea """ with triple quotes', "seed": 1}]
            kid = illustrate.build_kernel(Path(d), items, "someone")
            self.assertEqual(kid, "someone/docu-illustrate")
            meta = json.loads((Path(d) / "kernel-metadata.json").read_text())
            self.assertTrue(meta["enable_gpu"] and meta["is_private"] and meta["enable_internet"])
            code = (Path(d) / "illustrate.py").read_text()
            compile(code, "illustrate.py", "exec")
            # Read JOB the same way the notebook does: evaluate that one assignment.
            import ast
            tree = ast.parse(code)
            assign = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "JOB")
            job = json.loads(ast.literal_eval(assign.value.args[0]))
            self.assertEqual(job["items"][0]["prompt"], items[0]["prompt"])

    def test_missing_kaggle_secrets_give_a_clear_error(self):
        with mock.patch.dict(os.environ, {"DOCU_MOCK_ILLUSTRATE": "", "KAGGLE_USERNAME": "", "KAGGLE_API_TOKEN": ""}):
            with self.assertRaises(illustrate.IllustrateError) as e:
                illustrate.run_on_kaggle([{"id": "x", "prompt": "p", "seed": 1}], Path("."))
        self.assertIn("KAGGLE_API_TOKEN", str(e.exception))


class Flow(unittest.TestCase):
    def setUp(self):
        research.run("Test Illustrate", offline=True)
        script.run(SLUG)
        with mock.patch("pipeline.visuals.time.sleep"):
            visuals.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)

    def test_worker_draws_queued_beats_and_keeps_your_picks(self):
        m = self.ep.read("visuals.json")
        todo = illustrate.candidates_for(m)
        self.assertTrue(todo)
        target = next(b for b in m["beats"] if b["id"] == todo[0])
        picked = target["candidates"][target["chosen"]]

        meta = self.ep.read("meta.json")
        meta["illustrate"] = {"state": "queued", "beats": todo, "requested_at": "2026-01-01T00:00:00+00:00"}
        self.ep.write("meta.json", meta)
        self.assertEqual(worker.main(), 0)

        self.assertEqual(self.ep.read("meta.json")["illustrate"]["state"], "done")
        m2 = self.ep.read("visuals.json")
        b2 = next(b for b in m2["beats"] if b["id"] == todo[0])
        ai = [c for c in b2["candidates"] if c["source"] == "ai"]
        self.assertEqual(len(ai), illustrate.cfg()["per_beat"])
        self.assertEqual(b2["candidates"][b2["chosen"]], picked)  # your pick didn't move
        self.assertEqual(b2["candidates"][-1]["source"], "card")  # AI goes before the fallback card
        mime, data = visuals.load_upload(self.ep, ai[0]["file"])
        self.assertEqual(mime, "image/jpeg")
        self.assertNotIn("ai", [l for l in visuals.credits(m2) if "AI" in l])
        self.assertEqual(illustrate.candidates_for(m2), [])  # everything queued got drawn

    def test_queue_cannot_smuggle_a_real_world_beat(self):
        m = self.ep.read("visuals.json")
        real = next(b["id"] for b in m["beats"] if b["type"] == "archive_photo")
        with self.assertRaises(illustrate.IllustrateError):
            illustrate.run(SLUG, [real])


if __name__ == "__main__":
    unittest.main()

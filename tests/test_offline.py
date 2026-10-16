"""Offline tests: no internet, no API key. Run with:  python -m unittest discover tests"""
import os
import shutil
import unittest

os.environ["DOCU_MOCK_LLM"] = "1"

from pipeline import research, script, topics  # noqa: E402
from pipeline.config import EPISODES_DIR  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

SLUG = "test-nokia"


class OfflinePipeline(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)

    def test_topics_filters_and_dedupes(self):
        found = topics.find_topics(offline=True)
        titles = [t["title"] for t in found]
        self.assertIn("Nokia", titles)
        self.assertNotIn("Taylor Swift", titles)      # not tech
        self.assertNotIn("Main Page", titles)         # noise
        self.assertEqual(sum("replace the smartphone" in t for t in titles), 1)  # dupe dropped

    def test_research_then_script(self):
        ep = research.run("Test Nokia", offline=True)
        self.assertEqual(ep.slug, SLUG)
        fs = ep.read("factsheet.json")
        self.assertTrue(fs["timeline"])
        self.assertIn("S1", fs["sources_index"])
        self.assertIn("Verify before scripting", ep.read("factsheet.md"))

        script.run(SLUG, angle="The memo that admitted an empire was on fire")
        data = ep.read("script.json")
        self.assertEqual(data["chapters"][0]["beats"][0]["id"], "c0b01")
        report = ep.read("lint.json")
        self.assertTrue(report["ok"], report["issues"])  # no banned phrases, no unbacked numbers
        self.assertEqual(Episode.load(SLUG).read("meta.json")["status"], "scripted")

    def test_lint_catches_ai_phrases_and_invented_numbers(self):
        ep = research.run("Test Nokia", offline=True)
        s = script.generate("Test Nokia", ep.read("factsheet.json"))
        s.chapters[1].beats[0].narration = "Let's dive in. In 1999 Nokia sold 75 million phones."
        report = script.lint(s, ep.read("factsheet.json"))
        joined = " ".join(report["issues"])
        self.assertIn("let's dive in", joined)
        self.assertIn("1999", joined)
        self.assertIn("75 million", joined)
        self.assertFalse(report["ok"])

    def test_bad_short_range_rejected(self):
        ep = research.run("Test Nokia", offline=True)
        s = script.generate("Test Nokia", ep.read("factsheet.json")).model_dump()
        s["shorts"][0]["beats"] = [1, 99]
        with self.assertRaises(Exception):
            script.Script.model_validate(s)

    def test_prompt_placeholders_all_filled(self):
        prompt = script.build_system_prompt()
        for ph in ("{voice_style}", "{banned_phrases}", "{target_words}", "{shorts_count}"):
            self.assertNotIn(ph, prompt)
        self.assertIn('"title_options"', prompt)  # JSON braces survived


if __name__ == "__main__":
    unittest.main()

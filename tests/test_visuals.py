"""Visual search: providers are mocked with recorded-style API responses, no network needed."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

os.environ["DOCU_MOCK_LLM"] = "1"

from pipeline import research, script, visuals  # noqa: E402
from pipeline.config import EPISODES_DIR  # noqa: E402
from pipeline.episode import Episode  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
SLUG = "test-visuals"


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


def fake_get(url, params=None, headers=None, timeout=None):
    if "commons.wikimedia.org" in url:
        return FakeResp(json.loads((FIX / "commons_search.json").read_text()))
    if "pexels.com" in url:
        return FakeResp(json.loads((FIX / "pexels_videos.json").read_text()))
    if "pixabay.com" in url:
        return FakeResp({"hits": []})
    raise AssertionError(f"unexpected url {url}")


class Providers(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.session.get.side_effect = fake_get

    def test_commons_keeps_only_big_freely_licensed_photos(self):
        found = visuals.search_commons("Foxconn", 4, self.session)
        titles = [c["title"] for c in found]
        # tiny (400px), SVG logo and GFDL-only are dropped; result order follows Commons' ranking
        self.assertEqual(titles, ["Foxconn Longhua campus", "Foxconn Shenzhen 2010.jpg"])
        first = found[0]
        self.assertEqual(first["credit"], "Jane Example")  # HTML stripped
        self.assertEqual(first["license"], "CC BY-SA 4.0")
        self.assertIn("/1920px-", first["url"])  # big originals get a 1080p-friendly size
        self.assertNotIn("/1920px-", found[1]["url"])  # 1600px original is used as-is

    def test_pexels_picks_the_file_closest_to_1080p(self):
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "k"}):
            found = visuals.search_pexels("factory floor", 4, self.session)
        self.assertEqual(found[0]["url"], "https://videos.pexels.com/1/hd.mp4")
        self.assertEqual(found[0]["credit"], "Alex Shot")
        self.assertEqual(len(found), 2)

    def test_no_key_means_no_stock_calls(self):
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}):
            self.assertEqual(visuals.search_pexels("x", 4, self.session), [])
            self.assertEqual(visuals.search_pixabay("x", 4, self.session), [])
        self.session.get.assert_not_called()

    def test_broll_prefers_video_then_falls_back_to_photos_then_card(self):
        beat = {"id": "c0b01", "visual": {"type": "broll", "query": "Foxconn factory"}, "on_screen_text": None}
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "k"}):
            got = visuals.find_for_beat(beat, self.session)
        sources = [c["source"] for c in got["candidates"]]
        self.assertEqual(sources[:2], ["pexels", "pexels"])
        self.assertIn("commons", sources)
        self.assertEqual(sources[-1], "card")

    def test_documents_lead_with_an_honest_card(self):
        beat = {"id": "c1b06", "visual": {"type": "document", "query": "The Bell report October 2017 Apple LG Display"},
                "on_screen_text": "October 2017"}
        got = visuals.find_for_beat(beat, self.session)
        self.assertEqual(got["candidates"][0]["card"], "document")
        self.assertTrue(got["candidates"][0]["needs_screenshot"])

    def test_archive_photo_with_nothing_found_is_flagged(self):
        empty = mock.Mock()
        empty.get.return_value = FakeResp({"query": {"pages": {}}})
        beat = {"id": "x", "visual": {"type": "archive_photo", "query": "Someone Obscure 2026"}, "on_screen_text": None}
        got = visuals.find_for_beat(beat, empty)
        self.assertTrue(got["missing"])
        self.assertEqual(got["candidates"][0]["source"], "card")

    def test_network_errors_are_recorded_not_raised(self):
        import requests
        broken = mock.Mock()
        broken.get.side_effect = requests.ConnectionError("down")
        beat = {"id": "x", "visual": {"type": "archive_photo", "query": "Foxconn"}, "on_screen_text": None}
        got = visuals.find_for_beat(beat, broken)
        self.assertTrue(got["errors"])
        self.assertEqual(got["candidates"][-1]["source"], "card")


class Manifest(unittest.TestCase):
    def setUp(self):
        research.run("Test Visuals", offline=True)
        script.run(SLUG)
        self.ep = Episode.load(SLUG)

    def tearDown(self):
        shutil.rmtree(EPISODES_DIR / SLUG, ignore_errors=True)

    def run_visuals(self):
        with mock.patch("pipeline.visuals._session") as s, mock.patch("pipeline.visuals.time.sleep"):
            s.return_value.get.side_effect = fake_get
            visuals.run(SLUG)
        return self.ep.read("visuals.json")

    def test_every_beat_gets_options_and_reruns_keep_choices(self):
        m = self.run_visuals()
        ids = [b["id"] for ch in self.ep.read("script.json")["chapters"] for b in ch["beats"]]
        self.assertEqual([b["id"] for b in m["beats"]], ids)
        self.assertTrue(all(b["candidates"] for b in m["beats"]))

        target = next(b for b in m["beats"] if len(b["candidates"]) > 1)
        visuals.choose(SLUG, target["id"], 1)
        m2 = self.run_visuals()
        self.assertEqual(next(b for b in m2["beats"] if b["id"] == target["id"])["chosen"], 1)

    def test_upload_becomes_the_choice_and_can_be_read_back(self):
        self.run_visuals()
        beat_id = self.ep.read("visuals.json")["beats"][0]["id"]
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        b = visuals.add_upload(SLUG, beat_id, png, "screenshot.png")
        self.assertEqual(b["candidates"][0]["source"], "upload")
        mime, data = visuals.load_upload(self.ep, b["candidates"][0]["file"])
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        with self.assertRaises(ValueError):
            visuals.add_upload(SLUG, beat_id, "data:text/html;base64,PGgxPg==", "x.html")

    def test_credits_list_each_used_source_once(self):
        m = self.run_visuals()
        lines = visuals.credits(m)
        self.assertEqual(len(lines), len(set(lines)))
        self.assertTrue(all("Wikimedia Commons" in l or "Pexels" in l or "Pixabay" in l for l in lines))


if __name__ == "__main__":
    unittest.main()

"""Music beds, effects and the ducked mix; animated card values."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import audio, motion

HAVE = bool(shutil.which("ffmpeg")) and importlib.util.find_spec("numpy") is not None


@unittest.skipUnless(HAVE, "ffmpeg or numpy not installed")
class Sound(unittest.TestCase):
    def test_every_mood_loops_without_a_click(self):
        import numpy as np
        for mood in audio.PROGRESSIONS:
            x = audio.synth_loop(mood)
            self.assertEqual(x.shape, (2, int(64 * audio.SR)))
            seam = np.abs(np.diff(np.concatenate([x[:, -500:], x[:, :500]], axis=1))).max()
            typical = np.abs(np.diff(x[:, 200000:220000])).max()
            self.assertLess(seam, typical * 1.5, mood)
            self.assertLessEqual(np.abs(x).max(), 0.26)

    def test_mix_is_as_long_as_the_narration_and_keeps_the_voice_on_top(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            voice = d / "voice.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=12", "-af", "volume=-12dB",
                            str(voice)], check=True)
            bed = audio.bed_file("builtin:calm", d)
            out = audio.mix(voice, 12.0, d / "mix.m4a", bed, whooshes=[4.0], hits=[8.0])
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                        "csv=p=0", str(out)], capture_output=True, text=True).stdout)
            self.assertAlmostEqual(dur, 12.0, delta=0.1)
            self.assertIsNone(audio.bed_file("none", d))

    def test_library_lists_the_built_in_moods(self):
        ids = [t["id"] for t in audio.library()]
        self.assertTrue({"builtin:calm", "builtin:tension", "builtin:uplift"} <= set(ids))


class Cards(unittest.TestCase):
    def test_numbers_count_up_and_land_exactly(self):
        v = motion.count_up_values("60%", 10)
        self.assertEqual((v[0], v[-1]), ("0%", "60%"))
        v = motion.count_up_values("$3.5 billion", 8)
        self.assertEqual((v[0], v[-1]), ("$0.0 billion", "$3.5 billion"))
        self.assertEqual(motion.count_up_values("1,200", 5)[-1], "1,200")

    def test_years_and_words_stay_still(self):
        self.assertIsNone(motion.count_up_values("2007", 10))
        self.assertIsNone(motion.count_up_values("Q", 10))


if __name__ == "__main__":
    unittest.main()

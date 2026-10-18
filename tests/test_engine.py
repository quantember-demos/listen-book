"""Integration test against the real Kokoro voice engine. Opt-in:

  LISTEN_BOOK_TEST_ENGINE=1 <venv python> -m unittest discover -s tests -p test_engine.py

Needs an installed prefix (venv + models): LISTEN_BOOK_TEST_PREFIX, else
~/.local/opt/listen-book. Loading the model takes 10-60 s on a CPU.
"""
import _env  # noqa: F401  (must come first)

import os
import time
import unittest
from pathlib import Path

import soundfile as sf

import listen

PREFIX = Path(os.environ.get("LISTEN_BOOK_TEST_PREFIX", str(Path.home() / ".local/opt/listen-book")))
PY = PREFIX / "venv" / "bin" / "python"
MODELS = PREFIX / "models"
INSTALLED = PY.exists() and (MODELS / "kokoro-v1_0.pth").is_file()
WANTED = os.environ.get("LISTEN_BOOK_TEST_ENGINE") == "1"
SKIP_WHY = "set LISTEN_BOOK_TEST_ENGINE=1 to run" if not WANTED else f"no Listen Book install at {PREFIX}"
SYNTH_TIMEOUT = 300  # per request; a hung engine fails the test instead of hanging it


def use_real_engine(case_cls):
    """Point listen at the real engine; restored even if setup fails part-way."""
    saved = (listen.KOKORO_PY, listen.MODELS)

    def restore():
        listen.KOKORO_PY, listen.MODELS = saved

    case_cls.addClassCleanup(restore)
    listen.KOKORO_PY, listen.MODELS = PY, MODELS


@unittest.skipUnless(WANTED and INSTALLED, SKIP_WHY)
class RealEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_real_engine(cls)
        cls.engine = listen.Engine("af_heart", 1.0)
        cls.addClassCleanup(cls.engine.stop)
        t0 = time.time()
        cls.engine.start()
        cls.load_seconds = time.time() - t0

    def synth(self, text, dest, voice):
        return self.engine.synth(text, dest, voice, timeout=SYNTH_TIMEOUT)

    def test_speaks_a_sentence(self):
        dest = _env.TMP / "engine" / "hello.wav"
        self.assertIsNone(self.synth("Hello, this is a short test.", dest, "af_heart"))
        audio, sr = sf.read(str(dest))
        self.assertEqual(sr, 24000)
        self.assertGreater(audio.size / sr, 0.8)
        self.assertGreater(abs(audio).max(), 0.05, "synthesized audio is silent")

    def test_every_offered_voice_works(self):
        for vid, _label in listen.VOICE_CHOICES:
            dest = _env.TMP / "engine" / f"{vid}.wav"
            self.assertIsNone(self.synth("One two three.", dest, vid), vid)

    def test_unknown_voice_is_an_error_not_a_crash(self):
        err = self.synth("Hi there.", _env.TMP / "engine" / "x.wav", "zz_nobody")
        self.assertIn("not installed", err)
        # The engine is still alive and still works afterwards
        self.assertIsNone(self.engine.proc.poll())
        self.assertIsNone(self.synth("Still here.", _env.TMP / "engine" / "y.wav", "af_heart"))

    def test_cached_paragraph_is_not_resynthesized(self):
        dest = _env.TMP / "engine" / "cached.wav"
        self.synth("Cache me please.", dest, "af_heart")
        mtime = dest.stat().st_mtime_ns
        self.assertIsNone(self.synth("Cache me please.", dest, "af_heart"))
        self.assertEqual(dest.stat().st_mtime_ns, mtime)


@unittest.skipUnless(WANTED and INSTALLED, SKIP_WHY)
class RealEngineShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_real_engine(cls)

    def test_quit_exits_cleanly(self):
        engine = listen.Engine("af_heart", 1.0)
        engine.start()
        engine.stop()
        self.assertEqual(engine.proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

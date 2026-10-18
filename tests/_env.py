"""Test environment, imported first by every test module.

Points Listen Book at throwaway config/cache directories and at the fakes in
tests/fakes/ (voice worker, pw-cat, pw-play, wpctl), so tests never touch the
real settings, cache or speakers. Must run before `listen` is imported:
its paths are read at import time.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
FAKES = TESTS / "fakes"
TMP = Path(tempfile.mkdtemp(prefix="listen-book-test-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

os.environ["LISTEN_BOOK_CACHE"] = str(TMP / "cache")
os.environ["LISTEN_BOOK_CONFIG"] = str(TMP / "config")
os.environ["LISTEN_BOOK_PYTHON"] = str(FAKES / "python")
os.environ["LISTEN_BOOK_MODELS"] = str(TMP / "models")
os.environ["FAKE_REAL_PYTHON"] = sys.executable
os.environ["FAKE_AUDIO_DIR"] = str(TMP / "audio")
os.environ["PATH"] = f"{FAKES}:{os.environ['PATH']}"
os.environ.pop("LISTEN_BOOK_AVOID_SINKS", None)
(TMP / "audio").mkdir()

sys.path.insert(0, str(REPO / "app"))


def reset_audio() -> Path:
    """Empty the fake audio recordings; returns their directory."""
    d = TMP / "audio"
    for f in d.iterdir():
        f.unlink()
    return d


def write_book(name: str, paragraphs: list[str]) -> Path:
    p = TMP / "books" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return p

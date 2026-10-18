"""Unit tests: text extraction, output selection, audio processing, and the
command line (with the fake voice worker and fake PipeWire tools)."""
import _env  # noqa: F401  (must come first)

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import listen
import listen_gui
import listen_paths

LISTEN_PY = str(_env.REPO / "app" / "listen.py")


class TextTests(unittest.TestCase):
    def test_markdown_markup_removed(self):
        md = "# Chapter One\n\nShe said **no** and *left*. See [the map](x.png) and `code`.\n\n> Quoted line."
        text = listen.from_markdown(md)
        self.assertEqual(text, "Chapter One\n\nShe said no and left. See the map and code.\n\nQuoted line.")

    def test_code_blocks_tables_and_rules_skipped(self):
        md = "Before.\n\n```\nprint('x')\n```\n\n| a | b |\n|---|---|\n\n---\n\nAfter."
        self.assertEqual(listen.split_paragraphs(listen.from_markdown(md)), ["Before.", "After."])

    def test_frontmatter_removed(self):
        path = _env.write_book("fm.md", ["---\ntitle: X\nstatus: draft\n---", "Real text here."])
        self.assertEqual(listen.split_paragraphs(listen.extract_text(path)), ["Real text here."])

    def test_lines_starting_with_status_are_kept(self):
        # Regression: the private version dropped any line beginning "Status"
        md = "Status meant everything to him.\n\nCross-reference the maps, she said."
        self.assertEqual(len(listen.split_paragraphs(listen.from_markdown(md))), 2)

    def test_html_entities_and_paragraphs(self):
        html = "<h1>Title</h1><p>Fish &amp; chips&nbsp;tonight.</p><script>x()</script><p>Second.</p>"
        self.assertEqual(listen.split_paragraphs(listen.from_html(html)),
                         ["Title", "Fish & chips tonight.", "Second."])

    def test_scene_breaks_are_not_spoken(self):
        self.assertEqual(listen.split_paragraphs("One here.\n\n* * *\n\n#\n\nTwo here."), ["One here.", "Two here."])

    def test_opening_scene_break_is_not_front_matter(self):
        # Regression: text between an opening "---" and a later "---" was dropped
        md = "---\n\nThe storm broke at dawn.\n\n---\n\nBy noon it had passed."
        self.assertEqual(listen.split_paragraphs(listen.from_markdown(listen.strip_frontmatter(md))),
                         ["The storm broke at dawn.", "By noon it had passed."])

    def test_bom_and_utf16_files(self):
        p = _env.TMP / "books" / "bom.md"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes("\ufeff---\ntitle: X\n---\n\nBody text here.".encode("utf-8"))
        self.assertEqual(listen.split_paragraphs(listen.extract_text(p)), ["Body text here."])
        u = _env.TMP / "books" / "wide.txt"
        u.write_text("Sixteen bit text here.", encoding="utf-16")
        self.assertEqual(listen.split_paragraphs(listen.extract_text(u)), ["Sixteen bit text here."])

    def test_long_paragraph_split_at_sentences(self):
        sentence = "This sentence is exactly fifty characters long ok. "
        parts = listen.split_paragraphs(sentence * 100)
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(p) <= listen.MAX_UNIT_CHARS for p in parts))
        self.assertTrue(all(p.endswith("ok.") for p in parts))
        self.assertEqual(" ".join(parts), (sentence * 100).strip())

    def test_overlong_sentence_split_at_spaces(self):
        parts = listen.split_long("word " * 1000)
        self.assertTrue(all(len(p) <= listen.MAX_UNIT_CHARS for p in parts))
        self.assertEqual(" ".join(parts).split(), ["word"] * 1000)

    def test_unclosed_fence_does_not_swallow_the_book(self):
        md = "Before the fence.\n\n```\n\nAfter the fence, still the book."
        # The lone ``` line is kept as text but has nothing to speak, so it drops out
        self.assertEqual(listen.split_paragraphs(listen.from_markdown(md)),
                         ["Before the fence.", "After the fence, still the book."])

    def test_tilde_fences_setext_and_numbers(self):
        md = "Title here\n==========\n\n~~~\ncode()\n~~~\n\n1984\n\nThe end."
        self.assertEqual(listen.split_paragraphs(listen.from_markdown(md)), ["Title here", "1984", "The end."])

    def test_utf16_without_bom(self):
        p = _env.TMP / "books" / "nobom.txt"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes("No byte order mark here.".encode("utf-16-le"))
        self.assertEqual(listen.split_paragraphs(listen.extract_text(p)), ["No byte order mark here."])

    def test_cache_path_depends_on_voice_speed_and_text(self):
        a = listen.cache_path("af_heart", 1.0, "Hello")
        self.assertEqual(a, listen.cache_path("af_heart", 1.0, "Hello"))
        self.assertNotEqual(a, listen.cache_path("am_onyx", 1.0, "Hello"))
        self.assertNotEqual(a, listen.cache_path("af_heart", 0.9, "Hello"))
        self.assertNotEqual(a, listen.cache_path("af_heart", 1.0, "Hello!"))


class EspeakPathLimitTests(unittest.TestCase):
    """espeak-ng truncates its data path at 159 characters and exits the process."""

    def test_limit_boundary(self):
        base = _env.TMP / "espeak"
        base.mkdir(exist_ok=True)
        ok = "/" + "a" * (listen_paths.ESPEAK_PATH_MAX - 1)
        self.assertIsNone(listen_paths.espeak_path_problem(ok))
        self.assertIn("too long", listen_paths.espeak_path_problem(ok + "b"))

    def test_symlink_does_not_hide_a_long_target(self):
        target = _env.TMP / ("t" * 160)
        target.mkdir(exist_ok=True)
        link = _env.TMP / "short"
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        self.assertIn("too long", listen_paths.espeak_path_problem(link))


class OutputSelectionTests(unittest.TestCase):
    """tests/fakes/wpctl: analog 52, HDMI 60, and a default virtual sink 70."""

    def setUp(self):
        self.sinks = listen.list_sinks()

    def test_only_audio_sinks_parsed(self):
        self.assertEqual([s["id"] for s in self.sinks], [52, 60, 70])
        self.assertEqual(self.sinks[2]["name"], "recorder-null")
        self.assertTrue(self.sinks[2]["default"])

    def test_default_output_used_without_target(self):
        self.assertEqual(listen.pick_play_target("default", set(), self.sinks)["name"], "")

    def test_avoided_default_falls_back_to_real_output(self):
        t = listen.pick_play_target("default", {"recorder-null"}, self.sinks)
        self.assertEqual(t["name"], "alsa_output.pci-0000_00_1b.0.analog-stereo")

    def test_explicit_avoided_sink_refused(self):
        with self.assertRaises(SystemExit):
            listen.pick_play_target("70", {"recorder-null"}, self.sinks)

    def test_named_and_numbered_devices(self):
        self.assertIn("hdmi", listen.pick_play_target("hdmi", set(), self.sinks)["name"])
        self.assertIn("analog", listen.pick_play_target("52", set(), self.sinks)["name"])
        self.assertIsNone(listen.pick_play_target("none", set(), self.sinks))
        with self.assertRaises(SystemExit):
            listen.pick_play_target("bluetooth", set(), self.sinks)

    def test_avoid_matching_ignores_case_and_accepts_id(self):
        # Regression: these all used to play into the avoided default sink
        for entry in ["Recorder-Null", "70", "recorder", "RECORDER NULL SINK"]:
            t = listen.pick_play_target("default", {entry}, self.sinks)
            self.assertNotEqual(t["name"], "recorder-null", entry)

    def test_avoid_list_pins_explicit_target(self):
        # With an avoid list, "default" resolves to a named sink, so a later
        # change of the default output cannot redirect speech
        t = listen.pick_play_target("default", {"something-else"}, self.sinks)
        self.assertEqual(t["name"], "recorder-null")

    def test_unmatched_avoid_entries_reported(self):
        self.assertEqual(listen.unmatched_avoid_entries({"rec", "recorder", "typo-sink"}, self.sinks),
                         ["rec", "typo-sink"])

    def test_device_id_beats_substring(self):
        self.assertEqual(listen.pick_play_target("60", set(), self.sinks)["id"], 60)
        self.assertEqual(listen.pick_play_target("52", set(), self.sinks)["id"], 52)

    def test_avoid_list_merges_option_and_environment(self):
        os.environ["LISTEN_BOOK_AVOID_SINKS"] = "a, b"
        try:
            self.assertEqual(listen.avoid_list(["c"]), {"a", "b", "c"})
        finally:
            del os.environ["LISTEN_BOOK_AVOID_SINKS"]


class AudioTests(unittest.TestCase):
    def tone(self, hz, seconds=0.5):
        t = np.arange(int(24000 * seconds)) / 24000
        return (0.25 * np.sin(2 * np.pi * hz * t)).astype(np.float32)

    def test_trim_keeps_speech_and_pad(self):
        y = np.concatenate([np.zeros(24000), self.tone(440), np.zeros(24000)])
        out = listen_gui.trim_silence(y)
        self.assertAlmostEqual(out.size / 24000, 0.5 + 2 * listen_gui.TRIM_PAD_MS / 1000, delta=0.01)

    def test_silence_becomes_short_gap(self):
        self.assertEqual(listen_gui.trim_silence(np.zeros(48000)).size, int(24000 * 0.05))

    def test_zero_db_band_is_bypassed(self):
        b = listen_gui.Biquad()
        b.set(listen_gui.biquad_coeffs("low", 24000, 120, 0.0), 0.0)
        x = self.tone(100)
        self.assertIs(b.process(x), x)

    def test_bass_boost_lifts_low_not_high(self):
        def rms_after(hz):
            b = listen_gui.Biquad()
            b.set(listen_gui.biquad_coeffs("low", 24000, 120, 6.0), 6.0)
            y = b.process(self.tone(hz, 1.0))[12000:]
            return np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(self.tone(hz, 1.0)[12000:] ** 2))
        self.assertAlmostEqual(20 * np.log10(rms_after(40)), 6.0, delta=0.6)
        self.assertAlmostEqual(20 * np.log10(rms_after(5000)), 0.0, delta=0.3)


class SettingsTests(unittest.TestCase):
    def tearDown(self):
        listen.STATE_PATH.unlink(missing_ok=True)

    def test_corrupt_state_files_give_defaults(self):
        listen.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        for raw in [b"[]", b"null", b'"x"', b"{bad json", b"\xff\xfe\x00garbage"]:
            listen.STATE_PATH.write_bytes(raw)
            self.assertEqual(listen.load_state(), {}, raw)

    def test_malformed_values_fall_back(self):
        d = {"volume": "loud", "bass": None, "mid": float("nan"), "treble": 99}
        self.assertEqual(listen_gui._num(d, "volume", 0.85, 0, 1), 0.85)
        self.assertEqual(listen_gui._num(d, "bass", 0.0, -12, 12), 0.0)
        self.assertEqual(listen_gui._num(d, "mid", 0.0, -12, 12), 0.0)
        self.assertEqual(listen_gui._num(d, "treble", 0.0, -12, 12), 12)


class CacheTests(unittest.TestCase):
    def setUp(self):
        # A private cache: trimming the shared one would delete other tests' files
        self.saved = listen.CACHE
        listen.CACHE = _env.TMP / "trim-cache"
        self.addCleanup(setattr, listen, "CACHE", self.saved)

    def test_trim_removes_oldest_and_partials(self):
        d = listen.CACHE / "trimtest"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            f = d / f"{i}.wav"
            f.write_bytes(b"x" * 100_000)
            os.utime(f, (1000 + i, 1000 + i))
        (d / "x.partial.wav").write_bytes(b"x")
        listen.trim_cache(limit_mb=0.5)  # 10 x 100 kB > 0.5 MB
        left = sorted(int(f.stem) for f in d.glob("*.wav"))
        self.assertTrue(left and left[-1] == 9 and left[0] > 0, left)
        self.assertFalse((d / "x.partial.wav").exists())


class EngineProtocolTests(unittest.TestCase):
    """The engine wrapper against the fake worker."""

    def setUp(self):
        self.engine = listen.Engine("af_heart", 1.0)

    def tearDown(self):
        self.engine.stop()
        os.environ.pop("FAKE_HANG_START", None)

    def test_stray_output_does_not_shift_replies(self):
        self.engine.start()
        d = _env.TMP / "proto"
        self.assertIsNone(self.engine.synth("NOISE first here.", d / "a.wav"))
        self.assertIsNone(self.engine.synth("Second one.", d / "b.wav"))
        self.assertTrue((d / "b.wav").is_file())

    def test_hung_paragraph_times_out(self):
        self.engine.start()
        t0 = time.time()
        err = self.engine.synth("HANG forever.", _env.TMP / "proto" / "h.wav", timeout=1.0)
        self.assertIn("stopped responding", err)
        self.assertFalse(self.engine.alive())
        self.assertLess(time.time() - t0, 2.5, "a timeout must not add a QUIT wait")

    def test_hung_startup_times_out(self):
        os.environ["FAKE_HANG_START"] = "1"
        with self.assertRaises(listen.EngineError) as cm:
            self.engine.start(timeout=1.0)
        self.assertIn("did not start", str(cm.exception))


def run_cli(*args, timeout=60):
    return subprocess.run([sys.executable, LISTEN_PY, *args], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "DISPLAY": "", "WAYLAND_DISPLAY": ""})


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.audio = _env.reset_audio()

    def test_cli_plays_each_paragraph_on_default_output(self):
        book = _env.write_book("cli.md", ["First paragraph.", "Second paragraph."])
        r = run_cli(str(book), "--cli")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = (self.audio / "pw-play.args").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--target", calls[0])

    def test_no_play_fills_cache_and_joins_out(self):
        book = _env.write_book("np.md", ["Alpha here.", "Beta here.", "Gamma here."])
        out = _env.TMP / "joined.wav"
        r = run_cli(str(book), "--no-play", "--out", str(out), "--voice", "am_onyx")
        self.assertEqual(r.returncode, 0, r.stderr)
        audio, sr = sf.read(str(out))
        self.assertEqual(sr, 24000)
        self.assertAlmostEqual(audio.size / sr, 0.9, delta=0.02)
        self.assertFalse((self.audio / "pw-play.args").exists())

    def test_synthesis_error_reported_with_paragraph(self):
        book = _env.write_book("err.md", ["Fine here.", "FAILME here."])
        r = run_cli(str(book), "--no-play")
        self.assertEqual(r.returncode, 1)
        self.assertIn("paragraph 2: simulated failure", r.stderr)

    def test_engine_crash_reported(self):
        book = _env.write_book("die.md", ["DIEHERE now."])
        r = run_cli(str(book), "--no-play")
        self.assertEqual(r.returncode, 1)
        self.assertIn("voice engine stopped", r.stderr)

    def test_from_and_max_select_paragraphs(self):
        book = _env.write_book("fm2.md", ["One here.", "Two here.", "Three here.", "Four here."])
        r = run_cli(str(book), "--cli", "--from", "2", "--max", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[fm2.md 2] Two here.", r.stdout)
        self.assertIn("[fm2.md 3] Three here.", r.stdout)
        self.assertNotIn("Four", r.stdout)

    def test_bad_arguments(self):
        book = _env.write_book("b.md", ["Text here."])
        self.assertNotEqual(run_cli("missing.md", "--cli").returncode, 0)
        self.assertNotEqual(run_cli(str(book), "--from", "0").returncode, 0)
        self.assertNotEqual(run_cli(str(book), "--speed", "0").returncode, 0)
        r = run_cli(str(book), "--cli", "--device", "70", "--avoid-sink", "recorder-null")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("avoid list", r.stderr)

    def test_engine_missing_model_message(self):
        # With the real worker script but no model, startup fails with a clear message
        real = Path(sys.executable)
        r = subprocess.run([sys.executable, LISTEN_PY, "x.md", "--no-play"], capture_output=True, text=True,
                           env={**os.environ, "LISTEN_BOOK_PYTHON": str(real)}, timeout=60)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""Player window tests, with the fake voice worker and fake pw-cat.

Need a display; run headless with:  xvfb-run -a <venv python> -m unittest discover -s tests
Each test runs the real run_gui() and drives it from Tk timers through a list
of steps: wait until the window shows something, then act (press a button,
pick a voice, click a paragraph). Any exception inside a Tk callback fails
the test; Tk would otherwise only print it.
"""
import _env  # noqa: F401  (must come first)

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import listen
import listen_gui

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def labels(widget) -> list[str]:
    out = []
    for child in widget.winfo_children():
        if child.winfo_class() == "Label":
            out.append(str(child.cget("text")))
        out += labels(child)
    return out


def find(widget, cls: str, text: str | None = None):
    for child in widget.winfo_children():
        if child.winfo_class() == cls and (text is None or child.cget("text") == text):
            return child
        hit = find(child, cls, text)
        if hit is not None:
            return hit
    return None


def press(text):
    return lambda root: find(root, "Button", text).invoke()


def choose_voice(text):
    return lambda root: find(root, "Radiobutton", text).invoke()


def click_paragraph(i):
    def act(root):
        body = find(root, "Text")
        first = body.tag_ranges(f"u{i}")[0]
        body.see(first)
        body.update_idletasks()
        x, y, _w, _h = body.bbox(first)
        body.event_generate("<Button-1>", x=x + 2, y=y + 2)
    return act


def status(root) -> str:
    return labels(root)[0]


def shows(prefix):
    return lambda root: status(root).startswith(prefix)


@unittest.skipUnless(HAVE_DISPLAY, "needs a display (use xvfb-run)")
class PlayerWindowTests(unittest.TestCase):
    def setUp(self):
        self.audio = _env.reset_audio()
        listen.STATE_PATH.unlink(missing_ok=True)

    def run_window(self, paragraphs, steps, target="", voice="af_heart", timeout=20.0,
                   state=None, book_name=None, start_at=None):
        """Open the window on a book and run steps = [(condition, action or None), ...].

        Closes the window after the last step (or at timeout). Returns a dict:
        reached (all steps done), status at the end, step_times (seconds from
        start at which each condition held), close_seconds.
        """
        book = _env.write_book(book_name or f"{self.id().split('.')[-1]}.md", paragraphs)
        units, files = listen_gui.load_units([str(book)], voice, 1.0)
        if state is not None:
            listen.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            listen.STATE_PATH.write_text(state(files) if callable(state) else state)
        engine = listen.Engine(voice, 1.0)
        out = {"reached": False, "status": "", "step_times": [], "close_seconds": None}
        errors: list[str] = []
        t0 = time.time()
        deadline = t0 + timeout
        real_tk = listen_gui.tk.Tk

        class TkSpy(real_tk):
            def __init__(spy, *a, **k):
                super().__init__(*a, **k)
                spy.report_callback_exception = lambda exc, val, tb: errors.append(f"{exc.__name__}: {val}")
                todo = list(steps)

                def poll():
                    while todo and todo[0][0](spy):
                        _cond, act = todo.pop(0)
                        out["step_times"].append(round(time.time() - t0, 2))
                        if act:
                            act(spy)
                    if not todo or time.time() > deadline:
                        out["reached"] = not todo
                        out["status"] = status(spy)
                        c0 = time.time()
                        spy.tk.call(spy.wm_protocol("WM_DELETE_WINDOW"))
                        out["close_seconds"] = time.time() - c0
                    else:
                        spy.after(50, poll)

                spy.after(50, poll)

        try:
            with mock.patch.object(listen_gui.tk, "Tk", TkSpy):
                listen_gui.run_gui(engine, units, target, "test", files=files, voice=voice, speed=1.0,
                                   start_at=start_at)
        finally:
            engine.stop()
        self.assertEqual(errors, [], "exception in a Tk callback")
        return out

    # ── playback ──────────────────────────────────────────────────────────────

    def test_plays_every_paragraph_then_finishes(self):
        r = self.run_window(["One here.", "Two here.", "Three here."], [(shows("FINISHED"), None)])
        self.assertTrue(r["reached"], r)
        raw = (self.audio / "pw-cat.raw").stat().st_size
        self.assertGreaterEqual(raw, 3 * int(0.3 * 24000) * 8)  # 3 x 0.3 s float32 stereo
        self.assertNotIn("--target", (self.audio / "pw-cat.args").read_text())

    def test_explicit_output_is_targeted(self):
        r = self.run_window(["Only one."], [(shows("FINISHED"), None)], target="some-sink")
        self.assertTrue(r["reached"])
        self.assertIn("--target some-sink", (self.audio / "pw-cat.args").read_text())

    def test_play_after_finished_starts_again(self):
        r = self.run_window(["One here.", "Two here."],
                            [(shows("FINISHED"), press("Play")), (shows("PLAYING"), None), (shows("FINISHED"), None)])
        self.assertTrue(r["reached"], r)

    def test_voice_change_after_finished_does_not_crash(self):
        r = self.run_window(["One here.", "Two here."],
                            [(shows("FINISHED"), choose_voice("Male — Onyx")), (lambda root: True, None)])
        self.assertTrue(r["reached"], r)

    def test_click_far_ahead_is_synthesized_first(self):
        # 30 paragraphs at 0.3 s each: waiting behind the queue would take ~8 s
        os.environ["FAKE_SYNTH_MS"] = "300"
        try:
            paras = [f"Seek test paragraph {i} here." for i in range(1, 31)]

            def at_or_after_26(root):
                # The label refreshes every 400 ms; a 0.3 s paragraph can be skipped
                shown = int(labels(root)[1].split()[1])
                return shown >= 26

            r = self.run_window(paras, [(shows("PLAYING"), click_paragraph(25)),
                                        (at_or_after_26, None)], timeout=25)
        finally:
            del os.environ["FAKE_SYNTH_MS"]
        self.assertTrue(r["reached"], r)
        self.assertLess(r["step_times"][1] - r["step_times"][0], 3.0, r)

    def test_click_behind_the_window_is_synthesized(self):
        # Start mid-book (synthesis queued from there), then, while playing,
        # click an earlier paragraph that was never synthesized. The look-ahead
        # refill only reaches forward, so only the click's own re-queue helps.
        os.environ["FAKE_SYNTH_MS"] = "200"
        try:
            paras = [f"Backward seek paragraph {i} here." for i in range(1, 31)]

            def playing_early(root):
                # Started at 21; anything before 15 proves the backward jump played.
                # (A narrower check races the 400 ms label refresh.)
                return status(root) == "PLAYING" and int(labels(root)[1].split()[1]) < 15

            r = self.run_window(paras, [(shows("PLAYING"), click_paragraph(1)), (playing_early, None)],
                                start_at=20, timeout=15)
        finally:
            del os.environ["FAKE_SYNTH_MS"]
        self.assertTrue(r["reached"], r)
        self.assertLess(r["step_times"][1] - r["step_times"][0], 3.0, r)

    def test_from_starts_there_and_saves_whole_book_place(self):
        r = self.run_window(["One here.", "Two here.", "Three here.", "Four here."],
                            [(shows("FINISHED"), None)], start_at=2)
        self.assertTrue(r["reached"], r)
        self.assertEqual(json.loads(listen.STATE_PATH.read_text())["index"], 4)
        raw = (self.audio / "pw-cat.raw").stat().st_size
        self.assertLess(raw, 3 * int(0.3 * 24000) * 8, "played from paragraph 3, not 1")

    def test_empty_book_says_so(self):
        r = self.run_window(["* * *"], [(shows("NO TEXT LOADED"), None)])
        self.assertTrue(r["reached"], r)

    def test_synthesis_stays_a_window_ahead(self):
        # Opening a long book must not synthesize all of it up front
        paras = [f"Look-ahead test paragraph {i} here." for i in range(1, 61)]
        r = self.run_window(paras, [(shows("PLAYING"), press("Stop")),
                                    (lambda root: time.sleep(1.0) or True, None)])
        self.assertTrue(r["reached"], r)
        made = sum(listen.cache_path("af_heart", 1.0, p).is_file() for p in paras)
        self.assertLessEqual(made, listen_gui.LOOKAHEAD + 2)

    # ── errors are shown, never an endless "loading" ──────────────────────────

    def test_synthesis_error_is_shown(self):
        r = self.run_window(["Fine here.", "FAILME here.", "Never reached."],
                            [(shows("ERROR at paragraph 2"), None)])
        self.assertTrue(r["reached"], r)
        self.assertIn("simulated failure", r["status"])

    def test_engine_crash_is_shown(self):
        r = self.run_window(["DIEHERE now."], [(shows("VOICE ENGINE:"), None)])
        self.assertTrue(r["reached"], r)

    def test_play_restarts_a_crashed_engine(self):
        (self.audio / "died-once").unlink(missing_ok=True)
        r = self.run_window(["DIEONCE here.", "Two here."],
                            [(shows("VOICE ENGINE:"), press("Play")), (shows("FINISHED"), None)])
        self.assertTrue(r["reached"], r)

    def test_engine_that_cannot_start_is_shown(self):
        with mock.patch.object(listen, "KOKORO_PY", Path("/etc/hostname")):  # exists, not executable
            r = self.run_window(["One here."], [(shows("VOICE ENGINE: cannot start"), None)], timeout=8)
        self.assertTrue(r["reached"], r)

    def test_unreadable_cache_is_shown_and_window_keeps_running(self):
        locked = listen.CACHE / "af_heart"
        locked.mkdir(parents=True, exist_ok=True)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        r = self.run_window(["One here."], [(lambda root: "cannot read the audio cache" in status(root)
                                             or status(root).startswith("ERROR"), None),
                                            (lambda root: time.sleep(1) or True, None)], timeout=10)
        self.assertTrue(r["reached"], r)

    def test_late_failure_for_old_voice_is_dropped(self):
        # The old voice's job fails 1.5 s after the switch; it must not mark
        # the paragraph failed for the new voice (stale generation)
        r = self.run_window(["FAILFOR:bf_emma here.", "Two here."],
                            [(shows("PREPARING"), choose_voice("Female — Heart")),
                             (shows("FINISHED"), None)], voice="bf_emma")
        self.assertTrue(r["reached"], r)

    def test_corrupt_cached_audio_is_regenerated(self):
        text = "Cached but corrupt."
        bad = listen.cache_path("af_heart", 1.0, text)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not audio" * 500)
        r = self.run_window([text], [(shows("ERROR at paragraph 1"), press("Play")), (shows("FINISHED"), None)])
        self.assertTrue(r["reached"], r)

    def test_missing_pw_cat_is_shown(self):
        real = listen_gui.subprocess.Popen

        def popen(cmd, *a, **k):
            if cmd[0] == "pw-cat":
                raise FileNotFoundError(2, "No such file or directory")
            return real(cmd, *a, **k)

        with mock.patch.object(listen_gui.subprocess, "Popen", popen):
            r = self.run_window(["One here."], [(shows("AUDIO OUTPUT: cannot start pw-cat"), None)])
        self.assertTrue(r["reached"], r)

    def test_pw_cat_dying_repeatedly_is_shown(self):
        os.environ["FAKE_PWCAT_MODE"] = "exit"
        try:
            r = self.run_window(["One here.", "Two here."], [(shows("AUDIO OUTPUT: audio output keeps failing"), None)])
        finally:
            del os.environ["FAKE_PWCAT_MODE"]
        self.assertTrue(r["reached"], r)

    def test_close_does_not_hang_when_pw_cat_stalls(self):
        # Regression: closing deadlocked while the player was blocked writing
        os.environ["FAKE_PWCAT_MODE"] = "stall"
        try:
            paras = [f"Paragraph {i} here." for i in range(20)]
            r = self.run_window(paras, [(shows("PLAYING"), None), (lambda root: time.sleep(1.5) or True, None)])
        finally:
            del os.environ["FAKE_PWCAT_MODE"]
        self.assertTrue(r["reached"], r)
        self.assertLess(r["close_seconds"], 5.0)

    def test_window_opens_before_engine_is_ready(self):
        os.environ["FAKE_HANG_START"] = "1"
        try:
            r = self.run_window(["One here."], [(shows("LOADING VOICE ENGINE"), None)], timeout=10)
        finally:
            del os.environ["FAKE_HANG_START"]
        self.assertTrue(r["reached"], r)
        self.assertLess(r["step_times"][0], 2.0)
        self.assertLess(r["close_seconds"], 2.0)

    # ── settings ──────────────────────────────────────────────────────────────

    def test_close_saves_place_and_settings(self):
        r = self.run_window(["One here.", "Two here."], [(shows("FINISHED"), None)])
        self.assertTrue(r["reached"])
        state = json.loads(listen.STATE_PATH.read_text())
        self.assertEqual(state["index"], 2)  # past the last paragraph = finished
        self.assertEqual(state["voice"], "af_heart")

    def test_same_book_resumes_paused_at_saved_place(self):
        r = self.run_window(["One here.", "Two here.", "Three here."],
                            [(shows("PAUSED — press Play to resume"), None)],
                            state=lambda files: json.dumps({"files": files, "index": 2}))
        self.assertTrue(r["reached"], r)
        self.assertFalse((self.audio / "pw-cat.raw").exists(), "must not start playing on resume")

    def test_absurd_saved_window_size_still_opens(self):
        bad = json.dumps({"geometry": "99999999x99999999+0+0"})
        r = self.run_window(["One here."], [(shows("FINISHED"), None)], state=bad)
        self.assertTrue(r["reached"], r)

    def test_corrupt_settings_still_open(self):
        bad = json.dumps({"files": "nope", "index": "abc", "volume": "loud", "geometry": "foo", "follow": None})
        r = self.run_window(["One here."], [(shows("FINISHED"), None)], state=bad)
        self.assertTrue(r["reached"], r)

    def test_unwritable_settings_do_not_freeze_the_window(self):
        with mock.patch.object(listen, "STATE_PATH", Path("/proc/listen-book-test/state.json")):
            r = self.run_window(["One here.", "Two here."], [(shows("FINISHED"), None)])
        self.assertTrue(r["reached"], r)


@unittest.skipUnless(HAVE_DISPLAY, "needs a display (use xvfb-run)")
class WindowSignalTests(unittest.TestCase):
    def test_sigterm_saves_state_and_stops_worker(self):
        audio = _env.reset_audio()
        listen.STATE_PATH.unlink(missing_ok=True)
        book = _env.write_book("sig.md", [f"Paragraph {i} here." for i in range(40)])
        proc = subprocess.Popen([sys.executable, str(_env.REPO / "app" / "listen.py"), str(book)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 20
        while time.time() < deadline and not (audio / "pw-cat.raw").exists():
            time.sleep(0.2)
        self.assertTrue((audio / "pw-cat.raw").exists(), "playback never started")
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(listen.STATE_PATH.exists())
        worker_pid = int((audio / "worker.pid").read_text())
        with self.assertRaises(ProcessLookupError, msg="voice worker left running"):
            os.kill(worker_pid, 0)


if __name__ == "__main__":
    unittest.main()

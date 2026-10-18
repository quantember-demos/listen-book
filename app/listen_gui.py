#!/usr/bin/env python3
"""Player window for listen.py: play / pause / stop, click-to-seek, voices,
volume and bass / mid / treble.

Threads:
  Tk (main)    the window; the only thread that touches widgets
  prefetch     owns the voice engine: starts it, then synthesizes a window of
               paragraphs ahead of the one playing
  player       reads cached audio, applies volume/EQ, streams to one pw-cat

Every synthesis job carries a generation number. Opening a book or changing
voice starts a new generation, so a result that arrives late for the old
book or voice is dropped instead of being applied to the new one.
"""
from __future__ import annotations

import gc
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog

import numpy as np
import soundfile as sf

from listen import (
    DEFAULT_VOICE,
    VOICE_CHOICES,
    WORKER_LOG,
    Engine,
    EngineError,
    cache_path,
    extract_text,
    load_state,
    save_state,
    speakable,
    split_paragraphs,
)

SR = 24000
BLOCK = 2048
GAP_MS = 60
TRIM_THRESH = 0.012
TRIM_PAD_MS = 40
LOOKAHEAD = 12  # paragraphs synthesized ahead of the one playing


def load_units(paths: list[str], voice: str, speed: float) -> tuple[list[tuple[str, str, Path]], list[str]]:
    units: list[tuple[str, str, Path]] = []
    files: list[str] = []
    for raw in paths:
        src = Path(raw)
        if not src.is_file():
            continue
        files.append(str(src.resolve()))
        for text in split_paragraphs(extract_text(src)):
            units.append((src.name, text, cache_path(voice, speed, text)))
    return units, files


def trim_silence(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """Cut Kokoro's leading/trailing near-silence, keeping a short pad."""
    if y.ndim > 1:
        y = np.mean(y, axis=1)
    y = y.astype(np.float32, copy=False)
    hits = np.where(np.abs(y) > TRIM_THRESH)[0]
    if hits.size == 0:
        return np.zeros(int(sr * 0.05), dtype=np.float32)
    pad = int(sr * TRIM_PAD_MS / 1000)
    a = max(0, int(hits[0]) - pad)
    b = min(y.size, int(hits[-1]) + pad)
    return y[a:b]


def biquad_coeffs(kind: str, sr: float, f0: float, gain_db: float, q: float = 0.7):
    """RBJ audio-EQ-cookbook shelf ("low"/"high") or peaking ("peak") filter."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sr
    cosw, sinw = np.cos(w0), np.sin(w0)
    alpha = sinw / (2.0 * q)
    if kind == "low":
        b0 = A * ((A + 1) - (A - 1) * cosw + 2 * np.sqrt(A) * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cosw)
        b2 = A * ((A + 1) - (A - 1) * cosw - 2 * np.sqrt(A) * alpha)
        a0 = (A + 1) + (A - 1) * cosw + 2 * np.sqrt(A) * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cosw)
        a2 = (A + 1) + (A - 1) * cosw - 2 * np.sqrt(A) * alpha
    elif kind == "high":
        b0 = A * ((A + 1) + (A - 1) * cosw + 2 * np.sqrt(A) * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cosw)
        b2 = A * ((A + 1) + (A - 1) * cosw - 2 * np.sqrt(A) * alpha)
        a0 = (A + 1) - (A - 1) * cosw + 2 * np.sqrt(A) * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cosw)
        a2 = (A + 1) - (A - 1) * cosw - 2 * np.sqrt(A) * alpha
    else:
        b0 = 1 + alpha * A
        b1 = -2 * cosw
        b2 = 1 - alpha * A
        a0 = 1 + alpha / A
        a1 = -2 * cosw
        a2 = 1 - alpha / A
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class Biquad:
    def __init__(self):
        self.gain_db = 0.0
        self.set((1.0, 0.0, 0.0, 0.0, 0.0), 0.0)

    def set(self, coeffs, gain_db: float):
        self.b0, self.b1, self.b2, self.a1, self.a2 = coeffs
        self.gain_db = gain_db
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        # A 0 dB band is the identity; skipping it saves most of the CPU,
        # since this loop runs per sample in Python
        if self.gain_db == 0.0:
            return x
        out = np.empty_like(x)
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        for i, s in enumerate(x):
            y = b0 * s + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, s
            y2, y1 = y1, y
            out[i] = y
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


class Player:
    """Plays cached paragraphs in order through one long-lived pw-cat."""

    PIPE_FAILS = 3  # pw-cat deaths within PIPE_WINDOW seconds before giving up
    PIPE_WINDOW = 5.0

    def __init__(self, target_name: str):
        self.target = target_name  # "" = PipeWire default output
        self.lock = threading.Lock()
        self.paused = threading.Event()
        self.stop_flag = threading.Event()
        self.quit_flag = threading.Event()
        self.paused.set()
        self.volume = 0.85
        self.bass = 0.0
        self.mid = 0.0
        self.treble = 0.0
        self.units: list[tuple[str, str, Path]] = []
        self.failed: dict[int, str] = {}  # unit index -> why it cannot play
        self.error = ""  # output problem (pw-cat), shown by the window
        self.index = 0
        self.status = "stopped"
        self._abort_unit = False
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._pipe_deaths: deque[float] = deque(maxlen=self.PIPE_FAILS)
        self._thread: threading.Thread | None = None
        self.bass_f = Biquad()
        self.mid_f = Biquad()
        self.treble_f = Biquad()
        self._eq_dirty = True

    def set_units(self, units: list[tuple[str, str, Path]], index: int = 0):
        with self.lock:
            self.units = units
            self.failed = {}
            self.index = max(0, min(index, len(units) - 1)) if units else 0
            self._abort_unit = True

    def seek(self, i: int):
        with self.lock:
            if not self.units:
                return
            self.index = max(0, min(i, len(self.units) - 1))
            self._abort_unit = True

    def play(self):
        with self.lock:
            if self.index >= len(self.units):
                self.index = 0  # Play after the end starts the book again
        self.error = ""
        self._pipe_deaths.clear()
        self.stop_flag.clear()
        self.paused.clear()
        self.status = "playing"
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def pause(self):
        self.paused.set()
        self.status = "paused"

    def stop(self):
        """Stop sound, keep the place (audiobook resume)."""
        self.stop_flag.set()
        self.paused.set()
        self.status = "stopped"
        self._kill_pipe()

    def close(self):
        self.quit_flag.set()
        self.stop_flag.set()
        self.paused.clear()
        # Kill, never close-and-wait, from this thread: if pw-cat stopped
        # reading, the player thread is blocked writing to it, and closing its
        # stdin here would wait on that same write forever
        self._kill_pipe()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._reap_pipe()

    def _kill_pipe(self):
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()

    def _reap_pipe(self):
        """Release pw-cat. Only the player thread (or close() after it ends) calls this."""
        with self._proc_lock:
            p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    def _fail(self, message: str):
        self.error = message
        self.status = "error"
        self.paused.set()

    def _set_status(self, status: str):
        """Status written by the player thread; never overrides a pause or stop
        the Tk thread has just made (that status is the user's, not ours)."""
        with self.lock:
            if not (self.paused.is_set() or self.stop_flag.is_set()):
                self.status = status

    def _pipe_died(self) -> bool:
        """Record a pw-cat death; True (and error set) if it keeps happening."""
        self._reap_pipe()
        now = time.monotonic()
        self._pipe_deaths.append(now)
        if len(self._pipe_deaths) == self.PIPE_FAILS and now - self._pipe_deaths[0] < self.PIPE_WINDOW:
            self._fail("audio output keeps failing (pw-cat exits at once); check --device and PipeWire")
            return True
        return False

    def _open_pipe(self) -> str:
        """Start pw-cat: "ok", "died" (exited at once; retry) or "fatal" (error set)."""
        self._reap_pipe()
        cmd = ["pw-cat", "--playback", "--rate", str(SR), "--channels", "2",
               "--format", "f32", "--media-role", "Speech"]
        if self.target:
            cmd += ["--target", self.target]
        try:
            proc = subprocess.Popen([*cmd, "-"], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._fail(f"cannot start pw-cat: {e.strerror or e}")
            return "fatal"
        with self._proc_lock:
            self._proc = proc
        # A pw-cat that cannot open the output exits within moments. Writes in
        # that moment would still "succeed" into the 64 KB pipe buffer, so give
        # it a short time to fail before trusting it.
        try:
            proc.wait(timeout=0.15)
            return "died"
        except subprocess.TimeoutExpired:
            return "ok"

    def _refresh_eq(self):
        self.bass_f.set(biquad_coeffs("low", SR, 120.0, self.bass, 0.7), self.bass)
        self.mid_f.set(biquad_coeffs("peak", SR, 1000.0, self.mid, 1.0), self.mid)
        self.treble_f.set(biquad_coeffs("high", SR, 6000.0, self.treble, 0.7), self.treble)
        self._eq_dirty = False

    def _write(self, mono: np.ndarray) -> bool:
        """Send audio to pw-cat. False if output is unavailable (error already set)."""
        if self._eq_dirty:
            self._refresh_eq()
        y = self.treble_f.process(self.mid_f.process(self.bass_f.process(mono)))
        y = np.clip(y * float(self.volume), -1.0, 1.0)
        data = np.column_stack((y, y)).astype(np.float32, copy=False).tobytes()
        while not (self.stop_flag.is_set() or self.quit_flag.is_set()):
            if self._proc is None:
                opened = self._open_pipe()
                if opened == "fatal":
                    return False
                if opened == "died":
                    if self._pipe_died():
                        return False
                    continue
            try:
                self._proc.stdin.write(data)
                # A write can "succeed" into the pipe buffer after pw-cat has
                # already exited, so check that it is still running
                if self._proc.poll() is None:
                    return True
            except (BrokenPipeError, ValueError, OSError):
                pass
            if self.stop_flag.is_set() or self.quit_flag.is_set():
                break  # killed by stop/close: not a failure
            if self._pipe_died():
                return False
        self._reap_pipe()
        return False

    def _run(self):
        try:
            self._loop()
        except Exception as e:  # noqa: BLE001 — never let playback die silently
            self._fail(f"playback error: {e}")
        finally:
            self._reap_pipe()

    def _loop(self):
        gap = np.zeros(int(SR * GAP_MS / 1000), dtype=np.float32)
        while not self.quit_flag.is_set():
            if self.stop_flag.is_set():
                self._reap_pipe()
                time.sleep(0.05)
                continue
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            with self.lock:
                i = self.index
                units = self.units
                failed = self.failed.get(i)
                self._abort_unit = False
            if i >= len(units):
                self.status = "finished"
                self.paused.set()
                self._reap_pipe()
                continue
            if failed:
                # Stop here instead of waiting forever for audio that will not come
                self.status = "error"
                self.paused.set()
                continue
            _name, text, wav = units[i]
            if not speakable(text):
                with self.lock:
                    if self.index == i:
                        self.index = i + 1
                continue
            if not wav.is_file() or wav.stat().st_size < 1000:
                self._set_status("loading")
                time.sleep(0.05)
                continue
            try:
                audio, _sr = sf.read(str(wav), dtype="float32")
            except (RuntimeError, OSError):
                wav.unlink(missing_ok=True)
                with self.lock:
                    self.failed[i] = "its cached audio was unreadable and was deleted; Play regenerates it"
                continue
            mono = trim_silence(audio, SR)
            self._set_status("playing")
            pos = 0
            while pos < mono.size and not self.stop_flag.is_set() and not self.quit_flag.is_set():
                if self._abort_unit:
                    break
                if self.paused.is_set():
                    time.sleep(0.05)
                    continue
                n = min(BLOCK, mono.size - pos)
                if not self._write(mono[pos : pos + n]):
                    break
                pos += n
            if pos < mono.size or self._abort_unit:
                continue  # aborted by seek / stop / error (even during the last block): do not advance
            if self._write(gap):
                with self.lock:
                    if self.index == i:
                        self.index = i + 1


class Prefetcher(threading.Thread):
    """Owns the voice engine: starts it, then works through synthesis jobs.

    jobs:  (generation, index, text, dest, voice)
    done:  ("ready",) | ("engine-error", message) | ("unit", generation, index, error or None)
    """

    def __init__(self, engine: Engine):
        super().__init__(daemon=True)
        self.engine = engine
        self.jobs: queue.Queue = queue.Queue()
        self.done: queue.Queue = queue.Queue()
        self.generation = 0
        self.stop_event = threading.Event()

    def run(self):
        if not self.engine.alive():
            try:
                self.engine.start()
            except EngineError as e:
                self.done.put(("engine-error", str(e)))
                return
            except (OSError, ValueError) as e:
                # Expected when the window closed during loading; otherwise a
                # real failure (bad interpreter, unwritable cache) to report
                if not self.stop_event.is_set():
                    self.done.put(("engine-error", f"cannot start: {e}"))
                return
        self.done.put(("ready",))
        while not self.stop_event.is_set():
            try:
                gen, idx, text, dest, voice = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if gen != self.generation:
                continue
            try:
                err = self.engine.synth(text, dest, voice)
            except Exception as e:  # noqa: BLE001 — report per paragraph, keep the thread alive
                err = f"unexpected error: {e}"
            self.done.put(("unit", gen, idx, err))
            if not self.engine.alive() and not self.stop_event.is_set():
                self.done.put(("engine-error", err or "voice engine stopped"))
                return


def _num(d: dict, key: str, default: float, lo: float, hi: float) -> float:
    """A number from saved settings, or the default if missing or malformed."""
    try:
        v = float(d.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v)) if v == v else default  # v == v rejects NaN


def run_gui(engine: Engine, units: list[tuple[str, str, Path]], target_name: str, title: str,
            files: list[str] | None = None, voice: str = DEFAULT_VOICE, speed: float = 1.0,
            start_at: int | None = None) -> int:
    """Open the player window; returns when it is closed.

    start_at: paragraph to start from (--from), instead of the saved place.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    engines = [engine]  # the window may start replacements; all are stopped on exit
    try:
        return _window(engines, units, target_name, title, files, voice, speed, start_at)
    finally:
        for e in engines[1:]:
            e.stop()
        for sig, handler in saved.items():
            signal.signal(sig, handler)  # ours reference the closed window
        # The closed window's widgets and variables are cyclic garbage. Collect
        # them here, on the Tk thread: if the collector ran them down in the
        # player or prefetch thread, Tk would abort the process
        # ("Tcl_AsyncDelete: async handler deleted by the wrong thread").
        gc.collect()


def _window(engines, units, target_name, title, files, voice, speed, start_at) -> int:
    files = [str(Path(f).resolve()) for f in (files or [])]
    title_s = [title]
    prev = load_state()
    same_book = bool(files) and prev.get("files") == files
    start_index = int(_num(prev, "index", 0, 0, 10**9)) if same_book else 0
    if start_at is not None:
        same_book, start_index = False, start_at  # --from: start there and play
    if start_index >= len(units):
        start_index = 0

    player = Player(target_name)
    player.volume = _num(prev, "volume", 0.85, 0.0, 1.0)
    player.bass = _num(prev, "bass", 0.0, -12, 12)
    player.mid = _num(prev, "mid", 0.0, -12, 12)
    player.treble = _num(prev, "treble", 0.0, -12, 12)
    player.set_units(units, start_index)

    pf = [Prefetcher(engines[0])]  # replaced if the engine has to be restarted
    engine_state = {"ready": False, "error": ""}

    def restart_engine_if_dead():
        """After an engine failure, Play / Open / a voice change start a fresh one."""
        if not engine_state["error"]:
            return
        old = pf[0]
        old.stop_event.set()
        fresh = Engine(cur_voice[0], speed)
        engines.append(fresh)
        pf[0] = Prefetcher(fresh)
        pf[0].generation = old.generation
        engine_state.update(ready=False, error="")
        pf[0].start()
    horizon = [-1]  # last paragraph index queued for synthesis

    def queue_window(start: int, voice_id: str, new_generation: bool = False):
        """Queue synthesis for paragraphs start .. start+LOOKAHEAD."""
        if new_generation:
            pf[0].generation += 1
        while True:
            try:
                pf[0].jobs.get_nowait()
            except queue.Empty:
                break
        end = min(len(units), start + LOOKAHEAD + 1)
        for i in range(start, end):
            _n, text, dest = units[i]
            pf[0].jobs.put((pf[0].generation, i, text, dest, voice_id))
        horizon[0] = end - 1

    root = tk.Tk()
    root.title(f"Listen — {title}")
    geometry = prev.get("geometry")
    m = re.fullmatch(r"(\d{1,5})x(\d{1,5})([+-]-?\d{1,5}[+-]-?\d{1,5})?", geometry) if isinstance(geometry, str) else None
    if m:
        # Never larger than the screen: an absurd size makes X abort the process
        w = min(int(m.group(1)), root.winfo_screenwidth())
        h = min(int(m.group(2)), root.winfo_screenheight())
        root.geometry(f"{max(w, 640)}x{max(h, 620)}{m.group(3) or ''}")
    else:
        root.geometry("780x780")
    root.minsize(640, 620)
    bg, fg, acc = "#1e1e1e", "#f2f2f2", "#4aa3df"
    root.configure(bg=bg)

    status_var = tk.StringVar(value="LOADING VOICE ENGINE…")
    prog_var = tk.StringVar(value="0 / 0")
    page_var = tk.StringVar(value="page 1")
    follow_var = tk.BooleanVar(value=bool(prev.get("follow", True)))

    top = tk.Frame(root, bg=bg)
    top.pack(fill="x", padx=16, pady=(12, 4))
    tk.Label(top, textvariable=status_var, fg=acc, bg=bg, font=("Sans", 16, "bold"),
             anchor="w", justify="left", wraplength=740).pack(anchor="w", fill="x")
    tk.Label(top, textvariable=prog_var, fg=fg, bg=bg, font=("Sans", 11)).pack(anchor="w")

    nav = tk.Frame(root, bg=bg)
    nav.pack(fill="x", padx=16, pady=4)

    reader = tk.Frame(root, bg="#2a2a2a")
    scroll = tk.Scrollbar(reader)
    scroll.pack(side="right", fill="y")
    body = tk.Text(reader, wrap="word", font=("Sans", 13), bg="#2a2a2a", fg=fg, insertbackground=fg,
                   selectbackground="#3d5a73", relief="flat", padx=12, pady=12,
                   yscrollcommand=scroll.set, spacing3=8)
    scroll.config(command=body.yview)
    body.pack(side="left", fill="both", expand=True)
    # Current paragraph: background AND underline, so it never relies on color alone
    body.tag_configure("current", background="#1a3a55", foreground="#ffffff", underline=True)
    body.tag_configure("heading", foreground=acc, font=("Sans", 14, "bold"))

    def fill_body():
        body.configure(state="normal")
        body.delete("1.0", "end")
        if not units:
            body.insert("end", "File → Open…  to load a chapter.\n", ("heading",))
        for i, (name, text, _wav) in enumerate(units):
            if i == 0 or units[i - 1][0] != name:
                body.insert("end", f"{name}\n", ("heading", f"u{i}"))
            body.insert("end", text.strip() + "\n\n", (f"u{i}",))
        body.configure(state="disabled")

    fill_body()

    def current_page() -> tuple[int, int]:
        try:
            last = float(body.index("end-1c").split(".")[0])
            first = float(str(body.index("@0,0")).split(".")[0])
            vis = float(body.index(f"@0,{body.winfo_height() - 2}").split(".")[0])
            page_h = max(1.0, vis - first)
            pages = max(1, int(np.ceil(last / page_h)))
            return min(pages, max(1, int(np.floor((first - 1) / page_h) + 1))), pages
        except tk.TclError:
            return 1, 1

    def center_on(index):
        """Keep the spoken paragraph mid-window, not just somewhere in view."""
        try:
            body.see(index)
            body.update_idletasks()
            info = body.dlineinfo(index)
            if not info:
                return
            _x, y, _w, h, _baseline = info
            delta_px = (y + h / 2.0) - body.winfo_height() / 2.0
            if abs(delta_px) < 12:
                return
            top_f, bot_f = body.yview()
            frac = top_f + (delta_px / max(body.winfo_height(), 1)) * (bot_f - top_f)
            body.yview_moveto(max(0.0, min(1.0, frac)))
        except tk.TclError:
            pass

    def highlight(i: int, force_see: bool = False):
        body.configure(state="normal")
        try:
            body.tag_remove("current", "1.0", "end")
            if 0 <= i < len(units):
                ranges = body.tag_ranges(f"u{i}")
                for a, b in zip(ranges[0::2], ranges[1::2]):
                    body.tag_add("current", a, b)
                if ranges and (follow_var.get() or force_see):
                    center_on(ranges[0])
        finally:
            body.configure(state="disabled")

    def page_scroll(delta: int):
        body.yview_scroll(delta, "pages")

    def click_seek(event):
        try:
            tags = body.tag_names(body.index(f"@{event.x},{event.y}"))
        except tk.TclError:
            return
        for t in tags:
            if t.startswith("u") and t[1:].isdigit():
                i = int(t[1:])
                player.seek(i)
                queue_window(i, cur_voice[0])  # synthesize from here, not after the whole queue
                highlight(i)
                break

    body.bind("<Button-1>", click_seek)

    last_saved: list[dict] = [{}]
    save_warned = [False]

    def persist():
        """Save place and settings, only when something changed. Never raises."""
        data = {
            "files": files,
            "index": int(player.index),
            "volume": float(player.volume),
            "bass": float(player.bass),
            "mid": float(player.mid),
            "treble": float(player.treble),
            "voice": cur_voice[0],
            "speed": speed,
            "geometry": root.geometry(),
            "follow": bool(follow_var.get()),
            "title": title_s[0],
        }
        if data == last_saved[0]:
            return
        try:
            save_state(data)
            last_saved[0] = data
        except OSError as e:
            if not save_warned[0]:
                print(f"warning: cannot save settings: {e}", file=sys.stderr, flush=True)
                save_warned[0] = True

    def do_play():
        restart_engine_if_dead()
        i = player.index
        if i >= len(units):
            i = 0
        if i in player.failed and units:
            with player.lock:
                player.failed.pop(i, None)
        queue_window(i, cur_voice[0])  # the retried paragraph goes first
        player.play()

    def do_pause():
        player.pause()
        persist()

    def do_stop():
        player.stop()
        persist()

    btn = dict(bg="#3a3a3a", fg=fg, activebackground="#555", activeforeground=fg)
    tk.Button(nav, text="Open…", command=lambda: open_book(), font=("Sans", 10, "bold"), **btn).pack(side="left", padx=4)
    tk.Button(nav, text="Prev page", command=lambda: page_scroll(-1), font=("Sans", 10), **btn).pack(side="left", padx=4)
    tk.Button(nav, text="Next page", command=lambda: page_scroll(1), font=("Sans", 10), **btn).pack(side="left", padx=4)
    tk.Checkbutton(nav, text="Follow speech", variable=follow_var, bg=bg, fg=fg, selectcolor="#333",
                   activebackground=bg, activeforeground=fg, font=("Sans", 10)).pack(side="left", padx=12)
    tk.Label(nav, textvariable=page_var, fg=fg, bg=bg, font=("Sans", 10)).pack(side="right")

    sliders = tk.Frame(root, bg=bg)
    sliders.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
    voices = tk.Frame(root, bg=bg)
    voices.pack(side="bottom", fill="x", padx=16, pady=4)
    btns = tk.Frame(root, bg=bg)
    btns.pack(side="bottom", fill="x", padx=16, pady=6)
    for lab, fn in (("Play", do_play), ("Pause", do_pause), ("Stop", do_stop)):
        tk.Button(btns, text=lab, command=fn, width=10, font=("Sans", 12, "bold"), **btn).pack(side="left", padx=6)

    # A voice given with --voice that is not in the list gets its own button
    choices = list(VOICE_CHOICES)
    if voice not in {v[0] for v in choices}:
        choices.append((voice, voice))
    cur_voice = [voice]
    voice_var = tk.StringVar(value=voice)
    tk.Label(voices, text="Voice", fg=fg, bg=bg, font=("Sans", 10, "bold")).pack(side="left", padx=(0, 8))

    reader.pack(fill="both", expand=True, padx=16, pady=6)

    def add_slider(parent, label, from_, to, init, setter, fmt):
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=8, anchor="w", fg=fg, bg=bg, font=("Sans", 10)).pack(side="left")
        val = tk.StringVar(value=fmt(init))
        sc = tk.Scale(row, from_=from_, to=to, orient="horizontal", showvalue=0, resolution=1, bg=bg, fg=fg,
                      troughcolor="#444", highlightthickness=0, length=380)
        sc.set(init)

        def on(_v):
            v = sc.get()
            setter(v)
            val.set(fmt(v))
            player._eq_dirty = True

        sc.configure(command=on)
        sc.pack(side="left", fill="x", expand=True, padx=8)
        tk.Label(row, textvariable=val, width=7, anchor="e", fg=fg, bg=bg, font=("Sans", 10)).pack(side="left")

    add_slider(sliders, "Volume", 0, 100, int(player.volume * 100), lambda v: setattr(player, "volume", v / 100.0), lambda v: f"{int(v)}%")
    add_slider(sliders, "Bass", -12, 12, int(player.bass), lambda v: setattr(player, "bass", float(v)), lambda v: f"{int(v)} dB")
    add_slider(sliders, "Mid", -12, 12, int(player.mid), lambda v: setattr(player, "mid", float(v)), lambda v: f"{int(v)} dB")
    add_slider(sliders, "Treble", -12, 12, int(player.treble), lambda v: setattr(player, "treble", float(v)), lambda v: f"{int(v)} dB")

    resume_when_ready = [False]  # play as soon as the current paragraph is ready

    def open_book():
        start = Path(files[0]).parent if files else Path.home()
        picked = filedialog.askopenfilenames(
            parent=root, title="Open chapters", initialdir=str(start),
            filetypes=[("Manuscripts", "*.md *.markdown *.txt *.html *.htm"), ("All files", "*.*")],
        )
        if not picked:
            return
        new_u, new_f = load_units(list(picked), cur_voice[0], speed)
        if not new_u:
            status_var.set("NO READABLE TEXT IN THOSE FILES")
            return
        restart_engine_if_dead()
        autostart[0] = False  # the start-up place belonged to the previous book
        player.stop()
        units[:] = new_u
        files[:] = new_f
        title_s[0] = " · ".join(Path(p).name for p in new_f)
        root.title(f"Listen — {title_s[0]}")
        player.set_units(list(units), 0)
        resume_when_ready[0] = False
        queue_window(0, cur_voice[0], new_generation=True)
        fill_body()
        highlight(0, force_see=True)
        persist()

    def apply_voice():
        v = voice_var.get()
        if v == cur_voice[0]:
            return
        was_playing = player.status in {"playing", "loading"}
        cur_voice[0] = v
        restart_engine_if_dead()
        units[:] = [(name, text, cache_path(v, speed, text)) for name, text, _old in units]
        idx = max(0, min(player.index, len(units) - 1)) if units else 0
        player.pause()
        player.set_units(list(units), idx)
        queue_window(idx, v, new_generation=True)
        resume_when_ready[0] = was_playing
        persist()

    for vid, lab in choices:
        tk.Radiobutton(voices, text=lab, variable=voice_var, value=vid, command=apply_voice, bg=bg, fg=fg,
                       selectcolor="#333", activebackground=bg, activeforeground=fg,
                       font=("Sans", 10)).pack(side="left", padx=6)

    closing = [False]
    timers: dict[str, str] = {}  # name -> Tk after() id, cancelled on close

    def on_close():
        if closing[0]:
            return
        closing[0] = True
        for job in timers.values():
            root.after_cancel(job)
        try:
            persist()
        finally:
            pf[0].stop_event.set()
            player.close()
            root.destroy()

    autostart = [True]

    def tick():
        if closing[0]:
            return
        try:
            update()
        except Exception as e:  # noqa: BLE001 — show it; never stop refreshing
            status_var.set(f"ERROR: {e}")
        finally:
            if not closing[0]:
                timers["tick"] = root.after(400, tick)

    def update():
        while True:
            try:
                item = pf[0].done.get_nowait()
            except queue.Empty:
                break
            if item[0] == "ready":
                engine_state["ready"] = True
            elif item[0] == "engine-error":
                engine_state["error"] = item[1]
            elif item[1] == pf[0].generation:  # drop results for an old book or voice
                _kind, _gen, idx, err = item
                if err:
                    with player.lock:
                        player.failed[idx] = err
        n = len(units)
        i = max(0, min(player.index, n - 1)) if n else 0
        # Keep synthesis LOOKAHEAD paragraphs ahead of playback. Re-queuing
        # paragraphs already done is cheap: a cached file returns at once.
        if n and engine_state["ready"] and horizon[0] < n - 1 and player.index + LOOKAHEAD // 2 >= horizon[0]:
            queue_window(player.index, cur_voice[0])
        cur_ready = n > 0 and units[i][2].is_file() and units[i][2].stat().st_size > 1000
        prog_var.set(f"paragraph {min(player.index + 1, n) if n else 0} / {n}")
        if not n:
            status_var.set("NO TEXT LOADED — File → Open…")
        elif engine_state["error"] and player.status == "playing":
            status_var.set(f"PLAYING (voice engine stopped: {engine_state['error']} — Play restarts it)")
        elif engine_state["error"]:
            status_var.set(f"VOICE ENGINE: {engine_state['error']} — Play restarts it")
        elif player.error:
            status_var.set(f"AUDIO OUTPUT: {player.error}")
        elif n and i in player.failed:
            status_var.set(f"ERROR at paragraph {i + 1}: {player.failed[i]} — Play retries")
        elif not engine_state["ready"]:
            status_var.set("LOADING VOICE ENGINE…")
        elif resume_when_ready[0]:
            if cur_ready:
                resume_when_ready[0] = False
                player.play()
            else:
                lab = next((l for vid, l in choices if vid == cur_voice[0]), cur_voice[0])
                status_var.set(f"LOADING {lab}…")
        elif autostart[0] and n:
            if cur_ready:
                autostart[0] = False
                if same_book and start_index > 0:
                    player.status = "paused"
                    status_var.set("PAUSED — press Play to resume")
                else:
                    player.play()
            else:
                status_var.set("PREPARING…")
        else:
            status_var.set(player.status.upper())
        if i != last_hi[0]:
            highlight(i, force_see=follow_var.get())
            last_hi[0] = i
        pg, pgs = current_page()
        page_var.set(f"page {pg} / {pgs}")
        persist()

    last_hi = [-1]
    root.protocol("WM_DELETE_WINDOW", on_close)
    # Logout / kill / Ctrl+C in the launching terminal: save place, then close.
    # tick() returns to Python every 400 ms, which lets these handlers run.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: root.after(0, on_close))
    menubar = tk.Menu(root)
    filem = tk.Menu(menubar, tearoff=0)
    filem.add_command(label="Open…", command=open_book, accelerator="Ctrl+O")
    filem.add_separator()
    filem.add_command(label="Quit", command=on_close)
    menubar.add_cascade(label="File", menu=filem)
    root.config(menu=menubar)
    root.bind("<Control-o>", lambda e: open_book())
    root.bind("<Prior>", lambda e: page_scroll(-1))
    root.bind("<Next>", lambda e: page_scroll(1))
    root.bind("<space>", lambda e: (do_pause() if player.status == "playing" else do_play()))

    queue_window(start_index, cur_voice[0])
    pf[0].start()
    highlight(start_index)
    timers["tick"] = root.after(200, tick)
    root.mainloop()
    return 0

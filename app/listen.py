#!/usr/bin/env python3
"""
Listen Book: read Markdown, plain-text or HTML chapters aloud with the local
Kokoro voice model. No network at run time.

Usage
  listen-book                          reopen the last book at the last place
  listen-book chapter1.md chapter2.md  open these, in order
  listen-book chapter.md --no-play     synthesize to the cache only
  listen-book chapter.md --from 12     start at paragraph 12
  listen-book --list-devices           show PipeWire outputs

Audio goes to the PipeWire default output unless --device names another.
Listen Book never changes the default output and never plays into a sink
listed with --avoid-sink (or LISTEN_BOOK_AVOID_SINKS, comma-separated), e.g.
a virtual sink another program is recording from.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from listen_paths import cache_dir, config_dir, kokoro_python, kokoro_site, models_dir

KOKORO_PY = kokoro_python()
MODELS = models_dir()
CACHE = cache_dir()
STATE_PATH = config_dir() / "state.json"
WORKER_LOG = CACHE / "kokoro-worker.err"

# Voices offered in the window; install.sh downloads these. Any other Kokoro
# voice works with --voice once its .pt file is in models/voices/.
VOICE_CHOICES = [
    ("af_heart", "Female — Heart"),
    ("af_nicole", "Female — Nicole"),
    ("bf_emma", "Female — Emma (British)"),
    ("am_michael", "Male — Michael"),
    ("am_onyx", "Male — Onyx"),
]
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0

# Longest text sent to the voice engine at once. Longer paragraphs are split
# at sentence ends, so playback starts quickly and memory stays bounded.
MAX_UNIT_CHARS = 1500

def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    print(f"warning: ignoring {name}={raw!r} (not a positive number); using {default:g}", file=sys.stderr)
    return default


# Seconds to wait for the engine: model load at startup, then per paragraph
STARTUP_TIMEOUT = _env_number("LISTEN_BOOK_STARTUP_TIMEOUT", 300)
SYNTH_TIMEOUT = _env_number("LISTEN_BOOK_SYNTH_TIMEOUT", 600)
CACHE_MAX_MB = _env_number("LISTEN_BOOK_CACHE_MAX_MB", 2048)


# ── settings ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Saved place and settings; {} if missing or unreadable (never raises)."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
        return {}
    return data if isinstance(data, dict) else {}


def save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def trim_cache(limit_mb: float = CACHE_MAX_MB) -> None:
    """Delete the least recently written audio once the cache passes limit_mb."""
    files = []
    total = 0
    for f in CACHE.glob("*/*.wav"):
        try:
            st = f.stat()
        except OSError:
            continue
        if f.name.endswith(".partial.wav"):  # left by an interrupted synthesis
            f.unlink(missing_ok=True)
            continue
        files.append((st.st_mtime, st.st_size, f))
        total += st.st_size
    limit = limit_mb * 1024 * 1024
    if total <= limit:
        return
    for _mtime, size, f in sorted(files):
        f.unlink(missing_ok=True)
        total -= size
        if total <= limit * 0.75:
            break


# ── text ──────────────────────────────────────────────────────────────────────

def read_text_file(path: Path) -> str:
    """Decode UTF-8 (with or without BOM) or UTF-16; warn about undecodable bytes."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:2000]
    if len(head) >= 4 and head.count(b"\x00") > len(head) // 4:
        # UTF-16 without a BOM: ASCII text leaves every other byte zero
        le = head[1::2].count(b"\x00") > head[0::2].count(b"\x00")
        return data.decode("utf-16-le" if le else "utf-16-be", errors="replace")
    text = data.decode("utf-8-sig", errors="replace")
    bad = text.count("�") - data.decode("utf-8-sig", errors="ignore").count("�")
    if bad:
        print(f"warning: {path.name}: {bad} characters are not valid UTF-8 and were replaced",
              file=sys.stderr, flush=True)
    return text


_YAML_LINE = re.compile(r"^\s*$|^[\w.-]+\s*:|^\s+\S|^\s*-\s")


def strip_frontmatter(raw: str) -> str:
    """Remove a leading YAML front-matter block, and only that.

    A chapter that opens with a "---" scene break is left alone: the block
    must be delimited by lines that are exactly "---" and hold only YAML-like
    lines (key: value, list items, indented continuations).
    """
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return raw
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            block = lines[1:end]
            if block and all(_YAML_LINE.match(line) for line in block):
                return "\n".join(lines[end + 1 :])
            return raw
    return raw


def from_markdown(text: str) -> str:
    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.strip().startswith(("```", "~~~"))]
    if len(fences) % 2:
        fences.pop()  # an unclosed fence is text, not the start of a code block to the end
    fence_set = set(fences)
    out: list[str] = []
    in_code = False
    for i, line in enumerate(lines):
        s = line.strip()
        if i in fence_set:
            in_code = not in_code
            continue
        if in_code:
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,}|={3,})$", s):
            out.append("")  # a rule or a setext heading underline: a break, never text
            continue
        if s.startswith("|"):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"\1", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
        s = re.sub(r"^>\s*", "", s)
        out.append(s)
    blob = "\n".join(out)
    blob = re.sub(r"[ \t]+", " ", blob)
    blob = re.sub(r"\n{3,}", "\n\n", blob)
    return blob.strip()


def from_html(raw: str) -> str:
    raw = re.sub(r"<(script|style|table)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"</(p|h[1-6]|li|div|section|article)>|<br\s*/?>", "\n\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", raw).strip()


def extract_text(path: Path) -> str:
    raw = read_text_file(path)
    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        return from_markdown(strip_frontmatter(raw))
    if ext in {".html", ".htm", ".xhtml"}:
        return from_html(raw)
    return raw.strip()


def speakable(text: str) -> bool:
    """Skip scene-break asterisks and other leftovers that make Kokoro hang.

    Needs two Latin letters or two digits ("1984" is spoken; "* * *" is not).
    """
    return bool(re.search(r"[A-Za-z]{2,}|[0-9]{2,}", text or ""))


_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")


def split_long(p: str, limit: int = MAX_UNIT_CHARS) -> list[str]:
    """Split text longer than `limit` at sentence ends (or spaces, as a last resort)."""
    if len(p) <= limit:
        return [p]
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(p):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    units: list[str] = []
    for s in pieces:
        if units and len(units[-1]) + 1 + len(s) <= limit:
            units[-1] += " " + s
        else:
            units.append(s)
    return units


def split_paragraphs(text: str) -> list[str]:
    out = []
    for p in re.split(r"\n\s*\n", text):
        p = " ".join(p.split())
        if speakable(p):
            out += [u for u in split_long(p) if speakable(u)]
    return out


def cache_path(voice: str, speed: float, text: str) -> Path:
    h = hashlib.sha1(f"{voice}|{speed:.3f}|{text}".encode("utf-8")).hexdigest()[:16]
    return CACHE / voice / f"{h}.wav"


# ── pipewire (read-only: never changes the default output) ────────────────────

def _wpctl(*args: str) -> str | None:
    try:
        return subprocess.check_output(["wpctl", *args], text=True, errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _node_name(inspect_output: str | None) -> str | None:
    for line in (inspect_output or "").splitlines():
        m = re.search(r'node\.name = "([^"]+)"', line)
        if m:
            return m.group(1)
    return None


def list_sinks() -> list[dict]:
    """Audio sinks from `wpctl status`: id, node name, description, default flag."""
    st = _wpctl("status")
    if st is None:
        sys.exit("wpctl not found or failed. Listen Book needs PipeWire (wireplumber).")
    sinks: list[dict] = []
    in_audio = in_sinks = False
    for line in st.splitlines():
        if re.match(r"^\S", line):
            in_audio = line.strip().startswith("Audio")
            in_sinks = False
            continue
        if not in_audio:
            continue
        if re.search(r"[├└]─ Sinks:", line):
            in_sinks = True
            continue
        if re.search(r"[├└]─ ", line):
            in_sinks = False
            continue
        if not in_sinks:
            continue
        m = re.search(r"(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*)?$", line)
        if not m:
            continue
        star, sid, desc = m.group(1), int(m.group(2)), m.group(3).strip()
        sinks.append({
            "id": sid,
            "name": _node_name(_wpctl("inspect", str(sid))) or desc,
            "description": desc,
            "default": star == "*",
        })
    return sinks


def avoid_list(cli: list[str] | None) -> set[str]:
    names = {n.strip() for n in cli or [] if n.strip()}
    env = os.environ.get("LISTEN_BOOK_AVOID_SINKS", "")
    names.update(n.strip() for n in env.split(",") if n.strip())
    return names


def sink_matches(sink: dict, entry: str) -> bool:
    """Case-insensitive match on id, node name or description; entries of 4+
    characters also match as part of the name or description."""
    e = entry.strip().lower()
    name, desc = sink["name"].lower(), sink["description"].lower()
    if not e:
        return False
    if e in {str(sink["id"]), name, desc}:
        return True
    return len(e) >= 4 and (e in name or e in desc)


def unmatched_avoid_entries(avoid: set[str], sinks: list[dict]) -> list[str]:
    """Avoid-list entries that match no current output (likely typos)."""
    return sorted(a for a in avoid if not any(sink_matches(s, a) for s in sinks))


def pick_play_target(requested: str, avoid: set[str], sinks: list[dict] | None = None) -> dict | None:
    """Sink to play on, or None for synth-only.

    {"name": ""} means "follow PipeWire's default output". With an avoid list
    the default is resolved to an explicit sink instead, so a later change of
    the default output cannot redirect speech into an avoided sink.
    """
    req = (requested or "default").strip().lower()
    if req in {"none", "off", "file"}:
        return None
    sinks = list_sinks() if sinks is None else sinks
    if not sinks:
        sys.exit("No PipeWire audio outputs found. Use --no-play to synthesize only.")
    for entry in unmatched_avoid_entries(avoid, sinks):
        print(f"warning: --avoid-sink {entry!r} matches no output (see --list-devices; "
              "partial names need 4+ characters)", file=sys.stderr, flush=True)

    def avoided(s: dict) -> bool:
        return any(sink_matches(s, a) for a in avoid)

    usable = [s for s in sinks if not avoided(s)]
    if req == "default":
        default = next((s for s in sinks if s["default"]), None)
        if default is not None and not avoided(default):
            return default if avoid else {"name": "", "description": "default output", "id": None}
        if not usable:
            sys.exit("Every audio output is in the avoid list.")
        if default is not None:
            print(f"Default output {default['name']!r} is in the avoid list; using {usable[0]['description']}.",
                  flush=True)
        return usable[0]
    if req in {"hdmi", "analog"}:
        match = [s for s in usable if req in s["description"].lower() or req in s["name"].lower()]
        if not match:
            sys.exit(f"No usable {req} output found. Try --list-devices.")
        return match[0]
    exact = [s for s in sinks if req in {str(s["id"]), s["name"].lower(), s["description"].lower()}]
    partial = [s for s in sinks if req in s["name"].lower() or req in s["description"].lower()]
    for s in exact or partial:
        if avoided(s):
            sys.exit(f"Refusing to play on {s['name']!r}: it is in the avoid list.")
        return s
    sys.exit(f"Unknown --device {requested!r}. Try --list-devices.")


def play_wav(path: Path, target: dict) -> int:
    """Play one file. Uses --target only for an explicit sink; never changes the default."""
    cmd = ["pw-play"]
    if target.get("name"):
        cmd += ["--target", target["name"]]
    try:
        return subprocess.call([*cmd, str(path)])
    except FileNotFoundError:
        sys.exit("pw-play not found. Install PipeWire's command-line tools (pipewire-bin).")


# ── voice engine ──────────────────────────────────────────────────────────────

class EngineError(Exception):
    pass


def worker_env() -> dict[str, str]:
    """Environment for the Kokoro venv interpreter.

    Conda/virtualenv variables from the calling shell make the venv's Python
    skip its own site-packages, so they are removed. HF_HUB_OFFLINE stops the
    kokoro package from reaching the network.
    """
    env = os.environ.copy()
    for k in list(env):
        if k.startswith(("CONDA_", "MAMBA_")) or k in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}:
            env.pop(k, None)
    env["PATH"] = str(KOKORO_PY.parent) + ":" + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(KOKORO_PY.parent.parent)
    env["PYTHONNOUSERSITE"] = "1"
    site = kokoro_site()
    if site is not None and site.is_dir():
        env["PYTHONPATH"] = str(site)
    env["LISTEN_BOOK_MODELS"] = str(MODELS)
    env["HF_HUB_OFFLINE"] = "1"
    return env


def worker_log_tail(n: int = 1500) -> str:
    try:
        return WORKER_LOG.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


class Engine:
    """The Kokoro voice worker process (app/_listen_synth.py).

    One request at a time: synth() may be called from one thread while
    stop() is called from another. Every read has a timeout, so a hung engine
    is reported instead of freezing the caller.
    """

    def __init__(self, voice: str, speed: float):
        self.voice = voice
        self.speed = speed
        self.proc: subprocess.Popen | None = None
        self._buf = b""
        self._write_lock = threading.Lock()

    def start(self, timeout: float = STARTUP_TIMEOUT) -> None:
        """Start the worker and wait for READY. Raises EngineError."""
        helper = Path(__file__).resolve().parent / "_listen_synth.py"
        if not KOKORO_PY.exists():
            raise EngineError(f"voice engine not installed: {KOKORO_PY} is missing (run install.sh)")
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(WORKER_LOG, "w", encoding="utf-8") as err_f:
            self.proc = subprocess.Popen(
                [str(KOKORO_PY), str(helper), "--voice", self.voice, "--speed", str(self.speed)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_f,
                env=worker_env(), bufsize=0,
            )
        try:
            line = self._readline(timeout)
        except TimeoutError:
            self.stop(grace=0)
            raise EngineError(f"voice engine did not start within {timeout:.0f} s (see {WORKER_LOG})") from None
        if line != "READY":
            self.stop()
            detail = line.removeprefix("SYNTH_ERR").strip() if line else ""
            tail = worker_log_tail(300).strip().splitlines()
            raise EngineError("voice engine failed to start: "
                              + (detail or (tail[-1] if tail else "no output")))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> str | None:
        """Next line from the worker; None at end of output; TimeoutError if none in time."""
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                ready, _, _ = select.select([fd], [], [], left)
                if not ready:
                    continue
                chunk = os.read(fd, 4096)
            except (OSError, ValueError):  # pipe closed by stop() from another thread
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def synth(self, text: str, dest: Path, voice: str | None = None,
              timeout: float = SYNTH_TIMEOUT) -> str | None:
        """Synthesize one paragraph to dest. Returns None on success, else an error message."""
        try:
            if dest.is_file() and dest.stat().st_size > 1000:
                return None
        except OSError as e:
            return f"cannot read the audio cache: {e.strerror or e}"
        if not self.alive():
            return "voice engine is not running"
        payload = {"text": text, "out": str(dest)}
        if voice:
            payload["voice"] = voice
        try:
            with self._write_lock:
                self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            while True:
                reply = self._readline(timeout)
                if reply is None:
                    break
                if reply.startswith("SYNTH_OK "):
                    if reply[len("SYNTH_OK "):] != str(dest):
                        # A reply for another request: the stream is out of step
                        self.stop()
                        return "voice engine reply out of order; engine stopped"
                    return None
                if reply.startswith("SYNTH_ERR"):
                    return reply.removeprefix("SYNTH_ERR").strip() or "synthesis failed"
                # anything else is not protocol: ignore it
        except TimeoutError:
            self.stop(grace=0)  # it is not reading; asking it to QUIT would only add a wait
            return f"voice engine stopped responding (no reply in {timeout:.0f} s)"
        except (BrokenPipeError, ValueError, OSError):
            pass
        tail = worker_log_tail(400).strip().splitlines()
        return "voice engine stopped" + (f": {tail[-1]}" if tail else f" (see {WORKER_LOG})")

    def stop(self, grace: float = 5) -> None:
        """Ask the worker to QUIT, wait up to `grace` seconds, then kill it."""
        p = self.proc
        if p is None:
            return
        if p.poll() is None:
            if grace > 0:
                try:
                    with self._write_lock:
                        p.stdin.write(b"QUIT\n")
                        p.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    pass
            try:
                p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            if p.poll() is None:
                p.kill()
                p.wait()
        for pipe in (p.stdin, p.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


# ── cli ───────────────────────────────────────────────────────────────────────

def cmd_list_devices(avoid: set[str]) -> int:
    for s in list_sinks():
        mark = "*" if s["default"] else " "
        flag = "  (avoided)" if any(sink_matches(s, a) for a in avoid) else ""
        print(f"  {mark} {s['id']:4}  {s['name']:48}  {s['description']}{flag}")
    print("\n* = default output. Listen Book plays there unless --device names another.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read chapters aloud with the local Kokoro voice model.")
    ap.add_argument("path", nargs="*", help=".md / .txt / .html files, read in order")
    ap.add_argument("--voice", default=None, help=f"Kokoro voice id (default: last used, else {DEFAULT_VOICE})")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="below 1 is slower")
    ap.add_argument("--from", dest="start", type=int, default=1, help="1-based paragraph to start at (each file)")
    ap.add_argument("--max", type=int, default=0, help="stop after N paragraphs per file (0 = all)")
    ap.add_argument("--no-play", action="store_true", help="synthesize to the cache only")
    ap.add_argument("--device", default="default",
                    help="default | hdmi | analog | <sink name or id> | none")
    ap.add_argument("--avoid-sink", action="append", metavar="NAME",
                    help="never play on this sink (repeatable; also LISTEN_BOOK_AVOID_SINKS)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--out", default=None, help="also join the spoken audio into this .wav (with --cli or --no-play)")
    ap.add_argument("--cli", "--no-gui", dest="cli", action="store_true",
                    help="play in the terminal instead of the window")
    args = ap.parse_args(argv)

    if args.start < 1:
        ap.error("--from must be 1 or more")
    if args.speed <= 0:
        ap.error("--speed must be above 0")
    avoid = avoid_list(args.avoid_sink)
    if args.list_devices:
        return cmd_list_devices(avoid)

    state = load_state()
    if not args.path:
        args.path = [p for p in state.get("files") or [] if isinstance(p, str) and Path(p).is_file()]
    saved_voice = state.get("voice") if isinstance(state.get("voice"), str) else None
    voice = args.voice or saved_voice or DEFAULT_VOICE

    sources: list[Path] = []
    for raw in args.path:
        src = Path(raw).expanduser()
        if not src.is_file():
            sys.exit(f"not found: {raw}")
        sources.append(src)

    want_play = not args.no_play and args.device.lower() not in {"none", "off", "file"}
    use_gui = want_play and not args.cli and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if use_gui and args.out:
        ap.error("--out works with --cli or --no-play, not in the window")
    if use_gui and args.max:
        ap.error("--max works with --cli or --no-play, not in the window")
    play_target = pick_play_target(args.device, avoid) if want_play else None
    if play_target and play_target["name"]:
        print(f"playing on: {play_target['description']}  ({play_target['name']})", flush=True)
    if not sources and not use_gui:
        print("No files given and none remembered. Usage: listen-book chapter.md", file=sys.stderr)
        return 2

    trim_cache()
    engine = Engine(voice, args.speed)
    try:
        if use_gui:
            # The window opens at once and starts the engine in the background
            from listen_gui import run_gui

            # The whole book is loaded; --from only sets where to start, so the
            # place saved for next time is a position in the whole book
            units: list[tuple[str, str, Path]] = []
            for src in sources:
                units += [(src.name, t, cache_path(voice, args.speed, t))
                          for t in split_paragraphs(extract_text(src))]
            return run_gui(
                engine,
                units,
                play_target["name"] if play_target else "",
                " · ".join(s.name for s in sources),
                files=[str(s.resolve()) for s in sources],
                voice=voice,
                speed=args.speed,
                start_at=args.start - 1 if args.start > 1 else None,
            )
        t0 = time.time()
        try:
            engine.start()
        except EngineError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"voice engine ready in {time.time() - t0:.1f}s", flush=True)
        return run_cli(engine, sources, args, voice, play_target)
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
        return 130
    finally:
        engine.stop()


def run_cli(engine: Engine, sources: list[Path], args, voice: str, play_target: dict | None) -> int:
    written: list[Path] = []
    for src in sources:
        paras = split_paragraphs(extract_text(src))[args.start - 1 :]
        if args.max > 0:
            paras = paras[: args.max]
        if not paras:
            print(f"{src}: no text to read", flush=True)
            continue
        print(f"{src}  paragraphs {args.start}–{args.start + len(paras) - 1}  "
              f"words={sum(len(p.split()) for p in paras)}  voice={voice} speed={args.speed}", flush=True)
        for i, para in enumerate(paras, start=args.start):
            dest = cache_path(voice, args.speed, para)
            print(f"[{src.name} {i}] {para[:72]}{'…' if len(para) > 72 else ''}", flush=True)
            err = engine.synth(para, dest, voice)
            if err:
                print(f"error: paragraph {i}: {err}", file=sys.stderr, flush=True)
                return 1
            written.append(dest)
            if play_target and play_wav(dest, play_target) != 0:
                print("playback stopped", flush=True)
                return 1

    if args.out and written:
        import numpy as np
        import soundfile as sf

        chunks = [sf.read(str(w), dtype="float32")[0] for w in written]
        outp = Path(args.out).expanduser()
        try:
            outp.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(outp), np.concatenate(chunks), 24000)
        except (OSError, RuntimeError) as e:
            print(f"error: cannot write {outp}: {e}", file=sys.stderr)
            return 1
        print(f"wrote {outp}", flush=True)
    print(f"done. cache: {CACHE / voice}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

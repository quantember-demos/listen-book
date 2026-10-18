#!/usr/bin/env python3
"""Kokoro voice worker for listen.py. Runs under the Kokoro venv (Python 3.10-3.12).

Protocol, one line per message:
  stdin:  {"text": "...", "out": "/path/to.wav", "voice": "af_heart"}   ("voice" optional)
          QUIT
  stdout: READY                    once, after the model has loaded
          SYNTH_OK /path/to.wav
          SYNTH_ERR <one-line message>

Protocol lines go out on a private duplicate of the original stdout; file
descriptor 1 itself is pointed at stderr, so nothing Kokoro, torch or espeak
prints, from Python or from C, can land in the reply stream. The parent sets HF_HUB_OFFLINE=1: the model and voices must already be
in the models directory; nothing is downloaded at run time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# When launched as the worker, listen_paths.py sits next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from listen_paths import MODEL_FILE, espeak_path_problem, kokoro_site, models_dir  # noqa: E402

MODELS = models_dir()
SR = 24000
_SITE = kokoro_site()
if _SITE is not None and _SITE.is_dir() and str(_SITE) not in sys.path:
    sys.path.insert(0, str(_SITE))


def load_pipelines():
    # Checked before kokoro imports espeak: past the limit, espeak-ng exits the
    # process instead of raising, and the parent would see only "no output"
    import espeakng_loader

    problem = espeak_path_problem(espeakng_loader.get_data_path())
    if problem:
        raise RuntimeError(problem)
    from kokoro import KModel, KPipeline

    cfg = MODELS / "config.json"
    pth = MODELS / MODEL_FILE
    if not cfg.is_file() or not pth.is_file():
        raise FileNotFoundError(f"Kokoro model missing under {MODELS} (run install.sh)")
    kmodel = KModel(config=str(cfg), model=str(pth)).to("cpu").eval()
    # Voice ids start with their language: a = American English, b = British English
    return {
        "a": KPipeline(lang_code="a", model=kmodel),
        "b": KPipeline(lang_code="b", model=kmodel),
    }


def synth_one(pipe, voice_path: str, speed: float, text: str, dest: Path) -> None:
    import numpy as np
    import soundfile as sf

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1000:
        return
    if not re.search(r"[A-Za-z]{2,}", text or ""):
        sf.write(str(dest), np.zeros(int(SR * 0.04), dtype=np.float32), SR)
        return
    chunks = []
    for _, _, audio in pipe(text, voice=voice_path, speed=speed):
        a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio
        chunks.append(a)
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    # Write then rename, so a reader never sees a half-written file
    tmp = dest.with_suffix(".partial.wav")
    sf.write(str(tmp), np.concatenate(chunks), SR)
    tmp.replace(dest)


def one_line(msg: object) -> str:
    """A newline inside an error would corrupt the line protocol."""
    return " ".join(str(msg).split()) or "unknown error"


def protocol_channel():
    """Keep the real stdout for protocol lines; send fd 1 (and sys.stdout) to stderr."""
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()
    proto = protocol_channel()

    def send(line: str) -> None:
        proto.write(line + "\n")
        proto.flush()

    try:
        pipes = load_pipelines()
    except Exception as e:  # noqa: BLE001 — reported to the parent, which shows it
        send(f"SYNTH_ERR {one_line(e)}")
        return 1

    for f in sorted((MODELS / "voices").glob("*.pt")):
        lang = f.stem[:1]
        if lang in pipes:
            try:
                pipes[lang].load_voice(str(f))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"voice preload {f.stem}: {e}\n")
    send("READY")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "QUIT":
            break
        try:
            job = json.loads(line)
            text = job["text"]
            dest = Path(job["out"])
            vname = job.get("voice") or args.voice
            voice_file = MODELS / "voices" / f"{vname}.pt"
            if not voice_file.is_file():
                raise FileNotFoundError(f"voice {vname!r} not installed")
            lang = vname[:1] if vname[:1] in pipes else "a"
            synth_one(pipes[lang], str(voice_file), args.speed, text, dest)
            send(f"SYNTH_OK {dest}")
        except Exception as e:  # noqa: BLE001 — one bad paragraph must not end the session
            send(f"SYNTH_ERR {one_line(e)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fake voice worker: same line protocol as app/_listen_synth.py.

Paragraph text controls failures, so a test can stage them:
  FAILME          SYNTH_ERR for any voice
  FAILFOR:<voice> SYNTH_ERR only when that voice is requested (after 1.5 s)
  DIEHERE         the worker exits, as a crashed engine would
  DIEONCE         exits the first time only (a later worker succeeds)
  NOISE           a non-protocol line is printed before the reply
  HANG            no reply at all
Environment: FAKE_SYNTH_MS delays every reply; FAKE_HANG_START never sends READY.
Everything else becomes 0.3 s of a 220 Hz tone.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ap = argparse.ArgumentParser()
ap.add_argument("--voice", default="af_heart")
ap.add_argument("--speed")
args, _ = ap.parse_known_args()
if os.environ.get("FAKE_AUDIO_DIR"):
    Path(os.environ["FAKE_AUDIO_DIR"], "worker.pid").write_text(str(os.getpid()))
if os.environ.get("FAKE_HANG_START"):
    time.sleep(3600)
print("READY", flush=True)
delay = float(os.environ.get("FAKE_SYNTH_MS", "0")) / 1000
for line in sys.stdin:
    line = line.strip()
    if line == "QUIT":
        break
    job = json.loads(line)
    text, voice = job["text"], job.get("voice") or args.voice
    time.sleep(delay)
    if "DIEHERE" in text:
        sys.exit(3)
    once = Path(os.environ.get("FAKE_AUDIO_DIR", "/tmp"), "died-once")
    if "DIEONCE" in text and not once.exists():
        once.write_text("x")
        sys.exit(3)
    if "HANG" in text:
        time.sleep(3600)
    if "NOISE" in text:
        print("Defaulting repo_id to hexgrad/Kokoro-82M.", flush=True)
    if "FAILME" in text:
        print("SYNTH_ERR simulated failure", flush=True)
        continue
    if f"FAILFOR:{voice}" in text:
        time.sleep(1.5)
        print("SYNTH_ERR simulated failure", flush=True)
        continue
    dest = Path(job["out"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(24000 * 0.3)) / 24000
    sf.write(str(dest), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 24000)
    print(f"SYNTH_OK {dest}", flush=True)

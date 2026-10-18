"""Where Listen Book finds its voice engine, model, settings and cache.

Install layout (install.sh, default ~/.local/opt/listen-book):
  PREFIX/app/*.py
  PREFIX/venv/          Python 3.10-3.12 with kokoro + torch (CPU)
  PREFIX/models/        kokoro-v1_0.pth, config.json, voices/*.pt

Every location can be overridden with an environment variable, so the app
also runs straight from a checkout:
  LISTEN_BOOK_PREFIX   install root
  LISTEN_BOOK_PYTHON   interpreter for the voice worker
  LISTEN_BOOK_MODELS   model directory
  LISTEN_BOOK_CACHE    synthesized audio cache
  LISTEN_BOOK_CONFIG   settings directory
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MODEL_FILE = "kokoro-v1_0.pth"


def prefix() -> Path:
    env = os.environ.get("LISTEN_BOOK_PREFIX")
    if env:
        return Path(env).expanduser().resolve()
    # app/ sits directly under the prefix
    return Path(__file__).resolve().parent.parent


def kokoro_python() -> Path:
    env = os.environ.get("LISTEN_BOOK_PYTHON")
    if env:
        return Path(env).expanduser()
    for name in ("python3", "python"):
        b = prefix() / "venv" / "bin" / name
        if b.exists():
            return b
    return Path(sys.executable)


def models_dir() -> Path:
    env = os.environ.get("LISTEN_BOOK_MODELS")
    if env:
        return Path(env).expanduser()
    return prefix() / "models"


def kokoro_site() -> Path | None:
    venv = kokoro_python().parent.parent
    for d in sorted((venv / "lib").glob("python3.*/site-packages")):
        return d
    return None


# espeak-ng (bundled by the espeakng_loader package) copies its data path into
# a 160-byte buffer and silently truncates longer paths; it then falls back to
# a path from its build machine and exits the whole process.
ESPEAK_PATH_MAX = 159


def espeak_path_problem(data_path: str | os.PathLike) -> str | None:
    """Why espeak-ng cannot use this data path, or None if it can."""
    real = os.path.realpath(data_path)  # phonemizer resolves symlinks too
    if len(real.encode()) > ESPEAK_PATH_MAX:
        return (f"install path too long for espeak-ng ({len(real.encode())} characters to its data, "
                f"limit {ESPEAK_PATH_MAX}); reinstall with a shorter PREFIX")
    return None


def cache_dir() -> Path:
    env = os.environ.get("LISTEN_BOOK_CACHE")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "listen-book"


def config_dir() -> Path:
    env = os.environ.get("LISTEN_BOOK_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "listen-book"

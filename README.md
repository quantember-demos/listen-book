# Listen Book

Read your chapters aloud on Linux with a natural-sounding voice model that runs on your own CPU. Made for writers and editors who catch more mistakes by ear: open a Markdown, text or HTML chapter, press Play, and the spoken paragraph is highlighted as it reads.

After installation it works offline. Nothing you open is sent anywhere.

- **Voice:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), 5 voices (US and British English, female and male).
- **Player:** play / pause / stop, click any paragraph to jump there, volume and bass / mid / treble.
- **Remembers your place:** reopen the app and it returns to the same book at the same paragraph.
- **Fast replays:** each spoken paragraph is cached, so going back is instant.
- **Gapless:** the next paragraphs are prepared while the current one plays.

## Requirements

- Linux with **PipeWire** (Ubuntu 22.10+, Fedora 34+, Debian 12+ and most current desktops)
- **Python 3.10, 3.11 or 3.12** with Tk. The voice engine does not support Python 3.13 yet.
- About **1.8 GB** of disk: 1.5 GB for CPU-only PyTorch and the voice engine, 315 MB for the model and voices. These are downloaded once, at install.
- `curl`

```bash
# Ubuntu / Debian
sudo apt install python3.12 python3.12-venv python3-tk pipewire-bin wireplumber curl
```

## Install

```bash
git clone https://github.com/quantember-demos/listen-book.git
cd listen-book
./install.sh
```

The installer:
1. creates a private Python environment in `~/.local/opt/listen-book`;
2. installs pinned versions of the voice engine;
3. downloads the model from Hugging Face at a pinned revision and verifies every file against `models.sha256`;
4. adds a `listen-book` command and a **Listen Book** menu entry.

Re-running it updates the app in place and reuses what is already downloaded.

Other options:
- `./install.sh /path/to/prefix` installs elsewhere. Keep the path short: espeak-ng, part of the voice engine, cannot use a data path longer than 159 characters, and the installer refuses a prefix that would exceed it.
- `./install.sh --uninstall` removes the install, the command and the menu entry, and keeps your settings and cache.
- **No network on the target machine?** Copy a `models/` folder from another install and run `LISTEN_BOOK_MODEL_SOURCE=/path/to/models ./install.sh`. The checksums still apply. The Python packages still need either a network or a local pip mirror.

## Use

```bash
listen-book                          # reopen the last book where you left off
listen-book chapter1.md chapter2.md  # open these, in order
```

In the window: **Open…** or Ctrl+O to load files, Space to play/pause, Page Up/Down to scroll. Click a paragraph to jump to it. Closing the window saves your place.

The window opens at once; the first paragraph starts after the voice model loads, which takes 10–60 seconds on a CPU. After that, the next dozen paragraphs are prepared ahead of playback. Any problem, such as a failed paragraph, a stopped voice engine or an audio output that won't open, is shown in the status line.

### Terminal options

| Option | Meaning |
|---|---|
| `--voice ID` | `af_heart`, `af_nicole`, `bf_emma`, `am_michael`, `am_onyx` (default: last used, else `af_heart`) |
| `--speed N` | below 1 is slower (default 1.0) |
| `--from N` | start at paragraph N |
| `--max N` | stop after N paragraphs (with `--cli` or `--no-play`) |
| `--no-play` | prepare audio in the cache only |
| `--out FILE.wav` | also join the spoken audio into one file |
| `--cli` | play in the terminal, no window |
| `--device NAME` | play on a specific output: `hdmi`, `analog`, a sink name or id from `--list-devices` |
| `--avoid-sink NAME` | never play on this output (repeatable) |

### Never play into a recording

If another program records from a virtual output, for example a null sink used to capture audio, Listen Book can keep its speech out of that recording. List the sink with `--avoid-sink NAME`, or set it permanently:

```bash
export LISTEN_BOOK_AVOID_SINKS="my-recorder-sink"
```

If that sink is currently the default output, Listen Book plays on a real output instead. It never changes your default output.

### Settings by environment variable

| Variable | Default | Meaning |
|---|---|---|
| `LISTEN_BOOK_AVOID_SINKS` | none | outputs never to play on, comma-separated |
| `LISTEN_BOOK_CACHE_MAX_MB` | 2048 | the oldest spoken paragraphs are deleted beyond this |
| `LISTEN_BOOK_STARTUP_TIMEOUT` | 300 | seconds to wait for the voice model to load |
| `LISTEN_BOOK_SYNTH_TIMEOUT` | 600 | seconds to wait for one paragraph |

## Files it uses

| Path | What |
|---|---|
| `~/.local/opt/listen-book` | the app, its Python environment and the model |
| `~/.config/listen-book/state.json` | last book, place, voice, volume and EQ |
| `~/.cache/listen-book/` | spoken paragraphs (safe to delete), plus `kokoro-worker.err`, the voice engine's log |

## Tests

```bash
PY=~/.local/opt/listen-book/venv/bin/python
xvfb-run -a $PY -m unittest discover -s tests       # everything except the real engine (~1 min), windows off-screen
tests/test_install.sh                               # installer safety (no download)
LISTEN_BOOK_TEST_ENGINE=1 $PY -m unittest discover -s tests -p test_engine.py   # the real voice engine
```

The window and terminal tests use stand-ins for the voice engine and the audio system (`tests/fakes/`), so they never make a sound. The stand-ins can fail, crash, hang and print noise on demand, so the error paths get tested too. The real-engine test is opt-in, because loading the model takes a minute or more on an older CPU. How the program fits together is in [docs/FLOW.md](docs/FLOW.md).

## Known limitations

- **Python packages:** pinned by version, but not hash-locked. The model files and the spaCy language model are checksum-verified; the rest come from PyPI at the pinned versions.
- **EQ cost:** bass / mid / treble run in Python, and any non-zero band costs some CPU on slow machines. Bands left at 0 dB cost nothing.
- **Front-matter detection:** a Markdown file that begins with a `---` line, then only lines shaped like `word: text`, then another `---`, is treated as YAML front matter and not read aloud, even if it was meant as prose.
- **English only:** American and British English. Kokoro has other languages, but Listen Book's text handling assumes English.

## License

MIT, see [LICENSE](LICENSE). The voice model and Python packages that `install.sh` downloads have their own licenses, listed in [NOTICE](NOTICE).

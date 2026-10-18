#!/usr/bin/env bash
# Install Listen Book for the current user. No root needed (see README for
# the system packages).
#
#   ./install.sh [PREFIX]              install or update (default ~/.local/opt/listen-book)
#   ./install.sh --uninstall [PREFIX]  remove the install, menu entry and command
#
# Re-running updates the app files in place; the voice engine venv and the
# model are reused when already valid. The model comes from Hugging Face at a
# pinned revision and is checked against models.sha256. To install offline,
# point LISTEN_BOOK_MODEL_SOURCE at a directory holding the same files.
#
# Locations of the command and menu entry: BIN_DIR, APP_DIR, ICON_DIR.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
APP_DIR="${APP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/applications}"
ICON_DIR="${ICON_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps}"
HF_REPO="hexgrad/Kokoro-82M"
HF_REV="f3ff3571791e39611d31c381e3a41a3af07b4987"
MARKER=".listen-book-install"
DESKTOP_MARKER="X-Installed-By=listen-book-install.sh"

die() { echo "error: $*" >&2; exit 1; }

# Quote a path for a .desktop Exec key. The Desktop Entry spec applies two
# escaping layers to Exec (string escapes, then quoting), so a backslash
# becomes four
desktop_quote() {
  local s=$1
  s=${s//\\/\\\\\\\\}
  s=${s//\"/\\\"}
  s=${s//\`/\\\`}
  s=${s//\$/\\\$}
  printf '"%s"' "$s"
}

# True if $1 is a symlink resolving to $2
links_to() {
  [[ -L "$1" && "$(readlink -f -- "$1")" == "$(readlink -f -- "$2")" ]]
}

# kokoro supports Python 3.10-3.12; the window also needs tkinter
find_python() {
  local v py
  for v in 3.12 3.11 3.10; do
    py="$(command -v "python$v" || true)"
    [[ -n $py ]] || continue
    if "$py" -c "import venv, tkinter" 2>/dev/null; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

fetch_models() {
  local models=$1 sum file src
  mkdir -p "$models/voices"
  while read -r sum file; do
    [[ -n $file ]] || continue
    if [[ -f "$models/$file" ]] && echo "$sum  $models/$file" | sha256sum -c --status; then
      continue
    fi
    if [[ -n ${LISTEN_BOOK_MODEL_SOURCE:-} ]]; then
      src="$LISTEN_BOOK_MODEL_SOURCE/$file"
      [[ -f $src ]] || die "$src not found (LISTEN_BOOK_MODEL_SOURCE)"
      echo "  copying $file"
      cp -- "$src" "$models/$file.part"
    else
      echo "  downloading $file"
      curl -fL --retry 3 --progress-bar -o "$models/$file.part" \
        "https://huggingface.co/$HF_REPO/resolve/$HF_REV/$file"
    fi
    if ! echo "$sum  $models/$file.part" | sha256sum -c --status; then
      rm -f -- "$models/$file.part"
      die "checksum mismatch for $file; nothing was installed for it"
    fi
    mv -- "$models/$file.part" "$models/$file"
  done < "$REPO/models.sha256"
}

install() {
  local prefix py dest
  prefix="$(realpath -m -- "$1")"  # a relative path would break the command and menu entry
  [[ $prefix != "$REPO" ]] || die "install somewhere other than this checkout (default: ~/.local/opt/listen-book)"
  py="$(find_python)" || die "need Python 3.10, 3.11 or 3.12 with tkinter.
  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv python3-tk
  (kokoro does not support Python 3.13 yet)"
  for cmd in pw-cat pw-play wpctl sha256sum; do
    command -v "$cmd" >/dev/null || die "missing command: $cmd (see README: Requirements)"
  done
  if [[ -z ${LISTEN_BOOK_MODEL_SOURCE:-} ]]; then
    command -v curl >/dev/null || die "missing command: curl (needed to download the model)"
  fi

  # espeak-ng, inside the voice engine, truncates its data path at 159
  # characters and then exits; refuse such a prefix before downloading anything
  local pyver espeak_data
  pyver="$("$py" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  espeak_data="$(realpath -m -- "$prefix")/venv/lib/python$pyver/site-packages/espeakng_loader/espeak-ng-data"
  if (( $(printf '%s' "$espeak_data" | wc -c) > 159 )); then
    die "install path too long: the voice engine (espeak-ng) needs its data path,
  $espeak_data
  to be at most 159 characters. Choose a shorter PREFIX."
  fi

  echo "Installing to $prefix (Python $("$py" -c 'import sys; print(sys.version.split()[0])'))"
  if [[ -e $prefix && ! -f $prefix/$MARKER && -n "$(ls -A -- "$prefix" 2>/dev/null)" ]]; then
    die "$prefix exists and was not created by this installer; choose another PREFIX"
  fi
  mkdir -p "$prefix"
  touch "$prefix/$MARKER"

  # App files: replaced in place; venv and models are left alone
  rm -rf -- "${prefix:?}/app"
  cp -r -- "$REPO/app" "$prefix/app"
  cp -- "$REPO/listen-book" "$REPO/models.sha256" "$REPO/requirements.txt" "$prefix/"

  if [[ ! -x $prefix/venv/bin/python ]]; then
    echo "Creating the voice engine environment…"
    "$py" -m venv "$prefix/venv"
  fi
  echo "Installing Python packages (torch is ~200 MB; first install takes a few minutes)…"
  "$prefix/venv/bin/python" -m pip install -q --upgrade pip
  "$prefix/venv/bin/python" -m pip install -q \
    --extra-index-url https://download.pytorch.org/whl/cpu -r "$prefix/requirements.txt"

  echo "Fetching the voice model (~330 MB, checked against models.sha256)…"
  fetch_models "$prefix/models"

  mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
  dest="$BIN_DIR/listen-book"
  if [[ -e $dest && ! -L $dest ]]; then
    echo "skipped $dest: a file with that name already exists" >&2
  elif [[ -L $dest && "$(readlink -- "$dest")" != */listen-book ]]; then
    echo "skipped $dest: it is a link to something else" >&2
  else
    ln -sfn -- "$prefix/listen-book" "$dest"
  fi
  cp -- "$REPO/share/icons/hicolor/scalable/apps/listen-book.svg" "$ICON_DIR/listen-book.svg"
  if [[ -e $APP_DIR/listen-book.desktop ]] && ! grep -qx "$DESKTOP_MARKER" "$APP_DIR/listen-book.desktop"; then
    echo "skipped $APP_DIR/listen-book.desktop: a menu entry not created by this installer exists" >&2
  else
    local line
    while IFS= read -r line; do
      if [[ $line == Exec=* ]]; then
        printf 'Exec=%s %%F\n' "$(desktop_quote "$prefix/listen-book" | sed 's/%/%%/g')"
      else
        printf '%s\n' "$line"
      fi
    done < "$REPO/share/applications/listen-book.desktop" > "$APP_DIR/listen-book.desktop"
    echo "$DESKTOP_MARKER" >> "$APP_DIR/listen-book.desktop"
  fi
  if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APP_DIR" 2>/dev/null || true
  fi

  echo
  echo "Installed. Run:  listen-book chapter.md   (or Listen Book in the app menu)"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH; run $prefix/listen-book directly or add it" ;;
  esac
}

uninstall() {
  local prefix
  prefix="$(realpath -m -- "$1")"
  if links_to "$BIN_DIR/listen-book" "$prefix/listen-book"; then
    rm -- "$BIN_DIR/listen-book"
    echo "removed $BIN_DIR/listen-book"
  fi
  # Only the menu entry that launches *this* install
  if [[ -f $APP_DIR/listen-book.desktop ]] && grep -qx "$DESKTOP_MARKER" "$APP_DIR/listen-book.desktop" \
     && grep -qF "$(desktop_quote "$prefix/listen-book" | sed 's/%/%%/g')" "$APP_DIR/listen-book.desktop"; then
    rm -- "$APP_DIR/listen-book.desktop"
    rm -f -- "$ICON_DIR/listen-book.svg"  # the icon goes with the menu entry that used it
    echo "removed $APP_DIR/listen-book.desktop"
  fi
  if [[ -f $prefix/$MARKER ]]; then
    rm -rf -- "${prefix:?}"
    echo "removed $prefix"
  elif [[ -e $prefix ]]; then
    echo "left $prefix alone: not created by this installer" >&2
  fi
  echo "Your place and settings (~/.config/listen-book) and audio cache (~/.cache/listen-book) were kept."
}

case "${1:-}" in
  --uninstall) uninstall "${2:-$HOME/.local/opt/listen-book}" ;;
  -h|--help) sed -n '2,13p' "$0" ;;
  -*) die "unknown option $1 (use --uninstall or --help)" ;;
  *) install "${1:-$HOME/.local/opt/listen-book}" ;;
esac

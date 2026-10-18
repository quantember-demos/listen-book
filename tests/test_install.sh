#!/usr/bin/env bash
# install.sh safety checks that need no download. Works only in a temp dir.
# check() evaluates its condition later, so conditions are single-quoted
# shellcheck disable=SC2016

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export BIN_DIR="$TMP/bin" APP_DIR="$TMP/apps" ICON_DIR="$TMP/icons"
mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

pass=0
fail=0
check() { if eval "$2"; then echo "  PASS  $1"; pass=$((pass+1)); else echo "  FAIL  $1"; fail=$((fail+1)); fi; }

echo "install.sh tests"

# 1. A non-empty directory the installer did not create is never installed into
mkdir -p "$TMP/foreign"
echo keep > "$TMP/foreign/precious.txt"
"$REPO/install.sh" "$TMP/foreign" >"$TMP/out" 2>&1
rc=$?
check "foreign prefix refused (rc=$rc)" '[[ $rc -ne 0 ]] && grep -q "not created by this installer" "$TMP/out"'
check "foreign prefix untouched" '[[ "$(ls "$TMP/foreign")" == precious.txt ]]'

# 2. Uninstall leaves a directory it did not create alone
"$REPO/install.sh" --uninstall "$TMP/foreign" >"$TMP/out" 2>&1
check "uninstall leaves foreign prefix" '[[ -f "$TMP/foreign/precious.txt" ]]'

# 3. Uninstall of a real (marked) install removes it, its link and menu entry,
#    and nothing else in the same directories
mkdir -p "$TMP/mine"
touch "$TMP/mine/.listen-book-install" "$TMP/mine/listen-book"
ln -s "$TMP/mine/listen-book" "$BIN_DIR/listen-book"
echo other > "$BIN_DIR/other-tool"
printf '[Desktop Entry]\nName=Listen Book\nExec="%s/listen-book" %%%%F\nX-Installed-By=listen-book-install.sh\n' "$TMP/mine" > "$APP_DIR/listen-book.desktop"
printf '[Desktop Entry]\nName=Other\n' > "$APP_DIR/other.desktop"
"$REPO/install.sh" --uninstall "$TMP/mine" >"$TMP/out" 2>&1
check "marked prefix removed" '[[ ! -e "$TMP/mine" ]]'
check "its command link removed" '[[ ! -e "$BIN_DIR/listen-book" ]]'
check "its menu entry removed" '[[ ! -e "$APP_DIR/listen-book.desktop" ]]'
check "other files kept" '[[ -f "$BIN_DIR/other-tool" && -f "$APP_DIR/other.desktop" ]]'

# 4a. Uninstalling one install keeps the menu entry of another
mkdir -p "$TMP/other"
touch "$TMP/other/.listen-book-install"
printf '[Desktop Entry]\nName=Listen Book\nExec="%s/listen-book" %%%%F\nX-Installed-By=listen-book-install.sh\n' "$TMP/elsewhere" > "$APP_DIR/listen-book.desktop"
"$REPO/install.sh" --uninstall "$TMP/other" >"$TMP/out" 2>&1
check "other install's menu entry kept" '[[ -f "$APP_DIR/listen-book.desktop" ]]'
rm -f "$APP_DIR/listen-book.desktop"

# 4b. A relative prefix is resolved against the current directory
mkdir -p "$TMP/rel/mine2"
touch "$TMP/rel/mine2/.listen-book-install"
(cd "$TMP/rel" && "$REPO/install.sh" --uninstall mine2 >"$TMP/out" 2>&1)
check "relative prefix resolved on uninstall" '[[ ! -e "$TMP/rel/mine2" ]]'

# 4c. Installing into the checkout itself is refused
"$REPO/install.sh" "$REPO" >"$TMP/out" 2>&1
check "checkout as prefix refused" 'grep -q "other than this checkout\|not created by this installer\|need Python" "$TMP/out" && [[ -d "$REPO/app" ]]'

# 4. A menu entry somebody else wrote is not removed
printf '[Desktop Entry]\nName=Listen Book (hand-made)\n' > "$APP_DIR/listen-book.desktop"
"$REPO/install.sh" --uninstall "$TMP/none" >"$TMP/out" 2>&1
check "unmarked menu entry kept" '[[ -f "$APP_DIR/listen-book.desktop" ]]'

# 5. A prefix too long for espeak-ng's 159-character data path is refused
#    before anything is downloaded (needs Python 3.10-3.12 with tkinter)
long="$TMP/$(head -c 120 /dev/zero | tr '\0' 'p')"
"$REPO/install.sh" "$long" >"$TMP/out" 2>&1
rc=$?
if grep -q "need Python 3.10" "$TMP/out"; then
  echo "  SKIP  long prefix (no suitable Python here)"
else
  check "too-long prefix refused (rc=$rc)" '[[ $rc -ne 0 ]] && grep -q "install path too long" "$TMP/out"'
  check "nothing created for it" '[[ ! -e "$long" ]]'
fi

# 6. Arguments
"$REPO/install.sh" --bogus >/dev/null 2>&1
rc=$?
check "unknown option rejected (rc=$rc)" '[[ $rc -ne 0 ]]'
check "--help prints usage" '"$REPO/install.sh" --help | grep -q "install or update"'

echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]

#!/usr/bin/env bash
#
# Arhivator - zero-setup launcher (Linux / macOS)
#
# Mirrors run.bat. Keep the two in sync when changing either.
#
# System packages that PySide6 needs at runtime are NOT installed by this
# script - that would require sudo. On Debian/Ubuntu:
#
#   sudo apt install libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
#                    libdbus-1-3 libfontconfig1 libxcb-cursor0 libxcb-icccm4 \
#                    libxcb-keysyms1 libxcb-shape0 libxcb-xkb1
#
# On a headless machine (SSH without X forwarding) there is no display to draw
# on; set QT_QPA_PLATFORM=offscreen to run without one.

set -euo pipefail

PROJECT_NAME="Arhivator"
MAIN_SOURCE_FILE="wavesnip"
VENV_DIR=".venv"
MIN_PYTHON="3.12"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Run from the project root regardless of where we were invoked, so
# double-clicking the script in a file manager works.
cd "$(dirname "$0")"

pause_if_interactive() {
    if [ -t 0 ]; then
        read -r -p "Press Enter to close..." _
    fi
}

die() {
    echo
    echo "ERROR: $1"
    shift
    for line in "$@"; do
        echo "$line"
    done
    pause_if_interactive
    exit 1
}

echo "==============================="
echo "  Starting $PROJECT_NAME"
echo "==============================="

echo ""
echo "         .-=+*#######*+=-."
echo "     =@@@@p. :#@@@@@#: .q@@@@@="
echo "    +@@@@@.#@. _-*-_ .@#.@@@@@+"
echo "   .@@@@@@.#@@@@@@@@@@@#.@@@@@@."
echo "   %@@@@@@ @@@P@@@@@P@@@ @@@@@@%"
echo "   +@%+   +@@ @ #@# @ @@+  +@%+"
echo "       -+#@@@@@@* *@@@@@@#+-"
echo "        -%@@@@:= : =:@@@@%-"
echo "         *@@@@@@@@@@@@@@@*"
echo "         "
echo "          M i š a s o f t"
echo ""

# ---------- Find a suitable Python ----------
# Python 3.12+ is required: the stdlib "wave" module only learned to read
# WAVE_FORMAT_EXTENSIBLE in 3.12, and many recorders emit it.
PYEXE=""
for candidate in python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
            >/dev/null 2>&1; then
        PYEXE="$candidate"
        break
    fi
done

[ -n "$PYEXE" ] || die \
    "Python $MIN_PYTHON or newer is required, but was not found." \
    'The stdlib "wave" module cannot read WAVE_FORMAT_EXTENSIBLE before' \
    "3.12, which many audio recorders emit." \
    "" \
    "  Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv" \
    "  Fedora:         sudo dnf install python3.12" \
    "  Otherwise:      https://www.python.org/downloads/"

"$PYEXE" -c 'import sys; print("Found Python", sys.version.split()[0])'

[ -f requirements.txt ] || die \
    "requirements.txt not found in $(pwd)." \
    "This script must be run from the project root."

# ---------- Create virtual environment if needed ----------
# Also recreates it when the existing one was built with an older Python.
if [ -x "$VENV_DIR/bin/python" ] &&
    ! "$VENV_DIR/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        >/dev/null 2>&1; then
    echo "Removing outdated virtual environment..."
    rm -rf "$VENV_DIR"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Creating virtual environment..."
    "$PYEXE" -m venv "$VENV_DIR" || die \
        "Failed to create the virtual environment." \
        "On Debian/Ubuntu the venv module ships separately:" \
        "  sudo apt install python3.12-venv"
fi

VENV_PY="$VENV_DIR/bin/python"

# No `source activate` here: calling the venv interpreter directly avoids
# mutating PATH, which is the usual source of "activated but still wrong
# python" problems.

# ---------- Upgrade pip ----------
echo "Updating pip..."
"$VENV_PY" -m pip install --upgrade pip ||
    echo "WARNING: pip upgrade failed, continuing."

# ---------- Install dependencies ----------
# Not silenced on purpose: first run downloads PySide6, which is large.
echo "Installing dependencies (first run downloads PySide6, please wait)..."
"$VENV_PY" -m pip install -r requirements.txt ||
    die "Dependency installation failed."

# ---------- Run app ----------
echo "Running application..."
"$VENV_PY" -m "$MAIN_SOURCE_FILE" || die "Application exited with an error."

echo
echo "Laku noc."

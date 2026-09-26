#!/usr/bin/env bash
# Installs Chess Mistake Coach and starts the web app (macOS and Linux).
#
#   bash install.sh
#
# Safe to run again: it skips whatever is already done, so running it later
# simply starts the app. Options:
#   --no-launch        set everything up but don't start the app
#   --skip-stockfish   don't try to install Stockfish

set -euo pipefail
cd "$(dirname "$0")"

LAUNCH=1
INSTALL_STOCKFISH=1
for arg in "$@"; do
  case "$arg" in
    --no-launch) LAUNCH=0 ;;
    --skip-stockfish) INSTALL_STOCKFISH=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# 1. Python 3.10 or newer -------------------------------------------------------
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || fail "Python 3.10 or newer was not found. Install it (macOS: 'brew install python'; Debian/Ubuntu: 'sudo apt install python3 python3-venv') and run this again."

# 2. Virtual environment + the app ----------------------------------------------
VENV=".venvs/chess"
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating a virtual environment in $VENV"
  "$PYTHON" -m venv "$VENV" ||
    fail "Could not create the virtual environment. On Debian/Ubuntu install the venv package: sudo apt install python3-venv"
fi

# Installed under its old name (Chess Error Tracker)? Remove that first so the old command doesn't linger.
"$VENV/bin/python" -m pip uninstall -y chess-error-tracker >/dev/null 2>&1 || true

say "Installing Chess Mistake Coach (this can take a minute the first time)"
"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -e ".[web]"

# 3. Stockfish ---------------------------------------------------------------------
find_stockfish() {
  "$VENV/bin/python" -c 'from chess_mistake_coach.engine import find_engine; print(find_engine() or "")'
}

install_stockfish() {
  if command -v brew >/dev/null 2>&1; then
    brew install stockfish
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y stockfish
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y stockfish
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm stockfish
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y stockfish
  else
    return 1
  fi
}

if [ "$INSTALL_STOCKFISH" = 1 ]; then
  if [ -n "$(find_stockfish)" ]; then
    say "Stockfish found"
  else
    say "Installing Stockfish"
    if install_stockfish && [ -n "$(find_stockfish)" ]; then
      say "Stockfish installed"
    else
      warn "Couldn't install Stockfish automatically. Install it with your package manager (the package is called 'stockfish'), or download it from https://stockfishchess.org/download/ and put it on your PATH."
    fi
  fi
fi

# 4. Start --------------------------------------------------------------------------
if [ "$LAUNCH" = 0 ]; then
  say "Done. Start the app any time with: $VENV/bin/chess-mistake-coach serve"
  exit 0
fi

say "Starting the app (press Ctrl+C to stop it)"
exec "$VENV/bin/chess-mistake-coach" serve

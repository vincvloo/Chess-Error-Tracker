"""Stockfish binary discovery."""

from __future__ import annotations

import glob
import os
import shutil
import sys


def find_engine() -> str | None:
    """
    Locate a Stockfish binary without the user having to say where it is.

    Order: an explicit CHESS_ENGINE environment variable, then PATH, then the
    handful of places each platform actually installs it. Returns None if
    nothing turns up, so the caller can print a useful message.
    """
    env = os.environ.get("CHESS_ENGINE")
    if env and os.path.isfile(env):
        return env

    for name in ("stockfish", "stockfish.exe"):
        found = shutil.which(name)
        if found:
            return found

    candidates: list[str] = []

    if sys.platform == "win32":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.path.expanduser("~"),
            "C:\\Tools",
        ]
        # winget shims, plus the usual manual-unzip locations
        for root in filter(None, roots):
            candidates += [
                os.path.join(root, "Microsoft", "WinGet", "Links", "stockfish.exe"),
                os.path.join(root, "stockfish", "stockfish.exe"),
                os.path.join(root, "Stockfish", "stockfish.exe"),
                os.path.join(root, "Downloads", "stockfish", "stockfish.exe"),
            ]
        # Official builds ship as stockfish-windows-x86-64-<arch>.exe, so glob
        # for whatever variant was downloaded rather than guessing the suffix.
        for root in filter(None, roots):
            for pattern in ("stockfish*/stockfish*.exe", "stockfish*.exe",
                            "Downloads/stockfish*/*/stockfish*.exe",
                            "Downloads/stockfish*/stockfish*.exe"):
                candidates += sorted(glob.glob(os.path.join(root, pattern)))

        # winget install Stockfish drops a portable package here instead of a
        # PATH shim, under a publisher-hash suffix that changes between
        # machines, so it has to be globbed rather than named outright.
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidates += sorted(glob.glob(os.path.join(
                localappdata, "Microsoft", "WinGet", "Packages",
                "Stockfish.Stockfish_*", "stockfish", "stockfish*.exe")))

    elif sys.platform == "darwin":
        candidates += [
            "/opt/homebrew/bin/stockfish",   # Apple silicon
            "/usr/local/bin/stockfish",      # Intel
            "/opt/local/bin/stockfish",      # MacPorts
        ]

    else:
        candidates += [
            "/usr/games/stockfish",          # Debian and Ubuntu package
            "/usr/bin/stockfish",
            "/usr/local/bin/stockfish",
            "/snap/bin/stockfish",
            os.path.expanduser("~/.local/bin/stockfish"),
        ]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK if sys.platform != "win32" else os.F_OK):
            return path
    return None


ENGINE_HELP = """
Stockfish was not found. Either pass --engine with the full path, set the
CHESS_ENGINE environment variable, or install it:

  Windows   winget install Stockfish
            or download from stockfishchess.org and unzip to C:\\Tools\\stockfish
  macOS     brew install stockfish
  Debian    sudo apt install stockfish
""".strip()

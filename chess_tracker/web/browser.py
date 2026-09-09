"""Launch a browser window in "app mode" (no address bar) pointed at the
local server. Separate probing logic from engine.py's Stockfish discovery --
different binaries, different install locations -- but the same spirit:
check PATH, then the handful of places each platform actually installs
Chrome/Edge."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def find_app_mode_browser() -> str | None:
    """
    Locate a Chrome- or Edge-family binary that supports --app= launch mode.
    Returns None if nothing turns up, so the caller can fall back to a plain
    webbrowser.open().
    """
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser",
                 "msedge", "microsoft-edge", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found

    candidates: list[str] = []

    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for root in filter(None, roots):
            candidates += [
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]

    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]

    else:
        candidates += [
            "/usr/bin/google-chrome", "/usr/bin/chromium",
            "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def open_app_window(url: str) -> bool:
    """
    Open `url` in a chrome-less app-mode window if a Chrome/Edge binary is
    found. Returns True if it launched one; the caller should fall back to
    webbrowser.open(url) if this returns False.
    """
    browser = find_app_mode_browser()
    if not browser:
        return False
    subprocess.Popen([browser, f"--app={url}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True

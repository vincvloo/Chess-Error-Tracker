"""Checking for and applying updates from the app's own git repo.

End users install this app as a real git checkout (`pip install -e .`
against a local clone) -- there's no packaged installer and no auto-update,
so without this, staying current means personally knowing to run
`git pull` in a terminal. This lets the web app do that itself: a quiet
background check against the configured upstream, and a one-click way to
pull it. Deliberately does NOT try to restart the running server -- it has
no restart mechanism today (uvicorn.run() blocks in-process), so a failed
self-restart could leave the app not running at all with no way back in.
Pulling the code and asking the user to restart manually has no failure
mode worse than quitting the app normally does.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Git commands here talk to whatever the user's own remote/upstream is --
# never assume it's GitHub specifically. A network hiccup or a slow remote
# must not hang the background check indefinitely.
_GIT_TIMEOUT = 10


def find_git() -> str | None:
    """Anyone who could `git clone` this repo already has git on PATH --
    no extra install-location guessing needed, unlike find_engine()."""
    return shutil.which("git")


def repo_root() -> Path:
    """The checkout root: chess_mistake_coach/web/updater.py -> chess_mistake_coach/web
    -> chess_mistake_coach -> repo root, per pyproject.toml's package layout
    (chess_mistake_coach/ sits directly under it)."""
    return Path(__file__).resolve().parent.parent.parent


def _git(git_path: str, repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git_path, "-C", str(repo), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT)


def check_for_update(git_path: str, repo: Path) -> dict:
    """
    {"available": bool, "reason": str | None}. "reason" explains a False
    result that isn't really "you're up to date" (not a git checkout, no
    upstream configured, the fetch itself failed) so the caller can stay
    silent rather than show a confusing banner over something that isn't
    the user's fault.
    """
    try:
        inside = _git(git_path, repo, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"available": False, "reason": "not a git checkout"}

        upstream = _git(git_path, repo, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{u}")
        if upstream.returncode != 0:
            # Also the normal case on a dev feature branch with no upstream
            # configured -- correctly makes this a no-op there.
            return {"available": False, "reason": "no upstream configured"}

        fetch = _git(git_path, repo, "fetch", "--quiet")
        if fetch.returncode != 0:
            return {"available": False, "reason": f"fetch failed: {fetch.stderr.strip()}"}

        head = _git(git_path, repo, "rev-parse", "HEAD")
        remote = _git(git_path, repo, "rev-parse", "@{u}")
        if head.returncode != 0 or remote.returncode != 0:
            return {"available": False, "reason": "couldn't read commits"}

        return {"available": head.stdout.strip() != remote.stdout.strip(), "reason": None}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "reason": str(exc)}


def apply_update(git_path: str, repo: Path) -> dict:
    """
    {"ok": bool, "message": str}. Only called when the user clicks Update,
    so unlike check_for_update() it's fine (expected, even) to surface a
    real error here.
    """
    try:
        status = _git(git_path, repo, "status", "--porcelain")
        if status.returncode != 0:
            return {"ok": False, "message": "Couldn't check the app folder for local changes."}
        if status.stdout.strip():
            return {"ok": False, "message": "There are local changes in the app folder, "
                    "so an automatic update was skipped to avoid overwriting anything."}

        # --ff-only: never merges, never rebases -- fails cleanly with
        # nothing changed if history has diverged for any reason, rather
        # than doing anything that could need manual conflict resolution.
        pull = _git(git_path, repo, "pull", "--ff-only")
        if pull.returncode != 0:
            return {"ok": False, "message": f"Update failed: {pull.stderr.strip()}"}

        message = "Updated! Close and reopen the app to finish."
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", f"{repo}[web]"],
                capture_output=True, text=True, timeout=120, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            # Best-effort: the code update above already succeeded, which
            # matters more than picking up a dependency change immediately.
            message += (" (Couldn't refresh dependencies automatically -- if "
                        "something looks broken after restarting, run "
                        "\"pip install -e .[web]\" once.)")
        return {"ok": True, "message": message}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "message": f"Update failed: {exc}"}

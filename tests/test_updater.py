"""Real local git repos, no network and no mocking of git itself -- cheap
and fast to set up, and gives genuine confidence that the actual git
commands in updater.py behave the way it assumes."""
import subprocess
from pathlib import Path

import pytest

from chess_tracker.web import updater

GIT = updater.find_git()
pytestmark = pytest.mark.skipif(GIT is None, reason="git not found on PATH")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _commit(repo: Path, filename: str = "file.txt", content: str = "hello") -> None:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test",
        "commit", "-m", "commit")


def _origin_with_a_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare "origin" repo with one commit, and a clone of it (the
    "local" checkout under test) sitting at that same commit."""
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run([GIT, "clone", str(origin), str(seed)], check=True, capture_output=True)
    _commit(seed)
    _git(seed, "push", "origin", "HEAD")

    local = tmp_path / "local"
    subprocess.run([GIT, "clone", str(origin), str(local)], check=True, capture_output=True)
    return origin, local


def test_up_to_date_reports_unavailable(tmp_path):
    _origin, local = _origin_with_a_clone(tmp_path)
    result = updater.check_for_update(GIT, local)
    assert result == {"available": False, "reason": None}


def test_new_commit_upstream_is_detected(tmp_path):
    origin, local = _origin_with_a_clone(tmp_path)

    pusher = tmp_path / "pusher"
    subprocess.run([GIT, "clone", str(origin), str(pusher)], check=True, capture_output=True)
    _commit(pusher, "new_file.txt", "new stuff")
    _git(pusher, "push", "origin", "HEAD")

    result = updater.check_for_update(GIT, local)
    assert result == {"available": True, "reason": None}


def test_not_a_git_checkout_is_silent(tmp_path):
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    result = updater.check_for_update(GIT, plain_dir)
    assert result["available"] is False
    assert result["reason"]


def test_no_upstream_configured_is_silent(tmp_path):
    repo = tmp_path / "standalone"
    subprocess.run([GIT, "init", str(repo)], check=True, capture_output=True)
    _commit(repo)
    result = updater.check_for_update(GIT, repo)
    assert result == {"available": False, "reason": "no upstream configured"}


def test_apply_update_refuses_with_a_dirty_working_tree(tmp_path):
    _origin, local = _origin_with_a_clone(tmp_path)
    (local / "file.txt").write_text("uncommitted edit")

    before = _git(local, "rev-parse", "HEAD").stdout
    result = updater.apply_update(GIT, local)
    after = _git(local, "rev-parse", "HEAD").stdout

    assert result["ok"] is False
    assert "local changes" in result["message"]
    assert before == after


def test_apply_update_pulls_the_new_commit(tmp_path):
    origin, local = _origin_with_a_clone(tmp_path)

    pusher = tmp_path / "pusher"
    subprocess.run([GIT, "clone", str(origin), str(pusher)], check=True, capture_output=True)
    _commit(pusher, "new_file.txt", "new stuff")
    _git(pusher, "push", "origin", "HEAD")

    # apply_update() also best-effort pip-installs the repo as a package
    # afterwards, which fails here since this fake repo isn't one -- that's
    # fine, it's already only ever a warning appended to the message, never
    # a reason for "ok" to be False; what matters is the git pull worked.
    result = updater.apply_update(GIT, local)
    assert result["ok"] is True

    local_head = _git(local, "rev-parse", "HEAD").stdout
    origin_head = _git(origin, "rev-parse", "HEAD").stdout
    assert local_head == origin_head

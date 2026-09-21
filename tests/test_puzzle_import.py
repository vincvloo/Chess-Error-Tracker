import threading
import time

import pytest

from chess_tracker.db import open_db
from chess_tracker.puzzles import PuzzleImportCancelled
from chess_tracker.web.puzzle_import import PuzzleImportAlreadyRunningError, PuzzleImportManager


def _wait_for(mgr: PuzzleImportManager, job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = mgr.get_status(job_id)
        if status["state"] not in ("queued", "downloading", "importing"):
            return status
        time.sleep(0.01)
    raise TimeoutError(f"puzzle import job {job_id} did not finish in time")


def _manager(tmp_path) -> PuzzleImportManager:
    db_path = str(tmp_path / "test.db")
    open_db(db_path).close()
    return PuzzleImportManager(db_path)


def test_start_job_reaches_done_state_reusing_a_cached_source(tmp_path, monkeypatch):
    """When find_puzzle_source() already finds a cached CSV, no download
    should happen at all -- straight to importing."""
    calls = []
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/already/cached.csv")

    def fake_import_puzzles(conn, source_path, **kwargs):
        calls.append(source_path)
        if kwargs.get("progress_cb"):
            kwargs["progress_cb"](100, 10)
        return None
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    mgr = _manager(tmp_path)
    status = mgr.start_job(min_rating=1000, max_rating=2000)
    final = _wait_for(mgr, status.id)

    assert final["state"] == "done"
    assert calls == ["/already/cached.csv"]
    assert final["scanned"] == 100
    assert final["imported"] == 10


def test_start_job_downloads_when_no_source_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source", lambda: None)
    download_calls = []

    def fake_download(progress_cb=None, cancel_event=None):
        download_calls.append(True)
        if progress_cb:
            progress_cb(500, 1000)
        return "/freshly/downloaded.csv"
    monkeypatch.setattr("chess_tracker.web.puzzle_import.download_puzzle_source", fake_download)
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles",
                        lambda conn, source_path, **kwargs: None)

    mgr = _manager(tmp_path)
    status = mgr.start_job(min_rating=None, max_rating=None)
    final = _wait_for(mgr, status.id)

    assert final["state"] == "done"
    assert download_calls == [True]


def test_start_job_rejects_concurrent_imports(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")

    def fake_import_puzzles(conn, source_path, **kwargs):
        started.set()
        release.wait(timeout=2)
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    mgr = _manager(tmp_path)
    mgr.start_job(min_rating=None, max_rating=None)
    assert started.wait(timeout=2)

    with pytest.raises(PuzzleImportAlreadyRunningError):
        mgr.start_job(min_rating=None, max_rating=None)

    release.set()


def test_cancel_reports_cancelled_state(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")

    def fake_import_puzzles(conn, source_path, cancel_event=None, **kwargs):
        cancel_event.wait(timeout=2)
        raise PuzzleImportCancelled()
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    mgr = _manager(tmp_path)
    status = mgr.start_job(min_rating=None, max_rating=None)
    assert mgr.cancel(status.id) is True
    final = _wait_for(mgr, status.id)
    assert final["state"] == "cancelled"


def test_cancel_unknown_job_returns_false(tmp_path):
    assert _manager(tmp_path).cancel("no-such-job") is False


def test_get_status_unknown_job_returns_none(tmp_path):
    assert _manager(tmp_path).get_status("no-such-job") is None


def test_get_active_job_id_reflects_running_and_finished_state(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")

    def fake_import_puzzles(conn, source_path, **kwargs):
        started.set()
        release.wait(timeout=2)
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    mgr = _manager(tmp_path)
    assert mgr.get_active_job_id() is None
    status = mgr.start_job(min_rating=None, max_rating=None)
    assert started.wait(timeout=2)
    assert mgr.get_active_job_id() == status.id

    release.set()
    _wait_for(mgr, status.id)
    assert mgr.get_active_job_id() is None


def test_job_error_state_captures_unexpected_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")

    def fake_import_puzzles(conn, source_path, **kwargs):
        raise RuntimeError("disk is full")
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    mgr = _manager(tmp_path)
    status = mgr.start_job(min_rating=None, max_rating=None)
    final = _wait_for(mgr, status.id)
    assert final["state"] == "error"
    assert "disk is full" in final["error"]

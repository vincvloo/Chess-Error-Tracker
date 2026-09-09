import threading
import time

import pytest

from chess_tracker.chesscom import ChessComError
from chess_tracker.db import open_db
from chess_tracker.web.jobs import JobAlreadyRunningError, JobManager


def _wait_for(jobs: JobManager, job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = jobs.get_status(job_id)
        if status["state"] not in ("queued", "running"):
            return status
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not finish in time")


def _jobs(tmp_path) -> JobManager:
    db_path = str(tmp_path / "test.db")
    open_db(db_path).close()
    return JobManager(db_path)


def test_start_job_reaches_done_state(tmp_path, monkeypatch):
    calls = []

    def fake_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        calls.append(users)
        if progress_cb:
            progress_cb(users[0], 1, 1)

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", fake_run_analysis)

    jobs = _jobs(tmp_path)
    status = jobs.start_job(["alice"], "you@example.com", "/fake/engine", 14, 2, 0.1)
    final = _wait_for(jobs, status.id)

    assert final["state"] == "done"
    assert calls == [["alice"]]
    assert final["current_index"] == 1
    assert final["current_total"] == 1
    assert final["per_user"]["alice"] == {"analysed": 1, "todo": 1}


def test_start_job_rejects_concurrent_jobs(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", fake_run_analysis)

    jobs = _jobs(tmp_path)
    jobs.start_job(["alice"], "you@example.com", "/fake/engine", 14, 2, 0.1)
    assert started.wait(timeout=2)

    with pytest.raises(JobAlreadyRunningError):
        jobs.start_job(["bob"], "you@example.com", "/fake/engine", 14, 2, 0.1)

    release.set()


def test_job_error_state_captures_chesscomerror(tmp_path, monkeypatch):
    def fake_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        raise ChessComError("No such Chess.com user: bogus")

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", fake_run_analysis)

    jobs = _jobs(tmp_path)
    status = jobs.start_job(["bogus"], "you@example.com", "/fake/engine", 14, 2, 0.1)
    final = _wait_for(jobs, status.id)

    assert final["state"] == "error"
    assert "No such Chess.com user" in final["error"]


def test_job_error_state_captures_unexpected_exceptions(tmp_path, monkeypatch):
    def fake_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", fake_run_analysis)

    jobs = _jobs(tmp_path)
    status = jobs.start_job(["alice"], "you@example.com", "/fake/engine", 14, 2, 0.1)
    final = _wait_for(jobs, status.id)

    assert final["state"] == "error"
    assert "boom" in final["error"]
    # the slot must be freed even after an unexpected exception, or every
    # future job start would be rejected as "already running" forever
    jobs.start_job(["bob"], "you@example.com", "/fake/engine", 14, 2, 0.1)


def test_cancel_reports_cancelled_state(tmp_path, monkeypatch):
    def fake_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        cancel_event.wait(timeout=2)

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", fake_run_analysis)

    jobs = _jobs(tmp_path)
    status = jobs.start_job(["alice"], "you@example.com", "/fake/engine", 14, 2, 0.1)
    assert jobs.cancel(status.id) is True
    final = _wait_for(jobs, status.id)
    assert final["state"] == "cancelled"


def test_cancel_unknown_job_returns_false(tmp_path):
    assert _jobs(tmp_path).cancel("nonexistent") is False


def test_get_status_unknown_job_returns_none(tmp_path):
    assert _jobs(tmp_path).get_status("nonexistent") is None

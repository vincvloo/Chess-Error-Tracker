"""The progress page's time estimates come from this machine's own past runs,
not a fixed per-game guess."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from chess_tracker.analysis_runner import DEFAULT_WORKERS
from chess_tracker.db import open_db
from chess_tracker.web.app import create_app
from chess_tracker.web.jobs import (SECONDS_PER_GAME, estimate_seconds_per_game,
                                    measured_seconds_per_game)


def _run(conn, seconds, games, depth=14, user="alice"):
    conn.execute(
        "INSERT INTO runs (username, started_at, finished_at, requests_made, games_new, depth) "
        "VALUES (?, ?, ?, 2, ?, ?)",
        (user, "2026-01-01T10:00:00+00:00",
         f"2026-01-01T10:{seconds // 60:02d}:{seconds % 60:02d}+00:00", games, depth))
    conn.commit()


def test_no_history_falls_back_to_the_fixed_guess_spread_over_the_workers():
    est = estimate_seconds_per_game(open_db(":memory:"), 14, 100, 8)
    assert est["serial"] == SECONDS_PER_GAME
    assert est["parallel"] == SECONDS_PER_GAME / 8
    assert not est["serialMeasured"] and not est["parallelMeasured"]


def test_parallel_and_serial_speeds_are_measured_separately():
    conn = open_db(":memory:")
    _run(conn, 158, 152)   # a parallel-sized batch: about 1 s/game
    _run(conn, 60, 10)     # a small serial one: 6 s/game
    serial, parallel = measured_seconds_per_game(conn, 14, 100)
    assert round(parallel, 2) == round(158 / 152, 2)
    assert serial == 6
    est = estimate_seconds_per_game(conn, 14, 100, 8)
    assert est["serialMeasured"] and est["parallelMeasured"]


def test_speed_is_the_median_of_recent_runs_so_one_freak_run_cannot_skew_it():
    conn = open_db(":memory:")
    for seconds in (100, 110, 90):   # about 1 s/game
        _run(conn, seconds, 100)
    _run(conn, 3000, 100)            # a laptop that slept mid-run: 30 s/game
    assert measured_seconds_per_game(conn, 14, 100)[1] == 1.05


def test_only_the_most_recent_runs_count():
    conn = open_db(":memory:")
    for _ in range(10):
        _run(conn, 5000, 100)        # old, slow era: 50 s/game
    for _ in range(5):
        _run(conn, 100, 100)         # recent: 1 s/game
    assert measured_seconds_per_game(conn, 14, 100)[1] == 1.0


def test_only_runs_at_the_same_depth_count():
    conn = open_db(":memory:")
    _run(conn, 100, 100, depth=20)
    assert measured_seconds_per_game(conn, 14, 100) == (None, None)


def test_tiny_and_empty_runs_are_ignored_as_overhead():
    conn = open_db(":memory:")
    _run(conn, 30, 2)   # too small to say anything about per-game speed
    _run(conn, 5, 0)    # nothing analysed
    assert measured_seconds_per_game(conn, 14, 100) == (None, None)


def test_a_run_missing_its_finish_time_is_skipped_not_fatal():
    conn = open_db(":memory:")
    conn.execute("INSERT INTO runs (username, started_at, finished_at, games_new, depth) "
                 "VALUES ('alice', '2026-01-01T10:00:00+00:00', NULL, 50, 14)")
    conn.commit()
    assert measured_seconds_per_game(conn, 14, 100) == (None, None)


def _progress_page(db_path):
    app = create_app(db_path)
    app.state.jobs = MagicMock()
    app.state.jobs.get_status.return_value = {"users": ["alice"], "state": "running"}
    return TestClient(app).get("/jobs/abc").text


def test_progress_page_uses_the_measured_speed_when_there_is_history(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = open_db(db_path)
    _run(conn, 120, 120)   # 1 s/game across a parallel-sized batch
    conn.close()
    page = _progress_page(db_path)
    assert "SECONDS_PER_GAME_PARALLEL = 1.0" in page
    assert "parallel: true" in page


def test_progress_page_says_roughly_until_it_has_history(tmp_path):
    db_path = str(tmp_path / "t.db")
    open_db(db_path).close()
    page = _progress_page(db_path)
    assert "parallel: false" in page and "serial: false" in page
    # The guess is spread over however many workers this machine gets.
    assert f"SECONDS_PER_GAME_PARALLEL = {SECONDS_PER_GAME / DEFAULT_WORKERS}" in page

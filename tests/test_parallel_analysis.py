"""Tests for the parallel-analysis path in analysis_runner.py.

A real spawned worker process (Windows uses the `spawn` start method) always
re-imports the real, unpatched module -- unittest.mock.patch cannot reach
into it. So true multi-process correctness (N real workers, a real Stockfish
binary, a real temp-file db) is verified manually against a real backlog,
never here -- matching this codebase's existing convention of never needing
a real Stockfish binary in the automated suite. What IS tested here, entirely
in-process:

- The pure helpers (_partition_games, _resolve_db_path) directly.
- _analyse_shard() called directly (not via a process) with a real
  multiprocessing.Queue/Event and a mocked engine/analyse_game -- this is
  legitimate because calling it directly never crosses a process boundary,
  so mocking works normally.
- _run_parallel()'s orchestration (progress aggregation, the runs row,
  shard-failure handling, cancellation forwarding) by substituting
  ProcessPoolExecutor with ThreadPoolExecutor (real async Future timing,
  same process, so mocking still works) and patching _analyse_shard itself
  with a fast fake.
- run_analysis()'s threshold/gating decision, by patching _run_parallel
  itself with a MagicMock.
"""
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from chess_tracker.analysis import INACCURACY
from chess_tracker.analysis_runner import (
    _analyse_shard,
    _partition_games,
    _resolve_db_path,
    run_analysis,
)
from chess_tracker.db import open_db, save_game


def _fake_rec(url, username):
    return {
        "url": url, "username": username, "end_time": 1000, "date": "2024-01-01",
        "time_class": "blitz", "my_colour": "white", "my_rating": 1500,
        "opp_rating": 1400, "result": "win", "eco": "C00", "moves_played": 20,
        "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
    }


# ---- _partition_games ------------------------------------------------

def test_partition_games_even_split():
    games = [{"url": f"g{i}"} for i in range(8)]
    shards = _partition_games(games, 4)
    assert len(shards) == 4
    assert all(len(s) == 2 for s in shards)
    assert sorted(g["url"] for s in shards for g in s) == [f"g{i}" for i in range(8)]


def test_partition_games_uneven_split_round_robins_the_remainder():
    games = [{"url": f"g{i}"} for i in range(10)]
    shards = _partition_games(games, 3)
    assert len(shards) == 3
    assert sorted(len(s) for s in shards) == [3, 3, 4]
    assert sum(len(s) for s in shards) == 10


def test_partition_games_workers_one_returns_one_sorted_shard():
    games = [{"url": "g2"}, {"url": "g0"}, {"url": "g1"}]
    shards = _partition_games(games, 1)
    assert len(shards) == 1
    assert [g["url"] for g in shards[0]] == ["g0", "g1", "g2"]


def test_partition_games_more_workers_than_games_yields_empty_shards():
    games = [{"url": "g0"}, {"url": "g1"}]
    shards = _partition_games(games, 5)
    assert len(shards) == 5
    assert sum(len(s) for s in shards) == 2
    assert sum(1 for s in shards if not s) == 3


def test_partition_games_is_deterministic_regardless_of_input_order():
    ordered = [{"url": f"g{i}"} for i in range(9)]
    shuffled = list(reversed(ordered))
    assert _partition_games(ordered, 3) == _partition_games(shuffled, 3)


# ---- _resolve_db_path --------------------------------------------------

def test_resolve_db_path_is_none_for_in_memory_db():
    conn = open_db(":memory:")
    assert _resolve_db_path(conn) is None


def test_resolve_db_path_returns_the_real_file_path(tmp_path):
    db_path = str(tmp_path / "real.db")
    conn = open_db(db_path)
    resolved = _resolve_db_path(conn)
    assert resolved is not None
    assert resolved.endswith("real.db")


# ---- _analyse_shard (called directly, not via a process) --------------

@patch("chess.engine.SimpleEngine.popen_uci")
def test_analyse_shard_saves_games_and_returns_the_count(mock_popen, tmp_path):
    mock_popen.return_value = MagicMock()
    db_path = str(tmp_path / "shard.db")
    open_db(db_path).close()
    games = [{"url": "https://x/g1"}, {"url": "https://x/g2"}]

    def fake_analyse(game_json, user, engine, depth, min_loss):
        return _fake_rec(game_json["url"], user), []

    q: multiprocessing.Queue = multiprocessing.Queue()
    with patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        new = _analyse_shard(db_path, "alice", games, "/fake/stockfish", 14, 2,
                             INACCURACY, q, 0, multiprocessing.Event())

    assert new == 2
    conn = open_db(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM games WHERE username='alice'"
                        ).fetchone()["c"] == 2


@patch("chess.engine.SimpleEngine.popen_uci")
def test_analyse_shard_reports_progress_via_the_queue(mock_popen, tmp_path):
    mock_popen.return_value = MagicMock()
    db_path = str(tmp_path / "shard.db")
    open_db(db_path).close()
    games = [{"url": "https://x/g1"}, {"url": "https://x/g2"}]

    def fake_analyse(game_json, user, engine, depth, min_loss):
        return _fake_rec(game_json["url"], user), []

    q: multiprocessing.Queue = multiprocessing.Queue()
    with patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        _analyse_shard(db_path, "alice", games, "/fake/stockfish", 14, 2,
                       INACCURACY, q, 3, multiprocessing.Event())

    seen = []
    while not q.empty():
        seen.append(q.get())
    assert seen == [(3, 1, 2), (3, 2, 2)]


@patch("chess.engine.SimpleEngine.popen_uci")
def test_analyse_shard_stops_early_once_cancelled(mock_popen, tmp_path):
    mock_popen.return_value = MagicMock()
    db_path = str(tmp_path / "shard.db")
    open_db(db_path).close()
    games = [{"url": f"https://x/g{i}"} for i in range(5)]
    cancel = multiprocessing.Event()
    processed = []

    def fake_analyse(game_json, user, engine, depth, min_loss):
        processed.append(game_json["url"])
        if len(processed) == 2:
            cancel.set()
        return _fake_rec(game_json["url"], user), []

    q: multiprocessing.Queue = multiprocessing.Queue()
    with patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        new = _analyse_shard(db_path, "alice", games, "/fake/stockfish", 14, 2,
                             INACCURACY, q, 0, cancel)

    assert new == 2
    assert len(processed) == 2


# ---- run_analysis()'s threshold/gating decision ------------------------

@patch("chess_tracker.analysis_runner._run_parallel")
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_stays_serial_below_the_threshold(mock_popen, mock_parallel, tmp_path):
    mock_popen.return_value = MagicMock()
    games = [{"url": "https://x/g1"}]
    conn = open_db(str(tmp_path / "t.db"))
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game",
              side_effect=lambda g, u, e, d, m: (_fake_rec(g["url"], u), [])):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True, parallel_threshold=0, workers=1)
    mock_parallel.assert_not_called()


@patch("chess_tracker.analysis_runner._run_parallel")
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_goes_parallel_above_the_threshold_with_a_real_db(
        mock_popen, mock_parallel, tmp_path):
    mock_popen.return_value = MagicMock()
    mock_parallel.return_value = 3
    games = [{"url": f"https://x/g{i}"} for i in range(5)]
    conn = open_db(str(tmp_path / "t.db"))
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True, parallel_threshold=2, workers=4)

    mock_parallel.assert_called_once()
    args = mock_parallel.call_args[0]
    assert args[1] == "alice"
    assert len(args[2]) == 5
    run_row = conn.execute("SELECT * FROM runs WHERE username='alice'").fetchone()
    assert run_row["games_new"] == 3


@patch("chess_tracker.analysis_runner._run_parallel")
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_stays_serial_for_an_in_memory_db_even_above_threshold(
        mock_popen, mock_parallel):
    mock_popen.return_value = MagicMock()
    games = [{"url": f"https://x/g{i}"} for i in range(5)]
    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game",
              side_effect=lambda g, u, e, d, m: (_fake_rec(g["url"], u), [])):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True, parallel_threshold=2, workers=4)
    mock_parallel.assert_not_called()


# ---- _run_parallel orchestration, via ThreadPoolExecutor in place of ----
# ---- ProcessPoolExecutor (real async Future timing, same process,   ----
# ---- so mocking _analyse_shard still works)                         ----

def _fake_shard_worker(db_path, user, games, engine_path, depth, threads, min_loss,
                       progress_queue, shard_index, cancel_event, delay=0.02):
    """Stands in for _analyse_shard in _run_parallel tests: no real engine,
    just reports progress and respects cancellation like the real one does,
    with a small sleep so the parent's polling loop gets to iterate."""
    new = 0
    for i, g in enumerate(games, 1):
        if cancel_event.is_set():
            break
        time.sleep(delay)
        new += 1
        progress_queue.put((shard_index, i, len(games)))
    return new


@patch("chess_tracker.analysis_runner._analyse_shard", side_effect=_fake_shard_worker)
@patch("chess_tracker.analysis_runner.ProcessPoolExecutor", ThreadPoolExecutor)
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_parallel_aggregates_progress_and_runs_row(
        mock_popen, mock_shard, tmp_path):
    mock_popen.return_value = MagicMock()
    games = [{"url": f"https://x/g{i}"} for i in range(12)]
    conn = open_db(str(tmp_path / "t.db"))
    progress_calls = []

    with patch("chess_tracker.analysis_runner.collect_games", return_value=games):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=4, pause=0, quiet=True, parallel_threshold=2, workers=3,
                     progress_cb=lambda u, i, t: progress_calls.append((u, i, t)))

    assert progress_calls, "progress_cb should have been called at least once"
    assert progress_calls[-1] == ("alice", 12, 12)
    assert all(t == 12 for _, _, t in progress_calls)
    assert list(i for _, i, _ in progress_calls) == sorted(i for _, i, _ in progress_calls)

    run_row = conn.execute("SELECT * FROM runs WHERE username='alice'").fetchone()
    assert run_row["games_new"] == 12


@patch("chess_tracker.analysis_runner.ProcessPoolExecutor", ThreadPoolExecutor)
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_parallel_records_partial_progress_on_shard_failure(
        mock_popen, tmp_path):
    mock_popen.return_value = MagicMock()
    games = [{"url": f"https://x/g{i}"} for i in range(6)]
    conn = open_db(str(tmp_path / "t.db"))

    def flaky_shard(db_path, user, shard_games, engine_path, depth, threads, min_loss,
                    progress_queue, shard_index, cancel_event):
        if shard_index == 1:
            raise RuntimeError("engine crashed")
        return _fake_shard_worker(db_path, user, shard_games, engine_path, depth,
                                  threads, min_loss, progress_queue, shard_index,
                                  cancel_event)

    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner._analyse_shard", side_effect=flaky_shard):
        try:
            run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                         threads=2, pause=0, quiet=True, parallel_threshold=2, workers=3)
            raised = False
        except RuntimeError:
            raised = True

    assert raised, "a shard failure must propagate to the caller"
    run_row = conn.execute("SELECT * FROM runs WHERE username='alice'").fetchone()
    assert run_row is not None
    assert 0 < run_row["games_new"] < 6


def _slow_fake_shard_worker(*args, **kwargs):
    # A longer per-game delay than the other _run_parallel tests use, so a
    # shard is still mid-flight (not already finished) by the time the
    # parent's polling loop has a chance to observe and forward cancellation
    # -- avoids flakiness from a shard racing to completion before the
    # cancel_event is even set.
    return _fake_shard_worker(*args, delay=0.05, **kwargs)


@patch("chess_tracker.analysis_runner._analyse_shard", side_effect=_slow_fake_shard_worker)
@patch("chess_tracker.analysis_runner.ProcessPoolExecutor", ThreadPoolExecutor)
@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_parallel_forwards_cancel_event_and_saves_partial_progress(
        mock_popen, mock_shard, tmp_path):
    mock_popen.return_value = MagicMock()
    games = [{"url": f"https://x/g{i}"} for i in range(60)]
    conn = open_db(str(tmp_path / "t.db"))
    cancel_event = threading.Event()

    def progress_cb(user, i, total):
        if i >= 5:
            cancel_event.set()

    with patch("chess_tracker.analysis_runner.collect_games", return_value=games):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=4, pause=0, quiet=True, parallel_threshold=2, workers=3,
                     progress_cb=progress_cb, cancel_event=cancel_event)

    run_row = conn.execute("SELECT * FROM runs WHERE username='alice'").fetchone()
    # Each shard has 20 games at 0.05s/game (~1s to finish uncancelled) --
    # cancellation should catch all three shards well before completion.
    assert 0 < run_row["games_new"] < 60

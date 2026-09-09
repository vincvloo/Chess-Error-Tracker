import threading
from unittest.mock import MagicMock, patch

import pytest

from chess_tracker.analysis_runner import run_analysis
from chess_tracker.chesscom import ChessComError
from chess_tracker.db import open_db, save_game


def _fake_rec(url, username):
    return {
        "url": url, "username": username, "end_time": 1000, "date": "2024-01-01",
        "time_class": "blitz", "my_colour": "white", "my_rating": 1500,
        "opp_rating": 1400, "result": "win", "eco": "C00", "moves_played": 20,
        "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
    }


def _fake_mistake(url, username):
    return {
        "game_url": url, "username": username, "date": "2024-01-01", "end_time": 1000,
        "time_class": "blitz", "my_rating": 1500, "my_colour": "white", "move_number": 5,
        "phase": "opening", "severity": "blunder", "cp_loss": 300, "category": "hung a pawn",
        "played": "e4", "best": "d4", "clock_seconds": 20.0, "fen": "fen-string",
    }


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_saves_games_and_records_a_run(mock_popen):
    mock_popen.return_value = MagicMock()
    games = [{"url": "https://example.com/g1"}, {"url": "https://example.com/g2"}]

    def fake_analyse(game_json, user, engine, depth, min_loss):
        return _fake_rec(game_json["url"], user), [_fake_mistake(game_json["url"], user)]

    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True)

    saved = conn.execute("SELECT * FROM games WHERE username = 'alice'").fetchall()
    assert len(saved) == 2
    run_row = conn.execute("SELECT * FROM runs WHERE username = 'alice'").fetchone()
    assert run_row["games_new"] == 2
    mock_popen.return_value.quit.assert_called_once()


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_calls_progress_cb_per_game(mock_popen):
    mock_popen.return_value = MagicMock()
    games = [{"url": "https://example.com/g1"}, {"url": "https://example.com/g2"}]

    def fake_analyse(game_json, user, engine, depth, min_loss):
        return _fake_rec(game_json["url"], user), []

    calls = []
    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True,
                     progress_cb=lambda u, i, t: calls.append((u, i, t)))

    assert calls == [("alice", 1, 2), ("alice", 2, 2)]


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_respects_cancel_event(mock_popen):
    mock_popen.return_value = MagicMock()
    games = [{"url": f"https://example.com/g{i}"} for i in range(5)]
    analysed_urls = []

    def fake_analyse(game_json, user, engine, depth, min_loss):
        analysed_urls.append(game_json["url"])
        return _fake_rec(game_json["url"], user), []

    cancel_event = threading.Event()

    def progress_cb(user, i, total):
        if i == 2:
            cancel_event.set()

    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True, progress_cb=progress_cb,
                     cancel_event=cancel_event)

    assert len(analysed_urls) == 2
    saved = conn.execute("SELECT * FROM games WHERE username = 'alice'").fetchall()
    assert len(saved) == 2


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_swallows_keyboardinterrupt_and_returns_normally(mock_popen):
    mock_popen.return_value = MagicMock()
    games = [{"url": "https://example.com/g1"}]

    def fake_analyse(game_json, user, engine, depth, min_loss):
        raise KeyboardInterrupt

    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True)  # must not raise

    run_row = conn.execute("SELECT * FROM runs WHERE username = 'alice'").fetchone()
    assert run_row is not None
    assert run_row["games_new"] == 0
    mock_popen.return_value.quit.assert_called_once()


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_skips_already_analysed_games(mock_popen):
    mock_popen.return_value = MagicMock()
    conn = open_db(":memory:")
    save_game(conn, _fake_rec("https://example.com/g1", "alice"), [], depth=14)

    games = [{"url": "https://example.com/g1"}, {"url": "https://example.com/g2"}]
    analysed_urls = []

    def fake_analyse(game_json, user, engine, depth, min_loss):
        analysed_urls.append(game_json["url"])
        return _fake_rec(game_json["url"], user), []

    with patch("chess_tracker.analysis_runner.collect_games", return_value=games), \
         patch("chess_tracker.analysis_runner.analyse_game", side_effect=fake_analyse):
        run_analysis(conn, ["alice"], "you@example.com", "/fake/stockfish", depth=14,
                     threads=2, pause=0, quiet=True)

    assert analysed_urls == ["https://example.com/g2"]


@patch("chess.engine.SimpleEngine.popen_uci")
def test_run_analysis_propagates_chesscomerror(mock_popen):
    mock_popen.return_value = MagicMock()
    conn = open_db(":memory:")
    with patch("chess_tracker.analysis_runner.collect_games",
               side_effect=ChessComError("No such Chess.com user: bogus")):
        with pytest.raises(ChessComError, match="No such Chess.com user"):
            run_analysis(conn, ["bogus"], "you@example.com", "/fake/stockfish", depth=14,
                         threads=2, pause=0, quiet=True)
    mock_popen.return_value.quit.assert_called_once()

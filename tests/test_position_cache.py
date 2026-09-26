"""The position cache and the "skip the after-move analysis when the move is
the engine's best" shortcut. Both are pure speed-ups, so the tests that matter
most check they never change what ends up recorded."""

import io
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

from chess_mistake_coach.analysis import analyse_bot_game, analyse_game, score_cp, score_move
from chess_mistake_coach.analysis_runner import run_analysis
from chess_mistake_coach.db import open_db
from chess_mistake_coach.position_cache import MAX_FULLMOVE, CachingEngine, PositionCache


class FakeEngine:
    """Deterministic 'engine': the score and best move depend only on the
    position, so cached and fresh answers can be compared exactly. Counts calls."""

    id = {"name": "FakeFish 1"}

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _value(board):
        material = sum({chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
                        chess.ROOK: 500, chess.QUEEN: 900}.get(p.piece_type, 0)
                       * (1 if p.color == chess.WHITE else -1)
                       for p in board.piece_map().values())
        return material + (sum((i + 1) * ord(c) for i, c in enumerate(board.epd())) % 400) - 200   # white's POV

    def analyse(self, board, limit, **kwargs):
        self.calls += 1
        moves = sorted(board.legal_moves, key=lambda m: m.uci())
        return {"score": chess.engine.PovScore(chess.engine.Cp(self._value(board)), chess.WHITE),
                "pv": [moves[0]] if moves else []}

    def configure(self, options):
        pass

    def quit(self):
        pass


DEPTH = chess.engine.Limit(depth=8)


def _conn():
    return open_db(":memory:")


# ---- the cache -------------------------------------------------------------

def test_a_repeat_position_is_answered_without_the_engine_and_identically():
    conn, engine = _conn(), FakeEngine()
    cached = CachingEngine(engine, conn)
    board = chess.Board()
    first = cached.analyse(board, DEPTH)
    second = cached.analyse(board, DEPTH)
    assert engine.calls == 1
    assert score_cp(second, chess.WHITE) == score_cp(first, chess.WHITE)
    assert score_cp(second, chess.BLACK) == score_cp(first, chess.BLACK)
    assert second["pv"][0] == first["pv"][0]
    assert cached.cache.hits == 1 and cached.cache.lookups == 2


def test_the_same_position_reached_by_a_different_move_order_is_a_hit():
    conn, engine = _conn(), FakeEngine()
    cached = CachingEngine(engine, conn)
    a, b = chess.Board(), chess.Board()
    for uci in ("g1f3", "g8f6", "b1c3", "b8c6"):
        a.push_uci(uci)
    for uci in ("b1c3", "b8c6", "g1f3", "g8f6"):
        b.push_uci(uci)
    cached.analyse(a, DEPTH)
    cached.analyse(b, DEPTH)
    assert engine.calls == 1


def test_mate_scores_round_trip_for_both_colours():
    conn = _conn()
    cache = PositionCache(conn, "x")
    board = chess.Board()
    mate_info = {"score": chess.engine.PovScore(chess.engine.Mate(-3), chess.BLACK),
                 "pv": [chess.Move.from_uci("e2e4")]}
    cache.put(board, 8, mate_info)
    got = cache.get(board, 8)
    for colour in (chess.WHITE, chess.BLACK):
        assert score_cp(got, colour) == score_cp(mate_info, colour)


def test_different_depth_or_engine_is_a_miss():
    conn, engine = _conn(), FakeEngine()
    CachingEngine(engine, conn).analyse(chess.Board(), DEPTH)
    conn_engine = CachingEngine(engine, conn)
    conn_engine.analyse(chess.Board(), chess.engine.Limit(depth=20))      # other depth
    assert engine.calls == 2
    other = FakeEngine()
    other.id = {"name": "FakeFish 2"}                                    # other engine version
    CachingEngine(other, conn).analyse(chess.Board(), DEPTH)
    assert other.calls == 1


def test_only_opening_positions_are_cached():
    conn, engine = _conn(), FakeEngine()
    cached = CachingEngine(engine, conn)
    late = chess.Board()
    late.fullmove_number = MAX_FULLMOVE + 1
    cached.analyse(late, DEPTH)
    cached.analyse(late, DEPTH)
    assert engine.calls == 2                                              # never cached
    early = chess.Board()
    early.fullmove_number = MAX_FULLMOVE
    cached.analyse(early, DEPTH)
    cached.analyse(early, DEPTH)
    assert engine.calls == 3


def test_calls_the_cache_cannot_answer_go_straight_to_the_engine():
    conn, engine = _conn(), FakeEngine()
    cached = CachingEngine(engine, conn)
    cached.analyse(chess.Board(), chess.engine.Limit(time=0.1))           # no depth
    cached.analyse(chess.Board(), chess.engine.Limit(time=0.1))
    cached.analyse(chess.Board(), DEPTH, root_moves=[chess.Move.from_uci("e2e4")])
    cached.analyse(chess.Board(), DEPTH, root_moves=[chess.Move.from_uci("e2e4")])
    assert engine.calls == 4


def test_other_engine_methods_pass_through():
    engine = FakeEngine()
    engine.quit = lambda: "bye"
    assert CachingEngine(engine, _conn()).quit() == "bye"


def test_flushed_results_are_shared_with_other_connections(tmp_path):
    db = str(tmp_path / "c.db")
    first_conn, second_conn = open_db(db), open_db(db)
    e1, e2 = FakeEngine(), FakeEngine()
    one, two = CachingEngine(e1, first_conn), CachingEngine(e2, second_conn)
    one.analyse(chess.Board(), DEPTH)
    two.analyse(chess.Board(), DEPTH)
    assert e2.calls == 1                                                  # not flushed yet
    one.flush()
    three = CachingEngine(FakeEngine(), second_conn)
    three.analyse(chess.Board(), DEPTH)
    assert three._engine.calls == 0                                       # a worker's results reach the others
    two.flush()                                                           # duplicate: ignored, not an error
    assert first_conn.execute("SELECT COUNT(*) FROM position_evals").fetchone()[0] == 1


def test_a_busy_database_only_delays_the_flush():
    import sqlite3
    conn = _conn()
    cache = CachingEngine(FakeEngine(), conn)
    cache.analyse(chess.Board(), DEPTH)

    class Locked:
        def __getattr__(self, name):
            return getattr(conn, name)

        def __enter__(self):
            raise sqlite3.OperationalError("database is locked")

        def __exit__(self, *a):
            return False

    cache.cache._conn = Locked()
    cache.flush()                                                         # must not raise
    cache.cache._conn = conn
    cache.flush()                                                         # succeeds next time
    assert conn.execute("SELECT COUNT(*) FROM position_evals").fetchone()[0] == 1


# ---- skipping the after-move analysis -----------------------------------------------

def test_playing_the_engines_best_move_costs_one_analysis_not_two():
    engine = FakeEngine()
    board = chess.Board()
    best = engine.analyse(board, DEPTH)["pv"][0]
    engine.calls = 0
    result = score_move(board, best, chess.WHITE, engine, DEPTH)
    assert engine.calls == 1
    assert result[0] == 0 and result[1] == best


def test_any_other_move_still_gets_both_analyses():
    engine = FakeEngine()
    board = chess.Board()
    best = engine.analyse(board, DEPTH)["pv"][0]
    other = next(m for m in board.legal_moves if m != best)
    engine.calls = 0
    score_move(board, other, chess.WHITE, engine, DEPTH)
    assert engine.calls == 2


def _reference_two_call_mistakes(moves, me, engine, min_loss):
    """The behaviour before the shortcut: always analyse before AND after."""
    from chess_mistake_coach.analysis import (BLUNDER, CP_LOSS_CAP, MISTAKE, _iter_own_moves_from_list,
                                        classify, game_phase)
    out = []
    for board, played in _iter_own_moves_from_list(moves, me):
        info_before = engine.analyse(board, DEPTH)
        cp_before = score_cp(info_before, me)
        best = info_before["pv"][0]
        after = board.copy()
        after.push(played)
        info_after = engine.analyse(after, DEPTH)
        cp_after = score_cp(info_after, me)
        reply = info_after["pv"][0] if info_after["pv"] else None
        cp_loss = cp_before - cp_after
        if cp_loss >= min_loss and played != best:
            out.append((board.fullmove_number, game_phase(board, board.fullmove_number),
                        min(cp_loss, CP_LOSS_CAP), classify(board, played, best, me, reply,
                                                            cp_before, cp_after),
                        board.san(played), board.san(best), board.fen()))
    return out


GAME = ("e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5 c2c3 d7d6 e1g1 e8g8 h2h3 h7h6 "
        "b1d2 c8e6 c4b3 d8d7 f1e1 a7a6 d2f1 e6b3 d1b3 a8b8").split()


def test_the_shortcut_and_the_cache_record_exactly_what_the_old_way_did():
    moves = [chess.Move.from_uci(u) for u in GAME]
    for me in (chess.WHITE, chess.BLACK):
        reference = _reference_two_call_mistakes(moves, me, FakeEngine(), 50)
        plain = analyse_bot_game(moves, me, FakeEngine(), 8, 50)
        cached_engine = CachingEngine(FakeEngine(), _conn())
        cached = analyse_bot_game(moves, me, cached_engine, 8, 50)
        again = analyse_bot_game(moves, me, cached_engine, 8, 50)     # now mostly from cache
        for got in (plain, cached, again):
            assert [(m["move_number"], m["phase"], m["cp_loss"], m["category"], m["played"],
                     m["best"], m["fen"]) for m in got] == reference
        assert reference, "the fake game should contain some mistakes to compare"


# ---- end to end through the runner ------------------------------------------------------

def _chesscom_game(url, moves_uci, white="alice", black="bob"):
    board = chess.Board()
    game = chess.pgn.Game()
    game.headers.update({"White": white, "Black": black, "ECO": "C50"})
    node = game
    for u in moves_uci:
        node = node.add_variation(chess.Move.from_uci(u))
    return {"url": url, "pgn": str(game), "end_time": 1_700_000_000, "time_class": "blitz",
            "white": {"username": white, "rating": 1500, "result": "win"},
            "black": {"username": black, "rating": 1500, "result": "lose"}}


def _run(games, tmp_path, name):
    engine = FakeEngine()
    conn = open_db(str(tmp_path / f"{name}.db"))
    with patch("chess.engine.SimpleEngine.popen_uci", return_value=engine), \
         patch("chess_mistake_coach.analysis_runner.collect_games", return_value=games):
        run_analysis(conn, ["alice"], "me@example.com", "/fake/stockfish", depth=8,
                     threads=1, pause=0, quiet=True)
    rows = conn.execute("SELECT game_url, move_number, cp_loss, category, played, best "
                        "FROM mistakes ORDER BY game_url, move_number").fetchall()
    return engine.calls, [tuple(r) for r in rows], conn


def test_run_analysis_uses_the_cache_across_games_and_records_the_same_mistakes(tmp_path):
    same_opening = GAME[:14]
    games = [_chesscom_game(f"https://chess.com/game/{i}", same_opening + GAME[14:14 + 2 * i])
             for i in range(1, 5)]
    calls, rows, conn = _run(games, tmp_path, "cached")

    # The same four games with the cache switched off record identical mistakes...
    with patch("chess_mistake_coach.analysis_runner.CachingEngine",
               lambda engine, conn: engine.__class__ and _NoCache(engine)):
        plain_calls, plain_rows, _ = _run(games, tmp_path, "plain")
    assert rows == plain_rows and rows
    # ...but ask the engine noticeably less.
    assert calls < plain_calls
    assert conn.execute("SELECT COUNT(*) FROM position_evals").fetchone()[0] > 0


class _NoCache:
    """CachingEngine stand-in that never caches (for the comparison run)."""

    def __init__(self, engine):
        self._engine = engine
        self.cache = type("C", (), {"lookups": 0, "hits": 0})()

    def analyse(self, board, limit, **kw):
        return self._engine.analyse(board, limit, **kw)

    def flush(self):
        pass

    def __getattr__(self, name):
        return getattr(self._engine, name)

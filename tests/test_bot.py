import random
from unittest.mock import MagicMock

import chess
import chess.engine

from chess_tracker.bot import (BEGINNER_MIN_ELO, STOCKFISH_MIN_ELO, beginner_depth,
                               beginner_move, beginner_random_chance, choose_bot_move)

_MIDGAME_FEN = "r1bq1rk1/2p1bppp/p1n2n2/1p1pp3/4P3/1B3N2/PPPP1PPP/RNBQR1K1 w - - 0 9"


def _mock_top_move(uci: str, cp: int = 20):
    """A stand-in engine for the no-MultiPV path: analyse() returns one
    info dict with the given move as pv[0]."""
    engine = MagicMock()
    engine.analyse.return_value = {
        "score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE),
        "pv": [chess.Move.from_uci(uci)],
    }
    return engine


def _mock_multipv(candidates: list[tuple[str, int]]):
    """A stand-in engine for the MultiPV path: analyse(..., multipv=N)
    returns one info dict per (uci, cp) candidate, best score first --
    matching real MultiPV output ordering."""
    engine = MagicMock()

    def analyse(board, limit, multipv=None):
        infos = [{"score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
                  "pv": [chess.Move.from_uci(uci)]} for uci, cp in candidates]
        if multipv is None:
            return infos[0]
        return infos

    engine.analyse.side_effect = analyse
    return engine


def test_no_adaptive_plays_top_move_without_multipv():
    board = chess.Board(_MIDGAME_FEN)
    engine = _mock_top_move("e4d5")
    move = choose_bot_move(board, engine, "stockfish", chess.engine.Limit(depth=14),
                           eligible_phases=set())
    assert move == chess.Move.from_uci("e4d5")
    engine.analyse.assert_called_once()
    assert "multipv" not in engine.analyse.call_args.kwargs


def test_adaptive_picks_candidate_landing_in_eligible_phase(monkeypatch):
    """Steering should prefer a lower-ranked candidate (still within margin)
    over the engine's own top move, when only that candidate's resulting
    phase is eligible. Phase lookup is faked to isolate this from real
    board/phase mechanics (see test_adaptive_respects_per_engine_margin)."""
    board = chess.Board(_MIDGAME_FEN)

    def fake_phase(b, move_number):
        return "endgame" if b.move_stack[-1] == chess.Move.from_uci("d2d3") else "middlegame"
    monkeypatch.setattr("chess_tracker.bot.game_phase", fake_phase)

    # Both well within Stockfish's own 40cp margin, so this isolates the
    # phase-matching logic itself, not the margin filter.
    engine = _mock_multipv([("e4d5", 20), ("d2d3", 10)])
    move = choose_bot_move(board, engine, "stockfish", chess.engine.Limit(depth=14),
                           eligible_phases={"endgame"})
    assert move == chess.Move.from_uci("d2d3")


def test_adaptive_falls_back_to_top_move_when_no_candidate_is_eligible():
    board = chess.Board(_MIDGAME_FEN)
    # A middlegame position -- no one-ply candidate can reach "endgame".
    engine = _mock_multipv([("e4d5", 20), ("d2d3", 5), ("b1c3", -5)])
    move = choose_bot_move(board, engine, "stockfish", chess.engine.Limit(depth=14),
                           eligible_phases={"endgame"})
    assert move == chess.Move.from_uci("e4d5")


def test_adaptive_respects_per_engine_margin(monkeypatch):
    """The empirical reason MARGIN_CP is per-engine, not shared (see bot.py):
    a 100cp-worse candidate should be reachable for Maia's wider margin but
    excluded for Stockfish's tighter one. Phase lookup is faked so this
    isolates the margin filter itself from real board/phase mechanics."""
    board = chess.Board(_MIDGAME_FEN)

    def fake_phase(b, move_number):
        return "endgame" if b.move_stack[-1] == chess.Move.from_uci("d2d3") else "middlegame"
    monkeypatch.setattr("chess_tracker.bot.game_phase", fake_phase)

    engine = _mock_multipv([("e4d5", 100), ("d2d3", 0)])  # 100cp gap

    stockfish_move = choose_bot_move(board, engine, "stockfish", chess.engine.Limit(depth=14),
                                     eligible_phases={"endgame"})
    maia_move = choose_bot_move(board, engine, "maia", chess.engine.Limit(depth=14),
                                eligible_phases={"endgame"})

    # Stockfish's margin (40) excludes the 100cp-worse candidate -> falls
    # back to the engine's own top move, which isn't the "eligible" one.
    assert stockfish_move == chess.Move.from_uci("e4d5")
    # Maia's margin (150) includes it -> steers to the eligible candidate.
    assert maia_move == chess.Move.from_uci("d2d3")


def test_beginner_depth_increases_with_elo():
    assert beginner_depth(BEGINNER_MIN_ELO) < beginner_depth(1319)


def test_beginner_random_chance_decreases_with_elo():
    lo = beginner_random_chance(BEGINNER_MIN_ELO, STOCKFISH_MIN_ELO)
    hi = beginner_random_chance(STOCKFISH_MIN_ELO - 1, STOCKFISH_MIN_ELO)
    assert lo > hi
    assert 0.0 <= hi <= lo <= 1.0


def test_beginner_random_chance_uses_the_given_floor_not_a_hardcoded_one():
    # Maia selected but the requested Elo is below even Maia's own lowest
    # installed weight file (1100) -- the floor passed in should be that
    # 1100, not Stockfish's 1320, so the ramp still reaches ~0.05 right
    # below whichever engine was actually asked for.
    near_maia_floor = beginner_random_chance(1099, elo_floor=1100)
    near_stockfish_floor = beginner_random_chance(1099, elo_floor=STOCKFISH_MIN_ELO)
    assert near_maia_floor < near_stockfish_floor


def test_beginner_move_plays_random_legal_move_when_rng_forces_it():
    board = chess.Board()
    engine = MagicMock()  # must not be consulted
    move = beginner_move(board, engine, elo=BEGINNER_MIN_ELO, elo_floor=STOCKFISH_MIN_ELO,
                         rng=random.Random(0))
    assert move in board.legal_moves
    engine.analyse.assert_not_called()


def test_beginner_move_falls_through_to_engine_when_rng_doesnt_force_random():
    board = chess.Board()
    engine = _mock_top_move("e2e4")

    class _AlwaysHigh:
        def random(self):
            return 0.999999  # never below any beginner_random_chance()

    move = beginner_move(board, engine, elo=1319, elo_floor=STOCKFISH_MIN_ELO,
                         rng=_AlwaysHigh())
    assert move == chess.Move.from_uci("e2e4")
    engine.analyse.assert_called_once()
    assert engine.analyse.call_args.args[1].depth == beginner_depth(1319)

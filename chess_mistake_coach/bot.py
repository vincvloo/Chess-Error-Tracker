"""Move selection for phase 5's live play mode: an engine move, optionally
steered toward a game phase the current player statistically struggles in.
"""

from __future__ import annotations

import random

import chess
import chess.engine

from .analysis import game_phase

# How many centipawns below the best MultiPV candidate still counts as
# "close enough to steer among". Per-engine, not shared -- benchmarked
# empirically against both installed engines: Stockfish's deep-search
# MultiPV candidates cluster tightly (a 40cp margin leaves several real
# options in a typical middlegame position), while Maia's shallow-node
# candidates spread far wider (in testing, a 50cp margin left only Maia's
# own top move as a "candidate"). Ship conservative separate defaults;
# expect to retune both once real games are played.
MARGIN_CP = {"stockfish": 40, "maia": 150}

MULTIPV = 5

# Overall play-form Elo range. The gap between BEGINNER_MIN_ELO and whichever
# real engine's own floor applies (STOCKFISH_MIN_ELO, or Maia's lowest
# installed weight file) is covered by beginner_move() below -- no
# calibrated engine exists that low for either engine.
PLAY_MIN_ELO = 400
PLAY_MAX_ELO = 3190
BEGINNER_MIN_ELO = PLAY_MIN_ELO

# The installed Stockfish's own UCI_Elo floor (confirmed via `uci`: "option
# name UCI_Elo type spin default 1320 min 1320 max 3190"). Below this,
# UCI_LimitStrength/UCI_Elo simply can't represent the requested difficulty.
STOCKFISH_MIN_ELO = 1320


def choose_bot_move(board: chess.Board, engine: chess.engine.SimpleEngine,
                    engine_kind: str, limit: chess.engine.Limit,
                    eligible_phases: set[str]) -> chess.Move:
    """
    Pick the bot's move for one ply.

    If `eligible_phases` is empty -- adaptive mode is off, or this player
    has no game phase with enough mistake samples yet (see
    reports.eligible_phases()) -- just play the engine's own top move at
    whatever strength it's configured for. No MultiPV call needed.

    Otherwise, ask for the engine's top MULTIPV candidates, keep the ones
    within MARGIN_CP[engine_kind] of the best, and play the first one (by
    the engine's own ranking, best to worst) whose resulting position falls
    in an eligible phase. Falls back to the engine's own top move if none of
    the candidates land in an eligible phase.
    """
    if not eligible_phases:
        info = engine.analyse(board, limit)
        return info["pv"][0]

    infos = engine.analyse(board, limit, multipv=MULTIPV)
    best_score = infos[0]["score"].relative.score(mate_score=10000)
    margin = MARGIN_CP.get(engine_kind, MARGIN_CP["stockfish"])

    candidates = [info for info in infos
                  if best_score - info["score"].relative.score(mate_score=10000) <= margin]

    for info in candidates:
        move = info["pv"][0]
        board_after = board.copy()
        board_after.push(move)
        if game_phase(board_after, board_after.fullmove_number) in eligible_phases:
            return move

    return infos[0]["pv"][0]


def beginner_depth(elo: int) -> int:
    """Search depth for the Beginner band. A coarse step function, not a
    calibrated mapping -- see beginner_move()'s docstring."""
    if elo < 700:
        return 1
    if elo < 1000:
        return 2
    return 3


def beginner_random_chance(elo: int, elo_floor: int) -> float:
    """
    Chance of playing a uniformly random legal move instead of the (shallow)
    engine's own choice: linear from 0.85 at BEGINNER_MIN_ELO down to 0.05
    just below `elo_floor` -- the real engine's own minimum for whichever
    engine was actually requested (STOCKFISH_MIN_ELO, or Maia's lowest
    installed weight file when Maia was picked but the requested Elo is
    below even that).
    """
    span = max(1, elo_floor - 1 - BEGINNER_MIN_ELO)
    frac = max(0.0, min(1.0, (elo - BEGINNER_MIN_ELO) / span))
    return 0.85 - frac * 0.80


def beginner_move(board: chess.Board, engine: chess.engine.SimpleEngine, elo: int,
                  elo_floor: int, rng: random.Random | None = None) -> chess.Move:
    """
    A move for the "Beginner" band (below BEGINNER_MIN_ELO..elo_floor): no
    calibrated engine exists this low for either Stockfish or Maia, so this
    is shallow-depth Stockfish with a chance of playing a uniformly random
    legal move instead -- the same shallow-search-plus-randomization
    approach sites like Lichess use for their own lowest bot levels, tuned
    by feel rather than against a reference engine (none exists to
    calibrate against). Explicitly NOT adaptive-steered: a mover that's
    mostly random already has nothing meaningful to steer.
    """
    rng = rng or random
    if rng.random() < beginner_random_chance(elo, elo_floor):
        return rng.choice(list(board.legal_moves))
    info = engine.analyse(board, chess.engine.Limit(depth=beginner_depth(elo)))
    return info["pv"][0]


def choose_bot_move(board: chess.Board, engine: chess.engine.SimpleEngine,
                    engine_kind: str, limit: chess.engine.Limit,
                    eligible_phases: set[str]) -> chess.Move:
    """
    Pick the bot's move for one ply.

    If `eligible_phases` is empty -- adaptive mode is off, or this player
    has no game phase with enough mistake samples yet (see
    reports.eligible_phases()) -- just play the engine's own top move at
    whatever strength it's configured for. No MultiPV call needed.

    Otherwise, ask for the engine's top MULTIPV candidates, keep the ones
    within MARGIN_CP[engine_kind] of the best, and play the first one (by
    the engine's own ranking, best to worst) whose resulting position falls
    in an eligible phase. Falls back to the engine's own top move if none of
    the candidates land in an eligible phase.
    """
    if not eligible_phases:
        info = engine.analyse(board, limit)
        return info["pv"][0]

    infos = engine.analyse(board, limit, multipv=MULTIPV)
    best_score = infos[0]["score"].relative.score(mate_score=10000)
    margin = MARGIN_CP.get(engine_kind, MARGIN_CP["stockfish"])

    candidates = [info for info in infos
                  if best_score - info["score"].relative.score(mate_score=10000) <= margin]

    for info in candidates:
        move = info["pv"][0]
        board_after = board.copy()
        board_after.push(move)
        if game_phase(board_after, board_after.fullmove_number) in eligible_phases:
            return move

    return infos[0]["pv"][0]

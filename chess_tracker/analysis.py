"""Stockfish analysis and mistake classification."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import TypedDict

import chess
import chess.engine
import chess.pgn

logger = logging.getLogger(__name__)

INACCURACY = 50
MISTAKE = 100
BLUNDER = 250

# Stockfish scores a forced mate as a flat 10,000 stand-in (see score_cp()'s
# mate_score), so a move stepping toward mate can otherwise produce a
# cp_loss far larger than any real material blunder. This keeps that from
# dominating aggregates/rankings while leaving real headroom above ordinary
# blunders (losing a queen is ~900) -- comfortably below the mate stand-in.
# Applies to newly analysed games only: only the capped value is ever
# stored, so raising this cannot recover the true severity of a mistake
# already analysed under the old cap.
CP_LOSS_CAP = 5000

PHASES = ("opening", "middlegame", "endgame")

PIECE_VALUE = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}


class GameRecord(TypedDict):
    url: str
    username: str
    end_time: int
    date: str
    time_class: str
    my_colour: str
    my_rating: int
    opp_rating: int
    result: str
    eco: str
    moves_played: int
    opening_moves: int
    middlegame_moves: int
    endgame_moves: int


class MistakeRecord(TypedDict):
    game_url: str
    username: str
    date: str
    end_time: int
    time_class: str
    my_rating: int
    my_colour: str
    move_number: int
    phase: str
    severity: str
    cp_loss: int
    category: str
    played: str
    best: str
    clock_seconds: float | None
    fen: str


def score_cp(info, colour: chess.Color) -> int:
    return info["score"].pov(colour).score(mate_score=10000)


def game_phase(board: chess.Board, move_number: int) -> str:
    if move_number <= 10:
        return "opening"
    heavy = sum(1 for p in board.piece_map().values()
                if p.piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT))
    return "endgame" if heavy <= 4 else "middlegame"


def classify(board_before: chess.Board, played: chess.Move, best: chess.Move,
             me: chess.Color, reply: chess.Move | None,
             cp_before: int, cp_after: int) -> str:
    """First matching rule wins, most specific first."""
    board_after = board_before.copy()
    board_after.push(played)

    if cp_after <= -9000:
        return "allowed forced mate"
    if cp_before >= 9000:
        return "missed forced mate"

    if reply is not None:
        if board_after.is_capture(reply):
            victim = board_after.piece_at(reply.to_square)
            gain = PIECE_VALUE[victim.piece_type] if victim else 1
            defended = board_after.is_attacked_by(me, reply.to_square)
            attacker = board_after.piece_at(reply.from_square)
            attacker_val = PIECE_VALUE[attacker.piece_type] if attacker else 0

            if gain >= 3 and not defended:
                if reply.to_square == played.to_square:
                    return "moved a piece onto an attacked square"
                return "left a piece undefended"
            if not defended and gain >= 1:
                return "hung a pawn"
            if defended and attacker_val < gain:
                return "underdefended piece, lost the exchange"

        if board_after.gives_check(reply):
            return "allowed a strong check or fork"

    if board_before.is_capture(best):
        target = board_before.piece_at(best.to_square)
        if target and PIECE_VALUE[target.piece_type] >= 3:
            return "missed a capture winning material"
        return "missed a favourable capture"

    if board_before.gives_check(best):
        return "missed a forcing check"

    board_best = board_before.copy()
    board_best.push(best)
    if any(board_best.is_capture(m) for m in board_best.legal_moves
           if board_best.piece_at(m.to_square)
           and PIECE_VALUE[board_best.piece_at(m.to_square).piece_type] >= 3):
        return "missed a tactic setting up material gain"

    return "positional or planning error"


def resolve_colour(game_json: dict, user: str) -> tuple[chess.Color, dict, dict] | None:
    """Which side `user` played, and each side's Chess.com player info."""
    white, black = game_json.get("white", {}), game_json.get("black", {})
    if white.get("username", "").lower() == user.lower():
        return chess.WHITE, white, black
    if black.get("username", "").lower() == user.lower():
        return chess.BLACK, black, white
    return None


def _iter_own_moves(game: chess.pgn.Game, me: chess.Color):
    """
    Walk the mainline, yielding (board, played, node) at each point where it
    is `me`'s move to play, with `board` reflecting the position just before
    that move. Shared by analyse_game() and count_phase_moves() so the
    walk-and-skip-opponent-moves logic exists in exactly one place.
    """
    board = game.board()
    for node in game.mainline():
        played = node.move
        if board.turn == me:
            yield board, played, node
        board.push(played)


def score_move(board_before: chess.Board, played: chess.Move, me: chess.Color,
               engine: chess.engine.SimpleEngine, limit: chess.engine.Limit
               ) -> tuple[int, chess.Move, chess.Move | None, int, int] | None:
    """
    cp_loss, the engine's best move, its likely reply, and the raw before/
    after scores for one of `me`'s moves from `board_before`. None if the
    engine found no best move at all (only possible in an already-terminal
    position). Shared by analyse_game() (chess.com PGN games) and
    analyse_bot_game() (phase 5 play-mode games) so the actual engine-
    scoring logic exists in exactly one place.
    """
    info_before = engine.analyse(board_before, limit)
    cp_before = score_cp(info_before, me)
    best = info_before.get("pv", [None])[0]
    if best is None:
        return None
    if played == best:
        # Playing the engine's own best move can never be an error, and every
        # caller discards the after-move score in that case -- so skip the
        # second (expensive) analysis entirely. ~16% less engine work, same result.
        return 0, best, None, cp_before, cp_before

    board_after = board_before.copy()
    board_after.push(played)
    info_after = engine.analyse(board_after, limit)
    cp_after = score_cp(info_after, me)
    reply = info_after.get("pv", [None])[0]
    return cp_before - cp_after, best, reply, cp_before, cp_after


def analyse_game(game_json: dict, user: str, engine: chess.engine.SimpleEngine,
                 depth: int, min_loss: int) -> tuple[GameRecord, list[MistakeRecord]] | None:
    pgn_text = game_json.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None

    resolved = resolve_colour(game_json, user)
    if resolved is None:
        return None
    me, mine, theirs = resolved

    end_time = game_json.get("end_time", 0)
    rec: GameRecord = {
        "url": game_json.get("url", ""),
        "username": user.lower(),
        "end_time": end_time,
        "date": datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d"),
        "time_class": game_json.get("time_class", "?"),
        "my_colour": "white" if me == chess.WHITE else "black",
        "my_rating": mine.get("rating", 0),
        "opp_rating": theirs.get("rating", 0),
        "result": mine.get("result", "?"),
        "eco": game.headers.get("ECO", "?"),
        "moves_played": 0,
        "opening_moves": 0,
        "middlegame_moves": 0,
        "endgame_moves": 0,
    }

    mistakes: list[MistakeRecord] = []
    limit = chess.engine.Limit(depth=depth)

    for board, played, node in _iter_own_moves(game, me):
        rec["moves_played"] += 1
        move_no = board.fullmove_number
        rec[f"{game_phase(board, move_no)}_moves"] += 1

        scored = score_move(board, played, me, engine, limit)
        if scored is None:
            continue
        cp_loss, best, reply, cp_before, cp_after = scored

        if cp_loss >= min_loss and played != best:
            mistakes.append({
                "game_url": rec["url"], "username": user.lower(), "date": rec["date"],
                "end_time": end_time, "time_class": rec["time_class"],
                "my_rating": rec["my_rating"], "my_colour": rec["my_colour"],
                "move_number": move_no,
                "phase": game_phase(board, move_no),
                "severity": ("blunder" if cp_loss >= BLUNDER
                             else "mistake" if cp_loss >= MISTAKE else "inaccuracy"),
                "cp_loss": min(cp_loss, CP_LOSS_CAP),
                "category": classify(board, played, best, me, reply,
                                     cp_before, cp_after),
                "played": board.san(played),
                "best": board.san(best),
                "clock_seconds": node.clock(),
                "fen": board.fen(),
            })

    if rec["moves_played"] == 0:
        return None  # unparseable or empty movetext, do not pollute the store

    return rec, mistakes


def _iter_own_moves_from_list(moves: list[chess.Move], me: chess.Color):
    """Same walk-and-skip-opponent-moves shape as _iter_own_moves(), driven
    by a plain move list instead of a chess.pgn.Game's mainline -- phase 5
    play-mode games have no PGN (they're never persisted), just the move
    list the client already tracked while the game was played."""
    board = chess.Board()
    for played in moves:
        if board.turn == me:
            yield board, played
        board.push(played)


def analyse_bot_game(moves: list[chess.Move], me: chess.Color,
                     engine: chess.engine.SimpleEngine, depth: int,
                     min_loss: int) -> list[dict]:
    """
    Same per-move cp_loss/category scoring as analyse_game(), for a phase 5
    play-mode game instead of a stored chess.com one. No GameRecord wrapper
    (a bot game has no rating/eco/url/time_class to store) and nothing is
    written to the database -- bot games stay ephemeral by design (see the
    phase 5 plan); this is a one-off scoring pass whose result is shown once,
    on request, not persisted.
    """
    limit = chess.engine.Limit(depth=depth)
    mistakes: list[dict] = []
    for board, played in _iter_own_moves_from_list(moves, me):
        move_no = board.fullmove_number
        scored = score_move(board, played, me, engine, limit)
        if scored is None:
            continue
        cp_loss, best, reply, cp_before, cp_after = scored

        if cp_loss >= min_loss and played != best:
            mistakes.append({
                "move_number": move_no,
                "phase": game_phase(board, move_no),
                "severity": ("blunder" if cp_loss >= BLUNDER
                             else "mistake" if cp_loss >= MISTAKE else "inaccuracy"),
                "cp_loss": min(cp_loss, CP_LOSS_CAP),
                "category": classify(board, played, best, me, reply, cp_before, cp_after),
                "played": board.san(played),
                "best": board.san(best),
                "fen": board.fen(),
            })
    return mistakes


def count_phase_moves(game_json: dict, user: str) -> dict[str, int] | None:
    """
    Same per-phase tally analyse_game() does, without the engine calls.
    Used to backfill games that were analysed before these columns existed.
    """
    pgn_text = game_json.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    resolved = resolve_colour(game_json, user)
    if resolved is None:
        return None
    me, _, _ = resolved

    counts = {"opening_moves": 0, "middlegame_moves": 0, "endgame_moves": 0}
    for board, played, node in _iter_own_moves(game, me):
        counts[f"{game_phase(board, board.fullmove_number)}_moves"] += 1
    return counts


def backfill_phase_moves(conn: sqlite3.Connection, user: str) -> None:
    """
    Fill in opening_moves/middlegame_moves/endgame_moves for games analysed
    before per-phase rates existed. Reads PGNs already cached in `archives`,
    so this needs no network access and no Stockfish.
    """
    user = user.lower()
    missing = conn.execute("""
        SELECT url FROM games
        WHERE username = ? AND (opening_moves IS NULL OR middlegame_moves IS NULL
                                 OR endgame_moves IS NULL)
    """, (user,)).fetchall()
    if not missing:
        return
    missing_urls = {r["url"] for r in missing}

    by_url: dict[str, dict] = {}
    for row in conn.execute("SELECT body FROM archives WHERE username = ?", (user,)):
        for g in json.loads(row["body"]):
            if g.get("url") in missing_urls:
                by_url[g["url"]] = g

    updated = skipped = 0
    with conn:
        for url in missing_urls:
            g_json = by_url.get(url)
            counts = count_phase_moves(g_json, user) if g_json else None
            if counts is None:
                skipped += 1
                continue
            conn.execute("""
                UPDATE games SET opening_moves = :opening_moves,
                       middlegame_moves = :middlegame_moves,
                       endgame_moves = :endgame_moves
                WHERE url = :url AND username = :user
            """, {**counts, "url": url, "user": user})
            updated += 1

    if updated:
        logger.info(f"[{user}] backfilled phase-move counts for {updated} games"
                    + (f", {skipped} skipped (PGN no longer cached)" if skipped else ""))

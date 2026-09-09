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

        info_before = engine.analyse(board, limit)
        cp_before = score_cp(info_before, me)
        best = info_before.get("pv", [None])[0]
        if best is None:
            continue

        board_before = board.copy()
        board_after = board.copy()
        board_after.push(played)

        info_after = engine.analyse(board_after, limit)
        cp_after = score_cp(info_after, me)
        reply = info_after.get("pv", [None])[0]
        cp_loss = cp_before - cp_after

        if cp_loss >= min_loss and played != best:
            mistakes.append({
                "game_url": rec["url"], "username": user.lower(), "date": rec["date"],
                "end_time": end_time, "time_class": rec["time_class"],
                "my_rating": rec["my_rating"], "my_colour": rec["my_colour"],
                "move_number": move_no,
                "phase": game_phase(board_before, move_no),
                "severity": ("blunder" if cp_loss >= BLUNDER
                             else "mistake" if cp_loss >= MISTAKE else "inaccuracy"),
                "cp_loss": min(cp_loss, 2000),
                "category": classify(board_before, played, best, me, reply,
                                     cp_before, cp_after),
                "played": board_before.san(played),
                "best": board_before.san(best),
                "clock_seconds": node.clock(),
                "fen": board_before.fen(),
            })

    if rec["moves_played"] == 0:
        return None  # unparseable or empty movetext, do not pollute the store

    return rec, mistakes


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

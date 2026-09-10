"""JSON API routes: job status/cancellation, and practice-mode move attempts."""

from __future__ import annotations

from datetime import datetime, timezone

import chess
import chess.engine
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..analysis import MISTAKE, score_cp
from ..db import open_db

router = APIRouter(prefix="/api")

# Depth for the live engine check on a practice-mode attempt. A plain
# constant here, not read from config -- this is a quick interactive check,
# not the archival analysis depth used when fetching games.
PRACTICE_JUDGE_DEPTH = 14


def _judge_with_engine(engine_path: str | None, board_before: chess.Board,
                       move: chess.Move) -> tuple[str, int | None]:
    """
    Whether a move that doesn't match the stored `best` was still fine or a
    real mistake, using the same cp_loss/MISTAKE yardstick as everywhere
    else in the app. Falls back to a flat "mistake" verdict (no cp_loss) if
    no engine is available or it fails -- practice mode must keep working
    with no Stockfish installed, same as phase 3.
    """
    if not engine_path:
        return "mistake", None

    limit = chess.engine.Limit(depth=PRACTICE_JUDGE_DEPTH)
    try:
        with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
            info_before = engine.analyse(board_before, limit)
            cp_before = score_cp(info_before, board_before.turn)

            board_after = board_before.copy()
            board_after.push(move)
            info_after = engine.analyse(board_after, limit)
            cp_after = score_cp(info_after, board_before.turn)
    except (chess.engine.EngineError, OSError):
        return "mistake", None

    cp_loss = cp_before - cp_after
    return ("also_fine" if cp_loss < MISTAKE else "mistake"), cp_loss


@router.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    status = request.app.state.jobs.get_status(job_id)
    if status is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return status


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    ok = request.app.state.jobs.cancel(job_id)
    if not ok:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"cancelled": True}


@router.get("/practice/{mistake_id}/hint")
def practice_hint(request: Request, mistake_id: int):
    """
    A partial hint: which square holds the piece that should move, and what
    kind of piece it is -- not the destination square, not the SAN, so it
    nudges without giving the move away. Still computed entirely
    server-side; /practice's initial page load never has `best` at all.
    """
    conn = open_db(request.app.state.db_path)
    try:
        row = conn.execute("SELECT fen, best FROM mistakes WHERE id = ?",
                           (mistake_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "no such mistake"}, status_code=404)

    board = chess.Board(row["fen"])
    move = board.parse_san(row["best"])
    piece = board.piece_at(move.from_square)
    return {
        "square": chess.square_name(move.from_square),
        "piece": chess.piece_name(piece.piece_type) if piece else None,
    }


@router.post("/practice/{mistake_id}/attempt")
async def practice_attempt(request: Request, mistake_id: int):
    """
    Judge one practice-mode move attempt against the mistake's stored `best`
    move. Re-fetches the mistake row directly by id rather than depending on
    a practice_queue() scope, and is the only place that ever reveals `best`
    -- the /practice page itself never sends it to the client.
    """
    body = await request.json()
    from_sq, to_sq = body.get("from"), body.get("to")
    promotion = body.get("promotion") or ""
    practicing_user = (body.get("practicingUser") or "").strip().lower()
    hint_used = bool(body.get("hintUsed"))
    if not from_sq or not to_sq:
        return JSONResponse({"error": "from and to are required"}, status_code=400)

    conn = open_db(request.app.state.db_path)
    try:
        row = conn.execute("SELECT fen, best, username, category FROM mistakes WHERE id = ?",
                           (mistake_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "no such mistake"}, status_code=404)

    board = chess.Board(row["fen"])
    try:
        move = chess.Move.from_uci(from_sq + to_sq + promotion)
    except chess.InvalidMoveError:
        return {"legal": False}
    if move not in board.legal_moves:
        return {"legal": False}

    your_san = board.san(move)
    your_board = board.copy()
    your_board.push(move)

    best_board = board.copy()
    best_board.push(best_board.parse_san(row["best"]))

    result = {
        "legal": True,
        "yourSan": your_san,
        "bestSan": row["best"],
        "yourFen": your_board.fen(),
        "bestFen": best_board.fen(),
    }

    if your_san == row["best"]:
        result["verdict"] = "best"
    else:
        verdict, your_cp_loss = _judge_with_engine(
            request.app.state.engine_path, board, move)
        result["verdict"] = verdict
        result["yourCpLoss"] = your_cp_loss

    if practicing_user:
        conn2 = open_db(request.app.state.db_path)
        try:
            conn2.execute(
                "INSERT INTO practice_attempts "
                "(practicing_user, mistake_id, owner, category, verdict, hint_used, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (practicing_user, mistake_id, row["username"], row["category"],
                 result["verdict"], int(hint_used),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn2.commit()
        finally:
            conn2.close()

    result["correct"] = result["verdict"] == "best"  # kept for older clients
    return result


@router.delete("/practice-attempts/{attempt_id}")
def delete_practice_attempt(request: Request, attempt_id: int):
    """Remove one logged attempt (a row in the Achievements "Recent practice
    sessions" table) -- e.g. one you didn't mean to have tracked."""
    conn = open_db(request.app.state.db_path)
    try:
        conn.execute("DELETE FROM practice_attempts WHERE id = ?", (attempt_id,))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": True}


@router.delete("/practice-attempts")
def reset_practice_attempts(request: Request, user: str):
    """Clear all of one player's practice-attempt history -- the "reset"
    button on Achievements, for starting the solve-rate/progress stats over
    from scratch."""
    conn = open_db(request.app.state.db_path)
    try:
        conn.execute("DELETE FROM practice_attempts WHERE practicing_user = ?",
                     (user.lower(),))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": True}

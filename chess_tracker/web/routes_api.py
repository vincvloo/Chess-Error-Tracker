"""JSON API routes: job status/cancellation, and practice-mode move attempts."""

from __future__ import annotations

import chess
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import open_db

router = APIRouter(prefix="/api")


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
    if not from_sq or not to_sq:
        return JSONResponse({"error": "from and to are required"}, status_code=400)

    conn = open_db(request.app.state.db_path)
    try:
        row = conn.execute("SELECT fen, best FROM mistakes WHERE id = ?",
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

    return {
        "legal": True,
        "correct": your_san == row["best"],
        "yourSan": your_san,
        "bestSan": row["best"],
        "yourFen": your_board.fen(),
        "bestFen": best_board.fen(),
    }

"""JSON API routes: job status/cancellation, practice-mode move attempts,
play-mode (phase 5) moves, and puzzle-mode attempts."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import chess
import chess.engine
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import gamification
from ..analysis import INACCURACY, MISTAKE, analyse_bot_game, score_cp
from ..bot import PLAY_MAX_ELO, PLAY_MIN_ELO, STOCKFISH_MIN_ELO, beginner_move, choose_bot_move
from ..db import open_db
from ..engine import find_engine, find_lc0, find_maia_weights
from ..puzzles import DEFAULT_MIN_PLAYS, check_puzzle_move, pick_random_puzzle, puzzle_position_payload
from .puzzle_import import PuzzleImportAlreadyRunningError
from ..reports import eligible_phases
from . import updater
from .play_engine import MAIA_NODES

router = APIRouter(prefix="/api")

# How long a "no update available" (or "couldn't check") result is trusted
# before /api/update/check runs a real `git fetch` again -- avoids a network
# call on every single home-page load within a session.
_UPDATE_CHECK_TTL_SECONDS = 600

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


@router.get("/jobs/active")
def active_job(request: Request):
    """Whether a job is currently running, and its id if so -- lets any
    page's JS discover this without already knowing a job_id. Registered
    ahead of /jobs/{job_id} since Starlette matches path routes in
    registration order; otherwise "active" would be swallowed as a
    job_id."""
    return {"job_id": request.app.state.jobs.get_active_job_id()}


@router.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    status = request.app.state.jobs.get_status(job_id)
    if status is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return status


def _month_range(start_month: str, end_month: str) -> list[str]:
    """Every "YYYY-MM" from start through end inclusive, so a quiet stretch
    with no archive row (Chess.com's monthly listing only includes months
    that have games) still shows up as real zero-height months instead of
    just vanishing from the picker."""
    y, m = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    months = []
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


@router.get("/users/{username}/density")
def user_game_density(request: Request, username: str, depth: int = 14):
    """Games still needing analysis per month, for one tracked player, for
    the big-update interstitial's date-range picker (progress.html). Fetched
    on demand (rather than baked into the progress page) because a
    multi-user job processes one user at a time, and whichever user the
    picker ends up being offered for isn't known until the job is already
    running.

    Per month: archived game count minus how many of that month's games are
    already in the local database at >= `depth` (db.already_analysed()'s own
    rule) -- not the raw archive count, which is every game ever played,
    most of which are typically already analysed. Both halves are plain
    local reads (`archives`, `games`), no network call, regardless of when
    this is asked."""
    username = username.strip().lower()
    conn = open_db(request.app.state.db_path)
    try:
        rows = conn.execute("""
            SELECT a.month AS month, a.game_count AS total, COALESCE(g.analysed, 0) AS analysed
            FROM archives a
            LEFT JOIN (
                SELECT substr(date, 1, 7) AS month, COUNT(*) AS analysed
                FROM games WHERE username = ? AND depth >= ?
                GROUP BY month
            ) g ON g.month = a.month
            WHERE a.username = ? AND a.month IS NOT NULL
        """, (username, depth, username)).fetchall()
    finally:
        conn.close()

    todo_by_month = {r["month"]: max(0, (r["total"] or 0) - r["analysed"]) for r in rows}
    if not todo_by_month:
        return {"density": []}

    today_month = datetime.now(timezone.utc).strftime("%Y-%m")
    months = _month_range(min(todo_by_month), max(max(todo_by_month), today_month))
    return {"density": [{"month": m, "games": todo_by_month.get(m, 0)} for m in months]}


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
            result["newBadges"] = gamification.record_progress(conn2, practicing_user)
        finally:
            conn2.close()

    result["correct"] = result["verdict"] == "best"  # kept for older clients
    return result


# Depth for play mode's Stockfish moves. A plain constant, not read from
# config, matching PRACTICE_JUDGE_DEPTH's rationale -- this needs to feel
# responsive during a live game, not match archival analysis depth.
# Benchmarked directly against the installed Stockfish: ~0.43s at
# multipv=5/UCI_Elo=1500, ~1s at depth 16 -- 14 is the sweet spot.
PLAY_STOCKFISH_DEPTH = 14


def _game_outcome(board: chess.Board) -> str | None:
    """None while the game is ongoing, else "white"/"black"/"draw".
    claim_draw=True is required for threefold-repetition/50-move draws --
    board.outcome() alone only catches checkmate/stalemate/insufficient
    material/75-move/5-fold. Returns (result, termination) -- termination is
    e.g. "checkmate"/"stalemate"/"insufficient_material"/"threefold_repetition"
    (chess.Termination's own names, lowercased) so the client can say *why*
    the game ended, not just who won -- "You win!" alone is meaningless
    without knowing it was checkmate rather than, say, the bot timing out."""
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None, None
    result = "draw" if outcome.winner is None else ("white" if outcome.winner else "black")
    return result, outcome.termination.name.lower()


def _bot_reply(request: Request, board: chess.Board, engine_kind: str, elo: int,
               adaptive: bool, time_class: str | None, user: str) -> tuple[chess.Move | None, JSONResponse | None]:
    """
    Shared by play_move() (human moved, now the bot replies) and
    play_first_move() (human chose Black, bot opens the game -- there's no
    human move to validate first). Returns (move, None) on success, or
    (None, error_response) on failure.
    """
    eligible = set()
    if adaptive and user and time_class:
        conn = open_db(request.app.state.db_path)
        try:
            eligible = eligible_phases(conn, user, time_class)
        finally:
            conn.close()

    weights = find_maia_weights() if engine_kind == "maia" else {}
    maia_min = min(weights) if weights else None

    if engine_kind == "maia" and maia_min is not None and elo >= maia_min:
        lc0_path = find_lc0()
        if not lc0_path:
            return None, JSONResponse({"error": "Maia (lc0) isn't installed"}, status_code=503)
        snapped = weights[min(weights, key=lambda e: abs(e - elo))]
        engine_path, key = lc0_path, ("maia", snapped)
        configure = {"WeightsFile": snapped}
        # Only used the first time this key is opened (PlayEngineManager
        # ignores it when reusing an already-open engine) -- absorbs lc0's
        # graph-compile cost here rather than on the move below.
        warm_up_board = chess.Board()
        limit = chess.engine.Limit(nodes=MAIA_NODES)
        move_fn = lambda engine: choose_bot_move(board, engine, "maia", limit, eligible)
    else:
        engine_path = request.app.state.engine_path or find_engine()
        if not engine_path or not os.path.isfile(engine_path):
            return None, JSONResponse({"error": "Stockfish isn't installed"}, status_code=503)
        if engine_kind == "stockfish" and elo >= STOCKFISH_MIN_ELO:
            key = ("stockfish", elo)
            configure = {"UCI_LimitStrength": True, "UCI_Elo": elo}
            warm_up_board = None
            limit = chess.engine.Limit(depth=PLAY_STOCKFISH_DEPTH)
            move_fn = lambda engine: choose_bot_move(board, engine, "stockfish", limit, eligible)
        else:
            # Beginner band: the requested Elo is below whatever real
            # engine's own floor applies (Stockfish's UCI_Elo floor, or
            # Maia's lowest installed weight file when Maia was picked but
            # asked to go lower than even that). No calibrated engine
            # exists this low for either -- see bot.beginner_move(). No
            # UCI_Elo/WeightsFile configuration at all, so the process
            # identity doesn't depend on the exact Elo value.
            elo_floor = maia_min if (engine_kind == "maia" and maia_min) else STOCKFISH_MIN_ELO
            key, configure, warm_up_board = ("beginner",), None, None
            move_fn = lambda engine: beginner_move(board, engine, elo, elo_floor)

    try:
        with request.app.state.play_engine.acquire(
                engine_path, key, configure=configure, warm_up_board=warm_up_board) as engine:
            return move_fn(engine), None
    except (chess.engine.EngineError, chess.engine.EngineTerminatedError, OSError):
        # A dead engine process is useless to keep around -- drop it so the
        # next request starts a fresh one instead of repeatedly failing
        # against the same dead handle.
        request.app.state.play_engine.close()
        return None, JSONResponse(
            {"error": "the engine crashed or is unavailable; try again"}, status_code=503)


def _play_request_settings(body: dict) -> tuple[str, int, bool, str | None, str]:
    engine_kind = body.get("engine") or "stockfish"
    elo = max(PLAY_MIN_ELO, min(PLAY_MAX_ELO, int(body.get("elo") or 1500)))
    adaptive = bool(body.get("adaptive"))
    time_class = (body.get("timeClass") or "").strip() or None
    user = (body.get("user") or "").strip().lower()
    return engine_kind, elo, adaptive, time_class, user


@router.get("/play/legal-moves")
def play_legal_moves(fen: str):
    """Legal moves for a client-held position, so play.html's board can
    highlight targets the same way practice.html's does -- practice mode
    gets this list for free from the stored mistake row; play mode has no
    such row, so it's re-derived on demand instead."""
    try:
        board = chess.Board(fen)
    except ValueError:
        return JSONResponse({"error": "invalid fen"}, status_code=400)
    if not board.is_valid():
        return JSONResponse({"error": "invalid position"}, status_code=400)
    return {"legalMoves": [m.uci() for m in board.legal_moves]}


@router.post("/play/first-move")
async def play_first_move(request: Request):
    """The bot's opening move, for when the human chose to play Black --
    there's no human move to validate first, unlike play_move()."""
    body = await request.json()
    engine_kind, elo, adaptive, time_class, user = _play_request_settings(body)
    board = chess.Board()

    bot_move, error = _bot_reply(request, board, engine_kind, elo, adaptive, time_class, user)
    if error is not None:
        return error
    board.push(bot_move)
    return {"fen": board.fen(), "botMove": bot_move.uci()}


@router.post("/play/move")
async def play_move(request: Request):
    """
    One ply of live play mode: validate + apply the human's move, then (if
    the game isn't already over) the bot's reply. Fully stateless like
    practice mode, but -- unlike practice mode -- there's no stored `mistakes`
    row to re-derive the position from (a bot game is never persisted), so
    the client's own `fen` is trusted as the current position. Same threat
    model as the rest of this local single-user app; the *move* is still
    validated as legal from that position before anything is applied.
    """
    body = await request.json()
    from_sq, to_sq = body.get("from"), body.get("to")
    promotion = body.get("promotion") or ""
    fen = body.get("fen")
    engine_kind, elo, adaptive, time_class, user = _play_request_settings(body)

    if not fen or not from_sq or not to_sq:
        return JSONResponse({"error": "fen, from and to are required"}, status_code=400)
    try:
        board = chess.Board(fen)
    except ValueError:
        return JSONResponse({"error": "invalid fen"}, status_code=400)
    if not board.is_valid():
        # chess.Board(fen) only checks syntax, not chess legality -- an
        # inconsistent position (e.g. the side not to move already in check)
        # can still generate a "legal" move that captures a king, which then
        # crashes the real engine binary outright (verified directly: Stockfish
        # exits with an access violation on such a position) rather than
        # raising a catchable Python error. Reject before that point.
        return JSONResponse({"error": "invalid position"}, status_code=400)
    try:
        move = chess.Move.from_uci(from_sq + to_sq + promotion)
    except chess.InvalidMoveError:
        return {"legal": False}
    if move not in board.legal_moves:
        return {"legal": False}

    board.push(move)
    outcome, termination = _game_outcome(board)
    if outcome is not None:
        return {"legal": True, "fen": board.fen(), "botMove": None,
                "gameOver": True, "outcome": outcome, "termination": termination}

    bot_move, error = _bot_reply(request, board, engine_kind, elo, adaptive, time_class, user)
    if error is not None:
        return error

    board.push(bot_move)
    outcome, termination = _game_outcome(board)
    return {"legal": True, "fen": board.fen(), "botMove": bot_move.uci(),
            "gameOver": outcome is not None, "outcome": outcome, "termination": termination}


# Depth for post-game analysis, matching the analysis depth used elsewhere
# for real chess.com games (see e.g. SETTINGS_DEFAULTS["depth"] in db.py) --
# unlike the two constants above, this isn't about feeling responsive during
# a live move, it's a one-off pass after the game the player is willing to
# wait a few seconds for.
POST_GAME_ANALYSIS_DEPTH = 14


@router.post("/play/analyze")
async def play_analyze(request: Request):
    """
    Full post-game analysis of a just-finished play-mode game -- the same
    cp_loss/category logic used everywhere else in the app
    (analysis.analyse_bot_game()), run against a fresh full-strength engine
    instance kept separate from the (possibly weakened) play engine used
    during the game itself, matching how practice mode's judge engine is
    kept separate from the archival analysis engine. Bot games are never
    persisted (see phase 5 plan) -- this is a one-off pass whose result is
    only ever returned to the client, never written to the database. (Its
    effect on the player's skill rating is the one thing recorded.)
    """
    body = await request.json()
    uci_moves = body.get("moves") or []
    colour = body.get("colour")
    if not uci_moves or colour not in ("white", "black"):
        return JSONResponse({"error": "moves and colour are required"}, status_code=400)

    try:
        moves = [chess.Move.from_uci(u) for u in uci_moves]
    except chess.InvalidMoveError:
        return JSONResponse({"error": "invalid move in list"}, status_code=400)

    # Replay the whole sequence first to confirm every move is actually
    # legal move-by-move -- analyse_bot_game() assumes a valid game, and a
    # malformed client-sent list would otherwise misbehave the same way an
    # invalid FEN did for /play/move (see board.is_valid() there).
    board = chess.Board()
    for move in moves:
        if move not in board.legal_moves:
            return JSONResponse({"error": "illegal move in list"}, status_code=400)
        board.push(move)

    engine_path = request.app.state.engine_path or find_engine()
    if not engine_path or not os.path.isfile(engine_path):
        return JSONResponse({"error": "Stockfish isn't installed"}, status_code=503)

    me = chess.WHITE if colour == "white" else chess.BLACK
    try:
        with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
            mistakes = analyse_bot_game(moves, me, engine, POST_GAME_ANALYSIS_DEPTH, INACCURACY)
    except (chess.engine.EngineError, chess.engine.EngineTerminatedError, OSError):
        return JSONResponse(
            {"error": "the engine crashed or is unavailable; try again"}, status_code=503)

    new_badges: list[dict] = []
    user = (body.get("user") or "").strip().lower()
    if user:
        # The bot game itself is still never stored -- only its effect on the
        # skill rating is, once, right now (it can't be backdated or replayed).
        conn = open_db(request.app.state.db_path)
        try:
            gamification.apply_bot_game(
                conn, user, mistakes, gamification.phase_move_counts(moves, me))
            new_badges = [{"code": c, "label": gamification.BADGES_BY_CODE[c]["label"],
                           "description": gamification.BADGES_BY_CODE[c]["description"]}
                          for c in gamification.evaluate_badges(conn, user)]
        finally:
            conn.close()

    return {"mistakes": mistakes, "totalMoves": len(moves), "newBadges": new_badges}


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


@router.get("/update/check")
def update_check(request: Request):
    """Whether a newer commit is available upstream -- powers the quiet
    banner on the home page. Never surfaces *why* a check came back
    negative (not a git checkout, no network, etc.): those all collapse to
    "no update", since none of them are the user's problem to solve."""
    cache = request.app.state.update_cache
    now = datetime.now(timezone.utc)
    if cache["checked_at"] is not None:
        age = (now - cache["checked_at"]).total_seconds()
        if age < _UPDATE_CHECK_TTL_SECONDS:
            return {"available": cache["result"]["available"]}

    git_path = updater.find_git()
    if git_path is None:
        result = {"available": False, "reason": "git not found"}
    else:
        result = updater.check_for_update(git_path, updater.repo_root())
    cache["checked_at"] = now
    cache["result"] = result
    return {"available": result["available"]}


@router.post("/update/apply")
def update_apply(request: Request):
    """Runs the actual update -- only reached by clicking the banner's own
    button, so (unlike /update/check) a real failure message is fine to
    show."""
    git_path = updater.find_git()
    if git_path is None:
        return {"ok": False, "message": "git wasn't found on this machine."}
    result = updater.apply_update(git_path, updater.repo_root())
    # A fresh check next time the banner asks, rather than trusting the
    # stale cached "yes" through to the next TTL window.
    request.app.state.update_cache = {"checked_at": None, "result": None}
    return result


def _log_puzzle_attempt(request: Request, practicing_user: str, puzzle_id: str,
                        verdict: str, move_index: int, puzzle_rating: int | None = None,
                        themes: str | None = None) -> list[dict]:
    """Log one finished puzzle, move the skill rating, count the day toward
    the streak. Returns any badges this attempt just earned."""
    if not practicing_user:
        return []
    conn = open_db(request.app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO puzzle_attempts "
            "(practicing_user, puzzle_id, verdict, move_index_reached, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (practicing_user, puzzle_id, verdict, move_index,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        gamification.update_puzzle_rating(
            conn, practicing_user, puzzle_rating, verdict == "solved", themes)
        return gamification.record_progress(conn, practicing_user)
    finally:
        conn.close()


@router.post("/puzzles/{puzzle_id}/attempt")
async def puzzle_attempt(request: Request, puzzle_id: str):
    """
    Judge one puzzle-mode move attempt. Unlike /api/play/move, the board is
    reconstructed authoritatively server-side from the puzzle's own stored
    fen/moves plus `moveIndex` -- there IS a backing row here (unlike a bot
    game), so this can and should follow practice_attempt's pattern of
    trusting the stored position, not a client-sent one.

    Puzzle solutions are forced "only moves" by construction, so correctness
    is a plain string comparison (puzzles.check_puzzle_move()) -- no engine
    call, unlike practice mode's cp_loss-judged "also_fine" nuance.
    """
    body = await request.json()
    move_index = body.get("moveIndex")
    from_sq, to_sq = body.get("from"), body.get("to")
    promotion = body.get("promotion") or ""
    practicing_user = (body.get("practicingUser") or "").strip().lower()
    if move_index is None or not from_sq or not to_sq:
        return JSONResponse({"error": "moveIndex, from and to are required"}, status_code=400)

    conn = open_db(request.app.state.db_path)
    try:
        row = conn.execute("SELECT fen, moves, rating, themes FROM puzzles WHERE puzzle_id = ?",
                           (puzzle_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "no such puzzle"}, status_code=404)

    moves = row["moves"].split()
    if not (1 <= move_index < len(moves)):
        return JSONResponse({"error": "invalid moveIndex"}, status_code=400)

    # Replay the stored setup move plus every move already confirmed correct
    # up to (not including) this one, to reach the position this attempt is
    # judged from.
    board = chess.Board(row["fen"])
    for uci in moves[:move_index]:
        board.push(chess.Move.from_uci(uci))

    try:
        move = chess.Move.from_uci(from_sq + to_sq + promotion)
    except chess.InvalidMoveError:
        return {"legal": False}
    if move not in board.legal_moves:
        return {"legal": False}

    your_san = board.san(move)
    correct = check_puzzle_move(row["moves"], move_index, move)

    if not correct:
        best_move = chess.Move.from_uci(moves[move_index])
        best_san = board.san(best_move)
        board.push(move)
        badges = _log_puzzle_attempt(request, practicing_user, puzzle_id, "failed",
                                     move_index, row["rating"], row["themes"])
        return {"legal": True, "correct": False, "solved": False, "yourSan": your_san,
                "bestSan": best_san, "fen": board.fen(), "newBadges": badges}

    board.push(move)
    next_index = move_index + 1
    if next_index >= len(moves):
        badges = _log_puzzle_attempt(request, practicing_user, puzzle_id, "solved",
                                     move_index, row["rating"], row["themes"])
        return {"legal": True, "correct": True, "solved": True, "yourSan": your_san,
                "fen": board.fen(), "opponentMove": None, "nextMoveIndex": None,
                "newBadges": badges}

    opponent_move = chess.Move.from_uci(moves[next_index])
    opponent_san = board.san(opponent_move)
    board.push(opponent_move)
    solved = (next_index + 1) >= len(moves)
    badges = []
    if solved:
        badges = _log_puzzle_attempt(request, practicing_user, puzzle_id, "solved",
                                     next_index, row["rating"], row["themes"])
    return {"legal": True, "correct": True, "solved": solved, "yourSan": your_san,
            "fen": board.fen(), "opponentMove": opponent_san,
            "nextMoveIndex": None if solved else next_index + 1, "newBadges": badges}


@router.get("/puzzles/rush/next")
def puzzle_rush_next(request: Request, user: str = ""):
    """The next puzzle for a rush run, drawn from a window around the
    player's own skill rating (the whole imported range if they have none)."""
    user = user.strip().lower()
    conn = open_db(request.app.state.db_path)
    try:
        window = gamification.smart_rating_window(conn, user) if user else None
        row = pick_random_puzzle(conn, *(window or (None, None)))
        if row is None and window:
            row = pick_random_puzzle(conn)
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "no puzzles imported"}, status_code=404)
    return puzzle_position_payload(row, user)


@router.post("/puzzles/rush/finish")
async def puzzle_rush_finish(request: Request):
    body = await request.json()
    user = (body.get("practicingUser") or "").strip().lower()
    try:
        score = max(0, int(body.get("score") or 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid score"}, status_code=400)
    if not user:
        return JSONResponse({"error": "practicingUser is required"}, status_code=400)
    conn = open_db(request.app.state.db_path)
    try:
        result = gamification.record_rush_score(conn, user, score)
        gamification.evaluate_badges(conn, user)
    finally:
        conn.close()
    return result


@router.post("/puzzles/import")
def start_puzzle_import(request: Request, minRating: int | None = None,
                        maxRating: int | None = None, minPlays: int = DEFAULT_MIN_PLAYS,
                        themes: str = ""):
    """
    "Get more puzzles" -- downloads the Lichess source CSV if it isn't
    already cached (see puzzles.find_puzzle_source()), then imports/tops up
    matching the given filter. Runs in the background the same way a
    chess.com fetch+analyse job does (see jobs.py), just tracked by a
    separate PuzzleImportManager since the two jobs' parameters don't share
    a shape. `minPlays` defaults to the same quality floor
    scripts/import_puzzles.py uses, not unfiltered -- a web-triggered top-up
    shouldn't be more permissive than the CLI's own default just because the
    picker doesn't expose that control.
    """
    theme_list = [t.strip() for t in themes.split(",") if t.strip()] or None
    try:
        status = request.app.state.puzzle_import.start_job(
            min_rating=minRating, max_rating=maxRating, min_plays=minPlays, themes=theme_list)
    except PuzzleImportAlreadyRunningError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return status.to_dict()


@router.get("/puzzles/import/active")
def active_puzzle_import(request: Request):
    """Same "discover an in-flight job without already knowing its id"
    purpose as /api/jobs/active -- registered ahead of
    /puzzles/import/{job_id} for the same reason (Starlette matches routes
    in registration order; "active" would otherwise be swallowed as a
    job_id)."""
    return {"job_id": request.app.state.puzzle_import.get_active_job_id()}


@router.get("/puzzles/import/{job_id}")
def puzzle_import_status(request: Request, job_id: str):
    status = request.app.state.puzzle_import.get_status(job_id)
    if status is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return status


@router.post("/puzzles/import/{job_id}/cancel")
def cancel_puzzle_import(request: Request, job_id: str):
    ok = request.app.state.puzzle_import.cancel(job_id)
    if not ok:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"cancelled": True}

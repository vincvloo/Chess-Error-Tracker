"""HTML page routes: home (account + hub), settings, job progress, the live
dashboard, achievements, and practice mode."""

from __future__ import annotations

import os

import chess
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import gamification
from ..analysis import INACCURACY
from ..analysis_runner import DEFAULT_PARALLEL_THRESHOLD, DEFAULT_WORKERS
from ..bot import PLAY_MAX_ELO, PLAY_MIN_ELO, STOCKFISH_MIN_ELO
from ..db import get_settings, open_db, set_settings
from ..engine import ENGINE_HELP, find_engine, find_maia_weights
from ..html_export import render_dashboard_html
from ..puzzles import (DEFAULT_MAX_RATING, DEFAULT_MIN_RATING, THEME_GROUPS,
                       pick_random_puzzle, puzzle_position_payload, source_stats_for_filter)
from ..reports import (adaptive_eligible_categories, practice_pool, practice_queue,
                       practice_stats, report_model, user_summaries)
from .demo_data import render_demo_dashboard_html
from .jobs import (BIG_UPDATE_THRESHOLD, FIRST_RUN_GAME_LIMIT, SECONDS_PER_GAME,
                   JobAlreadyRunningError)

router = APIRouter()
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _hub_context(request: Request, error: str | None = None) -> dict:
    """Context for the home page once a primary user (account) is set."""
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        users = user_summaries(conn)
    finally:
        conn.close()
    primary = next((u for u in users if u["username"] == settings["primary_user"]), None)
    others = [u for u in users if u["username"] != settings["primary_user"]]
    return {
        "onboarding": False,
        "primary_user": settings["primary_user"], "primary": primary, "others": others,
        "settings": settings, "error": error,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        users = user_summaries(conn)
    finally:
        conn.close()

    if not settings["primary_user"]:
        return templates.TemplateResponse(request, "home.html", {
            "onboarding": True, "users": users, "error": None,
            "settings": settings, "first_run_limit": FIRST_RUN_GAME_LIMIT,
        })

    return templates.TemplateResponse(request, "home.html", _hub_context(request))


@router.post("/account")
def set_primary_user(request: Request, username: str = Form(...),
                     email: str = Form(""), start_job: str = Form("")):
    username = username.strip().lower()
    if not username:
        return RedirectResponse("/", status_code=303)

    conn = open_db(request.app.state.db_path)
    try:
        was_onboarding = not get_settings(conn)["primary_user"]
        set_settings(conn, primary_user=username)
        if email.strip():
            set_settings(conn, email=email.strip())
        settings = get_settings(conn)
    finally:
        conn.close()

    # Only the very first time a primary user is set (onboarding) does
    # submitting kick off a background analysis -- switching accounts later
    # via Settings never starts a job on its own.
    if was_onboarding and start_job == "1":
        if not settings["email"]:
            return RedirectResponse("/settings?needs_email=1", status_code=303)
        engine_path = request.app.state.engine_path or find_engine()
        if not engine_path or not os.path.isfile(engine_path):
            return _job_error(request, ENGINE_HELP, 400)
        try:
            status = request.app.state.jobs.start_job(
                [username], settings["email"], engine_path,
                settings["depth"], settings["threads"], settings["pause"],
                min_loss=settings["min_loss"], limit=FIRST_RUN_GAME_LIMIT)
            return RedirectResponse(f"/demo-dashboard?job={status.id}", status_code=303)
        except JobAlreadyRunningError as exc:
            return _job_error(request, str(exc), 409)

    return RedirectResponse("/", status_code=303)


@router.get("/demo-dashboard", response_class=HTMLResponse)
def demo_dashboard(request: Request):
    """The sample/demo dashboard shown during first-run onboarding while a
    real analysis job runs in the background (job id read client-side from
    ?job=, not used server-side -- see dashboard_template.html)."""
    return HTMLResponse(render_demo_dashboard_html())


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, needs_email: str = "", saved: str = ""):
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "settings.html",
        {"settings": settings, "needs_email": bool(needs_email), "saved": bool(saved)})


@router.post("/settings")
def save_settings(request: Request, email: str = Form(""), depth: int = Form(14),
                  threads: int = Form(2), pause: float = Form(0.6),
                  min_loss: int = Form(INACCURACY)):
    conn = open_db(request.app.state.db_path)
    try:
        set_settings(conn, email=email.strip(), depth=depth, threads=threads,
                    pause=pause, min_loss=min_loss)
    finally:
        conn.close()
    return RedirectResponse("/settings?saved=1", status_code=303)


def _job_error(request: Request, message: str, status_code: int):
    return templates.TemplateResponse(
        request, "home.html", _hub_context(request, message), status_code=status_code)


@router.post("/jobs")
def start_job(
    request: Request,
    user: str = Form(...),
    email: str = Form(""),
    depth: int = Form(14),
    threads: int = Form(2),
    pause: float = Form(0.6),
    min_loss: int = Form(INACCURACY),
    since: str = Form(""),
    time_class: str = Form(""),
    limit: str = Form(""),
):
    users = [u.strip() for u in user.split(",") if u.strip()]
    if not users:
        return _job_error(request, "Enter at least one Chess.com username.", 400)
    if not email.strip():
        # The Update control never shows an email field (it's a saved
        # setting) -- there's nothing to fix inline, so send the user to
        # where they can actually set it.
        return RedirectResponse("/settings?needs_email=1", status_code=303)

    engine_path = request.app.state.engine_path or find_engine()
    if not engine_path or not os.path.isfile(engine_path):
        return _job_error(request, ENGINE_HELP, 400)

    try:
        status = request.app.state.jobs.start_job(
            users, email.strip(), engine_path, depth, threads, pause,
            since=since.strip() or None,
            time_class=time_class.strip() or None,
            limit=int(limit) if limit.strip() else None,
            min_loss=min_loss)
    except JobAlreadyRunningError as exc:
        return _job_error(request, str(exc), 409)

    return RedirectResponse(f"/jobs/{status.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_progress_page(request: Request, job_id: str):
    status = request.app.state.jobs.get_status(job_id)
    if status is None:
        return HTMLResponse("<p>No such job.</p>", status_code=404)
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "progress.html", {
        "job_id": job_id, "users": status["users"], "settings": settings,
        "big_update_threshold": BIG_UPDATE_THRESHOLD,
        "seconds_per_game": SECONDS_PER_GAME,
        "parallel_threshold": DEFAULT_PARALLEL_THRESHOLD, "workers": DEFAULT_WORKERS,
    })


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, users: str = ""):
    user_list = [u.strip() for u in users.split(",") if u.strip()]
    if not user_list:
        return HTMLResponse("<p>No users selected.</p>", status_code=400)

    conn = open_db(request.app.state.db_path)
    try:
        result = render_dashboard_html(conn, user_list)
    finally:
        conn.close()

    if result is None:
        return HTMLResponse("<p>No stored games for these users yet.</p>")
    html, _ = result
    return HTMLResponse(html)


@router.get("/achievements", response_class=HTMLResponse)
def achievements_page(request: Request, users: str = ""):
    """
    How each mistake category has moved over time, for one player, plus
    practice-mode usage stats (practice_stats()): overall progress, solve
    rate by category (yours vs. other players' via extend-to-others), hint
    usage, and recent session history.
    """
    user = next((u.strip() for u in users.split(",") if u.strip()), None)
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        if not user:
            user = settings["primary_user"]
        model = report_model(conn, user) if user else None
        stats = practice_stats(conn, user) if user else None
        progress = None
        if user:
            # Attempts made before badges existed still count: award whatever
            # the history already qualifies for.
            gamification.evaluate_badges(conn, user)
            progress = {
                "streak": gamification.get_streak(conn, user),
                "badges": gamification.get_badges(conn, user),
                "rating": gamification.get_skill_rating(conn, user),
                "rush_best": gamification.best_rush_score(conn, user),
                "activity": gamification.activity_days(conn, user),
            }
    finally:
        conn.close()

    if not user:
        return HTMLResponse("<p>No user selected.</p>", status_code=400)
    if model is None:
        return templates.TemplateResponse(request, "achievements.html",
            {"username": user, "empty": True, "settings": settings})

    halves = (model["trend"] or {}).get("halves") or []
    improved = sorted((d for d in halves if d[0] < 0), key=lambda d: d[0])
    worsened = sorted((d for d in halves if d[0] > 0), key=lambda d: -d[0])

    return templates.TemplateResponse(request, "achievements.html", {
        "username": user, "empty": False, "settings": settings,
        "n_serious": model["n_serious"], "recurring": model["recurring"],
        "improved": improved, "worsened": worsened,
        "has_trend": model["trend"] is not None,
        "practice": stats, "progress": progress,
    })


def _category_counts(conn, user: str) -> list[tuple[str, int]]:
    model = report_model(conn, user)
    return model["recurring"] if model else []


@router.get("/practice", response_class=HTMLResponse)
@router.get("/practice/{mistake_id}", response_class=HTMLResponse)
def practice_page(request: Request, mistake_id: int | None = None, users: str = "",
                  tc: str = "", phase: str = "", category: str = "", extend: str = ""):
    """
    A queue of stored mistakes to try again, worst-then-most-recent first
    (practice_queue()), for exactly one player and (once chosen) one
    category. Reachable from the dashboard's "Positions to review" panel (a
    specific `mistake_id`, category implied), the home page's Practice
    button (no id, no category -> a category picker), or a category link
    from that picker.
    """
    user = next((u.strip() for u in users.split(",") if u.strip()), None)
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        if not user:
            user = settings["primary_user"]
        if not user:
            return HTMLResponse("<p>No user selected.</p>", status_code=400)

        if not category and mistake_id is None:
            categories = _category_counts(conn, user)
            if not categories:
                return templates.TemplateResponse(request, "practice.html",
                    {"empty": True, "pickCategory": False, "username": user,
                     "settings": settings})
            return templates.TemplateResponse(request, "practice.html", {
                "empty": False, "pickCategory": True, "username": user,
                "categories": categories, "tc": tc, "phase": phase, "settings": settings,
            })

        queue = practice_queue(conn, user, time_class=tc or None, phase=phase or None,
                               category=category or None)
        pool_available = 0
        if category:
            pool_available = len(practice_pool(conn, category, exclude_user=user,
                                                time_class=tc or None, phase=phase or None))
        if extend == "1" and category:
            pool = practice_pool(conn, category, exclude_user=user,
                                 time_class=tc or None, phase=phase or None)
            queue = list(queue) + list(pool)
            pool_available = 0
    finally:
        conn.close()

    if not queue:
        return templates.TemplateResponse(request, "practice.html",
            {"empty": True, "pickCategory": False, "username": user, "settings": settings})

    if mistake_id is None:
        index = 0
    else:
        index = next((i for i, m in enumerate(queue) if m["id"] == mistake_id), None)
        if index is None:
            return HTMLResponse(
                "<p>That position isn't in this player's practice queue right now "
                "(it may be outside the current filter, or not among the worst "
                f"{len(queue)}).</p>", status_code=404)

    row = queue[index]
    board = chess.Board(row["fen"])
    legal_moves = [m.uci() for m in board.legal_moves]
    near_end = (index >= len(queue) - 3) and pool_available > 0 and extend != "1"

    data = {
        "mistakeId": row["id"],
        "fen": row["fen"],
        "colour": row["my_colour"],
        "moveNumber": row["move_number"],
        "phase": row["phase"],
        "category": row["category"],
        "cpLoss": row["cp_loss"],
        "gameUrl": row["game_url"],
        "owner": row["username"],
        "practicingUser": user,
        "legalMoves": legal_moves,
        "queuePosition": index + 1,
        "queueTotal": len(queue),
        "nextId": queue[index + 1]["id"] if index + 1 < len(queue) else None,
        "prevId": queue[index - 1]["id"] if index > 0 else None,
        "users": user, "tc": tc, "phaseFilter": phase, "categoryFilter": category,
        "nearEnd": near_end, "poolAvailable": pool_available, "extended": extend == "1",
        "extendUrl": f"/practice/{row['id']}?users={user}&tc={tc}&phase={phase}"
                    f"&category={category}&extend=1",
    }
    return templates.TemplateResponse(request, "practice.html",
        {"empty": False, "pickCategory": False, "username": user, "data": data,
         "settings": settings})


@router.get("/play", response_class=HTMLResponse)
def play_page(request: Request, users: str = ""):
    """
    Phase 5: play a full game against Stockfish or Maia, optionally steered
    toward whichever game phase this player statistically struggles in (once
    they have enough mistake data for at least one time class -- see
    reports.adaptive_eligible_categories()). The board/move UI reuses
    practice.html's hand-rolled JS approach rather than a client-side chess
    library.
    """
    user = next((u.strip() for u in users.split(",") if u.strip()), None)
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        if not user:
            user = settings["primary_user"]
        if not user:
            return HTMLResponse("<p>No user selected.</p>", status_code=400)

        time_classes = sorted(r["time_class"] for r in conn.execute(
            "SELECT DISTINCT time_class FROM games WHERE username = ? AND time_class IS NOT NULL",
            (user.lower(),)).fetchall())
        # Category names per time class, for the adaptive toggle's tooltip --
        # eligibility itself is scoped per time class (see reports.py), so
        # the JS re-checks this map when the player changes the dropdown
        # rather than gating on one flat yes/no for the whole page.
        eligible_by_tc = {
            tc: sorted(adaptive_eligible_categories(conn, user, tc)) for tc in time_classes
        }
    finally:
        conn.close()

    maia_bands = sorted(find_maia_weights().keys())
    return templates.TemplateResponse(request, "play.html", {
        "username": user, "settings": settings, "time_classes": time_classes,
        "eligible_by_tc": eligible_by_tc, "maia_available": bool(maia_bands),
        "maia_bands": maia_bands, "min_elo": PLAY_MIN_ELO, "max_elo": PLAY_MAX_ELO,
        "stockfish_min_elo": STOCKFISH_MIN_ELO,
    })


@router.get("/puzzles", response_class=HTMLResponse)
def puzzles_page(request: Request, users: str = "", minRating: int | None = None,
                 maxRating: int | None = None, themes: list[str] = Query(default=[])):
    """
    Lichess puzzle solving mode. Picks one random puzzle from the locally
    imported `puzzles` table matching the rating range/theme filter (see
    scripts/import_puzzles.py for how that table gets populated) -- unlike
    practice mode there's no per-user queue to page through, puzzles aren't
    tied to any tracked player's own mistakes. `themes` arrives as one
    query param per checked box (`?themes=fork&themes=pin`, the natural
    shape a <form> with repeated checkbox names submits as), not a
    comma-joined string.

    When the request carries no rating range (first visit, no form submitted
    yet) the range is pre-filled from the player's own skill rating, if they
    have one; once they submit the form, what they typed always wins.
    """
    user = next((u.strip() for u in users.split(",") if u.strip()), None)
    theme_list = [t.strip() for t in themes if t.strip()]
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        if not user:
            user = settings["primary_user"]
        if not user:
            return HTMLResponse("<p>No user selected.</p>", status_code=400)

        smart = None
        if minRating is None and maxRating is None:
            smart = gamification.smart_rating_window(conn, user)
        lo, hi = smart or (DEFAULT_MIN_RATING, DEFAULT_MAX_RATING)
        minRating = lo if minRating is None else minRating
        maxRating = hi if maxRating is None else maxRating

        row = pick_random_puzzle(conn, minRating, maxRating, theme_list or None)
        source_stats = None if row is not None else source_stats_for_filter(
            conn, minRating, maxRating, theme_list or None)
    finally:
        conn.close()

    return _puzzle_response(request, user, settings, row, {
        "minRating": minRating, "maxRating": maxRating, "themes": theme_list,
        "sourceStats": source_stats, "smartRange": smart is not None, "mode": "normal"})


def _puzzle_response(request: Request, user: str, settings: dict, row, extra: dict):
    ctx = {"username": user, "settings": settings, "themeGroups": THEME_GROUPS, **extra}
    if row is None:
        return templates.TemplateResponse(request, "puzzles.html", {**ctx, "found": False})
    return templates.TemplateResponse(request, "puzzles.html", {
        **ctx, "found": True, "data": puzzle_position_payload(row, user)})


def _resolve_user(conn, users: str):
    user = next((u.strip() for u in users.split(",") if u.strip()), None)
    settings = get_settings(conn)
    return (user or settings["primary_user"]), settings


@router.get("/puzzles/daily", response_class=HTMLResponse)
def daily_puzzle_page(request: Request, users: str = ""):
    """Today's puzzle -- the same one for everyone, picked at random the first
    time it's asked for each (UTC) day."""
    conn = open_db(request.app.state.db_path)
    try:
        user, settings = _resolve_user(conn, users)
        if not user:
            return HTMLResponse("<p>No user selected.</p>", status_code=400)
        row = gamification.get_or_assign_daily_puzzle(conn)
        solved = gamification.daily_solved(conn, user)
        source_stats = None if row is not None else source_stats_for_filter(
            conn, DEFAULT_MIN_RATING, DEFAULT_MAX_RATING, None)
    finally:
        conn.close()
    return _puzzle_response(request, user, settings, row, {
        "minRating": DEFAULT_MIN_RATING, "maxRating": DEFAULT_MAX_RATING, "themes": [],
        "sourceStats": source_stats, "smartRange": False, "mode": "daily",
        "dailySolved": solved})


@router.get("/puzzles/rush", response_class=HTMLResponse)
def puzzle_rush_page(request: Request, users: str = ""):
    """Timed puzzle rush: solve as many as possible before the clock runs out.
    Only the final score is stored (POST /api/puzzles/rush/finish)."""
    conn = open_db(request.app.state.db_path)
    try:
        user, settings = _resolve_user(conn, users)
        if not user:
            return HTMLResponse("<p>No user selected.</p>", status_code=400)
        window = gamification.smart_rating_window(conn, user)
        row = pick_random_puzzle(conn, *(window or (None, None)))
        if row is None and window:
            row = pick_random_puzzle(conn)
        best = gamification.best_rush_score(conn, user)
        source_stats = None if row is not None else source_stats_for_filter(
            conn, DEFAULT_MIN_RATING, DEFAULT_MAX_RATING, None)
    finally:
        conn.close()
    return _puzzle_response(request, user, settings, row, {
        "minRating": DEFAULT_MIN_RATING, "maxRating": DEFAULT_MAX_RATING, "themes": [],
        "sourceStats": source_stats, "smartRange": False, "mode": "rush",
        "rushSeconds": gamification.RUSH_DURATION_S, "rushBest": best})


@router.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request):
    """Every tracked player with some gamification activity, side by side --
    purely local: only the players in this machine's own database."""
    conn = open_db(request.app.state.db_path)
    try:
        settings = get_settings(conn)
        rows = gamification.leaderboard(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "leaderboard.html", {
        "rows": rows, "settings": settings, "primary_user": settings["primary_user"]})

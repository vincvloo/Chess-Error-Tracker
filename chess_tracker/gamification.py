"""Streaks, skill rating, badges, daily puzzle, puzzle rush, leaderboard.

Everything here takes an open sqlite3 connection and the username, and only
touches the gamification tables in db.py plus reads of the existing
practice_attempts / puzzle_attempts / games / mistakes / puzzles tables.

Skill rating has two halves that are never merged into one running number:
  * puzzle half  -- Elo-delta after every puzzle attempt (always "now", so a
    running update is safe);
  * game half    -- a bounded performance estimate rebuilt from scratch in
    chronological order (real games can arrive out of order or be reanalysed).
They are only blended when read (see get_skill_rating).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .puzzles import pick_random_puzzle, humanize_theme

BASE_RATING = 1200.0
PHASES = ("opening", "middlegame", "endgame")

# Puzzle half.
PUZZLE_K = 32

# Game half. A game's average capped cp loss per move is mapped onto a
# rating-like scale (heuristic, fitted loosely against real games' Chess.com
# ratings in the local database: ~53 cp/move around 1400, ~97 around 300),
# then folded into the running value as an exponential moving average, so the
# number stays bounded no matter how many games are replayed.
GAME_CP_CAP = 500           # one mate-blunder must not swamp a whole game
GAME_PERF_ANCHOR_CP = 53.0
GAME_PERF_ANCHOR_RATING = 1400.0
GAME_PERF_SLOPE = 12.0      # rating points per cp/move
GAME_PERF_MIN, GAME_PERF_MAX = 400.0, 2400.0
GAME_ALPHA = 0.03
MIN_PHASE_MOVES = 3         # too few moves in a phase = no signal

# Blending: each half counts for at most this many samples.
BLEND_CAP = 30
# The Puzzles page's default difficulty window around the overall rating.
SMART_RANGE = 150

RUSH_DURATION_S = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# --------------------------------------------------------------------------
# Streaks
# --------------------------------------------------------------------------

def record_activity(conn: sqlite3.Connection, username: str, on: date | None = None) -> dict:
    """Count today as a practice day. Puzzles and practice both call this.
    One streak freeze per ISO week bridges exactly one missed day."""
    username = username.lower()
    today = on or _today()
    row = conn.execute("SELECT * FROM user_streaks WHERE username = ?", (username,)).fetchone()
    if row is None:
        current, best, last, tokens, refill = 0, 0, None, 1, _week_key(today)
    else:
        current, best = row["current_streak"], row["best_streak"]
        last = date.fromisoformat(row["last_active_date"]) if row["last_active_date"] else None
        tokens, refill = row["freeze_tokens"], row["freeze_refill_week"]

    if refill != _week_key(today):
        tokens, refill = 1, _week_key(today)

    if last != today:
        gap = (today - last).days if last else None
        if gap == 1:
            current += 1
        elif gap == 2 and tokens > 0:
            tokens -= 1
            current += 1
        else:
            current = 1
        best = max(best, current)
        last = today

    conn.execute(
        "INSERT INTO user_streaks (username, current_streak, best_streak, last_active_date, "
        "freeze_tokens, freeze_refill_week, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET current_streak = excluded.current_streak, "
        "best_streak = excluded.best_streak, last_active_date = excluded.last_active_date, "
        "freeze_tokens = excluded.freeze_tokens, freeze_refill_week = excluded.freeze_refill_week, "
        "updated_at = excluded.updated_at",
        (username, current, best, last.isoformat(), tokens, refill, _now()))
    conn.commit()
    return get_streak(conn, username, today)


def get_streak(conn: sqlite3.Connection, username: str, today: date | None = None) -> dict:
    """What to show. The stored current_streak goes stale when days are missed,
    so a streak that can no longer be continued reads as 0."""
    today = today or _today()
    row = conn.execute("SELECT * FROM user_streaks WHERE username = ?",
                       (username.lower(),)).fetchone()
    if row is None or not row["last_active_date"]:
        return {"current": 0, "best": 0, "freezes": 1, "active_today": False, "at_risk": False}
    last = date.fromisoformat(row["last_active_date"])
    gap = (today - last).days
    tokens = row["freeze_tokens"] if row["freeze_refill_week"] == _week_key(today) else 1
    if gap <= 1:
        alive = True
    elif gap == 2 and tokens > 0:
        alive = True
    else:
        alive = False
    return {
        "current": row["current_streak"] if alive else 0,
        "best": row["best_streak"],
        "freezes": tokens,
        "active_today": gap == 0,
        "at_risk": alive and gap >= 1,
    }


# --------------------------------------------------------------------------
# Puzzle rating (live Elo-delta)
# --------------------------------------------------------------------------

def _elo_step(rating: float, opp: float, actual: float, k: float) -> float:
    expected = 1 / (1 + 10 ** ((opp - rating) / 400))
    return rating + k * (actual - expected)


def update_puzzle_rating(conn: sqlite3.Connection, username: str, puzzle_rating: int | None,
                         solved: bool, themes: str | list[str] | None = None) -> None:
    """Move the overall row and one row per theme the puzzle carries. `themes`
    is the raw space-separated column from `puzzles` (or a list of codes)."""
    if not puzzle_rating:
        return
    username = username.lower()
    codes = themes.split() if isinstance(themes, str) else list(themes or [])
    for theme in [""] + sorted(set(codes)):
        row = conn.execute(
            "SELECT rating, puzzles_seen FROM user_puzzle_ratings WHERE username = ? AND theme = ?",
            (username, theme)).fetchone()
        rating, seen = (row["rating"], row["puzzles_seen"]) if row else (BASE_RATING, 0)
        new = _elo_step(rating, puzzle_rating, 1.0 if solved else 0.0, PUZZLE_K)
        conn.execute(
            "INSERT INTO user_puzzle_ratings (username, theme, rating, puzzles_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(username, theme) DO UPDATE SET "
            "rating = excluded.rating, puzzles_seen = excluded.puzzles_seen, "
            "updated_at = excluded.updated_at",
            (username, theme, new, seen + 1, _now()))
    conn.commit()


# --------------------------------------------------------------------------
# Game rating (chronological replay + bot-game increments)
# --------------------------------------------------------------------------

def performance_rating(avg_cp_loss: float) -> float:
    """Average capped cp loss per move -> a rating-like number, clamped."""
    perf = GAME_PERF_ANCHOR_RATING - GAME_PERF_SLOPE * (avg_cp_loss - GAME_PERF_ANCHOR_CP)
    return max(GAME_PERF_MIN, min(GAME_PERF_MAX, perf))


def _ema(rating: float, perf: float) -> float:
    return rating + GAME_ALPHA * (perf - rating)


def _fold_game(state: dict, loss_total: float, moves: int,
               phase_loss: dict[str, float], phase_moves: dict[str, int]) -> None:
    """Fold one game into {theme: [rating, games_seen]} in place."""
    if moves <= 0:
        return
    r = state.setdefault("", [BASE_RATING, 0])
    r[0] = _ema(r[0], performance_rating(loss_total / moves))
    r[1] += 1
    for ph in PHASES:
        n = phase_moves.get(ph) or 0
        if n < MIN_PHASE_MOVES:
            continue
        pr = state.setdefault(ph, [BASE_RATING, 0])
        pr[0] = _ema(pr[0], performance_rating(phase_loss.get(ph, 0.0) / n))
        pr[1] += 1


def _write_game_state(conn: sqlite3.Connection, username: str, state: dict) -> None:
    conn.execute("DELETE FROM user_game_ratings WHERE username = ?", (username,))
    now = _now()
    conn.executemany(
        "INSERT INTO user_game_ratings (username, theme, rating, games_seen, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(username, theme, v[0], v[1], now) for theme, v in state.items()])
    conn.commit()


def recompute_game_ratings(conn: sqlite3.Connection, username: str) -> None:
    """Rebuild the game half from scratch, replaying every stored game in
    end_time order. Idempotent: same games in, same numbers out, whatever
    order they were fetched or reanalysed in."""
    username = username.lower()
    loss: dict[str, float] = {}
    phase_loss: dict[tuple[str, str], float] = {}
    for url, phase, total in conn.execute(
            "SELECT game_url, phase, SUM(MIN(cp_loss, ?)) FROM mistakes "
            "WHERE username = ? GROUP BY game_url, phase", (GAME_CP_CAP, username)):
        loss[url] = loss.get(url, 0.0) + (total or 0)
        phase_loss[(url, phase)] = total or 0

    state: dict[str, list] = {}
    for g in conn.execute(
            "SELECT url, moves_played, opening_moves, middlegame_moves, endgame_moves "
            "FROM games WHERE username = ? AND moves_played > 0 "
            "ORDER BY end_time, url", (username,)):
        url = g["url"]
        _fold_game(
            state, loss.get(url, 0.0), g["moves_played"],
            {ph: phase_loss.get((url, ph), 0.0) for ph in PHASES},
            {"opening": g["opening_moves"] or 0, "middlegame": g["middlegame_moves"] or 0,
             "endgame": g["endgame_moves"] or 0})
    _write_game_state(conn, username, state)


def phase_move_counts(moves, me) -> dict[str, int]:
    """How many of `me`'s moves fell in each phase, for a bot game's move list."""
    from .analysis import _iter_own_moves_from_list, game_phase
    counts = {ph: 0 for ph in PHASES}
    for board, _played in _iter_own_moves_from_list(moves, me):
        counts[game_phase(board, board.fullmove_number)] += 1
    return counts


def apply_bot_game(conn: sqlite3.Connection, username: str, mistakes: list[dict],
                   phase_moves: dict[str, int]) -> None:
    """One incremental update for a just-finished bot game. Bot games are never
    stored and always happen 'now', so they need no replay -- they just sit on
    top of whatever recompute_game_ratings last produced."""
    username = username.lower()
    total_moves = sum(phase_moves.values())
    loss_total = sum(min(m["cp_loss"], GAME_CP_CAP) for m in mistakes)
    phase_loss = {ph: 0.0 for ph in PHASES}
    for m in mistakes:
        if m["phase"] in phase_loss:
            phase_loss[m["phase"]] += min(m["cp_loss"], GAME_CP_CAP)

    state = {r["theme"]: [r["rating"], r["games_seen"]] for r in conn.execute(
        "SELECT theme, rating, games_seen FROM user_game_ratings WHERE username = ?",
        (username,))}
    _fold_game(state, loss_total, total_moves, phase_loss, phase_moves)
    _write_game_state(conn, username, state)


# --------------------------------------------------------------------------
# Reading the blended rating
# --------------------------------------------------------------------------

def _blend(p: tuple[float, int] | None, g: tuple[float, int] | None) -> tuple[float, int] | None:
    parts = [(r, min(n, BLEND_CAP), n) for r, n in (x for x in (p, g) if x is not None) if n]
    if not parts:
        return None
    wsum = sum(w for _, w, _ in parts)
    return sum(r * w for r, w, _ in parts) / wsum, sum(n for _, _, n in parts)


def get_skill_rating(conn: sqlite3.Connection, username: str) -> dict:
    """{"overall": {rating, samples} | None, "themes": [{theme, label, rating, samples}]}.
    Tactical themes are puzzle-only; the phase themes blend both halves."""
    username = username.lower()
    puz = {r["theme"]: (r["rating"], r["puzzles_seen"]) for r in conn.execute(
        "SELECT theme, rating, puzzles_seen FROM user_puzzle_ratings WHERE username = ?",
        (username,))}
    gam = {r["theme"]: (r["rating"], r["games_seen"]) for r in conn.execute(
        "SELECT theme, rating, games_seen FROM user_game_ratings WHERE username = ?",
        (username,))}

    overall = _blend(puz.get(""), gam.get(""))
    themes = []
    for theme in sorted((set(puz) | set(gam)) - {""}):
        blended = _blend(puz.get(theme), gam.get(theme))
        if blended:
            themes.append({"theme": theme, "label": humanize_theme(theme),
                           "rating": round(blended[0]), "samples": blended[1]})
    themes.sort(key=lambda t: (-t["samples"], t["theme"]))
    return {"overall": None if overall is None else
            {"rating": round(overall[0]), "samples": overall[1]},
            "themes": themes}


def smart_rating_window(conn: sqlite3.Connection, username: str) -> tuple[int, int] | None:
    overall = get_skill_rating(conn, username)["overall"]
    if overall is None:
        return None
    return overall["rating"] - SMART_RANGE, overall["rating"] + SMART_RANGE


# --------------------------------------------------------------------------
# Badges
# --------------------------------------------------------------------------

BADGE_DEFINITIONS = [
    {"code": "first_puzzle", "label": "First Steps", "description": "Solve your first puzzle",
     "check": lambda s: s["puzzles_solved"] >= 1},
    {"code": "first_practice", "label": "Getting Started",
     "description": "Complete your first practice attempt",
     "check": lambda s: s["practice_attempts"] >= 1},
    {"code": "streak_7", "label": "Week Warrior", "description": "Reach a 7-day streak",
     "check": lambda s: s["best_streak"] >= 7},
    {"code": "streak_30", "label": "Monthly Habit", "description": "Reach a 30-day streak",
     "check": lambda s: s["best_streak"] >= 30},
    {"code": "streak_100", "label": "Iron Will", "description": "Reach a 100-day streak",
     "check": lambda s: s["best_streak"] >= 100},
    {"code": "puzzles_100", "label": "Puzzle Century", "description": "Solve 100 puzzles",
     "check": lambda s: s["puzzles_solved"] >= 100},
    {"code": "puzzles_1000", "label": "Puzzle Grandmaster", "description": "Solve 1000 puzzles",
     "check": lambda s: s["puzzles_solved"] >= 1000},
    {"code": "no_hint_10", "label": "Sharp Eye",
     "description": "Solve 10 practice positions without a hint",
     "check": lambda s: s["practice_no_hint"] >= 10},
    {"code": "rating_1500", "label": "Rising Star", "description": "Skill rating reaches 1500",
     "check": lambda s: s["rating"] >= 1500},
    {"code": "rating_1800", "label": "Strong Player", "description": "Skill rating reaches 1800",
     "check": lambda s: s["rating"] >= 1800},
]
BADGES_BY_CODE = {b["code"]: b for b in BADGE_DEFINITIONS}


def _badge_stats(conn: sqlite3.Connection, username: str) -> dict:
    solved = conn.execute(
        "SELECT COUNT(DISTINCT puzzle_id) FROM puzzle_attempts "
        "WHERE practicing_user = ? AND verdict = 'solved'", (username,)).fetchone()[0]
    attempts = conn.execute(
        "SELECT COUNT(*) FROM practice_attempts WHERE practicing_user = ?",
        (username,)).fetchone()[0]
    no_hint = conn.execute(
        "SELECT COUNT(DISTINCT mistake_id) FROM practice_attempts WHERE practicing_user = ? "
        "AND hint_used = 0 AND verdict IN ('best', 'also_fine')", (username,)).fetchone()[0]
    streak = conn.execute("SELECT best_streak FROM user_streaks WHERE username = ?",
                          (username,)).fetchone()
    overall = get_skill_rating(conn, username)["overall"]
    return {"puzzles_solved": solved, "practice_attempts": attempts, "practice_no_hint": no_hint,
            "best_streak": streak[0] if streak else 0,
            "rating": overall["rating"] if overall else 0}


def evaluate_badges(conn: sqlite3.Connection, username: str) -> list[str]:
    """Award any newly-qualifying badges; returns the codes just earned. Earned
    badges are permanent -- deleting practice history does not revoke them."""
    username = username.lower()
    stats = _badge_stats(conn, username)
    earned = {r[0] for r in conn.execute(
        "SELECT badge_code FROM badges_earned WHERE username = ?", (username,))}
    new = []
    for badge in BADGE_DEFINITIONS:
        if badge["code"] not in earned and badge["check"](stats):
            conn.execute("INSERT OR IGNORE INTO badges_earned (username, badge_code, earned_at) "
                         "VALUES (?, ?, ?)", (username, badge["code"], _now()))
            new.append(badge["code"])
    conn.commit()
    return new


def get_badges(conn: sqlite3.Connection, username: str) -> dict:
    """{"earned": [{code,label,description,earned_at}], "locked": [{code,label,description}]}"""
    rows = {r["badge_code"]: r["earned_at"] for r in conn.execute(
        "SELECT badge_code, earned_at FROM badges_earned WHERE username = ?",
        (username.lower(),))}
    earned, locked = [], []
    for b in BADGE_DEFINITIONS:
        item = {"code": b["code"], "label": b["label"], "description": b["description"]}
        if b["code"] in rows:
            earned.append({**item, "earned_at": rows[b["code"]]})
        else:
            locked.append(item)
    earned.sort(key=lambda b: b["earned_at"])
    return {"earned": earned, "locked": locked}


def record_progress(conn: sqlite3.Connection, username: str) -> list[dict]:
    """Streak + badge check after any practice/puzzle attempt; returns the
    newly earned badges (label/description) so callers can announce them."""
    record_activity(conn, username)
    return [{"code": c, "label": BADGES_BY_CODE[c]["label"],
             "description": BADGES_BY_CODE[c]["description"]}
            for c in evaluate_badges(conn, username)]


# --------------------------------------------------------------------------
# Daily puzzle and puzzle rush
# --------------------------------------------------------------------------

def get_or_assign_daily_puzzle(conn: sqlite3.Connection, on: date | None = None):
    """Today's puzzle row, picking (and remembering) one at random the first
    time it's asked for. None if no puzzles are imported."""
    day = (on or _today()).isoformat()
    row = conn.execute("SELECT puzzle_id FROM daily_puzzles WHERE date = ?", (day,)).fetchone()
    if row is not None:
        puzzle = conn.execute("SELECT * FROM puzzles WHERE puzzle_id = ?",
                              (row["puzzle_id"],)).fetchone()
        if puzzle is not None:
            return puzzle
    puzzle = pick_random_puzzle(conn)
    if puzzle is None:
        return None
    conn.execute("INSERT OR REPLACE INTO daily_puzzles (date, puzzle_id, assigned_at) "
                 "VALUES (?, ?, ?)", (day, puzzle["puzzle_id"], _now()))
    conn.commit()
    return puzzle


def daily_solved(conn: sqlite3.Connection, username: str, on: date | None = None) -> bool:
    row = conn.execute("SELECT puzzle_id FROM daily_puzzles WHERE date = ?",
                       ((on or _today()).isoformat(),)).fetchone()
    if row is None:
        return False
    return conn.execute(
        "SELECT 1 FROM puzzle_attempts WHERE practicing_user = ? AND puzzle_id = ? "
        "AND verdict = 'solved' AND substr(created_at, 1, 10) = ? LIMIT 1",
        (username.lower(), row["puzzle_id"], (on or _today()).isoformat())).fetchone() is not None


def record_rush_score(conn: sqlite3.Connection, username: str, score: int,
                      duration_s: int = RUSH_DURATION_S) -> dict:
    username = username.lower()
    best_before = conn.execute("SELECT MAX(score) FROM puzzle_rush_scores WHERE username = ?",
                               (username,)).fetchone()[0]
    conn.execute("INSERT INTO puzzle_rush_scores (username, score, duration_s, played_at) "
                 "VALUES (?, ?, ?, ?)", (username, score, duration_s, _now()))
    conn.commit()
    return {"score": score, "best": max(score, best_before or 0),
            "personalBest": best_before is None or score > best_before}


def best_rush_score(conn: sqlite3.Connection, username: str) -> int | None:
    return conn.execute("SELECT MAX(score) FROM puzzle_rush_scores WHERE username = ?",
                        (username.lower(),)).fetchone()[0]


# --------------------------------------------------------------------------
# Leaderboard
# --------------------------------------------------------------------------

def leaderboard(conn: sqlite3.Connection) -> list[dict]:
    """Every tracked player with at least some gamification activity."""
    from .reports import user_summaries
    rows = []
    for u in user_summaries(conn):
        name = u["username"]
        streak = get_streak(conn, name)
        rating = get_skill_rating(conn, name)["overall"]
        n_badges = conn.execute("SELECT COUNT(*) FROM badges_earned WHERE username = ?",
                                (name,)).fetchone()[0]
        rush = best_rush_score(conn, name)
        active = (n_badges or rush is not None or streak["best"]
                  or conn.execute("SELECT 1 FROM user_puzzle_ratings WHERE username = ? LIMIT 1",
                                  (name,)).fetchone()
                  or conn.execute("SELECT 1 FROM practice_attempts WHERE practicing_user = ? LIMIT 1",
                                  (name,)).fetchone()
                  or conn.execute("SELECT 1 FROM puzzle_attempts WHERE practicing_user = ? LIMIT 1",
                                  (name,)).fetchone())
        if not active:
            continue
        rows.append({"username": name, "rating": rating["rating"] if rating else None,
                     "streak": streak["current"], "best_streak": streak["best"],
                     "badges": n_badges, "rush": rush})
    rows.sort(key=lambda r: (-(r["rating"] or 0), r["username"]))
    return rows

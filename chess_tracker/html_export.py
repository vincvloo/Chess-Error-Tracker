"""Self-contained, offline HTML dashboard export."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from .analysis import PHASES
from .reports import (
    CLOCK_BUCKET_LABELS,
    CLOCK_BUCKET_SQL_CASE,
    DELTA_EPSILON,
    ECO_MIN_GAMES,
    ECO_TOP_N,
    SERIOUS,
    TIME_PRESSURE_ALERT,
)

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates", "dashboard_template.html")

# Severity and colour have a fixed, known set of values (unlike time class,
# category or eco, which must be read from the data -- see the phase 2 plan
# on why a hardcoded time_class list silently dropped rows).
SEVERITIES = ("inaccuracy", "mistake", "blunder")
COLOURS = ("white", "black")

THIN_GAMES_THRESHOLD = 100


def _index_map(values) -> dict:
    return {v: i for i, v in enumerate(values)}


def _fact_table(dims: list[str], measures: list[str], data: list[list]) -> dict:
    return {"dims": dims, "measures": measures, "data": data}


def build_dashboard_data(conn: sqlite3.Connection, users: list[str]) -> dict:
    """
    Gather everything the dashboard's client-side JS needs to render itself,
    as a set of flat fact tables (a list of dimension-index + measure rows
    per table) rather than nested dicts. A table simply ignores dimensions
    it doesn't have, which is what gives the moves denominator the right
    semantics for free: it has no severity/category dimension, so it
    correctly ignores those filters, matching the terminal report.

    Every list of dimension values (users, time classes, categories, ecos,
    months) is read from the data, never hardcoded -- analyse_game can write
    time_class = '?', and a hardcoded list would silently drop those rows.
    """
    users = [u.lower() for u in users]
    placeholders = ",".join("?" * len(users))
    phases = list(PHASES)

    tc_rows = conn.execute(f"""
        SELECT DISTINCT time_class FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
    """, users).fetchall()
    time_classes = sorted(r["time_class"] for r in tc_rows)

    cat_rows = conn.execute(f"""
        SELECT DISTINCT category FROM mistakes WHERE username IN ({placeholders})
    """, users).fetchall()
    categories = sorted(r["category"] for r in cat_rows)

    eco_rows = conn.execute(f"""
        SELECT DISTINCT eco FROM games
        WHERE username IN ({placeholders}) AND eco IS NOT NULL AND eco != '?'
    """, users).fetchall()
    ecos = sorted(r["eco"] for r in eco_rows)

    month_rows = conn.execute(f"""
        SELECT DISTINCT substr(date,1,7) AS month FROM games
        WHERE username IN ({placeholders}) AND date IS NOT NULL
    """, users).fetchall()
    months = sorted(r["month"] for r in month_rows if r["month"])

    present_rows = conn.execute(f"""
        SELECT DISTINCT username FROM games WHERE username IN ({placeholders})
    """, users).fetchall()
    present = sorted(r["username"] for r in present_rows)

    if not present:
        return {
            "lists": {
                "users": [], "timeClasses": [], "phases": phases,
                "severities": list(SEVERITIES), "categories": [],
                "colours": list(COLOURS), "ecos": [], "months": [],
                "clockBuckets": list(CLOCK_BUCKET_LABELS),
            },
            "movesFacts": _fact_table(["user", "tc", "phase", "month"], ["moves"], []),
            "gamesFacts": _fact_table(["user", "tc", "month"],
                                      ["games", "movesPlayed", "unbackfilled"], []),
            "countFacts": _fact_table(
                ["user", "tc", "phase", "severity", "category", "month"], ["n"], []),
            "clockBucketFacts": _fact_table(
                ["user", "tc", "phase", "severity", "category", "month", "clockBucket"],
                ["n"], []),
            "colourMovesFacts": _fact_table(["user", "tc", "colour", "phase"],
                                            ["moves"], []),
            "colourCountFacts": _fact_table(
                ["user", "tc", "colour", "phase", "severity", "category"], ["n"], []),
            "ecoGamesFacts": _fact_table(["user", "tc", "eco", "colour"], ["games"], []),
            "ecoErrorFacts": _fact_table(
                ["user", "tc", "phase", "severity", "category", "eco", "colour"], ["n"], []),
            "meta": {
                "ecoMinGames": ECO_MIN_GAMES, "ecoTopN": ECO_TOP_N,
                "timePressureAlert": TIME_PRESSURE_ALERT,
                "deltaEpsilon": DELTA_EPSILON, "thinGamesThreshold": THIN_GAMES_THRESHOLD,
                "defaultSeverities": list(SERIOUS),
                "ratingEndpoints": {}, "dateEndpoints": {},
            },
        }

    u_ix, tc_ix, ph_ix = _index_map(present), _index_map(time_classes), _index_map(phases)
    sev_ix, cat_ix = _index_map(SEVERITIES), _index_map(categories)
    col_ix, eco_ix, mo_ix = _index_map(COLOURS), _index_map(ecos), _index_map(months)

    # ---- movesFacts + gamesFacts: one query serves both -------------------
    move_month_rows = conn.execute(f"""
        SELECT username, time_class, substr(date,1,7) AS month,
               COALESCE(SUM(opening_moves),0)    AS opening,
               COALESCE(SUM(middlegame_moves),0) AS middlegame,
               COALESCE(SUM(endgame_moves),0)    AS endgame,
               COUNT(*)                          AS games,
               COALESCE(SUM(moves_played),0)     AS moves_played,
               SUM(CASE WHEN opening_moves IS NULL THEN 1 ELSE 0 END) AS unbackfilled
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL AND date IS NOT NULL
        GROUP BY username, time_class, month
    """, users).fetchall()

    moves_data, games_data = [], []
    for r in move_month_rows:
        u, tc, mo = u_ix[r["username"]], tc_ix[r["time_class"]], mo_ix[r["month"]]
        for phase_name, moves in (("opening", r["opening"]), ("middlegame", r["middlegame"]),
                                  ("endgame", r["endgame"])):
            if moves:
                moves_data.append([u, tc, ph_ix[phase_name], mo, moves])
        games_data.append([u, tc, mo, r["games"], r["moves_played"], r["unbackfilled"]])

    # ---- countFacts ---------------------------------------------------------
    count_rows = conn.execute(f"""
        SELECT username, time_class, phase, severity, category, substr(date,1,7) AS month,
               COUNT(*) AS n
        FROM mistakes
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND phase IS NOT NULL AND date IS NOT NULL
        GROUP BY username, time_class, phase, severity, category, month
    """, users).fetchall()
    count_data = [[u_ix[r["username"]], tc_ix[r["time_class"]], ph_ix[r["phase"]],
                  sev_ix[r["severity"]], cat_ix[r["category"]], mo_ix[r["month"]], r["n"]]
                 for r in count_rows]

    # ---- clockBucketFacts ---------------------------------------------------
    clock_bucket_ix = _index_map(CLOCK_BUCKET_LABELS)
    clock_bucket_rows = conn.execute(f"""
        SELECT username, time_class, phase, severity, category, substr(date,1,7) AS month,
               {CLOCK_BUCKET_SQL_CASE} AS bucket, COUNT(*) AS n
        FROM mistakes
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND phase IS NOT NULL AND date IS NOT NULL AND clock_seconds IS NOT NULL
        GROUP BY username, time_class, phase, severity, category, month, bucket
    """, users).fetchall()
    clock_bucket_data = [[u_ix[r["username"]], tc_ix[r["time_class"]], ph_ix[r["phase"]],
                         sev_ix[r["severity"]], cat_ix[r["category"]], mo_ix[r["month"]],
                         clock_bucket_ix[r["bucket"]], r["n"]]
                        for r in clock_bucket_rows]

    # ---- colourMovesFacts / colourCountFacts -------------------------------
    # colour is a breakdown dimension of one panel, not a filter, so it does
    # not belong on the tables above (measured: adding it there nearly
    # doubles countFacts for no benefit).
    colour_moves_rows = conn.execute(f"""
        SELECT username, time_class, my_colour AS colour,
               COALESCE(SUM(opening_moves),0)    AS opening,
               COALESCE(SUM(middlegame_moves),0) AS middlegame,
               COALESCE(SUM(endgame_moves),0)    AS endgame
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND my_colour IS NOT NULL
        GROUP BY username, time_class, my_colour
    """, users).fetchall()
    colour_moves_data = []
    for r in colour_moves_rows:
        u, tc, co = u_ix[r["username"]], tc_ix[r["time_class"]], col_ix[r["colour"]]
        for phase_name, moves in (("opening", r["opening"]), ("middlegame", r["middlegame"]),
                                  ("endgame", r["endgame"])):
            if moves:
                colour_moves_data.append([u, tc, co, ph_ix[phase_name], moves])

    colour_count_rows = conn.execute(f"""
        SELECT username, time_class, my_colour AS colour, phase, severity, category,
               COUNT(*) AS n
        FROM mistakes
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND phase IS NOT NULL AND my_colour IS NOT NULL
        GROUP BY username, time_class, my_colour, phase, severity, category
    """, users).fetchall()
    colour_count_data = [[u_ix[r["username"]], tc_ix[r["time_class"]], col_ix[r["colour"]],
                         ph_ix[r["phase"]], sev_ix[r["severity"]], cat_ix[r["category"]],
                         r["n"]]
                        for r in colour_count_rows]

    # ---- ecoGamesFacts / ecoErrorFacts -------------------------------------
    # The >=3-games rule can only be applied per user across all time
    # classes (an eco with 2 bullet + 2 blitz games has 4 once both are
    # ticked), so it and the top-8 cut are applied client-side; the server
    # just emits raw per (user, tc, eco, colour) counts. colour is included
    # here (unlike the other tables) because an ECO code alone doesn't say
    # which side it was played from, and that's the whole point of this
    # panel -- e.g. C00 as White is a different opening than C00 as Black.
    eco_games_rows = conn.execute(f"""
        SELECT username, time_class, eco, my_colour AS colour, COUNT(*) AS games
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND eco IS NOT NULL AND eco != '?' AND my_colour IS NOT NULL
        GROUP BY username, time_class, eco, my_colour
    """, users).fetchall()
    eco_games_data = [[u_ix[r["username"]], tc_ix[r["time_class"]], eco_ix[r["eco"]],
                      col_ix[r["colour"]], r["games"]]
                     for r in eco_games_rows]

    # ECO respects the category filter, unlike the terminal report's ECO
    # section, but its denominator (eco_games_data above) is still games in
    # scope, not phase-filtered, while this numerator is phase-filtered --
    # reproducing the terminal report's existing asymmetry, not fixing it.
    eco_error_rows = conn.execute(f"""
        SELECT m.username, m.time_class, m.phase, m.severity, m.category, g.eco,
               m.my_colour AS colour, COUNT(*) AS n
        FROM mistakes m JOIN games g ON g.url = m.game_url AND g.username = m.username
        WHERE m.username IN ({placeholders}) AND m.time_class IS NOT NULL
              AND m.phase IS NOT NULL AND g.eco IS NOT NULL AND g.eco != '?'
              AND m.my_colour IS NOT NULL
        GROUP BY m.username, m.time_class, m.phase, m.severity, m.category, g.eco,
                 m.my_colour
    """, users).fetchall()
    eco_error_data = [[u_ix[r["username"]], tc_ix[r["time_class"]], ph_ix[r["phase"]],
                      sev_ix[r["severity"]], cat_ix[r["category"]], eco_ix[r["eco"]],
                      col_ix[r["colour"]], r["n"]]
                     for r in eco_error_rows]

    # ---- meta: thresholds shared with reports.py, plus rating/date range --
    rating_rows = conn.execute(f"""
        SELECT username, time_class, my_rating, date, end_time
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND my_rating > 0 AND date IS NOT NULL
        ORDER BY username, time_class, end_time
    """, users).fetchall()
    rating_endpoints: dict = {}
    for r in rating_rows:
        by_tc = rating_endpoints.setdefault(r["username"], {})
        entry = by_tc.setdefault(r["time_class"], {
            "firstRating": r["my_rating"], "firstDate": r["date"],
            "lastRating": r["my_rating"], "lastDate": r["date"],
        })
        entry["lastRating"] = r["my_rating"]
        entry["lastDate"] = r["date"]

    date_rows = conn.execute(f"""
        SELECT username, time_class, MIN(date) AS first_date, MAX(date) AS last_date
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL AND date IS NOT NULL
        GROUP BY username, time_class
    """, users).fetchall()
    date_endpoints = {}
    for r in date_rows:
        date_endpoints.setdefault(r["username"], {})[r["time_class"]] = {
            "first": r["first_date"], "last": r["last_date"],
        }

    return {
        "lists": {
            "users": present, "timeClasses": time_classes, "phases": phases,
            "severities": list(SEVERITIES), "categories": categories,
            "colours": list(COLOURS), "ecos": ecos, "months": months,
            "clockBuckets": list(CLOCK_BUCKET_LABELS),
        },
        "movesFacts": _fact_table(["user", "tc", "phase", "month"], ["moves"], moves_data),
        "gamesFacts": _fact_table(["user", "tc", "month"],
                                  ["games", "movesPlayed", "unbackfilled"], games_data),
        "countFacts": _fact_table(
            ["user", "tc", "phase", "severity", "category", "month"], ["n"], count_data),
        "clockBucketFacts": _fact_table(
            ["user", "tc", "phase", "severity", "category", "month", "clockBucket"],
            ["n"], clock_bucket_data),
        "colourMovesFacts": _fact_table(["user", "tc", "colour", "phase"],
                                        ["moves"], colour_moves_data),
        "colourCountFacts": _fact_table(
            ["user", "tc", "colour", "phase", "severity", "category"], ["n"],
            colour_count_data),
        "ecoGamesFacts": _fact_table(["user", "tc", "eco", "colour"], ["games"], eco_games_data),
        "ecoErrorFacts": _fact_table(
            ["user", "tc", "phase", "severity", "category", "eco", "colour"], ["n"],
            eco_error_data),
        "meta": {
            "ecoMinGames": ECO_MIN_GAMES, "ecoTopN": ECO_TOP_N,
            "timePressureAlert": TIME_PRESSURE_ALERT,
            "deltaEpsilon": DELTA_EPSILON, "thinGamesThreshold": THIN_GAMES_THRESHOLD,
            "defaultSeverities": list(SERIOUS),
            "ratingEndpoints": rating_endpoints, "dateEndpoints": date_endpoints,
        },
    }


def render_dashboard_html(conn: sqlite3.Connection, users: list[str]) -> tuple[str, int] | None:
    """
    Render the dashboard as an HTML string for the given users, or None if
    none of them have any stored games. Shared by export_html() (writes the
    result to a file) and the web app's live /dashboard route (serves it
    directly) -- same data, same template, same output either way.
    """
    data = build_dashboard_data(conn, users)
    if not data["lists"]["users"]:
        return None

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    html = (template
            .replace("__GENERATED_AT__", generated)
            .replace("__DATA_JSON__", json.dumps(data)))
    return html, len(data["lists"]["users"])


def export_html(conn: sqlite3.Connection, users: list[str], path: str) -> int:
    """
    Write a self-contained, offline HTML dashboard for the given users: filter
    by player, phase, time class, severity and mistake category, all
    client-side against a JSON blob embedded in the page. No server, no
    network calls once opened.
    """
    result = render_dashboard_html(conn, users)
    if result is None:
        return 0
    html, n = result
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return n

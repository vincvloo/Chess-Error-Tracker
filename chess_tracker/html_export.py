"""Self-contained, offline HTML dashboard export."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from .analysis import PHASES

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates", "dashboard_template.html")


def export_html(conn: sqlite3.Connection, users: list[str], path: str) -> int:
    """
    Write a self-contained, offline HTML dashboard for the given users: filter
    by player, phase, time class and mistake category, all client-side against
    a JSON blob embedded in the page. No server, no network calls once opened.

    Rates use the same denominator convention as compare(): per 100 of that
    player's own moves within whatever phase/time-class slice is selected.
    """
    users = [u.lower() for u in users]
    placeholders = ",".join("?" * len(users))
    time_classes = ["bullet", "blitz", "rapid", "daily"]
    phases = list(PHASES)

    moves_rows = conn.execute(f"""
        SELECT username, time_class,
               COALESCE(SUM(opening_moves),0)    AS opening,
               COALESCE(SUM(middlegame_moves),0) AS middlegame,
               COALESCE(SUM(endgame_moves),0)    AS endgame,
               COUNT(*)                          AS games
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
        GROUP BY username, time_class
    """, users).fetchall()

    count_rows = conn.execute(f"""
        SELECT username, time_class, phase, category, COUNT(*) AS n
        FROM mistakes
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL AND phase IS NOT NULL
        GROUP BY username, time_class, phase, category
    """, users).fetchall()

    # Same two breakdowns again, bucketed by calendar month, for the trend
    # chart. date is stored 'YYYY-MM-DD', so a substr gives 'YYYY-MM'.
    moves_by_month_rows = conn.execute(f"""
        SELECT username, time_class, substr(date,1,7) AS month,
               COALESCE(SUM(opening_moves),0)    AS opening,
               COALESCE(SUM(middlegame_moves),0) AS middlegame,
               COALESCE(SUM(endgame_moves),0)    AS endgame,
               COUNT(*)                          AS games
        FROM games
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL AND date IS NOT NULL
        GROUP BY username, time_class, month
    """, users).fetchall()

    count_by_month_rows = conn.execute(f"""
        SELECT username, time_class, phase, substr(date,1,7) AS month, category, COUNT(*) AS n
        FROM mistakes
        WHERE username IN ({placeholders}) AND time_class IS NOT NULL
              AND phase IS NOT NULL AND date IS NOT NULL
        GROUP BY username, time_class, phase, month, category
    """, users).fetchall()

    present = sorted({r["username"] for r in moves_rows})
    if not present:
        return 0
    categories = sorted({r["category"] for r in count_rows})

    moves: dict = {u: {} for u in present}
    for r in moves_rows:
        moves[r["username"]][r["time_class"]] = {
            "opening": r["opening"], "middlegame": r["middlegame"],
            "endgame": r["endgame"], "games": r["games"],
        }

    counts: dict = {u: {} for u in present}
    for r in count_rows:
        (counts.setdefault(r["username"], {})
               .setdefault(r["time_class"], {})
               .setdefault(r["phase"], {})[r["category"]]) = r["n"]

    months: set = set()
    moves_by_month: dict = {u: {} for u in present}
    for r in moves_by_month_rows:
        months.add(r["month"])
        (moves_by_month.setdefault(r["username"], {})
                        .setdefault(r["time_class"], {})[r["month"]]) = {
            "opening": r["opening"], "middlegame": r["middlegame"],
            "endgame": r["endgame"], "games": r["games"],
        }

    counts_by_month: dict = {u: {} for u in present}
    for r in count_by_month_rows:
        months.add(r["month"])
        (counts_by_month.setdefault(r["username"], {})
                         .setdefault(r["time_class"], {})
                         .setdefault(r["phase"], {})
                         .setdefault(r["month"], {})[r["category"]]) = r["n"]

    data = {
        "users": present,
        "phases": phases,
        "timeClasses": time_classes,
        "categories": categories,
        "moves": moves,
        "counts": counts,
        "months": sorted(months),
        "movesByMonth": moves_by_month,
        "countsByMonth": counts_by_month,
        "thinGamesThreshold": 100,
    }

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    html = (template
            .replace("__GENERATED_AT__", generated)
            .replace("__DATA_JSON__", json.dumps(data)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(present)

"""Synthetic dashboard data for first-run onboarding, so someone can see what
a populated dashboard looks like while their own analysis runs in the
background. Built through the real build_dashboard_data() pipeline against a
throwaway in-memory database, rather than hand-authored JSON, so it can never
drift from the real data shape -- same row-factory pattern already used by
tests/test_html_export_parity.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache

from ..db import open_db, save_game
from ..html_export import TEMPLATE_PATH, build_dashboard_data

DEMO_USERNAME = "demo"

# (date, time_class, colour, rating, eco, result, moves, opening, middlegame, endgame)
# A rising rating and a tapering mistake rate (see _MISTAKES below) tell a
# believable "you're improving" story, same as a real, well-tracked player.
_GAMES = [
    ("2026-01-04", "blitz", "white", 1180, "C00", "win",  38, 14, 16, 8),
    ("2026-01-09", "blitz", "black", 1172, "B01", "loss", 30, 12, 12, 6),
    ("2026-01-13", "rapid", "white", 1185, "C00", "win",  46, 16, 20, 10),
    ("2026-01-18", "blitz", "black", 1178, "C00", "loss", 28, 10, 12, 6),
    ("2026-01-24", "rapid", "white", 1190, "D00", "win",  50, 18, 22, 10),
    ("2026-02-02", "blitz", "black", 1195, "B01", "win",  34, 12, 14, 8),
    ("2026-02-07", "blitz", "white", 1188, "C00", "loss", 26, 10, 10, 6),
    ("2026-02-14", "rapid", "black", 1202, "B01", "win",  44, 16, 18, 10),
    ("2026-02-19", "blitz", "white", 1210, "C00", "win",  32, 12, 14, 6),
    ("2026-02-25", "rapid", "black", 1205, "D00", "loss", 40, 14, 18, 8),
    ("2026-03-03", "blitz", "white", 1218, "A00", "win",  36, 14, 14, 8),
    ("2026-03-08", "blitz", "black", 1224, "C00", "win",  30, 10, 12, 8),
    ("2026-03-12", "rapid", "white", 1220, "C00", "loss", 48, 18, 20, 10),
    ("2026-03-19", "blitz", "black", 1230, "B01", "win",  33, 12, 13, 8),
    ("2026-03-27", "rapid", "white", 1238, "C00", "win",  45, 16, 19, 10),
    ("2026-04-01", "blitz", "black", 1233, "D00", "loss", 27, 10, 11, 6),
    ("2026-04-06", "blitz", "white", 1241, "B01", "win",  35, 13, 14, 8),
    ("2026-04-11", "rapid", "black", 1248, "C00", "win",  47, 17, 20, 10),
    ("2026-04-17", "blitz", "white", 1252, "C00", "loss", 29, 11, 12, 6),
    ("2026-04-23", "rapid", "black", 1258, "A00", "win",  42, 15, 18, 9),
    ("2026-04-29", "blitz", "white", 1264, "B01", "win",  31, 12, 12, 7),
]

# (game_index, move_number, phase, severity, category, cp_loss, clock_seconds)
# Real category names from analysis.classify(); "positional or planning
# error" deliberately the most frequent, matching the real report's own
# example. Spans every phase, severity and clock bucket at least once.
_MISTAKES = [
    (0, 9, "opening", "blunder", "left a piece undefended", 320, 45.0),
    (0, 22, "middlegame", "mistake", "positional or planning error", 140, 18.0),
    (1, 14, "opening", "mistake", "hung a pawn", 110, 90.0),
    (1, 24, "middlegame", "blunder", "moved a piece onto an attacked square", 410, 12.0),
    (2, 30, "middlegame", "mistake", "positional or planning error", 130, 55.0),
    (2, 40, "endgame", "blunder", "missed forced mate", 900, 8.0),
    (3, 10, "opening", "blunder", "left a piece undefended", 300, 70.0),
    (3, 20, "middlegame", "mistake", "missed a favourable capture", 120, 40.0),
    (4, 33, "middlegame", "blunder", "allowed a strong check or fork", 260, 22.0),
    (5, 12, "opening", "mistake", "positional or planning error", 105, 100.0),
    (5, 28, "middlegame", "mistake", "missed a tactic setting up material gain", 115, 50.0),
    (6, 8, "opening", "blunder", "hung a pawn", 270, 65.0),
    (7, 34, "middlegame", "mistake", "positional or planning error", 125, 33.0),
    (7, 38, "endgame", "mistake", "positional or planning error", 108, 20.0),
    (8, 9, "opening", "mistake", "missed a favourable capture", 118, 80.0),
    (9, 30, "middlegame", "blunder", "left a piece undefended", 330, 15.0),
    (9, 34, "endgame", "blunder", "allowed forced mate", 950, 6.0),
    (10, 11, "opening", "mistake", "positional or planning error", 112, 95.0),
    (11, 24, "middlegame", "mistake", "missed a tactic setting up material gain", 122, 44.0),
    (12, 36, "middlegame", "blunder", "moved a piece onto an attacked square", 300, 25.0),
    (13, 10, "opening", "inaccuracy", "positional or planning error", 60, 110.0),
    (13, 26, "middlegame", "mistake", "hung a pawn", 105, 38.0),
    (14, 32, "middlegame", "mistake", "positional or planning error", 118, 60.0),
    (15, 9, "opening", "blunder", "left a piece undefended", 290, 50.0),
    (16, 24, "middlegame", "inaccuracy", "positional or planning error", 55, 130.0),
    (17, 35, "endgame", "mistake", "positional or planning error", 110, 28.0),
    (18, 8, "opening", "mistake", "hung a pawn", 108, 85.0),
    (19, 30, "middlegame", "inaccuracy", "missed a favourable capture", 58, 150.0),
    (20, 22, "middlegame", "mistake", "positional or planning error", 102, 40.0),
]


def _seed_demo_db():
    conn = open_db(":memory:")
    for i, (date, tc, colour, rating, eco, result, moves, om, mm, em) in enumerate(_GAMES):
        url = f"https://www.chess.com/game/live/demo{i}"
        end_time = int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())
        game = {
            "url": url, "username": DEMO_USERNAME, "end_time": end_time, "date": date,
            "time_class": tc, "my_colour": colour, "my_rating": rating,
            "opp_rating": rating - 20, "result": result, "eco": eco,
            "moves_played": moves, "opening_moves": om, "middlegame_moves": mm,
            "endgame_moves": em,
        }
        mistakes = [{
            "game_url": url, "username": DEMO_USERNAME, "date": date, "end_time": end_time,
            "time_class": tc, "my_rating": rating, "my_colour": colour,
            "move_number": move_number, "phase": phase, "severity": severity,
            "cp_loss": cp_loss, "category": category, "played": "played-move",
            "best": "best-move", "clock_seconds": clock_seconds, "fen": "-",
        } for (gi, move_number, phase, severity, category, cp_loss, clock_seconds)
          in _MISTAKES if gi == i]
        save_game(conn, game, mistakes, depth=14)
    return conn


@lru_cache(maxsize=1)
def demo_dashboard_data() -> dict:
    conn = _seed_demo_db()
    try:
        data = build_dashboard_data(conn, [DEMO_USERNAME])
    finally:
        conn.close()
    data["meta"]["demo"] = True
    return data


def render_demo_dashboard_html() -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    return (template
            .replace("__GENERATED_AT__", generated)
            .replace("__DATA_JSON__", json.dumps(demo_dashboard_data())))

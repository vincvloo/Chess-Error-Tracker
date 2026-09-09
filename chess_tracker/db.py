"""SQLite schema, migration, and persistence."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    url           TEXT NOT NULL,
    username      TEXT NOT NULL,
    end_time      INTEGER,
    date          TEXT,
    time_class    TEXT,
    my_colour     TEXT,
    my_rating     INTEGER,
    opp_rating    INTEGER,
    result        TEXT,
    eco           TEXT,
    moves_played  INTEGER,
    -- How many of moves_played fell in each phase, so per-phase error rates
    -- can be normalised against the right denominator. opening_moves +
    -- middlegame_moves + endgame_moves always equals moves_played.
    opening_moves     INTEGER,
    middlegame_moves  INTEGER,
    endgame_moves     INTEGER,
    depth         INTEGER,
    analysed_at   TEXT,
    -- Two tracked players can appear in the same game. Each needs their own
    -- row, scored from their own side of the board.
    PRIMARY KEY (url, username)
);

CREATE TABLE IF NOT EXISTS mistakes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_url      TEXT NOT NULL,
    username      TEXT NOT NULL,
    date          TEXT,
    end_time      INTEGER,
    time_class    TEXT,
    my_rating     INTEGER,
    my_colour     TEXT,
    move_number   INTEGER,
    phase         TEXT,
    severity      TEXT,
    cp_loss       INTEGER,
    category      TEXT,
    played        TEXT,
    best          TEXT,
    clock_seconds REAL,
    fen           TEXT,
    FOREIGN KEY (game_url, username) REFERENCES games(url, username) ON DELETE CASCADE
);

-- Monthly PGN archives. Past months never change, so once stored we stop
-- asking Chess.com for them entirely.
CREATE TABLE IF NOT EXISTS archives (
    url           TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    month         TEXT,
    etag          TEXT,
    last_modified TEXT,
    body          TEXT,
    game_count    INTEGER,
    fetched_at    TEXT,
    complete      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    requests_made INTEGER,
    games_new     INTEGER,
    depth         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(username);
CREATE INDEX IF NOT EXISTS idx_mistakes_cat  ON mistakes(username, category);
CREATE INDEX IF NOT EXISTS idx_games_user    ON games(username, end_time);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """
    Databases written before multi-user support keyed `games` on the URL alone,
    which silently drops a game when two tracked players faced each other.
    Rebuild those tables with the composite key, preserving all rows.
    """
    cols = conn.execute("PRAGMA table_info(games)").fetchall()
    if not cols:
        return  # brand new database, schema is already current
    pk_cols = {c["name"] for c in cols if c["pk"]}
    if "username" in pk_cols:
        add_phase_move_columns(conn)
        return  # per-user keying already migrated

    print("Migrating database to per-user keying...", file=sys.stderr)
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE games RENAME TO games_old")
        conn.execute("ALTER TABLE mistakes RENAME TO mistakes_old")
        conn.executescript(SCHEMA)
        conn.execute("""
            INSERT OR IGNORE INTO games
            SELECT url, username, end_time, date, time_class, my_colour,
                   my_rating, opp_rating, result, eco, moves_played, depth,
                   analysed_at FROM games_old
        """)
        conn.execute("""
            INSERT INTO mistakes
            (game_url, username, date, end_time, time_class, my_rating, my_colour,
             move_number, phase, severity, cp_loss, category, played, best,
             clock_seconds, fen)
            SELECT game_url, username, date, end_time, time_class, my_rating,
                   my_colour, move_number, phase, severity, cp_loss, category,
                   played, best, clock_seconds, fen FROM mistakes_old
        """)
        conn.execute("DROP TABLE mistakes_old")
        conn.execute("DROP TABLE games_old")
        conn.execute("PRAGMA foreign_keys = ON")
    print("Migration complete. No data lost.", file=sys.stderr)

    add_phase_move_columns(conn)


def add_phase_move_columns(conn: sqlite3.Connection) -> None:
    """
    Databases written before per-phase rates existed lack these columns.
    Adding them is a plain ALTER (existing rows just get NULL); the actual
    numbers get filled in by backfill_phase_moves() from cached PGNs.
    """
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(games)").fetchall()}
    if not cols or "opening_moves" in cols:
        return
    with conn:
        for col in ("opening_moves", "middlegame_moves", "endgame_moves"):
            conn.execute(f"ALTER TABLE games ADD COLUMN {col} INTEGER")


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def already_analysed(conn: sqlite3.Connection, url: str, username: str,
                     depth: int) -> bool:
    """Scoped to the user. A game shared by two tracked players is analysed
    once per player, from each player's own perspective."""
    row = conn.execute(
        "SELECT depth FROM games WHERE url = ? AND username = ?",
        (url, username.lower())).fetchone()
    return row is not None and row["depth"] >= depth


def save_game(conn: sqlite3.Connection, rec: dict, mistakes: list[dict], depth: int) -> None:
    """Write one analysed game. Replaces any earlier, shallower analysis."""
    with conn:
        conn.execute("DELETE FROM mistakes WHERE game_url = ? AND username = ?",
                     (rec["url"], rec["username"]))
        conn.execute("""
            INSERT OR REPLACE INTO games
            (url, username, end_time, date, time_class, my_colour, my_rating,
             opp_rating, result, eco, moves_played, opening_moves,
             middlegame_moves, endgame_moves, depth, analysed_at)
            VALUES (:url, :username, :end_time, :date, :time_class, :my_colour,
                    :my_rating, :opp_rating, :result, :eco, :moves_played,
                    :opening_moves, :middlegame_moves, :endgame_moves,
                    :depth, :analysed_at)
        """, {**rec, "depth": depth,
              "analysed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if mistakes:
            conn.executemany("""
                INSERT INTO mistakes
                (game_url, username, date, end_time, time_class, my_rating, my_colour,
                 move_number, phase, severity, cp_loss, category, played, best,
                 clock_seconds, fen)
                VALUES (:game_url, :username, :date, :end_time, :time_class, :my_rating,
                        :my_colour, :move_number, :phase, :severity, :cp_loss,
                        :category, :played, :best, :clock_seconds, :fen)
            """, mistakes)

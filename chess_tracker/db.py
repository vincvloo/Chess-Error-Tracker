"""SQLite schema and persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .analysis import INACCURACY, GameRecord, MistakeRecord

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

-- Web-app-only key/value settings: which tracked player is "you" (so
-- Practice/Achievements/Analyse have a default), plus the fetch parameters
-- (email, depth, threads, pause, min_loss) the home page pre-fills instead
-- of asking for on every run. The CLI's own ~/.chess-tracker.json config is
-- untouched -- this table only exists for chess-tracker serve.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Practice-mode attempt log. One row per real (legal) move attempt from
-- /practice. owner/category are copied from the mistakes row at attempt
-- time (not joined) so history survives save_game()'s delete-then-reinsert
-- on reanalysis, which changes mistakes.id. No FK to mistakes(id): with
-- PRAGMA foreign_keys=ON, a FK would block that delete outright. There is
-- no stored "solved" flag -- a hint-free correct attempt on a given
-- (practicing_user, mistake_id) counts as solved even if an earlier
-- hint-assisted attempt on the same position did not (see practice_stats()
-- in reports.py).
CREATE TABLE IF NOT EXISTS practice_attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    practicing_user  TEXT NOT NULL,
    mistake_id       INTEGER NOT NULL,
    owner            TEXT NOT NULL,
    category         TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    hint_used        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

-- Lichess puzzle database import. Unlike every other data table, this one
-- has no `username` column -- puzzles aren't tied to a tracked chess.com
-- player, they're a shared, global resource (the `settings` key/value table
-- is the only other un-scoped table, but that's scalar config, not a large
-- relational dataset). Imported/topped-up by scripts/import_puzzles.py.
CREATE TABLE IF NOT EXISTS puzzles (
    puzzle_id         TEXT PRIMARY KEY,
    fen               TEXT NOT NULL,
    -- Full original space-separated UCI move list. moves[0] is the
    -- opponent's forced setup move (auto-applied to reach the real puzzle
    -- position); the solver's own moves start at moves[1] and alternate
    -- with more auto-played opponent replies from there. See
    -- chess_tracker/puzzles.py for the walk logic.
    moves             TEXT NOT NULL,
    rating            INTEGER,
    rating_deviation  INTEGER,
    popularity        INTEGER,
    nb_plays          INTEGER,
    -- Space-padded (leading and trailing space) so a plain
    -- `LIKE '% fork %'` filter can't false-positive on a theme name that's
    -- a substring of another -- same simplicity tradeoff as `category`
    -- being a plain TEXT column on `mistakes` rather than a join table.
    themes            TEXT,
    game_url          TEXT,
    opening_tags      TEXT,
    imported_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_puzzles_rating ON puzzles(rating);

-- Puzzle-mode attempt log, same shape as practice_attempts (no FK to
-- puzzles(puzzle_id) for the same reason practice_attempts has none: a
-- future re-import shouldn't be blocked by attempt history referencing it).
CREATE TABLE IF NOT EXISTS puzzle_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    practicing_user     TEXT NOT NULL,
    puzzle_id           TEXT NOT NULL,
    verdict             TEXT NOT NULL,   -- "solved" | "failed"
    move_index_reached  INTEGER,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_puzzle_attempts_user ON puzzle_attempts(practicing_user, puzzle_id);

-- How many puzzles exist in the *source* CSV for a given rating bucket or
-- theme, regardless of whether they were actually imported -- refreshed by
-- every import/top-up scan (see puzzles.import_puzzles()). Answers "how
-- many more are out there" without needing a live query against Lichess.
CREATE TABLE IF NOT EXISTS puzzle_source_stats (
    bucket_key   TEXT PRIMARY KEY,   -- e.g. "total", "rating:1400-1500", "theme:fork"
    total_count  INTEGER,
    updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(username);
CREATE INDEX IF NOT EXISTS idx_mistakes_cat  ON mistakes(username, category);
CREATE INDEX IF NOT EXISTS idx_games_user    ON games(username, end_time);
CREATE INDEX IF NOT EXISTS idx_practice_attempts_user ON practice_attempts(practicing_user, mistake_id);
"""

SETTINGS_DEFAULTS = {
    "primary_user": None, "email": "", "depth": 14, "threads": 2,
    "pause": 0.6, "min_loss": INACCURACY,
    # Phase 5 (play mode): which engine/difficulty the /play form pre-fills,
    # and whether adaptive steering starts checked. Not enforced server-side
    # beyond the default -- the /play form can always override per game.
    "bot_engine": "stockfish", "bot_elo": 1500, "bot_adaptive": False,
}


def get_settings(conn: sqlite3.Connection) -> dict:
    """All web-app settings, stored values merged over SETTINGS_DEFAULTS.
    Values come back cast to the same type as their default (settings are
    stored as TEXT -- sqlite has no other choice for a generic key/value
    table). bool is special-cased: bool("False") is True in plain Python,
    which would make a stored "False" read back as enabled."""
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    merged = dict(SETTINGS_DEFAULTS)
    for key, value in stored.items():
        default = SETTINGS_DEFAULTS.get(key)
        if default is None:
            merged[key] = value
        elif isinstance(default, bool):
            merged[key] = value in ("1", "true", "True")
        else:
            merged[key] = type(default)(value)
    return merged


def set_settings(conn: sqlite3.Connection, **kwargs) -> None:
    """Upsert one or more settings, e.g. set_settings(conn, depth=16)."""
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in kwargs.items()])


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader (e.g. the web app's dashboard route) see a consistent
    # view without blocking on a concurrent writer (a running analysis job).
    # It does NOT make two concurrent writers safe -- busy_timeout covers
    # that: a writer that does collide with another (e.g. the CLI run
    # against the same file while the app has a job going) retries for up
    # to 5s instead of immediately raising "database is locked". Both are
    # no-ops for an in-memory database (as used throughout the test suite).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def already_analysed(conn: sqlite3.Connection, url: str, username: str,
                     depth: int) -> bool:
    """Scoped to the user. A game shared by two tracked players is analysed
    once per player, from each player's own perspective."""
    row = conn.execute(
        "SELECT depth FROM games WHERE url = ? AND username = ?",
        (url, username.lower())).fetchone()
    return row is not None and row["depth"] >= depth


def save_game(conn: sqlite3.Connection, rec: GameRecord, mistakes: list[MistakeRecord],
              depth: int) -> None:
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

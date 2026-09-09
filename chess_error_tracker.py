#!/usr/bin/env python3
"""
Longitudinal chess error tracker with a persistent store.

Pulls your Chess.com game history, runs Stockfish over every position where it
was your move, classifies each significant mistake, and keeps everything in a
local SQLite database so the picture gets sharper every time you run it.

Second and later runs only fetch the current month plus any months never seen.
Games already analysed at the same depth or deeper are skipped.

Usage:
    # first run, builds the database
    python chess_error_tracker.py --user vincent --email you@example.com

    # later runs, only new games get analysed
    python chess_error_tracker.py --user vincent --email you@example.com

    # report on everything already stored, no network, no engine
    python chess_error_tracker.py --user vincent --report-only

    # deeper re-analysis of games previously done shallow
    python chess_error_tracker.py --user vincent --email you@example.com --depth 20

Requirements:
    pip install chess requests
    A Stockfish binary. Found automatically on PATH or in the usual install
    locations for Windows, macOS and Linux; override with --engine or the
    CHESS_ENGINE environment variable.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn
import requests

API = "https://api.chess.com/pub"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "chess_tracker.db")


def find_engine() -> str | None:
    """
    Locate a Stockfish binary without the user having to say where it is.

    Order: an explicit CHESS_ENGINE environment variable, then PATH, then the
    handful of places each platform actually installs it. Returns None if
    nothing turns up, so the caller can print a useful message.
    """
    env = os.environ.get("CHESS_ENGINE")
    if env and os.path.isfile(env):
        return env

    for name in ("stockfish", "stockfish.exe"):
        found = shutil.which(name)
        if found:
            return found

    candidates: list[str] = []

    if sys.platform == "win32":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.path.expanduser("~"),
            "C:\\Tools",
        ]
        # winget shims, plus the usual manual-unzip locations
        for root in filter(None, roots):
            candidates += [
                os.path.join(root, "Microsoft", "WinGet", "Links", "stockfish.exe"),
                os.path.join(root, "stockfish", "stockfish.exe"),
                os.path.join(root, "Stockfish", "stockfish.exe"),
                os.path.join(root, "Downloads", "stockfish", "stockfish.exe"),
            ]
        # Official builds ship as stockfish-windows-x86-64-<arch>.exe, so glob
        # for whatever variant was downloaded rather than guessing the suffix.
        for root in filter(None, roots):
            for pattern in ("stockfish*/stockfish*.exe", "stockfish*.exe",
                            "Downloads/stockfish*/*/stockfish*.exe",
                            "Downloads/stockfish*/stockfish*.exe"):
                candidates += sorted(glob.glob(os.path.join(root, pattern)))

        # winget install Stockfish drops a portable package here instead of a
        # PATH shim, under a publisher-hash suffix that changes between
        # machines, so it has to be globbed rather than named outright.
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidates += sorted(glob.glob(os.path.join(
                localappdata, "Microsoft", "WinGet", "Packages",
                "Stockfish.Stockfish_*", "stockfish", "stockfish*.exe")))

    elif sys.platform == "darwin":
        candidates += [
            "/opt/homebrew/bin/stockfish",   # Apple silicon
            "/usr/local/bin/stockfish",      # Intel
            "/opt/local/bin/stockfish",      # MacPorts
        ]

    else:
        candidates += [
            "/usr/games/stockfish",          # Debian and Ubuntu package
            "/usr/bin/stockfish",
            "/usr/local/bin/stockfish",
            "/snap/bin/stockfish",
            os.path.expanduser("~/.local/bin/stockfish"),
        ]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK if sys.platform != "win32" else os.F_OK):
            return path
    return None


ENGINE_HELP = """
Stockfish was not found. Either pass --engine with the full path, set the
CHESS_ENGINE environment variable, or install it:

  Windows   winget install Stockfish
            or download from stockfishchess.org and unzip to C:\\Tools\\stockfish
  macOS     brew install stockfish
  Debian    sudo apt install stockfish
""".strip()

INACCURACY = 50
MISTAKE = 100
BLUNDER = 250

PHASES = ("opening", "middlegame", "endgame")

PIECE_VALUE = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}


# ==========================================================================
# Storage
# ==========================================================================

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


# ==========================================================================
# Fetching, deliberately gentle
# ==========================================================================

class ChessComClient:
    """
    Serial, cached, conditional. Chess.com states that serial access is
    unlimited and that parallel requests are what trigger 429s, so this makes
    exactly one request at a time and sleeps between them.
    """

    def __init__(self, email: str, conn: sqlite3.Connection, pause: float = 0.6):
        self.headers = {"User-Agent": f"chess-error-tracker/2.0 ({email})"}
        self.conn = conn
        self.pause = pause
        self.requests_made = 0

    def _get(self, url: str, extra: dict | None = None) -> requests.Response:
        headers = {**self.headers, **(extra or {})}
        last_exc: requests.exceptions.RequestException | None = None
        for attempt in range(5):
            time.sleep(self.pause)
            try:
                r = requests.get(url, headers=headers, timeout=60)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                wait = 2 ** attempt
                print(f"\n  {exc.__class__.__name__}, retrying in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            self.requests_made += 1
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                print(f"\n  429 received, backing off {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            return r
        if last_exc is not None:
            raise RuntimeError(f"Gave up on {url} after repeated connection errors") from last_exc
        raise RuntimeError(f"Gave up on {url} after repeated 429s")

    def archives(self, user: str) -> list[str]:
        r = self._get(f"{API}/player/{user.lower()}/games/archives")
        if r.status_code == 404:
            sys.exit(f"No such Chess.com user: {user}")
        if r.status_code == 403:
            sys.exit("403 from Chess.com. The User-Agent was rejected. Pass a real --email.")
        r.raise_for_status()
        return r.json().get("archives", [])

    def month(self, url: str, user: str, current_month: str) -> list[dict]:
        """
        Games for one monthly archive.

        Past months come straight from the database with no network call.
        The current month is revalidated with an ETag, so an unchanged month
        costs a 304 and no payload.
        """
        month = url[-7:].replace("/", "-")
        row = self.conn.execute("SELECT * FROM archives WHERE url = ?", (url,)).fetchone()

        if row and row["complete"] and row["body"]:
            return json.loads(row["body"])

        extra: dict[str, str] = {}
        if row and row["etag"]:
            extra["If-None-Match"] = row["etag"]
        elif row and row["last_modified"]:
            extra["If-Modified-Since"] = row["last_modified"]

        r = self._get(url, extra)

        if r.status_code == 304 and row and row["body"]:
            if month < current_month:
                with self.conn:
                    self.conn.execute("UPDATE archives SET complete = 1 WHERE url = ?", (url,))
            return json.loads(row["body"])

        r.raise_for_status()
        games = r.json().get("games", [])
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO archives
                (url, username, month, etag, last_modified, body, game_count,
                 fetched_at, complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (url, user.lower(), month, r.headers.get("ETag"),
                  r.headers.get("Last-Modified"), json.dumps(games), len(games),
                  datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  1 if month < current_month else 0))
        return games


def collect_games(client: ChessComClient, user: str, since: str | None,
                  time_class: str | None, limit: int | None) -> list[dict]:
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    archives = client.archives(user)
    if since:
        archives = [a for a in archives if a[-7:].replace("/", "-") >= since]

    games: list[dict] = []
    cached = fetched = 0
    for url in archives:
        before = client.requests_made
        month_games = client.month(url, user, current_month)
        if client.requests_made == before:
            cached += 1
        else:
            fetched += 1
        games.extend(month_games)

    print(f"  {len(archives)} months: {cached} from local store, {fetched} fetched "
          f"({client.requests_made} HTTP requests this run)", file=sys.stderr)

    if time_class:
        games = [g for g in games if g.get("time_class") == time_class]
    games = [g for g in games if g.get("rules") == "chess"]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    return games[:limit] if limit else games


# ==========================================================================
# Analysis
# ==========================================================================

def score_cp(info, colour: chess.Color) -> int:
    return info["score"].pov(colour).score(mate_score=10000)


def game_phase(board: chess.Board, move_number: int) -> str:
    if move_number <= 10:
        return "opening"
    heavy = sum(1 for p in board.piece_map().values()
                if p.piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT))
    return "endgame" if heavy <= 4 else "middlegame"


def classify(board_before: chess.Board, played: chess.Move, best: chess.Move,
             me: chess.Color, reply: chess.Move | None,
             cp_before: int, cp_after: int) -> str:
    """First matching rule wins, most specific first."""
    board_after = board_before.copy()
    board_after.push(played)

    if cp_after <= -9000:
        return "allowed forced mate"
    if cp_before >= 9000:
        return "missed forced mate"

    if reply is not None:
        if board_after.is_capture(reply):
            victim = board_after.piece_at(reply.to_square)
            gain = PIECE_VALUE[victim.piece_type] if victim else 1
            defended = board_after.is_attacked_by(me, reply.to_square)
            attacker = board_after.piece_at(reply.from_square)
            attacker_val = PIECE_VALUE[attacker.piece_type] if attacker else 0

            if gain >= 3 and not defended:
                if reply.to_square == played.to_square:
                    return "moved a piece onto an attacked square"
                return "left a piece undefended"
            if not defended and gain >= 1:
                return "hung a pawn"
            if defended and attacker_val < gain:
                return "underdefended piece, lost the exchange"

        if board_after.gives_check(reply):
            return "allowed a strong check or fork"

    if board_before.is_capture(best):
        target = board_before.piece_at(best.to_square)
        if target and PIECE_VALUE[target.piece_type] >= 3:
            return "missed a capture winning material"
        return "missed a favourable capture"

    if board_before.gives_check(best):
        return "missed a forcing check"

    board_best = board_before.copy()
    board_best.push(best)
    if any(board_best.is_capture(m) for m in board_best.legal_moves
           if board_best.piece_at(m.to_square)
           and PIECE_VALUE[board_best.piece_at(m.to_square).piece_type] >= 3):
        return "missed a tactic setting up material gain"

    return "positional or planning error"


def resolve_colour(game_json: dict, user: str) -> tuple[chess.Color, dict, dict] | None:
    """Which side `user` played, and each side's Chess.com player info."""
    white, black = game_json.get("white", {}), game_json.get("black", {})
    if white.get("username", "").lower() == user.lower():
        return chess.WHITE, white, black
    if black.get("username", "").lower() == user.lower():
        return chess.BLACK, black, white
    return None


def analyse_game(game_json: dict, user: str, engine: chess.engine.SimpleEngine,
                 depth: int, min_loss: int) -> tuple[dict, list[dict]] | None:
    pgn_text = game_json.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None

    resolved = resolve_colour(game_json, user)
    if resolved is None:
        return None
    me, mine, theirs = resolved

    end_time = game_json.get("end_time", 0)
    rec = {
        "url": game_json.get("url", ""),
        "username": user.lower(),
        "end_time": end_time,
        "date": datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d"),
        "time_class": game_json.get("time_class", "?"),
        "my_colour": "white" if me == chess.WHITE else "black",
        "my_rating": mine.get("rating", 0),
        "opp_rating": theirs.get("rating", 0),
        "result": mine.get("result", "?"),
        "eco": game.headers.get("ECO", "?"),
        "moves_played": 0,
        "opening_moves": 0,
        "middlegame_moves": 0,
        "endgame_moves": 0,
    }

    mistakes: list[dict] = []
    board = game.board()
    limit = chess.engine.Limit(depth=depth)

    for node in game.mainline():
        played = node.move
        if board.turn != me:
            board.push(played)
            continue

        rec["moves_played"] += 1
        move_no = board.fullmove_number
        rec[f"{game_phase(board, move_no)}_moves"] += 1

        info_before = engine.analyse(board, limit)
        cp_before = score_cp(info_before, me)
        best = info_before.get("pv", [None])[0]
        if best is None:
            board.push(played)
            continue

        board_before = board.copy()
        board.push(played)

        info_after = engine.analyse(board, limit)
        cp_after = score_cp(info_after, me)
        reply = info_after.get("pv", [None])[0]
        cp_loss = cp_before - cp_after

        if cp_loss >= min_loss and played != best:
            mistakes.append({
                "game_url": rec["url"], "username": user.lower(), "date": rec["date"],
                "end_time": end_time, "time_class": rec["time_class"],
                "my_rating": rec["my_rating"], "my_colour": rec["my_colour"],
                "move_number": move_no,
                "phase": game_phase(board_before, move_no),
                "severity": ("blunder" if cp_loss >= BLUNDER
                             else "mistake" if cp_loss >= MISTAKE else "inaccuracy"),
                "cp_loss": min(cp_loss, 2000),
                "category": classify(board_before, played, best, me, reply,
                                     cp_before, cp_after),
                "played": board_before.san(played),
                "best": board_before.san(best),
                "clock_seconds": node.clock(),
                "fen": board_before.fen(),
            })

    if rec["moves_played"] == 0:
        return None  # unparseable or empty movetext, do not pollute the store

    return rec, mistakes


def count_phase_moves(game_json: dict, user: str) -> dict[str, int] | None:
    """
    Same per-phase tally analyse_game() does, without the engine calls.
    Used to backfill games that were analysed before these columns existed.
    """
    pgn_text = game_json.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    resolved = resolve_colour(game_json, user)
    if resolved is None:
        return None
    me, _, _ = resolved

    counts = {"opening_moves": 0, "middlegame_moves": 0, "endgame_moves": 0}
    board = game.board()
    for node in game.mainline():
        played = node.move
        if board.turn != me:
            board.push(played)
            continue
        counts[f"{game_phase(board, board.fullmove_number)}_moves"] += 1
        board.push(played)
    return counts


def backfill_phase_moves(conn: sqlite3.Connection, user: str) -> None:
    """
    Fill in opening_moves/middlegame_moves/endgame_moves for games analysed
    before per-phase rates existed. Reads PGNs already cached in `archives`,
    so this needs no network access and no Stockfish.
    """
    user = user.lower()
    missing = conn.execute("""
        SELECT url FROM games
        WHERE username = ? AND (opening_moves IS NULL OR middlegame_moves IS NULL
                                 OR endgame_moves IS NULL)
    """, (user,)).fetchall()
    if not missing:
        return
    missing_urls = {r["url"] for r in missing}

    by_url: dict[str, dict] = {}
    for row in conn.execute("SELECT body FROM archives WHERE username = ?", (user,)):
        for g in json.loads(row["body"]):
            if g.get("url") in missing_urls:
                by_url[g["url"]] = g

    updated = skipped = 0
    with conn:
        for url in missing_urls:
            g_json = by_url.get(url)
            counts = count_phase_moves(g_json, user) if g_json else None
            if counts is None:
                skipped += 1
                continue
            conn.execute("""
                UPDATE games SET opening_moves = :opening_moves,
                       middlegame_moves = :middlegame_moves,
                       endgame_moves = :endgame_moves
                WHERE url = :url AND username = :user
            """, {**counts, "url": url, "user": user})
            updated += 1

    if updated:
        print(f"[{user}] backfilled phase-move counts for {updated} games"
              + (f", {skipped} skipped (PGN no longer cached)" if skipped else ""),
              file=sys.stderr)


# ==========================================================================
# Reporting, straight from the database
# ==========================================================================

def list_users(conn: sqlite3.Connection) -> str:
    """Who is in this database and how solid is each sample."""
    rows = conn.execute("""
        SELECT g.username,
               COUNT(*)                AS games,
               SUM(g.moves_played)     AS moves,
               MIN(g.date)             AS first_game,
               MAX(g.date)             AS last_game,
               MIN(g.depth)            AS min_depth
        FROM games g GROUP BY g.username ORDER BY games DESC
    """).fetchall()
    if not rows:
        return "No users stored yet."

    out = ["=" * 72,
           "USERS IN THIS DATABASE",
           "=" * 72,
           f"{'user':<18}{'games':>7}{'moves':>8}{'err/100':>9}{'depth':>7}  range",
           "-" * 72]
    for r in rows:
        errs = conn.execute("""SELECT COUNT(*) c FROM mistakes
            WHERE username = ? AND severity IN ('mistake','blunder')""",
            (r["username"],)).fetchone()["c"]
        rate = errs / r["moves"] * 100 if r["moves"] else 0
        note = "" if r["games"] >= 100 else "  (thin sample)"
        out.append(f"{r['username']:<18}{r['games']:>7}{r['moves']:>8}"
                   f"{rate:>9.2f}{r['min_depth']:>7}  "
                   f"{r['first_game']} to {r['last_game']}{note}")
    out.append("=" * 72)
    return "\n".join(out)


def compare(conn: sqlite3.Connection, users: list[str],
            time_class: str | None = None, phase: str | None = None) -> str:
    """
    Side by side error rates across every mistake category, at every
    severity (inaccuracy, mistake, blunder). The point is not who is better
    overall, it is which categories differ. Rates are per 100 of that
    player's own moves (or, with --phase, per 100 of that player's own moves
    in that phase), so unequal sample sizes stay comparable.
    """
    users = [u.lower() for u in users]
    stats: dict[str, dict] = {}
    moves_col = f"{phase}_moves" if phase else "moves_played"

    for u in users:
        params: list = [u]
        games_clause = "WHERE username = ?"
        if time_class:
            games_clause += " AND time_class = ?"
            params.append(time_class)
        mistakes_clause = games_clause
        mistakes_params = list(params)
        if phase:
            mistakes_clause += " AND phase = ?"
            mistakes_params.append(phase)

        moves = conn.execute(
            f"SELECT COALESCE(SUM({moves_col}),0) m, COUNT(*) g FROM games {games_clause}",
            params).fetchone()
        cats = conn.execute(
            f"""SELECT category, COUNT(*) c FROM mistakes {mistakes_clause}
                GROUP BY category""",
            mistakes_params).fetchall()
        stats[u] = {"moves": moves["m"], "games": moves["g"],
                    "cats": {r["category"]: r["c"] for r in cats}}

    present = [u for u in users if stats[u]["moves"]]
    if len(present) < 2:
        return ("Need at least two users with stored games to compare. "
                "Run the scan for each of them first" +
                (" (per-phase rates need the games backfilled/reanalysed first)"
                 if phase else "") + ".")

    w = max(max(len(u) for u in present), 9)
    out = ["=" * (34 + (w + 2) * len(present)),
           "COMPARISON  (errors per 100 of that player's own"
           + (f" {phase}" if phase else "") + " moves, "
           "inaccuracy + mistake + blunder)"
           + (f"  [{time_class}]" if time_class else ""),
           "=" * (34 + (w + 2) * len(present)),
           f"{'':<32}" + "".join(f"{u:>{w + 2}}" for u in present),
           "-" * (34 + (w + 2) * len(present)),
           f"{'games analysed':<32}"
           + "".join(f"{stats[u]['games']:>{w + 2}}" for u in present),
           f"{'moves analysed':<32}"
           + "".join(f"{stats[u]['moves']:>{w + 2}}" for u in present),
           ""]

    every_cat = sorted({c for u in present for c in stats[u]["cats"]})

    def rate(u: str, c: str) -> float:
        return stats[u]["cats"].get(c, 0) / stats[u]["moves"] * 100

    totals = {u: sum(stats[u]["cats"].values()) / stats[u]["moves"] * 100
              for u in present}
    out.append(f"{'ALL ERRORS':<32}"
               + "".join(f"{totals[u]:>{w + 2}.2f}" for u in present))
    out.append("-" * (34 + (w + 2) * len(present)))

    # Biggest gaps first. Those are the categories worth acting on.
    for c in sorted(every_cat, key=lambda c: -(max(rate(u, c) for u in present)
                                               - min(rate(u, c) for u in present))):
        label = c if len(c) <= 30 else c[:27] + "..."
        out.append(f"  {label:<30}" + "".join(f"{rate(u, c):>{w + 2}.2f}" for u in present))

    out.append("")
    ref = present[0]
    gaps = [(rate(ref, c) - min(rate(u, c) for u in present[1:]), c)
            for c in every_cat]
    worst = [g for g in sorted(gaps, reverse=True) if g[0] > 0][:3]
    if worst:
        out.append(f"Where {ref} loses the most ground against the others:")
        for gap, c in worst:
            out.append(f"  +{gap:5.2f} per 100 moves   {c}")
        out.append("")
        out.append("Those gaps, not the overall total, are the training targets.")
    out.append("=" * (34 + (w + 2) * len(present)))
    return "\n".join(out)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chess Mistake Explorer</title>
<style>
:root {
  --bg: #f7f7f8; --panel: #ffffff; --border: #e2e2e6; --text: #1c1c1f;
  --muted: #6b6b74; --accent: #3f6fd6; --accent-soft: #e8edfb;
  --max-cell: #fde2e2; --track: #ececf0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17181c; --panel: #1f2024; --border: #313238; --text: #ececef;
    --muted: #9a9aa2; --accent: #7da2ff; --accent-soft: #263252;
    --max-cell: #4a2a2c; --track: #2a2b30;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
header { padding: 20px 24px 8px; }
h1 { font-size: 20px; margin: 0 0 2px; }
.meta { color: var(--muted); font-size: 12.5px; }
.app { display: grid; grid-template-columns: 260px 1fr; gap: 20px; padding: 16px 24px 40px; align-items: start; }
@media (max-width: 800px) { .app { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.filters { display: flex; flex-direction: column; gap: 12px; position: sticky; top: 16px; }
@media (max-width: 800px) { .filters { position: static; } }
.group summary { cursor: pointer; font-weight: 600; font-size: 13px; padding: 2px 0; }
.group { margin-bottom: 4px; }
.groupbar { display: flex; justify-content: space-between; align-items: center; }
.grouplinks { display: flex; gap: 8px; }
.grouplinks button { background: none; border: none; color: var(--accent); cursor: pointer;
  font-size: 11.5px; padding: 0; }
.chklist { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; max-height: 240px; overflow-y: auto; }
label.chk { display: flex; align-items: center; gap: 7px; font-size: 12.5px; cursor: pointer; }
label.chk input { accent-color: var(--accent); }
.content { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
#summary { color: var(--muted); font-size: 12.5px; margin-bottom: 8px; }
.barrow { display: grid; grid-template-columns: 130px 1fr 52px; gap: 10px; align-items: center; margin: 6px 0; }
.barlabel { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bartrack { background: var(--track); border-radius: 5px; height: 10px; overflow: hidden; }
.barfill { background: var(--accent); height: 100%; border-radius: 5px; transition: width .15s; }
.barvalue { font-size: 12.5px; text-align: right; font-variant-numeric: tabular-nums; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { padding: 6px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background: var(--panel); }
thead th { color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border); }
tr.total-row td { font-weight: 700; border-bottom: 2px solid var(--border); }
td.maxcell { background: var(--max-cell); border-radius: 4px; }
.refrow { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin-bottom: 6px; }
select { font: inherit; padding: 3px 6px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--panel); color: var(--text); }
#gaps ul { margin: 6px 0 0; padding-left: 18px; }
#gaps li { margin: 3px 0; }
h2 { font-size: 14px; margin: 0 0 4px; }
.trendhead { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.seg button { background: var(--panel); color: var(--text); border: none; padding: 4px 12px;
  font: inherit; font-size: 12.5px; cursor: pointer; }
.seg button.active { background: var(--accent); color: #fff; }
.seg button + button { border-left: 1px solid var(--border); }
.trendwrap { overflow-x: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 6px 0 10px; font-size: 12px; color: var(--muted); }
.legend .item { display: flex; align-items: center; gap: 5px; }
.legend .swatch { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.warn-note { font-size: 12px; color: var(--muted); margin-top: 6px; }
.warn-note .ring { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  border: 1.5px solid var(--muted); vertical-align: middle; margin: 0 3px; }
svg.trend text { fill: var(--muted); font-size: 10.5px; }
svg.trend .gridline { stroke: var(--border); stroke-width: 1; }
svg.trend .empty { fill: var(--muted); font-size: 12.5px; }
</style>
</head>
<body>
<header>
  <h1>Chess Mistake Explorer</h1>
  <div class="meta">Generated __GENERATED_AT__ &middot; filter by player, game phase, time class and mistake type</div>
</header>
<div class="app">
  <aside class="filters">
    <div class="panel group" id="grp-users">
      <div class="groupbar"><summary style="cursor:default">Players</summary>
        <div class="grouplinks"><button data-act="all">all</button><button data-act="none">none</button></div></div>
      <div class="chklist" id="chk-users"></div>
    </div>
    <div class="panel group" id="grp-phases">
      <div class="groupbar"><summary style="cursor:default">Phase</summary>
        <div class="grouplinks"><button data-act="all">all</button><button data-act="none">none</button></div></div>
      <div class="chklist" id="chk-phases"></div>
    </div>
    <div class="panel group" id="grp-tc">
      <div class="groupbar"><summary style="cursor:default">Time class</summary>
        <div class="grouplinks"><button data-act="all">all</button><button data-act="none">none</button></div></div>
      <div class="chklist" id="chk-tc"></div>
    </div>
    <div class="panel group" id="grp-cats">
      <div class="groupbar"><summary style="cursor:default">Mistake type</summary>
        <div class="grouplinks"><button data-act="all">all</button><button data-act="none">none</button></div></div>
      <div class="chklist" id="chk-cats"></div>
    </div>
  </aside>
  <main class="content">
    <div class="panel">
      <h2>Error rate (per 100 moves, selected filters)</h2>
      <div id="summary"></div>
      <div id="bars"></div>
    </div>
    <div class="panel">
      <div class="trendhead">
        <h2 style="margin:0">Evolution over time</h2>
        <div class="seg" id="granularity">
          <button data-g="month" class="active">Month</button>
          <button data-g="quarter">3 Months</button>
          <button data-g="half">6 Months</button>
          <button data-g="year">Year</button>
        </div>
      </div>
      <div class="legend" id="trendLegend"></div>
      <div class="trendwrap"><svg id="trendChart" class="trend"></svg></div>
      <div class="warn-note" id="trendWarnNote"></div>
    </div>
    <div class="panel">
      <h2>By mistake type</h2>
      <div class="tablewrap"><table id="table"></table></div>
    </div>
    <div class="panel">
      <h2>Biggest gaps</h2>
      <div class="refrow">reference player
        <select id="refSelect"></select>
      </div>
      <div id="gaps"></div>
    </div>
  </main>
</div>
<script>
const DATA = __DATA_JSON__;

const state = {
  users: new Set(DATA.users),
  phases: new Set(DATA.phases),
  timeClasses: new Set(DATA.timeClasses),
  categories: new Set(DATA.categories),
  ref: DATA.users[0],
  granularity: "month",
};

const PALETTE = ["#3f6fd6", "#e08a3f", "#3fa66f", "#c1507a", "#8a63d6", "#c9a227", "#3fb6c9", "#a25a3f"];
function colorFor(user) {
  const idx = DATA.users.indexOf(user);
  return PALETTE[idx % PALETTE.length];
}

function buildGroup(containerId, items, selectedSet) {
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  items.forEach(item => {
    const label = document.createElement("label");
    label.className = "chk";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selectedSet.has(item);
    input.addEventListener("change", () => {
      if (input.checked) selectedSet.add(item); else selectedSet.delete(item);
      render();
    });
    const span = document.createElement("span");
    span.textContent = item;
    label.appendChild(input);
    label.appendChild(span);
    el.appendChild(label);
  });
}

function wireGroupLinks(groupId, containerId, items, selectedSet) {
  const grp = document.getElementById(groupId);
  grp.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", () => {
      selectedSet.clear();
      if (btn.dataset.act === "all") items.forEach(i => selectedSet.add(i));
      buildGroup(containerId, items, selectedSet);
      render();
    });
  });
}

function movesFor(user) {
  let total = 0;
  const tcData = DATA.moves[user] || {};
  for (const tc of state.timeClasses) {
    const ph = tcData[tc];
    if (!ph) continue;
    for (const p of state.phases) total += ph[p] || 0;
  }
  return total;
}

function gamesFor(user) {
  let total = 0;
  const tcData = DATA.moves[user] || {};
  for (const tc of state.timeClasses) {
    const ph = tcData[tc];
    if (ph) total += ph.games || 0;
  }
  return total;
}

function countFor(user, category) {
  let total = 0;
  const tcData = DATA.counts[user] || {};
  for (const tc of state.timeClasses) {
    const phData = tcData[tc];
    if (!phData) continue;
    for (const p of state.phases) {
      const catData = phData[p];
      if (catData) total += catData[category] || 0;
    }
  }
  return total;
}

function rateOf(user, category) {
  const m = movesFor(user);
  if (!m) return null;
  return countFor(user, category) / m * 100;
}

function totalRate(user) {
  const m = movesFor(user);
  if (!m) return null;
  let n = 0;
  for (const c of state.categories) n += countFor(user, c);
  return n / m * 100;
}

function movesForMonths(user, monthList) {
  let total = 0;
  const tcData = DATA.movesByMonth[user] || {};
  for (const tc of state.timeClasses) {
    const monthMap = tcData[tc];
    if (!monthMap) continue;
    for (const mo of monthList) {
      const md = monthMap[mo];
      if (!md) continue;
      for (const p of state.phases) total += md[p] || 0;
    }
  }
  return total;
}

function gamesForMonths(user, monthList) {
  let total = 0;
  const tcData = DATA.movesByMonth[user] || {};
  for (const tc of state.timeClasses) {
    const monthMap = tcData[tc];
    if (!monthMap) continue;
    for (const mo of monthList) {
      const md = monthMap[mo];
      if (md) total += md.games || 0;
    }
  }
  return total;
}

function countForMonths(user, monthList) {
  let total = 0;
  const tcData = DATA.countsByMonth[user] || {};
  for (const tc of state.timeClasses) {
    const phData = tcData[tc];
    if (!phData) continue;
    for (const p of state.phases) {
      const monthMap = phData[p];
      if (!monthMap) continue;
      for (const mo of monthList) {
        const catData = monthMap[mo];
        if (!catData) continue;
        for (const c of state.categories) total += catData[c] || 0;
      }
    }
  }
  return total;
}

function quarterKey(m) {
  const [y, mo] = m.split("-").map(Number);
  return y + "-Q" + Math.ceil(mo / 3);
}

function halfKey(m) {
  const [y, mo] = m.split("-").map(Number);
  return y + "-H" + (mo <= 6 ? 1 : 2);
}

function trendBuckets() {
  const all = DATA.months;
  const keyed = { year: m => m.slice(0, 4), quarter: quarterKey, half: halfKey }[state.granularity];
  if (!keyed) return all.map(m => ({ key: m, months: [m] }));
  // Lexicographic sort matches chronological order for all three key shapes
  // ('2026', '2026-Q3', '2026-H1'), since the year prefix sorts first.
  const keys = [...new Set(all.map(keyed))].sort();
  return keys.map(k => ({ key: k, months: all.filter(m => keyed(m) === k) }));
}

function trendSeries(user, bkts) {
  return bkts.map(b => {
    const moves = movesForMonths(user, b.months);
    const games = gamesForMonths(user, b.months);
    const n = countForMonths(user, b.months);
    return {
      key: b.key,
      rate: moves ? n / moves * 100 : null,
      moves, games,
      thin: games > 0 && games < DATA.thinGamesThreshold,
    };
  });
}

function renderTrend(users) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("trendChart");
  svg.innerHTML = "";
  const bkts = trendBuckets();

  if (!bkts.length || !users.length) {
    svg.setAttribute("viewBox", "0 0 400 100");
    svg.removeAttribute("width"); svg.removeAttribute("height");
    svg.setAttribute("style", "width:100%;height:100px");
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", 10); t.setAttribute("y", 50); t.setAttribute("class", "empty");
    t.textContent = "No data for the current filters.";
    svg.appendChild(t);
    document.getElementById("trendLegend").innerHTML = "";
    document.getElementById("trendWarnNote").textContent = "";
    return;
  }

  const seriesByUser = users.map(u => ({ user: u, pts: trendSeries(u, bkts) }));
  const allRates = seriesByUser.flatMap(s => s.pts.map(p => p.rate)).filter(v => v != null);
  const maxRate = Math.max(1, ...allRates) * 1.1;

  const padL = 34, padR = 16, padT = 14, padB = 34;
  const colW = 56;
  const w = Math.max(360, padL + padR + colW * bkts.length);
  const h = 260;
  const plotW = w - padL - padR, plotH = h - padT - padB;

  svg.setAttribute("viewBox", "0 0 " + w + " " + h);
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.removeAttribute("style");

  const xFor = i => padL + (bkts.length === 1 ? plotW / 2 : (i / (bkts.length - 1)) * plotW);
  const yFor = r => padT + plotH - (r / maxRate) * plotH;

  const steps = 4;
  for (let s = 0; s <= steps; s++) {
    const val = maxRate * s / steps;
    const y = yFor(val);
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", padL); line.setAttribute("x2", w - padR);
    line.setAttribute("y1", y); line.setAttribute("y2", y);
    line.setAttribute("class", "gridline");
    svg.appendChild(line);
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", padL - 6); label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.textContent = val.toFixed(0);
    svg.appendChild(label);
  }

  bkts.forEach((b, i) => {
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", xFor(i)); label.setAttribute("y", h - padB + 16);
    label.setAttribute("text-anchor", "middle");
    label.textContent = b.key;
    svg.appendChild(label);
  });

  let anyThin = false;

  seriesByUser.forEach(({ user, pts }) => {
    const color = colorFor(user);
    let d = "";
    let drawing = false;
    pts.forEach((p, i) => {
      if (p.rate == null) { drawing = false; return; }
      const x = xFor(i), y = yFor(p.rate);
      d += (drawing ? "L" : "M") + x + "," + y + " ";
      drawing = true;
    });
    if (d) {
      const poly = document.createElementNS(svgNS, "path");
      poly.setAttribute("d", d.trim());
      poly.setAttribute("fill", "none");
      poly.setAttribute("stroke", color);
      poly.setAttribute("stroke-width", "2");
      svg.appendChild(poly);
    }
    pts.forEach((p, i) => {
      if (p.rate == null) return;
      if (p.thin) anyThin = true;
      const x = xFor(i), y = yFor(p.rate);
      const c = document.createElementNS(svgNS, "circle");
      c.setAttribute("cx", x); c.setAttribute("cy", y);
      c.setAttribute("r", p.thin ? 4 : 3.2);
      c.setAttribute("fill", p.thin ? "var(--panel)" : color);
      c.setAttribute("stroke", color);
      c.setAttribute("stroke-width", p.thin ? 2 : 0);
      const title = document.createElementNS(svgNS, "title");
      title.textContent = user + " — " + p.key + ": " + p.rate.toFixed(2) +
        " per 100 moves (" + p.moves + " moves, " + p.games + " games)" +
        (p.thin ? " ⚠ fewer than " + DATA.thinGamesThreshold + " games this period, noisy" : "");
      c.appendChild(title);
      svg.appendChild(c);
    });
  });

  document.getElementById("trendLegend").innerHTML = users.map(u =>
    '<span class="item"><span class="swatch" style="background:' + colorFor(u) + '"></span>' + u + '</span>'
  ).join("");

  document.getElementById("trendWarnNote").innerHTML = anyThin
    ? '<span class="ring"></span>hollow points mark periods with fewer than ' +
      DATA.thinGamesThreshold + ' games for that player — treat those readings as noisy.'
    : "";
}

function render() {
  const users = DATA.users.filter(u => state.users.has(u));
  const cats = DATA.categories.filter(c => state.categories.has(c));

  document.getElementById("summary").textContent =
    users.length + " player" + (users.length === 1 ? "" : "s") + " · " +
    state.phases.size + " phase" + (state.phases.size === 1 ? "" : "s") + " · " +
    state.timeClasses.size + " time class" + (state.timeClasses.size === 1 ? "" : "es") + " · " +
    cats.length + " mistake type" + (cats.length === 1 ? "" : "s") + " selected";

  const totals = users.map(u => ({ u, r: totalRate(u), m: movesFor(u), g: gamesFor(u) }));
  const maxR = Math.max(1, ...totals.map(t => t.r || 0));
  const barsEl = document.getElementById("bars");
  barsEl.innerHTML = "";
  totals.forEach(t => {
    const row = document.createElement("div");
    row.className = "barrow";
    const width = t.r == null ? 0 : (t.r / maxR * 100);
    row.innerHTML =
      '<div class="barlabel" title="' + t.u + ' &middot; ' + t.g + ' games">' + t.u + '</div>' +
      '<div class="bartrack"><div class="barfill" style="width:' + width + '%"></div></div>' +
      '<div class="barvalue">' + (t.r == null ? "–" : t.r.toFixed(2)) + '</div>';
    barsEl.appendChild(row);
  });

  renderTrend(users);

  const rateRows = cats.map(c => ({ c, vals: users.map(u => rateOf(u, c)) }));
  rateRows.sort((a, b) => {
    const gapA = Math.max(...a.vals.map(v => v ?? 0)) - Math.min(...a.vals.map(v => v ?? 0));
    const gapB = Math.max(...b.vals.map(v => v ?? 0)) - Math.min(...b.vals.map(v => v ?? 0));
    return gapB - gapA;
  });

  const table = document.getElementById("table");
  let thead = "<thead><tr><th>mistake type</th>" + users.map(u => "<th>" + u + "</th>").join("") + "</tr></thead>";
  let tbody = "<tbody>";
  tbody += '<tr class="total-row"><td>ALL ERRORS</td>' +
    totals.map(t => "<td>" + (t.r == null ? "–" : t.r.toFixed(2)) + "</td>").join("") + "</tr>";
  rateRows.forEach(r => {
    const numeric = r.vals.filter(v => v != null);
    const maxV = numeric.length ? Math.max(...numeric) : null;
    tbody += "<tr><td>" + r.c + "</td>" + r.vals.map(v => {
      const cls = (v != null && maxV != null && v === maxV && users.length > 1) ? "maxcell" : "";
      return '<td class="' + cls + '">' + (v == null ? "–" : v.toFixed(2)) + "</td>";
    }).join("") + "</tr>";
  });
  tbody += "</tbody>";
  table.innerHTML = thead + tbody;

  const refSel = document.getElementById("refSelect");
  if (!users.includes(state.ref)) state.ref = users[0] || DATA.users[0];
  refSel.innerHTML = users.map(u =>
    '<option value="' + u + '"' + (u === state.ref ? " selected" : "") + ">" + u + "</option>").join("");

  const gapsEl = document.getElementById("gaps");
  gapsEl.innerHTML = "";
  if (users.length >= 2 && state.ref) {
    const others = users.filter(u => u !== state.ref);
    const gaps = cats.map(c => {
      const refR = rateOf(state.ref, c) ?? 0;
      const otherVals = others.map(u => rateOf(u, c)).filter(v => v != null);
      const otherMin = otherVals.length ? Math.min(...otherVals) : 0;
      return { c, gap: refR - otherMin };
    }).filter(g => g.gap > 0).sort((a, b) => b.gap - a.gap).slice(0, 6);
    if (gaps.length) {
      const ul = document.createElement("ul");
      gaps.forEach(g => {
        const li = document.createElement("li");
        li.textContent = "+" + g.gap.toFixed(2) + " per 100 moves — " + g.c;
        ul.appendChild(li);
      });
      gapsEl.appendChild(ul);
    } else {
      gapsEl.textContent = "No positive gaps for " + state.ref + " under the current filters.";
    }
  } else {
    gapsEl.textContent = "Select at least two players to see gaps.";
  }
}

buildGroup("chk-users", DATA.users, state.users);
buildGroup("chk-phases", DATA.phases, state.phases);
buildGroup("chk-tc", DATA.timeClasses, state.timeClasses);
buildGroup("chk-cats", DATA.categories, state.categories);
wireGroupLinks("grp-users", "chk-users", DATA.users, state.users);
wireGroupLinks("grp-phases", "chk-phases", DATA.phases, state.phases);
wireGroupLinks("grp-tc", "chk-tc", DATA.timeClasses, state.timeClasses);
wireGroupLinks("grp-cats", "chk-cats", DATA.categories, state.categories);
document.getElementById("refSelect").addEventListener("change", e => { state.ref = e.target.value; render(); });
document.getElementById("granularity").addEventListener("click", e => {
  const btn = e.target.closest("button[data-g]");
  if (!btn) return;
  state.granularity = btn.dataset.g;
  document.querySelectorAll("#granularity button").forEach(b => b.classList.toggle("active", b === btn));
  render();
});
render();
</script>
</body>
</html>
"""


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
    html = (HTML_TEMPLATE
            .replace("__GENERATED_AT__", generated)
            .replace("__DATA_JSON__", json.dumps(data)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(present)


def bar(n: int, total: int, width: int = 28) -> str:
    filled = 0 if total == 0 else round(width * n / total)
    return "#" * filled + "." * (width - filled)


def report(conn: sqlite3.Connection, user: str, time_class: str | None = None,
           last_days: int | None = None, phase: str | None = None) -> str:
    u = user.lower()
    games_where, games_params = "WHERE username = ?", [u]
    if time_class:
        games_where += " AND time_class = ?"
        games_params.append(time_class)
    if last_days:
        games_where += " AND end_time >= ?"
        games_params.append(int(time.time()) - last_days * 86400)

    mistakes_where, mistakes_params = games_where, list(games_params)
    if phase:
        mistakes_where += " AND phase = ?"
        mistakes_params.append(phase)

    games = conn.execute(f"SELECT * FROM games {games_where} ORDER BY end_time",
                         games_params).fetchall()
    if not games:
        return "No games stored yet for that filter."

    serious = conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} AND severity IN ('mistake','blunder')",
        mistakes_params).fetchall()

    out: list[str] = []

    def line(s: str = "") -> None:
        out.append(s)

    moves_col = f"{phase}_moves" if phase else "moves_played"
    total_moves = sum(g[moves_col] or 0 for g in games)
    not_backfilled = phase and sum(1 for g in games if g[moves_col] is None)
    ratings = [g["my_rating"] for g in games if g["my_rating"]]
    n_serious = len(serious)
    moves_label = f"your {phase} moves" if phase else "your moves"

    filters = ", ".join(filter(None, [time_class, phase]))
    line("=" * 64)
    line(f"CHESS ERROR PROFILE  |  {user}" + (f"  [{filters}]" if filters else ""))
    line("=" * 64)
    line(f"Games in store     : {len(games)}")
    line(f"Your moves         : {total_moves}" + (f"  ({phase})" if phase else ""))
    line(f"Date range         : {games[0]['date']} to {games[-1]['date']}")
    if ratings:
        line(f"Rating             : {ratings[0]} then, {ratings[-1]} now "
             f"({ratings[-1] - ratings[0]:+d})")
    line(f"Mistakes + blunders: {n_serious}  "
         f"({n_serious / max(total_moves, 1) * 100:.1f}% of {moves_label})")
    if not_backfilled:
        line(f"NOTE: {not_backfilled} game(s) have no per-phase move counts yet "
             f"(re-run without --report-only to backfill) -- they are excluded above.")
    line()

    line("-" * 64)
    line("RECURRING ERROR TYPES")
    line("-" * 64)
    for cat, n in Counter(m["category"] for m in serious).most_common():
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {cat}")
    line()

    if not phase:
        line("-" * 64)
        line("WHEN THEY HAPPEN")
        line("-" * 64)
        phases = Counter(m["phase"] for m in serious)
        for ph in PHASES:
            n = phases.get(ph, 0)
            line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {ph}")
        line()

    buckets = ["1-10", "11-20", "21-30", "31-40", "41+"]

    def bucket(mv: int) -> str:
        return (buckets[0] if mv <= 10 else buckets[1] if mv <= 20
                else buckets[2] if mv <= 30 else buckets[3] if mv <= 40 else buckets[4])

    by_move = Counter(bucket(m["move_number"]) for m in serious)
    line("By move number:")
    for b in buckets:
        n = by_move.get(b, 0)
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  moves {b}")
    line()

    clocked = [m for m in serious if m["clock_seconds"] is not None]
    if clocked:
        line("-" * 64)
        line("TIME PRESSURE")
        line("-" * 64)
        groups = (("under 30s left", [m for m in clocked if m["clock_seconds"] < 30]),
                  ("30 to 60s left", [m for m in clocked if 30 <= m["clock_seconds"] < 60]),
                  ("over 60s left", [m for m in clocked if m["clock_seconds"] >= 60]))
        for label, grp in groups:
            n = len(grp)
            line(f"{n:5d}  {n / len(clocked) * 100:5.1f}%  {bar(n, len(clocked))}  {label}")
        if len(groups[0][1]) / len(clocked) > 0.30:
            line()
            line(">> Most of your damage happens on a low clock. That is a time")
            line("   management problem, not a chess knowledge problem.")
        line()

    # ---- the whole point of persisting: movement over time -----------------
    monthly: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        monthly[g["date"][:7]][1] += g[moves_col] or 0
    for m in serious:
        monthly[m["date"][:7]][0] += 1

    if len(monthly) >= 2:
        line("-" * 64)
        line(f"TREND  (serious errors per 100 of {moves_label})")
        line("-" * 64)
        rows = sorted(monthly.items())
        rates = [(mo, e / mv * 100 if mv else 0.0, mv) for mo, (e, mv) in rows]
        peak = max((r[1] for r in rates), default=0) or 1
        for mo, rate, mv in rates:
            line(f"  {mo}  {rate:5.1f}  {'#' * round(30 * rate / peak)}  ({mv} moves)")
        first, last = rates[0][1], rates[-1][1]
        line()
        line(f"  {rates[0][0]} to {rates[-1][0]}: {first:.1f} -> {last:.1f} "
             f"({'improving' if last < first else 'getting worse'})")
        line()

        half = max(len(rows) // 2, 1)
        early_months = {mo for mo, _ in rows[:half]}
        early_moves = sum(mv for _, (_, mv) in rows[:half])
        late_moves = sum(mv for _, (_, mv) in rows[half:])
        early_c: Counter = Counter()
        late_c: Counter = Counter()
        for m in serious:
            (early_c if m["date"][:7] in early_months else late_c)[m["category"]] += 1
        if early_moves and late_moves:
            line("Per 100 moves, first half of the period vs second half:")
            deltas = []
            for c in set(early_c) | set(late_c):
                a = early_c[c] / early_moves * 100
                b = late_c[c] / late_moves * 100
                deltas.append((b - a, a, b, c))
            for d, a, b, c in sorted(deltas, key=lambda x: -abs(x[0]))[:6]:
                arrow = "worse " if d > 0.05 else "better" if d < -0.05 else "flat  "
                line(f"  {a:5.2f} -> {b:5.2f}  {arrow}  {c}")
            line()

    line("-" * 64)
    line("BY COLOUR AND TIME CONTROL")
    line("-" * 64)
    for key in ("my_colour", "time_class"):
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for g in games:
            agg[g[key]][1] += g[moves_col] or 0
        for m in serious:
            agg[m[key]][0] += 1
        for k, (e, mv) in sorted(agg.items()):
            if mv:
                line(f"  {k:<10} {e / mv * 100:5.2f} errors per 100 moves  ({mv} moves)")
    line()

    eco: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        if g["eco"] != "?":
            eco[g["eco"]][1] += 1
    url_to_eco = {g["url"]: g["eco"] for g in games}
    for m in serious:
        e = url_to_eco.get(m["game_url"])
        if e and e != "?":
            eco[e][0] += 1
    frequent = {k: v for k, v in eco.items() if v[1] >= 3}
    if frequent:
        line("-" * 64)
        line("OPENINGS YOU PLAY OFTEN")
        line("-" * 64)
        for k, (e, n) in sorted(frequent.items(), key=lambda kv: -kv[1][0] / kv[1][1])[:8]:
            line(f"  {k}   {n:3d} games   {e / n:.1f} serious errors per game")
        line()

    line("-" * 64)
    line("TOP 10 POSITIONS TO REVIEW")
    line("-" * 64)
    top = conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} AND severity IN ('mistake','blunder') "
        f"ORDER BY cp_loss DESC LIMIT 10", mistakes_params).fetchall()
    for m in top:
        clk = f"{m['clock_seconds']:.0f}s" if m["clock_seconds"] is not None else "?"
        line(f"  -{m['cp_loss']:>4}cp  {m['date']}  move {m['move_number']:<3} "
             f"played {m['played']:<7} best {m['best']:<7} clock {clk:>5}")
        line(f"            {m['category']}")
        line(f"            {m['game_url']}")
    line()
    line("=" * 64)
    return "\n".join(out)


# ==========================================================================

def main() -> None:
    p = argparse.ArgumentParser(description="Longitudinal chess error tracker")
    p.add_argument("--user", required=True,
                   help="Chess.com username. Comma-separate to scan several: "
                        "--user me,rival1,rival2")
    p.add_argument("--list-users", action="store_true",
                   help="Show every user stored in the database, then exit")
    p.add_argument("--compare", action="store_true",
                   help="Side-by-side comparison instead of per-user reports. "
                        "Needs two or more users in --user")
    p.add_argument("--email", help="Contact email for the User-Agent header. "
                                   "Required by Chess.com unless --report-only")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--engine", default=None,
                   help="Path to the Stockfish binary. Auto-detected if omitted.")
    p.add_argument("--depth", type=int, default=14)
    p.add_argument("--since", help="Earliest month to include, YYYY-MM")
    p.add_argument("--time-class", choices=["bullet", "blitz", "rapid", "daily"])
    p.add_argument("--phase", choices=list(PHASES),
                   help="Restrict the report/comparison to one phase of the game")
    p.add_argument("--limit", type=int, help="Cap on new games analysed per run")
    p.add_argument("--min-loss", type=int, default=INACCURACY)
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--pause", type=float, default=0.6,
                   help="Seconds between HTTP requests")
    p.add_argument("--report-only", action="store_true",
                   help="Report from the database, no network and no engine")
    p.add_argument("--last-days", type=int, help="Restrict the report to recent games")
    p.add_argument("--export", help="Write all stored mistakes to this CSV")
    p.add_argument("--export-html", help="Write an interactive, filterable "
                        "dashboard (by player, phase, time class, mistake "
                        "type) to this HTML file. Report-only, no network.")
    args = p.parse_args()

    conn = open_db(args.db)
    users = [u.strip() for u in args.user.split(",") if u.strip()]

    if args.list_users:
        print(list_users(conn))
        conn.close()
        return

    if not args.report_only:
        if not args.email:
            sys.exit("--email is required for fetching. Chess.com rejects requests "
                     "without a contact User-Agent.")
        engine_path = args.engine or find_engine()
        if not engine_path:
            sys.exit(ENGINE_HELP)
        if not os.path.isfile(engine_path):
            sys.exit(f"No Stockfish binary at {engine_path}\n\n{ENGINE_HELP}")
        if not args.engine:
            print(f"Using engine: {engine_path}", file=sys.stderr)

        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
        engine.configure({"Threads": args.threads})
        try:
            for user in users:
                started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                client = ChessComClient(args.email, conn, args.pause)

                print(f"\n[{user}] fetching game index...", file=sys.stderr)
                games = collect_games(client, user, args.since,
                                      args.time_class, args.limit)

                todo = [g for g in games
                        if not already_analysed(conn, g.get("url", ""), user, args.depth)]
                print(f"[{user}] {len(games)} games known, {len(todo)} need "
                      f"analysis at depth {args.depth}", file=sys.stderr)

                new = 0
                try:
                    for i, g in enumerate(todo, 1):
                        result = analyse_game(g, user, engine, args.depth, args.min_loss)
                        if result:
                            rec, mistakes = result
                            save_game(conn, rec, mistakes, args.depth)
                            new += 1
                        print(f"\r[{user}] analysed {i}/{len(todo)}", end="",
                              file=sys.stderr)
                except KeyboardInterrupt:
                    print(f"\n[{user}] interrupted. Everything analysed so far "
                          f"is saved.", file=sys.stderr)
                    raise
                finally:
                    if todo:
                        print(file=sys.stderr)
                    with conn:
                        conn.execute("""INSERT INTO runs
                            (username, started_at, finished_at, requests_made,
                             games_new, depth) VALUES (?, ?, ?, ?, ?, ?)""",
                                     (user.lower(), started,
                                      datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      client.requests_made, new, args.depth))
        except KeyboardInterrupt:
            print("Stopped.", file=sys.stderr)
        finally:
            engine.quit()

    if args.phase or args.export_html:
        for user in users:
            backfill_phase_moves(conn, user)

    if args.compare:
        if len(users) < 2:
            sys.exit("--compare needs two or more usernames, e.g. --user me,rival")
        print(compare(conn, users, args.time_class, args.phase))
    else:
        for user in users:
            print(report(conn, user, args.time_class, args.last_days, args.phase))
            print()

    if args.export:
        import csv
        placeholders = ",".join("?" * len(users))
        rows = conn.execute(
            f"SELECT * FROM mistakes WHERE username IN ({placeholders}) "
            f"ORDER BY username, end_time", [u.lower() for u in users]).fetchall()
        if rows:
            with open(args.export, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=rows[0].keys())
                w.writeheader()
                w.writerows(dict(r) for r in rows)
            print(f"Exported {len(rows)} mistakes to {args.export}", file=sys.stderr)

    if args.export_html:
        n = export_html(conn, users, args.export_html)
        if n:
            print(f"Wrote interactive dashboard for {n} user(s) to "
                  f"{args.export_html}", file=sys.stderr)
        else:
            print("No stored games for the given users, nothing written.",
                  file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()

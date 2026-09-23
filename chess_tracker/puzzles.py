"""Lichess puzzle database: CSV import and answer checking for puzzle-
solving mode. Source data: https://database.lichess.org/#puzzles (CC0),
~6.1M rows, columns PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,
NbPlays,Themes,GameUrl,OpeningTags. FEN is the position *before* the
opponent's setup move; Moves is space-separated UCI where moves[0] is that
forced setup move and the solver's own moves start at moves[1], alternating
with more auto-played opponent replies from there.
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import chess
import requests
import zstandard


class PuzzleImportCancelled(Exception):
    """Raised by download_puzzle_source()/import_puzzles() when the passed
    `cancel_event` is set mid-operation -- both are long enough (a 1.8GB
    download, a multi-million-row scan) that a user should be able to abort
    one partway through, not just before it starts."""

PUZZLE_SOURCE_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"

# Same discovery convention as engine.find_engine()/find_lc0(): an explicit
# env var override, else a default cache location -- here, a dotfolder next
# to CLI's own ~/.chess-tracker.json (see cli.py's DEFAULT_CONFIG_PATH), not
# inside the project directory. Unlike the sqlite db (small, gitignored),
# this is a ~1.8GB plain-text cache with no reason to live next to the code.
_DEFAULT_SOURCE_DIR = os.path.join(os.path.expanduser("~"), ".chess-tracker", "puzzles")
_DEFAULT_SOURCE_PATH = os.path.join(_DEFAULT_SOURCE_DIR, "lichess_db_puzzle.csv")

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB read/decompress chunks

# A practical subset of Lichess's ~60 theme codes worth exposing in the
# picker UI -- the full taxonomy (https://lichess.org/training/themes) is
# too much for a checkbox list, and its raw camelCase codes ("mateIn2",
# "discoveredAttack") aren't self-explanatory to someone who doesn't already
# know Lichess's naming. THEME_GROUPS pairs each code with a plain-English
# label and buckets them into a handful of categories a player actually
# thinks in (how does the puzzle end / what's the pattern / what stage of
# the game) -- purely presentational, the (code, label) pairs are what the
# picker renders, but only `code` is ever stored/filtered against.
THEME_GROUPS = (
    ("Checkmate", (
        ("mateIn1", "Mate in 1"),
        ("mateIn2", "Mate in 2"),
        ("mateIn3", "Mate in 3"),
        ("mateIn4", "Mate in 4"),
        ("mateIn5", "Mate in 5"),
        ("backRankMate", "Back-rank mate"),
    )),
    ("Tactical motif", (
        ("fork", "Fork"),
        ("pin", "Pin"),
        ("skewer", "Skewer"),
        ("discoveredAttack", "Discovered attack"),
        ("sacrifice", "Sacrifice"),
        ("hangingPiece", "Hanging piece"),
    )),
    ("Game phase", (
        ("opening", "Opening"),
        ("middlegame", "Middlegame"),
        ("endgame", "Endgame"),
    )),
)

# Flattened (code, label) pairs and bare codes, derived from THEME_GROUPS so
# there's exactly one place the actual set of exposed themes is defined.
COMMON_THEME_LABELS = {code: label for _, items in THEME_GROUPS for code, label in items}
COMMON_THEMES = tuple(COMMON_THEME_LABELS.keys())


def humanize_theme(code: str) -> str:
    """Friendly label for a theme code, for display only (never for
    filtering -- that stays on the raw code). Any puzzle's actual stored
    themes can include Lichess's full ~60-code taxonomy, not just the
    curated THEME_GROUPS subset the picker exposes, so this needs a
    fallback: the curated label if known, else a plain camelCase-to-"Words"
    split (e.g. "veryLong" -> "Very long") -- not hand-picked, but still
    more readable than the raw code."""
    if code in COMMON_THEME_LABELS:
        return COMMON_THEME_LABELS[code]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", code).lower()
    return spaced[0].upper() + spaced[1:] if spaced else spaced

# Width of each "how many puzzles exist at this difficulty" bucket recorded
# in puzzle_source_stats.
RATING_BUCKET_SIZE = 100

# Shared between scripts/import_puzzles.py's default import filter and the
# web picker's default rating range, so the picker's defaults match what
# actually got imported by default rather than drifting independently.
DEFAULT_MIN_RATING = 400
DEFAULT_MAX_RATING = 2800
# Checked against the real database (6,100,952 puzzles) before settling on
# this: nb_plays >= 50 let through 5,430,071 of them -- nowhere near a
# "filtered, lean" subset. nb_plays >= 10000 (real puzzles that thousands of
# Lichess users have actually solved, a strong quality signal) lands at
# ~254,000 -- solidly in the "tens to low hundreds of thousands" range this
# was meant to target.
DEFAULT_MIN_PLAYS = 10000


def find_puzzle_source() -> str | None:
    """
    Locate the cached, decompressed Lichess puzzle CSV: an explicit
    CHESS_PUZZLE_SOURCE env var, else the default cache path, else None if
    neither exists yet -- callers (the import script, the web app's top-up
    job) must degrade to "not available, offer to download" rather than
    treating this as an error, same contract as find_engine()/find_lc0().
    """
    env = os.environ.get("CHESS_PUZZLE_SOURCE")
    if env and os.path.isfile(env):
        return env
    if os.path.isfile(_DEFAULT_SOURCE_PATH):
        return _DEFAULT_SOURCE_PATH
    return None


def download_puzzle_source(dest_path: str | None = None,
                            progress_cb: Callable[[int, int | None], None] | None = None,
                            cancel_event: threading.Event | None = None) -> str:
    """
    Stream the Lichess puzzle .zst from PUZZLE_SOURCE_URL and decompress it
    on the fly to `dest_path` (default: the standard cache path). Kept as a
    plain CSV file on disk, not re-imported wholesale into SQLite -- see the
    module docstring / phase plan for why. `progress_cb(bytes_downloaded,
    total_bytes_or_None)` is called periodically; total is None if the
    server didn't send a Content-Length. Returns the path written.

    Raises PuzzleImportCancelled (leaving no partial `dest_path` behind --
    only the still-in-progress `.part` file, which the next attempt
    overwrites) if `cancel_event` becomes set mid-download.
    """
    dest_path = dest_path or _DEFAULT_SOURCE_PATH
    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    with requests.get(PUZZLE_SOURCE_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        try:
            total = int(response.headers["content-length"])
        except (KeyError, ValueError):
            total = None

        decompressor = zstandard.ZstdDecompressor()
        downloaded = 0
        tmp_path = dest_path + ".part"
        with open(tmp_path, "wb") as out, decompressor.stream_writer(out) as writer:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise PuzzleImportCancelled()
                writer.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)

    os.replace(tmp_path, dest_path)  # atomic-ish swap; no half-written file left as "the" source
    return dest_path


_INSERT_BATCH_SIZE = 500


def _rating_bucket(rating: int) -> str:
    lo = (rating // RATING_BUCKET_SIZE) * RATING_BUCKET_SIZE
    return f"rating:{lo}-{lo + RATING_BUCKET_SIZE}"


def parse_puzzle_row(row: dict) -> dict:
    """One Lichess CSV row (as csv.DictReader yields it) to a
    puzzles-table-shaped dict. `themes` is stored space-padded (a leading
    and trailing space) so callers can filter with a plain, safe
    `LIKE '% fork %'` without partial-word false positives."""
    themes = row.get("Themes", "") or ""
    return {
        "puzzle_id": row["PuzzleId"],
        "fen": row["FEN"],
        "moves": row["Moves"],
        "rating": int(row["Rating"]),
        "rating_deviation": int(row["RatingDeviation"]),
        "popularity": int(row["Popularity"]),
        "nb_plays": int(row["NbPlays"]),
        "themes": f" {themes} " if themes else "  ",
        "game_url": row.get("GameUrl", ""),
        "opening_tags": row.get("OpeningTags", ""),
    }


def _matches_filter(puzzle: dict, min_rating: int | None, max_rating: int | None,
                     min_plays: int | None, themes: list[str] | None) -> bool:
    if min_rating is not None and puzzle["rating"] < min_rating:
        return False
    if max_rating is not None and puzzle["rating"] > max_rating:
        return False
    if min_plays is not None and puzzle["nb_plays"] < min_plays:
        return False
    if themes and not any(f" {t} " in puzzle["themes"] for t in themes):
        return False
    return True


@dataclass
class ImportStats:
    scanned: int = 0
    imported: int = 0


def import_puzzles(conn: sqlite3.Connection, source_csv_path: str, *,
                    min_rating: int | None = None, max_rating: int | None = None,
                    min_plays: int | None = None, themes: list[str] | None = None,
                    limit: int | None = None,
                    progress_cb: Callable[[int, int], None] | None = None,
                    cancel_event: threading.Event | None = None) -> ImportStats:
    """
    Stream `source_csv_path` (the decompressed Lichess puzzle CSV), import
    rows matching the given filters into `puzzles` (INSERT OR REPLACE, so
    re-running is a safe top-up/refresh keyed by puzzle_id) -- and, while
    already scanning every row regardless of whether it passed the filter,
    tally total-matching counts per rating bucket and per theme into
    `puzzle_source_stats`. That tally is how "how many puzzles exist in the
    source, even ones not imported" gets answered without a second pass
    over a multi-gigabyte file.

    `limit` caps how many rows get imported (stats keep counting past it,
    for an accurate "N imported out of M available" once the cap is hit).
    None on any filter/limit parameter means "no restriction on that axis".

    If `cancel_event` becomes set mid-scan, raises PuzzleImportCancelled --
    but only after flushing whatever's already been batched, so a cancelled
    import keeps whatever progress it made rather than discarding it.
    """
    stats = ImportStats()
    bucket_counts: dict[str, int] = {}
    batch: list[dict] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with open(source_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats.scanned += 1
            puzzle = parse_puzzle_row(row)

            bucket_counts["total"] = bucket_counts.get("total", 0) + 1
            rb = _rating_bucket(puzzle["rating"])
            bucket_counts[rb] = bucket_counts.get(rb, 0) + 1
            for theme in puzzle["themes"].split():
                key = f"theme:{theme}"
                bucket_counts[key] = bucket_counts.get(key, 0) + 1

            under_limit = limit is None or stats.imported < limit
            if under_limit and _matches_filter(puzzle, min_rating, max_rating, min_plays, themes):
                puzzle["imported_at"] = now
                batch.append(puzzle)
                stats.imported += 1

            if len(batch) >= _INSERT_BATCH_SIZE:
                _insert_batch(conn, batch)
                batch.clear()
                if progress_cb:
                    progress_cb(stats.scanned, stats.imported)
                if cancel_event is not None and cancel_event.is_set():
                    _save_source_stats(conn, bucket_counts, now)
                    raise PuzzleImportCancelled()

    if batch:
        _insert_batch(conn, batch)
    _save_source_stats(conn, bucket_counts, now)
    if progress_cb:
        progress_cb(stats.scanned, stats.imported)
    return stats


def _insert_batch(conn: sqlite3.Connection, batch: list[dict]) -> None:
    with conn:
        conn.executemany("""
            INSERT OR REPLACE INTO puzzles
            (puzzle_id, fen, moves, rating, rating_deviation, popularity,
             nb_plays, themes, game_url, opening_tags, imported_at)
            VALUES (:puzzle_id, :fen, :moves, :rating, :rating_deviation,
                    :popularity, :nb_plays, :themes, :game_url, :opening_tags,
                    :imported_at)
        """, batch)


def _save_source_stats(conn: sqlite3.Connection, bucket_counts: dict[str, int], now: str) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO puzzle_source_stats (bucket_key, total_count, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(bucket_key) DO UPDATE SET total_count = excluded.total_count, "
            "updated_at = excluded.updated_at",
            [(key, count, now) for key, count in bucket_counts.items()])


def pick_random_puzzle(conn: sqlite3.Connection, min_rating: int | None = None,
                        max_rating: int | None = None,
                        themes: list[str] | None = None) -> sqlite3.Row | None:
    """One random locally-imported puzzle matching the given filters, or
    None if nothing matches. The `puzzles` table is expected to stay in the
    tens-to-low-hundreds-of-thousands range (filtered import, see
    scripts/import_puzzles.py), so ORDER BY RANDOM() is cheap here."""
    where: list[str] = []
    params: list = []
    if min_rating is not None:
        where.append("rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        where.append("rating <= ?")
        params.append(max_rating)
    if themes:
        where.append("(" + " OR ".join("themes LIKE ?" for _ in themes) + ")")
        params += [f"% {t} %" for t in themes]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT * FROM puzzles {clause} ORDER BY RANDOM() LIMIT 1", params).fetchone()


def source_stats_for_filter(conn: sqlite3.Connection, min_rating: int | None = None,
                             max_rating: int | None = None,
                             themes: list[str] | None = None) -> dict:
    """
    Best-effort "how many exist in the full source, even ones not
    imported" for the given filter, from puzzle_source_stats. NOT an exact
    combined-filter count -- those buckets are tallied per single dimension
    (a rating band, or a theme) during import, not cross-tabulated, so a
    rating-range-and-theme query can't be answered exactly without a second
    full scan. Returns independent figures for each dimension instead of
    pretending to have one, so the UI can be honest about what this is.
    """
    rows = conn.execute("SELECT bucket_key, total_count FROM puzzle_source_stats").fetchall()
    by_key = {row["bucket_key"]: row["total_count"] for row in rows}

    rating_total = 0
    lo = ((min_rating if min_rating is not None else 0) // RATING_BUCKET_SIZE) * RATING_BUCKET_SIZE
    hi_bound = max_rating if max_rating is not None else 4000
    hi = (hi_bound // RATING_BUCKET_SIZE) * RATING_BUCKET_SIZE
    bucket = lo
    while bucket <= hi:
        rating_total += by_key.get(f"rating:{bucket}-{bucket + RATING_BUCKET_SIZE}", 0)
        bucket += RATING_BUCKET_SIZE

    return {
        "grandTotal": by_key.get("total", 0),
        "ratingRangeTotal": rating_total,
        "themeTotals": {t: by_key.get(f"theme:{t}", 0) for t in (themes or [])},
    }


def check_puzzle_move(moves: str, move_index: int, move: chess.Move) -> bool:
    """
    Whether `move` matches the puzzle's known solution at `move_index` (an
    index into the space-separated `moves` string -- index 0 is the
    opponent's forced setup move, never checked against a player move,
    indices 1, 3, 5, ... are what the solver must find).

    Exact UCI comparison -- puzzle solutions are forced "only moves" by
    construction, so unlike practice mode's cp_loss-based "also_fine"
    judgement, no engine call is needed here at all.
    """
    parts = moves.split()
    if move_index >= len(parts):
        return False
    return move.uci() == parts[move_index]


def puzzle_position_payload(row: sqlite3.Row, practicing_user: str) -> dict:
    """What the puzzle board needs to start: the position the solver actually
    faces (the stored FEN is *before* the opponent's setup move, moves[0],
    so it's applied here), which side they play, and the legal moves."""
    board = chess.Board(row["fen"])
    board.push(chess.Move.from_uci(row["moves"].split()[0]))
    return {
        "puzzleId": row["puzzle_id"],
        "fen": board.fen(),
        "colour": "white" if board.turn == chess.WHITE else "black",
        "rating": row["rating"],
        "themes": [humanize_theme(t) for t in row["themes"].split()],
        "legalMoves": [m.uci() for m in board.legal_moves],
        "moveIndex": 1,
        "practicingUser": practicing_user,
    }

"""
One-off/repeatable maintenance script: import (or top up) Lichess's public
puzzle database into the local `puzzles` table.

Lichess publishes the full database at
https://database.lichess.org/lichess_db_puzzle.csv.zst (CC0, ~6.1M rows) --
download and decompress it yourself for now (e.g. 7-Zip on Windows can
extract .zst), then point this script at the plain CSV. Filtered by default
(rating range + a minimum play count) rather than importing all 6.1M rows,
to keep the local database lean; every filter is a flag, so re-run wider
whenever you want. Safe to re-run against the same or a refreshed source
file -- rows are INSERT OR REPLACEd by puzzle_id, so this doubles as a
top-up/refresh, not just a first-time import.

Usage:
    chess-tracker's venv python  scripts/import_puzzles.py path/to/lichess_db_puzzle.csv
        [--db PATH] [--min-rating N] [--max-rating N] [--min-plays N]
        [--themes fork,pin,...] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from chess_tracker.cli import DEFAULT_DB  # noqa: E402
from chess_tracker.db import open_db  # noqa: E402
from chess_tracker.puzzles import (DEFAULT_MAX_RATING, DEFAULT_MIN_PLAYS,  # noqa: E402
                                   DEFAULT_MIN_RATING, import_puzzles)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source_csv", help="Path to the decompressed Lichess puzzle CSV")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--min-rating", type=int, default=DEFAULT_MIN_RATING)
    p.add_argument("--max-rating", type=int, default=DEFAULT_MAX_RATING)
    p.add_argument("--min-plays", type=int, default=DEFAULT_MIN_PLAYS)
    p.add_argument("--themes", default="",
                   help="Comma-separated theme codes to require at least one of "
                        "(e.g. fork,pin,mateIn2). Empty means no theme filter.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap how many puzzles are imported (the source is still "
                        "fully scanned for accurate stats either way)")
    p.add_argument("--dry-run", action="store_true",
                   help="Scan and report without writing anything")
    args = p.parse_args()

    themes = [t.strip() for t in args.themes.split(",") if t.strip()] or None

    print(f"Scanning {args.source_csv}")
    print(f"  rating {args.min_rating}-{args.max_rating}, "
          f"min_plays >= {args.min_plays}"
          + (f", themes in {themes}" if themes else ", no theme filter")
          + (f", capped at {args.limit}" if args.limit else ""))

    if args.dry_run:
        # A dry run still needs a real (possibly scratch/in-memory) connection
        # since import_puzzles() writes source stats as it scans -- point it
        # at an in-memory db so the real one on disk is untouched.
        conn = open_db(":memory:")
    else:
        conn = open_db(args.db)

    t0 = time.perf_counter()
    stats = import_puzzles(
        conn, args.source_csv, min_rating=args.min_rating, max_rating=args.max_rating,
        min_plays=args.min_plays, themes=themes, limit=args.limit,
        progress_cb=lambda scanned, imported: print(
            f"\r  {scanned:,} scanned, {imported:,} imported", end="", flush=True))
    dt = time.perf_counter() - t0

    print(f"\nDone in {dt:.1f}s. {stats.scanned:,} rows scanned, "
         f"{stats.imported:,} imported"
         + (" (dry run -- nothing written)" if args.dry_run else "."))


if __name__ == "__main__":
    main()

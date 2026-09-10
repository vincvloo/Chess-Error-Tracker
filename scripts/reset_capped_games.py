"""
One-off maintenance script: mark every game that contains at least one
mistake capped at the old cp_loss ceiling (2000) as un-analysed, so the next
normal `chess-tracker` run re-runs Stockfish on exactly those games and
stores fresh cp_loss values under the new, higher cap.

Does NOT touch the network or Stockfish itself -- it only resets `depth` to
0 on the affected rows in `games`. `already_analysed()` treats depth 0 as
"needs (re)analysis", and `save_game()` REPLACEs the row (and its mistakes)
cleanly when that happens, so this is safe to run against the live database.

Usage:
    chess-tracker's venv python  scripts/reset_capped_games.py [--db PATH] [--dry-run]

Then re-run your normal fetch+analyse command (no --report-only) for each
affected user -- games already at the requested depth are skipped as usual,
so only the reset ones actually get re-analysed:

    chess-tracker --user <username> --email <you@example.com> --depth 14
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from chess_tracker.cli import DEFAULT_DB  # noqa: E402
from chess_tracker.db import open_db  # noqa: E402

OLD_CAP = 2000


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be reset without changing anything")
    args = p.parse_args()

    conn = open_db(args.db)
    affected = conn.execute(
        "SELECT DISTINCT username, game_url FROM mistakes WHERE cp_loss = ?",
        (OLD_CAP,)).fetchall()

    if not affected:
        print(f"No mistakes at the old cap ({OLD_CAP}) found. Nothing to do.")
        return

    by_user: dict[str, int] = {}
    for row in affected:
        by_user[row["username"]] = by_user.get(row["username"], 0) + 1

    print(f"{len(affected)} games contain at least one mistake capped at {OLD_CAP}:")
    for user, n in sorted(by_user.items(), key=lambda kv: -kv[1]):
        print(f"  {user:<20} {n} games")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    with conn:
        for row in affected:
            conn.execute(
                "UPDATE games SET depth = 0 WHERE username = ? AND url = ?",
                (row["username"], row["game_url"]))

    print(f"\nReset. Now re-run, per user, WITHOUT --report-only, e.g.:")
    for user in sorted(by_user):
        print(f"  chess-tracker --user {user} --email you@example.com --depth 14")
    print("\nGames already at that depth are skipped as usual -- only the "
         "reset ones above will actually be re-analysed.")


if __name__ == "__main__":
    main()

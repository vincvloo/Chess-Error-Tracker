"""Command-line entry point.

Usage:
    # first run, builds the database
    chess-tracker --user vincent --email you@example.com

    # later runs, only new games get analysed
    chess-tracker --user vincent --email you@example.com

    # report on everything already stored, no network, no engine
    chess-tracker --user vincent --report-only

    # deeper re-analysis of games previously done shallow
    chess-tracker --user vincent --email you@example.com --depth 20
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import chess.engine

from .analysis import INACCURACY, PHASES, analyse_game, backfill_phase_moves
from .chesscom import ChessComClient, ChessComError, collect_games
from .db import already_analysed, open_db, save_game
from .engine import ENGINE_HELP, find_engine
from .html_export import export_html
from .reports import compare, list_users, report

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "chess_tracker.db")


def build_parser() -> argparse.ArgumentParser:
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
    return p


def main() -> None:
    args = build_parser().parse_args()

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

        engine = None
        try:
            engine = chess.engine.SimpleEngine.popen_uci(engine_path)
            engine.configure({"Threads": args.threads})
            for user in users:
                started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                client = ChessComClient(args.email, conn, args.pause)

                print(f"\n[{user}] fetching game index...", file=sys.stderr)
                try:
                    games = collect_games(client, user, args.since,
                                          args.time_class, args.limit)
                except ChessComError as exc:
                    sys.exit(str(exc))

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
            if engine is not None:
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

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
import json
import logging
import os
import sys

from .analysis import INACCURACY, PHASES, backfill_phase_moves
from .analysis_runner import DEFAULT_PARALLEL_THRESHOLD, DEFAULT_WORKERS, run_analysis
from .chesscom import ChessComError
from .db import open_db
from .engine import ENGINE_HELP, find_engine
from .html_export import export_html
from .reports import compare, list_users, report

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "chess_tracker.db")
DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".chess-tracker.json")

# Config-file keys that are allowed to override a CLI default. --user is
# deliberately excluded: it stays a per-invocation, always-required flag
# rather than something you'd want silently defaulted from a file.
CONFIG_KEYS = {"email", "db", "engine", "depth", "threads", "pause",
               "min_loss", "time_class", "parallel_threshold", "workers"}


def load_config(path: str, required: bool) -> dict:
    """
    Read CLI defaults from a JSON object file. Silently returns {} when the
    default path doesn't exist; a path passed explicitly via --config is
    required to exist. Unrecognised keys are warned about, not fatal, so a
    typo doesn't outright break every run.
    """
    if not os.path.isfile(path):
        if required:
            sys.exit(f"Config file not found: {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            sys.exit(f"Invalid JSON in config file {path}: {exc}")

    if not isinstance(data, dict):
        sys.exit(f"Config file {path} must contain a JSON object")

    unknown = set(data) - CONFIG_KEYS
    if unknown:
        print(f"Warning: ignoring unrecognised config key(s) in {path}: "
              f"{', '.join(sorted(unknown))}", file=sys.stderr)
        data = {k: v for k, v in data.items() if k in CONFIG_KEYS}

    return data


def _peek_config_path(argv: list[str]) -> str | None:
    """Extract --config from argv without needing the rest of the real
    parser's required arguments to already be present."""
    peek = argparse.ArgumentParser(add_help=False)
    peek.add_argument("--config")
    ns, _ = peek.parse_known_args(argv)
    return ns.config


def build_parser(config: dict | None = None) -> argparse.ArgumentParser:
    config = config or {}
    p = argparse.ArgumentParser(description="Longitudinal chess error tracker")
    p.add_argument("--config",
                   help="Path to a JSON file of defaults for the options below "
                        f"(default: {DEFAULT_CONFIG_PATH} if present)")
    p.add_argument("--user", required=True,
                   help="Chess.com username. Comma-separate to scan several: "
                        "--user me,rival1,rival2")
    p.add_argument("--list-users", action="store_true",
                   help="Show every user stored in the database, then exit")
    p.add_argument("--compare", action="store_true",
                   help="Side-by-side comparison instead of per-user reports. "
                        "Needs two or more users in --user")
    p.add_argument("--email", default=config.get("email"),
                   help="Contact email for the User-Agent header. "
                        "Required by Chess.com unless --report-only")
    p.add_argument("--db", default=config.get("db", DEFAULT_DB), help="SQLite database path")
    p.add_argument("--engine", default=config.get("engine"),
                   help="Path to the Stockfish binary. Auto-detected if omitted.")
    p.add_argument("--depth", type=int, default=config.get("depth", 14))
    p.add_argument("--since", help="Earliest month to include, YYYY-MM")
    p.add_argument("--time-class", choices=["bullet", "blitz", "rapid", "daily"],
                   default=config.get("time_class"))
    p.add_argument("--phase", choices=list(PHASES),
                   help="Restrict the report/comparison to one phase of the game")
    p.add_argument("--limit", type=int, help="Cap on new games analysed per run")
    p.add_argument("--min-loss", type=int, default=config.get("min_loss", INACCURACY))
    p.add_argument("--threads", type=int, default=config.get("threads", 2))
    p.add_argument("--parallel-threshold", type=int,
                   default=config.get("parallel_threshold", DEFAULT_PARALLEL_THRESHOLD),
                   help="If more than this many games need analysis for a user, "
                        "split the work across --workers processes instead of one "
                        f"engine (default: {DEFAULT_PARALLEL_THRESHOLD})")
    p.add_argument("--workers", type=int, default=config.get("workers", DEFAULT_WORKERS),
                   help="Number of parallel analysis processes to use once a backlog "
                        f"exceeds --parallel-threshold (default: {DEFAULT_WORKERS} on "
                        "this machine). 1 disables parallelism.")
    p.add_argument("--pause", type=float, default=config.get("pause", 0.6),
                   help="Seconds between HTTP requests")
    p.add_argument("--report-only", action="store_true",
                   help="Report from the database, no network and no engine")
    p.add_argument("--last-days", type=int, help="Restrict the report to recent games")
    p.add_argument("--export", help="Write all stored mistakes to this CSV")
    p.add_argument("--export-html", help="Write an interactive, filterable "
                        "dashboard (by player, phase, time class, mistake "
                        "type) to this HTML file. Report-only, no network.")
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true",
                   help="Suppress routine progress messages; warnings and errors still show")
    verbosity.add_argument("--verbose", action="store_true",
                   help="Show extra detail, including each HTTP request made")
    return p


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        try:
            from .web.serve_cli import serve_main
        except ImportError:
            sys.exit('The web UI needs extra dependencies: pip install -e ".[web]"')
        serve_main(sys.argv[2:])
        return

    argv = sys.argv[1:]
    explicit_config = _peek_config_path(argv)
    config = load_config(explicit_config or DEFAULT_CONFIG_PATH, required=explicit_config is not None)

    args = build_parser(config).parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)

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
            logger.info(f"Using engine: {engine_path}")

        try:
            run_analysis(conn, users, args.email, engine_path, args.depth, args.threads,
                         args.pause, since=args.since, time_class=args.time_class,
                         limit=args.limit, min_loss=args.min_loss, quiet=args.quiet,
                         parallel_threshold=args.parallel_threshold, workers=args.workers)
        except ChessComError as exc:
            sys.exit(str(exc))

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
            logger.info(f"Exported {len(rows)} mistakes to {args.export}")

    if args.export_html:
        n = export_html(conn, users, args.export_html)
        if n:
            logger.info(f"Wrote interactive dashboard for {n} user(s) to "
                        f"{args.export_html}")
        else:
            logger.warning("No stored games for the given users, nothing written.")

    conn.close()


if __name__ == "__main__":
    main()

"""`chess-tracker serve` -- flag parsing and uvicorn bootstrap for the local
web app. Deliberately its own small argparse setup, not a subparser bolted
onto cli.py's build_parser(): serve's flags (--host, --port, --no-browser)
share nothing with the fetch/report flags, and build_parser() is directly
unit-tested with --user as a required top-level flag."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from ..cli import DEFAULT_DB
from ..engine import find_engine
from .browser import open_app_window


def build_serve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chess-tracker serve",
                                description="Run the local web app")
    p.add_argument("--host", default="127.0.0.1",
                   help="Interface to bind. Defaults to 127.0.0.1 -- this is "
                        "a single-user local tool, not meant to be exposed "
                        "on the network.")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--engine", default=None,
                   help="Path to the Stockfish binary. Auto-detected if omitted.")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't automatically open a browser window")
    return p


def serve_main(argv: list[str]) -> None:
    args = build_serve_parser().parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        sys.exit('The web UI needs extra dependencies: pip install -e ".[web]"')

    from .app import create_app

    engine_path = args.engine or find_engine()
    app = create_app(args.db, engine_path)

    url = f"http://{args.host}:{args.port}/"

    if not args.no_browser:
        def launch_browser() -> None:
            if not open_app_window(url):
                webbrowser.open(url)

        threading.Timer(1.0, launch_browser).start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except OSError as exc:
        sys.exit(f"Couldn't start the server on {args.host}:{args.port} ({exc}). "
                 f"Pass --port to pick another.")

"""Fetch-and-analyse orchestration, shared by the CLI and the web app's
background jobs."""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Callable

import chess.engine

from .analysis import INACCURACY, analyse_game
from .chesscom import ChessComClient, collect_games
from .db import already_analysed, save_game

logger = logging.getLogger(__name__)


def run_analysis(conn: sqlite3.Connection, users: list[str], email: str, engine_path: str,
                  depth: int, threads: int, pause: float, *, since: str | None = None,
                  time_class: str | None = None, limit: int | None = None,
                  min_loss: int = INACCURACY, quiet: bool = False,
                  progress_cb: Callable[[str, int, int], None] | None = None,
                  cancel_event: threading.Event | None = None) -> None:
    """
    Fetch and analyse new games for each user in `users`, saving results as it
    goes. Opens one Stockfish engine for the whole run and closes it when
    done, exactly like the CLI always has.

    `progress_cb(user, i, total)`, if given, is called after each game --
    purely additional to the `quiet`-gated stderr progress line, so a caller
    (e.g. the web app) can drive its own progress UI without parsing stderr.

    `cancel_event`, if given, is checked once per game; when set, the current
    user's remaining games are skipped and no further users are started --
    everything analysed so far stays saved, same guarantee Ctrl+C gives today.

    Raises ChessComError if fetching fails for a user (e.g. no such Chess.com
    username) -- the caller decides how to report that (the CLI exits with
    the message; the web app records it as a failed job). A KeyboardInterrupt
    during fetch/analysis is caught and logged, not re-raised: the run simply
    stops with whatever was saved, matching the CLI's existing "Ctrl+C still
    shows you the report" behavior.
    """
    engine = None
    try:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
        engine.configure({"Threads": threads})
        for user in users:
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            client = ChessComClient(email, conn, pause)

            logger.info(f"\n[{user}] fetching game index...")
            games = collect_games(client, user, since, time_class, limit)

            todo = [g for g in games
                    if not already_analysed(conn, g.get("url", ""), user, depth)]
            logger.info(f"[{user}] {len(games)} games known, {len(todo)} need "
                        f"analysis at depth {depth}")

            new = 0
            try:
                for i, g in enumerate(todo, 1):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    result = analyse_game(g, user, engine, depth, min_loss)
                    if result:
                        rec, mistakes = result
                        save_game(conn, rec, mistakes, depth)
                        new += 1
                    if not quiet:
                        print(f"\r[{user}] analysed {i}/{len(todo)}", end="",
                              file=sys.stderr)
                    if progress_cb is not None:
                        progress_cb(user, i, len(todo))
            except KeyboardInterrupt:
                logger.warning(f"\n[{user}] interrupted. Everything analysed so far "
                               f"is saved.")
                raise
            finally:
                if todo and not quiet:
                    print(file=sys.stderr)
                with conn:
                    conn.execute("""INSERT INTO runs
                        (username, started_at, finished_at, requests_made,
                         games_new, depth) VALUES (?, ?, ?, ?, ?, ?)""",
                                 (user.lower(), started,
                                  datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  client.requests_made, new, depth))

            if cancel_event is not None and cancel_event.is_set():
                break
    except KeyboardInterrupt:
        logger.warning("Stopped.")
    finally:
        if engine is not None:
            engine.quit()

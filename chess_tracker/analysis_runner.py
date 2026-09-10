"""Fetch-and-analyse orchestration, shared by the CLI and the web app's
background jobs."""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import sqlite3
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from typing import Callable

import chess.engine

from .analysis import INACCURACY, analyse_game
from .chesscom import ChessComClient, collect_games
from .db import already_analysed, open_db, save_game

logger = logging.getLogger(__name__)

# Above this many games needing analysis for one user, split the work across
# --workers processes instead of one engine -- see docs/parallel-analysis-design.md
# for the full reasoning. Below it, the serial path (today's only behaviour)
# is unchanged: not worth the process-startup overhead for routine syncs.
DEFAULT_PARALLEL_THRESHOLD = 200
# Each worker also spends its own `threads` on Stockfish's internal search,
# so total CPU use is workers * threads -- halving cpu_count() leaves
# headroom, capped at 8 since returns diminish and per-process engine memory
# adds up on very large machines.
DEFAULT_WORKERS = min(8, max(1, (os.cpu_count() or 2) // 2))


class _ShardAnalysisError(Exception):
    """Raised by _run_parallel() when it must abort (cancellation or a shard
    failure) after some shards already completed. Carries games_new (the
    partial count still worth recording in the runs row) and original (the
    exception or KeyboardInterrupt to actually propagate to the caller), so
    the per-user finally that writes the runs row sees the right count even
    when the parallel path aborts partway."""

    def __init__(self, games_new: int, original: BaseException):
        super().__init__(str(original))
        self.games_new = games_new
        self.original = original


def _resolve_db_path(conn: sqlite3.Connection) -> str | None:
    """The file backing this connection, or None for :memory: (whose `file`
    column is an empty string, not NULL). Parallel workers each need to open
    their own connection to a real file -- an in-memory db (the whole test
    suite, or anyone passing --db :memory:) can't be shared across processes,
    so it always falls back to the serial path regardless of backlog size."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return row["file"] or None
    return None


def _partition_games(games: list[dict], workers: int) -> list[list[dict]]:
    """Split `games` into `workers` shards, round-robin over a stable sort by
    url -- deterministic regardless of input order, matching the ordering
    already proven by scripts/reanalyse_flagged.py's --shard-index/--shard-count.
    May return empty shards when workers > len(games); callers filter those
    out before spawning a process+engine for nothing."""
    ordered = sorted(games, key=lambda g: g.get("url", ""))
    return [ordered[i::workers] for i in range(workers)]


def _analyse_shard(db_path: str, user: str, games: list[dict], engine_path: str,
                   depth: int, threads: int, min_loss: int,
                   progress_queue: multiprocessing.Queue, shard_index: int,
                   cancel_event) -> int:
    """
    Runs in its own worker process (module-level so it's picklable under
    Windows' spawn start method). Opens its own db connection and Stockfish
    engine -- neither can cross a process boundary -- and analyses its shard
    exactly like the serial loop below, reporting progress via a shared
    Queue instead of a direct callback (which also can't cross the boundary).
    """
    conn = open_db(db_path)
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    engine.configure({"Threads": threads})
    new = 0
    try:
        for i, g in enumerate(games, 1):
            if cancel_event.is_set():
                break
            result = analyse_game(g, user, engine, depth, min_loss)
            if result:
                rec, mistakes = result
                save_game(conn, rec, mistakes, depth)
                new += 1
            progress_queue.put((shard_index, i, len(games)))
    finally:
        engine.quit()
        conn.close()
    return new


def _run_parallel(db_path: str, user: str, todo: list[dict], engine_path: str,
                  depth: int, threads: int, min_loss: int, workers: int,
                  quiet: bool, progress_cb: Callable[[str, int, int], None] | None,
                  cancel_event: threading.Event | None) -> int:
    """Analyse one user's backlog across `workers` processes. See
    docs/parallel-analysis-design.md for the full design."""
    shards = [s for s in _partition_games(todo, workers) if s]
    per_worker_threads = max(1, threads // len(shards))
    total = len(todo)

    def report(total_done: int) -> None:
        if not quiet:
            print(f"\r[{user}] analysed {total_done}/{total}", end="", file=sys.stderr)
        if progress_cb is not None:
            progress_cb(user, total_done, total)

    # A plain multiprocessing.Queue/Event can only be pickled at the moment a
    # new Process is spawned with it passed in as a start argument -- it
    # canNOT be pickled through ProcessPoolExecutor's own internal call
    # channel to an already-running pool worker (confirmed the hard way:
    # "Queue objects should only be shared between processes through
    # inheritance"). A Manager's proxies go through the manager process via
    # RPC instead, so they work as ordinary submit() arguments.
    with multiprocessing.Manager() as manager:
        mp_cancel = manager.Event()
        mp_queue = manager.Queue()
        progress_state = [0] * len(shards)

        def drain_queue() -> None:
            while True:
                try:
                    shard_index, i, _ = mp_queue.get_nowait()
                except queue.Empty:
                    return
                progress_state[shard_index] = i

        with ProcessPoolExecutor(max_workers=len(shards)) as executor:
            futures = {
                executor.submit(_analyse_shard, db_path, user, shard, engine_path, depth,
                                per_worker_threads, min_loss, mp_queue, i, mp_cancel): i
                for i, shard in enumerate(shards)
            }
            try:
                while not all(f.done() for f in futures):
                    wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
                    drain_queue()
                    report(sum(progress_state))
                    if cancel_event is not None and cancel_event.is_set():
                        mp_cancel.set()
            except KeyboardInterrupt as ki:
                mp_cancel.set()
                wait(futures)
                drain_queue()
                partial = sum(f.result() for f in futures if not f.exception())
                raise _ShardAnalysisError(partial, ki)

            drain_queue()
            report(sum(progress_state))

            results = []
            failures = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error(f"[{user}] a parallel shard failed: {exc!r}")
                    failures.append(exc)

    if not quiet and total:
        print(file=sys.stderr)
    if failures:
        raise _ShardAnalysisError(sum(results), failures[0])
    return sum(results)


def run_analysis(conn: sqlite3.Connection, users: list[str], email: str, engine_path: str,
                  depth: int, threads: int, pause: float, *, since: str | None = None,
                  time_class: str | None = None, limit: int | None = None,
                  min_loss: int = INACCURACY, quiet: bool = False,
                  progress_cb: Callable[[str, int, int], None] | None = None,
                  cancel_event: threading.Event | None = None,
                  parallel_threshold: int = DEFAULT_PARALLEL_THRESHOLD,
                  workers: int = DEFAULT_WORKERS) -> None:
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

    When more than `parallel_threshold` games need analysis for a user (and
    `workers > 1`, and `conn` is backed by a real file, not :memory:), that
    user's backlog is split across `workers` separate processes instead of
    analysed one engine at a time -- see docs/parallel-analysis-design.md.
    Below the threshold, behaviour is completely unchanged from before this
    existed: one shared engine, one process, the same as always.

    Raises ChessComError if fetching fails for a user (e.g. no such Chess.com
    username) -- the caller decides how to report that (the CLI exits with
    the message; the web app records it as a failed job). A KeyboardInterrupt
    during fetch/analysis is caught and logged, not re-raised: the run simply
    stops with whatever was saved, matching the CLI's existing "Ctrl+C still
    shows you the report" behavior.
    """
    db_path = _resolve_db_path(conn)
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

            use_parallel = (workers > 1 and len(todo) > parallel_threshold
                            and db_path is not None)

            new = 0
            try:
                if use_parallel:
                    new = _run_parallel(db_path, user, todo, engine_path, depth,
                                        threads, min_loss, workers, quiet,
                                        progress_cb, cancel_event)
                else:
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
            except _ShardAnalysisError as exc:
                new = exc.games_new
                if isinstance(exc.original, KeyboardInterrupt):
                    logger.warning(f"\n[{user}] interrupted. Everything analysed so "
                                   f"far is saved.")
                    raise exc.original from None
                raise exc.original
            finally:
                if todo and not quiet and not use_parallel:
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

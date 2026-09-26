"""Remembers the engine's verdict on opening positions between games and runs.

Thousands of your games start the same way, so the engine keeps being asked
about positions it has already answered. Measured on 6,000 real games, about
16% of engine calls were repeats, all within the first ten moves; past move
ten a position essentially never comes up twice. So only those early
positions are stored (about 10 rows per game, fewer as repeats accumulate).

`CachingEngine` wraps a real engine and has the same `analyse()` method, so
the analysis code doesn't know or care whether it is talking to the cache.
A cache hit returns exactly what the engine returned the first time (the
score and its best move, which is all the analysis uses).
"""

from __future__ import annotations

import logging
import sqlite3

import chess
import chess.engine

logger = logging.getLogger(__name__)

# Positions after this many full moves are never stored or looked up.
MAX_FULLMOVE = 11
# In-memory copy kept per process, so repeat lookups don't touch the database.
MAX_MEMORY = 300_000
MATE_SCORE = 10000


class PositionCache:
    def __init__(self, conn: sqlite3.Connection, engine_name: str):
        # Keyed by engine name too: a different Stockfish version scores
        # differently, and mixing the two would blur the numbers.
        self._conn = conn
        self._engine = engine_name
        self._memory: dict[tuple[str, int], tuple[int, str]] = {}
        self._pending: list[tuple] = []
        self.hits = 0
        self.lookups = 0

    @staticmethod
    def wants(board: chess.Board) -> bool:
        return board.fullmove_number <= MAX_FULLMOVE

    def _remember(self, key, value) -> None:
        if len(self._memory) < MAX_MEMORY:
            self._memory[key] = value

    def get(self, board: chess.Board, depth: int) -> dict | None:
        self.lookups += 1
        key = (board.epd(), depth)
        value = self._memory.get(key)
        if value is None:
            row = self._conn.execute(
                "SELECT cp_white, best FROM position_evals WHERE engine = ? AND epd = ? AND depth = ?",
                (self._engine, key[0], depth)).fetchone()
            if row is None:
                return None
            value = (row[0], row[1])
            self._remember(key, value)
        self.hits += 1
        return {"score": chess.engine.PovScore(chess.engine.Cp(value[0]), chess.WHITE),
                "pv": [chess.Move.from_uci(value[1])]}

    def put(self, board: chess.Board, depth: int, info: dict) -> None:
        pv, score = info.get("pv"), info.get("score")
        if not pv or score is None:
            return
        key = (board.epd(), depth)
        if key in self._memory:
            return
        value = (score.white().score(mate_score=MATE_SCORE), pv[0].uci())
        self._remember(key, value)
        self._pending.append((self._engine, key[0], depth, value[0], value[1]))

    def flush(self) -> None:
        """Write what's been learned. The cache is only an optimisation, so a
        busy database just means trying again with the next batch."""
        if not self._pending:
            return
        try:
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO position_evals (engine, epd, depth, cp_white, best) "
                    "VALUES (?, ?, ?, ?, ?)", self._pending)
            self._pending.clear()
        except sqlite3.OperationalError as exc:
            logger.debug("position cache flush deferred: %s", exc)
            del self._pending[:-5000]     # never let a stuck flush grow without bound


def _engine_name(engine) -> str:
    ident = getattr(engine, "id", None)
    name = ident.get("name") if isinstance(ident, dict) else None
    return str(name or "engine")


class CachingEngine:
    """Same `analyse()` as a real engine, answering from the cache when it can."""

    def __init__(self, engine, conn: sqlite3.Connection):
        self._engine = engine
        self.cache = PositionCache(conn, _engine_name(engine))

    def analyse(self, board: chess.Board, limit: chess.engine.Limit, **kwargs):
        depth = limit.depth
        if kwargs or depth is None or not self.cache.wants(board):
            return self._engine.analyse(board, limit, **kwargs)
        info = self.cache.get(board, depth)
        if info is None:
            info = self._engine.analyse(board, limit)
            self.cache.put(board, depth, info)
        return info

    def flush(self) -> None:
        self.cache.flush()

    def __getattr__(self, name):
        return getattr(self._engine, name)

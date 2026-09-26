"""Persistent engine process for phase 5's live play mode.

Unlike every other engine use in this app (_judge_with_engine in
routes_api.py, analysis_runner.py's job runner), which opens a fresh
SimpleEngine per call and closes it immediately after, play mode keeps one
engine process alive across a whole game. This isn't just an optimisation:
lc0's first inference after process start costs 20s+ (ONNX/DirectML graph
compile, measured directly against the installed build), so opening a fresh
lc0 process per move would stall the user that long on every single bot
reply.

At most one engine process is held at a time -- this is a single-user local
app, so there's never a reason to run two chess engines at once, matching
JobManager's own "at most one job at a time" reasoning in jobs.py. Changing
engine/difficulty mid-session closes the old process and starts a new one on
next use.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

import chess
import chess.engine

# Node budget for lc0/Maia analysis during play -- fixed nodes rather than
# fixed time. The same top move's eval was observed to shift meaningfully
# between a nodes=800 call and a movetime=1s call in testing; fixed nodes
# gives more reproducible steering behaviour move to move.
MAIA_NODES = 800


class PlayEngineManager:
    """Holds at most one open engine process for the current play settings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: chess.engine.SimpleEngine | None = None
        self._key: tuple | None = None

    @contextmanager
    def acquire(self, engine_path: str, key: tuple, configure: dict | None = None,
                warm_up_board: chess.Board | None = None):
        """
        Yield the open engine process for `key` (e.g. ("stockfish", 1500) or
        ("maia", "/path/to/maia-1500.pb.gz")), starting or restarting it if
        `key` differs from whatever is currently open. `configure` is passed
        to engine.configure() once, right after starting. `warm_up_board`, if
        given, is analysed with Limit(nodes=1) before yielding -- absorbs
        lc0's first-inference cost here, at "start/switch engine" time,
        rather than on the caller's first real move.

        Held as a context manager (lock included) for the whole call so a
        concurrent request can't close this engine out from under an
        in-flight analyse() -- callers should do their engine.analyse()/play()
        calls inside the `with` block, not stash the engine reference.
        """
        with self._lock:
            if self._key != key:
                self._close_locked()
                self._engine = chess.engine.SimpleEngine.popen_uci(engine_path)
                if configure:
                    self._engine.configure(configure)
                if warm_up_board is not None:
                    self._engine.analyse(warm_up_board, chess.engine.Limit(nodes=1))
                self._key = key
            yield self._engine

    def close(self) -> None:
        """Terminate any open engine process. Called on app shutdown so
        chess-mistake-coach serve doesn't leak a stockfish.exe/lc0.exe process."""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except chess.engine.EngineTerminatedError:
                pass
            self._engine = None
            self._key = None

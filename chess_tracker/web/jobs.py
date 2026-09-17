"""Background job management for fetch+analyse runs.

A single JobManager instance (held on app.state) tracks at most one
in-flight job at a time -- matching how the CLI already only ever runs one
Stockfish process per invocation, and avoiding two engine processes
competing for CPU and writing to the same db file at once. Jobs run in a
plain threading.Thread; there's no need for Celery/Redis for a single-user
local tool.

No sqlite3.Connection is ever shared across threads: the job thread opens
its own connection via open_db() for the run's duration and closes it when
done, exactly like the CLI does. The only state shared between threads is
the JobStatus dict below, behind a Lock.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..analysis import INACCURACY
from ..analysis_runner import run_analysis
from ..chesscom import ChessComError
from ..db import open_db


class JobAlreadyRunningError(Exception):
    """Raised by start_job() when a job is already in flight."""


# The estimate every "how long will this take" figure in the app is built
# from -- see analysis_runner's own 20-40s/game note; 30 sits in the middle.
SECONDS_PER_GAME = 30

# Caps the very first analysis run so onboarding doesn't leave someone
# staring at a progress bar through their whole game history. Matches
# analysis_runner's own DEFAULT_PARALLEL_THRESHOLD exactly (no special-
# casing needed): a new user with enough history to hit this cap gets it
# split across every worker the machine has, same as any other job this
# size -- roughly 5 minutes at DEFAULT_WORKERS=8 (100 games / 8 * 30s).
# Someone with fewer games than this just gets everything they have,
# uncapped and serial, which is already fast.
FIRST_RUN_GAME_LIMIT = 100

# Above this many games needing analysis in a single-user job, the progress
# page offers a choice (keep going in the background / narrow the date
# range) instead of just grinding through a plain progress bar. Below
# analysis_runner's own parallel_threshold (100), so most jobs that cross
# this still run serially -- the choice screen's own estimate accounts for
# that, and for the cases at or above 100 that don't.
BIG_UPDATE_THRESHOLD = 12


@dataclass
class JobStatus:
    id: str
    users: list[str]
    state: str = "queued"  # queued | running | done | error | cancelled
    current_user: str | None = None
    current_index: int = 0
    current_total: int = 0
    per_user: dict = field(default_factory=dict)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "state": self.state, "users": self.users,
            "current_user": self.current_user, "current_index": self.current_index,
            "current_total": self.current_total, "per_user": self.per_user,
            "error": self.error, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """Tracks at most one running fetch+analyse job at a time."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._jobs: dict[str, JobStatus] = {}
        self._active_job_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}

    def start_job(self, users: list[str], email: str, engine_path: str, depth: int,
                  threads: int, pause: float, *, since: str | None = None,
                  time_class: str | None = None, limit: int | None = None,
                  min_loss: int = INACCURACY) -> JobStatus:
        with self._lock:
            if self._active_job_id is not None:
                raise JobAlreadyRunningError(
                    "A fetch/analyse job is already running. Wait for it to "
                    "finish (or cancel it) before starting another.")
            job_id = uuid.uuid4().hex
            status = JobStatus(id=job_id, users=list(users),
                                per_user={u: {} for u in users})
            self._jobs[job_id] = status
            self._active_job_id = job_id
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event

        thread = threading.Thread(
            target=self._run, name=f"chess-tracker-job-{job_id}",
            args=(job_id, users, email, engine_path, depth, threads, pause, cancel_event),
            kwargs=dict(since=since, time_class=time_class, limit=limit, min_loss=min_loss),
            daemon=True)
        thread.start()
        return status

    def _run(self, job_id, users, email, engine_path, depth, threads, pause,
              cancel_event, **kwargs) -> None:
        status = self._jobs[job_id]
        with self._lock:
            status.state = "running"
            status.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def progress_cb(user: str, i: int, total: int) -> None:
            with self._lock:
                status.current_user = user
                status.current_index = i
                status.current_total = total
                status.per_user.setdefault(user, {})["analysed"] = i
                status.per_user[user]["todo"] = total

        conn = open_db(self._db_path)
        try:
            run_analysis(conn, users, email, engine_path, depth, threads, pause,
                         progress_cb=progress_cb, cancel_event=cancel_event, **kwargs)
            with self._lock:
                status.state = "cancelled" if cancel_event.is_set() else "done"
        except ChessComError as exc:
            with self._lock:
                status.state = "error"
                status.error = str(exc)
        except Exception as exc:
            # Defensive: a background thread that dies with an uncaught
            # exception would otherwise vanish silently, leaving
            # _active_job_id permanently set and blocking every future job.
            with self._lock:
                status.state = "error"
                status.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            conn.close()
            with self._lock:
                status.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def get_status(self, job_id: str) -> dict | None:
        with self._lock:
            status = self._jobs.get(job_id)
            return None if status is None else status.to_dict()

    def get_active_job_id(self) -> str | None:
        """The id of the currently in-flight job, if any -- lets any page's
        JS discover "is something running right now" without already
        knowing a job_id (e.g. the sticky progress bar shown on every
        page)."""
        with self._lock:
            return self._active_job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

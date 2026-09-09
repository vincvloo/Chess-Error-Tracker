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

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

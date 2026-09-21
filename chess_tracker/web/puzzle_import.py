"""Background job management for downloading/importing/topping-up the
Lichess puzzle database (phase: puzzles) -- the "Get more puzzles" button
on /puzzles.

Same "single in-flight job, plain threading.Thread, cancel via
threading.Event" shape as jobs.JobManager, but a separate class: its
start_job() takes a puzzle rating/theme filter, not a chess.com fetch, too
different a shape to share one manager cleanly. Not reused directly; the
pattern is.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..db import open_db
from ..puzzles import (DEFAULT_MIN_PLAYS, PuzzleImportCancelled, download_puzzle_source,
                       find_puzzle_source, import_puzzles)


class PuzzleImportAlreadyRunningError(Exception):
    """Raised by start_job() when an import/top-up is already in flight."""


@dataclass
class PuzzleImportStatus:
    id: str
    state: str = "queued"  # queued | downloading | importing | done | error | cancelled
    bytes_downloaded: int = 0
    bytes_total: int | None = None
    scanned: int = 0
    imported: int = 0
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "state": self.state,
            "bytesDownloaded": self.bytes_downloaded, "bytesTotal": self.bytes_total,
            "scanned": self.scanned, "imported": self.imported,
            "error": self.error, "startedAt": self.started_at, "finishedAt": self.finished_at,
        }


class PuzzleImportManager:
    """Tracks at most one running puzzle download/import at a time."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._jobs: dict[str, PuzzleImportStatus] = {}
        self._active_job_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}

    def start_job(self, *, min_rating: int | None, max_rating: int | None,
                  min_plays: int | None = DEFAULT_MIN_PLAYS,
                  themes: list[str] | None = None) -> PuzzleImportStatus:
        with self._lock:
            if self._active_job_id is not None:
                raise PuzzleImportAlreadyRunningError(
                    "A puzzle import is already running. Wait for it to finish "
                    "(or cancel it) before starting another.")
            job_id = uuid.uuid4().hex
            status = PuzzleImportStatus(id=job_id)
            self._jobs[job_id] = status
            self._active_job_id = job_id
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event

        thread = threading.Thread(
            target=self._run, name=f"chess-tracker-puzzle-import-{job_id}",
            args=(job_id, min_rating, max_rating, min_plays, themes, cancel_event),
            daemon=True)
        thread.start()
        return status

    def _run(self, job_id: str, min_rating: int | None, max_rating: int | None,
             min_plays: int | None, themes: list[str] | None,
             cancel_event: threading.Event) -> None:
        status = self._jobs[job_id]
        with self._lock:
            status.state = "downloading"
            status.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            source_path = find_puzzle_source()
            if source_path is None:
                def dl_progress(downloaded: int, total: int | None) -> None:
                    with self._lock:
                        status.bytes_downloaded = downloaded
                        status.bytes_total = total
                source_path = download_puzzle_source(progress_cb=dl_progress,
                                                      cancel_event=cancel_event)

            with self._lock:
                status.state = "importing"

            def import_progress(scanned: int, imported: int) -> None:
                with self._lock:
                    status.scanned = scanned
                    status.imported = imported

            conn = open_db(self._db_path)
            try:
                import_puzzles(conn, source_path, min_rating=min_rating, max_rating=max_rating,
                              min_plays=min_plays, themes=themes,
                              progress_cb=import_progress, cancel_event=cancel_event)
            finally:
                conn.close()

            with self._lock:
                status.state = "done"
        except PuzzleImportCancelled:
            with self._lock:
                status.state = "cancelled"
        except Exception as exc:
            # Defensive: a background thread that dies with an uncaught
            # exception would otherwise vanish silently, leaving
            # _active_job_id permanently set and blocking every future job.
            with self._lock:
                status.state = "error"
                status.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            with self._lock:
                status.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def get_status(self, job_id: str) -> dict | None:
        with self._lock:
            status = self._jobs.get(job_id)
            return None if status is None else status.to_dict()

    def get_active_job_id(self) -> str | None:
        with self._lock:
            return self._active_job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

"""FastAPI application factory for the local web app."""

from __future__ import annotations

from fastapi import FastAPI

from .jobs import JobManager


def create_app(db_path: str, engine_path: str | None = None) -> FastAPI:
    """
    Build the FastAPI app. `db_path` is the SQLite database every route and
    the background job thread will open its own connection against (never
    shared across threads). `engine_path`, if given, skips auto-detecting
    Stockfish on every job start.
    """
    app = FastAPI(title="Chess Error Tracker")
    app.state.db_path = db_path
    app.state.engine_path = engine_path
    app.state.jobs = JobManager(db_path)
    # Populated lazily by the /api/update/check route -- a real git fetch,
    # so cached rather than re-run on every single home-page load.
    app.state.update_cache = {"checked_at": None, "result": None}

    from . import routes_api, routes_pages
    app.include_router(routes_pages.router)
    app.include_router(routes_api.router)

    return app

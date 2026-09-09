"""HTML page routes: home (tracked users + start-analysis form), job
progress, and the live dashboard."""

from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..analysis import INACCURACY
from ..cli import DEFAULT_CONFIG_PATH, load_config
from ..db import open_db
from ..engine import ENGINE_HELP, find_engine
from ..html_export import render_dashboard_html
from ..reports import user_summaries
from .jobs import JobAlreadyRunningError

router = APIRouter()
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _home_context(request: Request, error: str | None = None) -> dict:
    conn = open_db(request.app.state.db_path)
    try:
        users = user_summaries(conn)
    finally:
        conn.close()
    config = load_config(DEFAULT_CONFIG_PATH, required=False)
    return {
        "users": users,
        "default_email": config.get("email", ""),
        "default_depth": config.get("depth", 14),
        "default_threads": config.get("threads", 2),
        "default_pause": config.get("pause", 0.6),
        "default_min_loss": config.get("min_loss", INACCURACY),
        "error": error,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", _home_context(request))


@router.post("/jobs")
def start_job(
    request: Request,
    user: str = Form(...),
    email: str = Form(""),
    depth: int = Form(14),
    threads: int = Form(2),
    pause: float = Form(0.6),
    min_loss: int = Form(INACCURACY),
    since: str = Form(""),
    time_class: str = Form(""),
    limit: str = Form(""),
):
    users = [u.strip() for u in user.split(",") if u.strip()]
    if not users:
        return templates.TemplateResponse(
            request, "home.html",
            _home_context(request, "Enter at least one Chess.com username."),
            status_code=400)
    if not email.strip():
        return templates.TemplateResponse(
            request, "home.html",
            _home_context(request, "Email is required to fetch from Chess.com."),
            status_code=400)

    engine_path = request.app.state.engine_path or find_engine()
    if not engine_path or not os.path.isfile(engine_path):
        return templates.TemplateResponse(
            request, "home.html", _home_context(request, ENGINE_HELP), status_code=400)

    try:
        status = request.app.state.jobs.start_job(
            users, email.strip(), engine_path, depth, threads, pause,
            since=since.strip() or None,
            time_class=time_class.strip() or None,
            limit=int(limit) if limit.strip() else None,
            min_loss=min_loss)
    except JobAlreadyRunningError as exc:
        return templates.TemplateResponse(
            request, "home.html", _home_context(request, str(exc)), status_code=409)

    return RedirectResponse(f"/jobs/{status.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_progress_page(request: Request, job_id: str):
    if request.app.state.jobs.get_status(job_id) is None:
        return HTMLResponse("<p>No such job.</p>", status_code=404)
    return templates.TemplateResponse(request, "progress.html", {"job_id": job_id})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, users: str = ""):
    user_list = [u.strip() for u in users.split(",") if u.strip()]
    if not user_list:
        return HTMLResponse("<p>No users selected.</p>", status_code=400)

    conn = open_db(request.app.state.db_path)
    try:
        result = render_dashboard_html(conn, user_list)
    finally:
        conn.close()

    if result is None:
        return HTMLResponse("<p>No stored games for these users yet.</p>")
    html, _ = result
    return HTMLResponse(html)

"""JSON API routes: job status and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")


@router.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    status = request.app.state.jobs.get_status(job_id)
    if status is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return status


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    ok = request.app.state.jobs.cancel(job_id)
    if not ok:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"cancelled": True}

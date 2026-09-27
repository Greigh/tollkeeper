"""FastAPI backend for the Tollkeeper app.

Serves the bundled React frontend, adapter/quota JSON, background run
jobs with SSE event streaming, capsules, config, and the secrets store.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import tomllib
import uuid
import webbrowser
from contextlib import asynccontextmanager, closing
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.capsule import list_capsules, resume_brief
from ..core.ledger import _conn
from ..core.secrets import delete_secret, list_secrets, load_secrets, set_secret
from ..service import RouterService, config_path, load_config

def _frontend_dist() -> Path:
    """Locate bundled static assets, whether running from source or a PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = Path(meipass) / "router" / "app"
        else:
            base = Path(__file__).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    bundled = base / "static"
    if bundled.exists():
        return bundled
    # Fall back to a frontend/dist sibling when running from source without a build
    return Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


FRONTEND_DIST = _frontend_dist()
_jobs: dict[str, dict[str, object]] = {}


def _job_queue(job: dict[str, object]) -> queue.Queue:
    return job["events"]  # type: ignore[return-value]


class RunPayload(BaseModel):
    """Body for /api/plan and /api/runs."""

    task: str = Field(min_length=1, max_length=100_000)
    adapter: str | None = None
    model: str | None = None
    resume: str | None = None
    workdir: str | None = None


class ConfigPayload(BaseModel):
    """Body for PUT /api/config — raw TOML content."""

    content: str = Field(max_length=100_000)


class SecretPayload(BaseModel):
    """Body for PUT /api/secrets."""

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load persisted secrets into the env and initialize the ledger."""
    load_secrets()
    with closing(_conn()):
        pass
    yield


app = FastAPI(title="Tollkeeper", version="0.2.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    """Liveness probe."""
    return {"status": "ok", "version": "0.2.0"}


@app.get("/api/overview")
def overview():
    """30-day metrics, recent runs, and per-adapter spend from the ledger."""
    with closing(_conn()) as connection:
        summary = connection.execute(
            """SELECT COUNT(*), COALESCE(SUM(actual_cost_usd), 0),
                      COALESCE(SUM(CASE WHEN est_cost_usd > COALESCE(actual_cost_usd, 0)
                          THEN est_cost_usd - COALESCE(actual_cost_usd, 0) ELSE 0 END), 0)
               FROM runs WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')"""
        ).fetchone()
        recent = connection.execute(
            """SELECT task, task_class, adapter, model, actual_cost_usd, ts
               FROM runs WHERE dry_run=0 ORDER BY ts DESC LIMIT 25"""
        ).fetchall()
        spend = connection.execute(
            """SELECT adapter, COUNT(*), COALESCE(SUM(actual_cost_usd), 0)
               FROM runs WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')
               GROUP BY adapter ORDER BY 3 DESC"""
        ).fetchall()
    return {
        "metrics": {"runs": summary[0], "spend": summary[1], "savings": summary[2]},
        "recentRuns": [
            {"task": row[0], "taskClass": row[1], "adapter": row[2], "model": row[3],
             "cost": row[4] or 0.0, "timestamp": row[5]} for row in recent
        ],
        "spendByAdapter": [
            {"adapter": row[0], "runs": row[1], "spend": row[2]} for row in spend
        ]
    }


def _adapter_to_dict(adapter, *, force_probe: bool = False) -> dict:
    health_report = adapter.health(force_probe=force_probe)
    quota = health_report.quota
    quota_dict: dict[str, object] = {
        "state": quota.state.value,
        "detail": quota.detail,
        "remainingPercent": quota.remaining_percent,
        "resetAt": quota.reset_at.isoformat() if quota.reset_at else None,
        "observedAt": quota.observed_at.isoformat(),
    }
    if quota.extra:
        quota_dict["extra"] = quota.extra
    return {
        "name": adapter.name,
        "kind": adapter.kind,
        "reachable": health_report.reachable,
        "note": health_report.note,
        "quota": quota_dict,
    }


@app.get("/api/adapters")
async def adapters():
    """Every adapter with availability and its last quota report."""
    service = RouterService()
    return {"adapters": await asyncio.to_thread(lambda: [_adapter_to_dict(a) for a in service.adapters])}


@app.post("/api/adapters/{name}/probe")
async def probe_adapter(name: str):
    """Force a fresh quota probe for one adapter."""
    service = RouterService()
    adapter = next((a for a in service.adapters if a.name == name), None)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"adapter {name} not found")
    return await asyncio.to_thread(lambda: _adapter_to_dict(adapter, force_probe=True))


@app.post("/api/plan")
async def plan(payload: RunPayload):
    """Return the routing decision for a task without executing it."""
    try:
        decision = await asyncio.to_thread(
            RouterService().plan, payload.task, payload.adapter, payload.model)
        return asdict(decision)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs", status_code=202)
def start_run(payload: RunPayload):
    """Queue a run on a worker thread; events stream via /api/runs/{id}/events."""
    job_id = uuid.uuid4().hex
    events: queue.Queue = queue.Queue()
    job: dict[str, object] = {"id": job_id, "state": "queued", "events": events, "result": None, "error": None}
    _jobs[job_id] = job

    def emit(event: str, data):
        """Push an SSE event onto this job's queue."""
        events.put({"event": event, "data": data})

    def worker():
        """Run the job, streaming output/state events until completion."""
        job["state"] = "running"
        emit("state", {"state": "running"})
        try:
            result = RouterService().execute(
                payload.task, payload.adapter, payload.model, payload.resume,
                payload.workdir, lambda channel, text: emit("output", {"channel": channel, "text": text})
            )
            job["result"] = result
            job["state"] = "completed"
            emit("completed", result)
        except Exception as exc:
            job["error"] = str(exc)
            job["state"] = "failed"
            emit("failed", {"error": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True, name=f"router-run-{job_id[:8]}").start()
    return {"id": job_id, "state": "queued"}


@app.get("/api/runs/{job_id}")
def run_status(job_id: str):
    """Pollable job state/result/error."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="run not found")
    return {key: job[key] for key in ("id", "state", "result", "error") if key in job}


@app.get("/api/runs/{job_id}/events")
def run_events(job_id: str):
    """SSE stream of a job's events until it finishes."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="run not found")

    def stream():
        """Yield SSE frames until the worker's sentinel."""
        events_queue: queue.Queue = _job_queue(job)
        while True:
            event = events_queue.get()
            if event is None:
                break
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/capsules")
def capsules():
    """List stored session capsules."""
    return {"capsules": list_capsules()}


@app.get("/api/capsules/brief")
def capsule_brief(path: str):
    """Resume brief for a capsule; path must be one we recorded."""
    allowed = {item["path"] for item in list_capsules()}
    if path not in allowed:
        raise HTTPException(status_code=404, detail="capsule not found")
    return {"brief": resume_brief(path)}


@app.get("/api/config")
def get_config():
    """Raw + parsed router config.toml."""
    path = config_path()
    return {"path": str(path), "content": path.read_text() if path.exists() else "", "parsed": load_config()}


@app.put("/api/config")
def put_config(payload: ConfigPayload):
    """Validate TOML and atomically replace config.toml."""
    try:
        tomllib.loads(payload.content)
    except tomllib.TOMLDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid TOML: {exc}") from exc
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload.content)
    os.replace(temporary, path)
    return {"path": str(path), "saved": True}


@app.get("/api/secrets")
def get_secrets():
    """Masked secret names/values (never raw)."""
    return {"secrets": list_secrets()}


@app.put("/api/secrets")
def put_secret(payload: SecretPayload):
    """Store a secret in the secrets file."""
    set_secret(payload.name, payload.value)
    return {"name": payload.name, "saved": True}


@app.delete("/api/secrets/{name}")
def remove_secret(name: str):
    """Delete a stored secret."""
    delete_secret(name)
    return {"name": name, "deleted": True}


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        """SPA catch-all: serve static files, else index.html."""
        requested = FRONTEND_DIST / path
        if path and requested.is_file() and requested.resolve().is_relative_to(FRONTEND_DIST.resolve()):
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIST / "index.html")


def start_app(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    """Serve the app via uvicorn, optionally opening a browser tab."""
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


def main_app() -> None:
    """CLI wrapper: parse --host/--port/--no-open and start the app."""
    import argparse
    parser = argparse.ArgumentParser(prog="router-app",
                                     description="Start the Tollkeeper application")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="port to serve on (default: 8080)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()
    start_app(host=args.host, port=args.port, open_browser=not args.no_open)

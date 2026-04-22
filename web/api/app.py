"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.job_queue import GPUJob, JobStatus, JobType
from backend.project import projects_root

from . import persist
from .database import get_storage
from .deps import get_service, get_state
from .metrics import router as metrics_router
from .reaper import start_reaper
from .routes import admin, auth, clips, jobs, nodes, nodes_mgmt, orgs, presets, preview, projects, system, upload
from .status import router as status_router
from .worker import start_worker
from .ws import manager, websocket_endpoint

logger = logging.getLogger(__name__)
_app_start_time = 0.0


def _track_consumed_credits(queue) -> None:
    """Track GPU-seconds consumed by the most recently completed job (CRKY-6)."""
    import time as _t

    try:
        history = queue.history_snapshot
        if not history:
            return
        # Most recent completed job
        job = history[-1]
        if job.status.value != "completed":
            logger.info(f"Credit tracking: job {job.id} status={job.status.value}, skipping")
            return
        if not job.org_id:
            logger.info(f"Credit tracking: job {job.id} has no org_id, skipping")
            return
        if not job.started_at:
            logger.info(f"Credit tracking: job {job.id} has no started_at, skipping")
            return
        elapsed = _t.time() - job.started_at
        if elapsed <= 0:
            return
        from .gpu_credits import add_consumed

        add_consumed(job.org_id, elapsed)
        logger.info(f"Credit tracking: {elapsed:.1f}s consumed by org {job.org_id} (job {job.id})")
    except Exception as e:
        logger.debug(f"Credit tracking failed: {e}")


def _save_history_snapshot(queue) -> None:
    """Serialize and persist the job history."""
    history = queue.history_snapshot
    get_storage().save_job_history(
        [
            {
                "id": j.id,
                "job_type": j.job_type.value,
                "clip_name": j.clip_name,
                "status": j.status.value,
                "error_message": j.error_message,
                "claimed_by": j.claimed_by,
                "current_frame": j.current_frame,
                "total_frames": j.total_frames,
                "started_at": j.started_at,
                "completed_at": j.completed_at,
                "submitted_by": j.submitted_by,
                "org_id": j.org_id,
            }
            for j in history
        ]
    )


# Resolve clips directory from env or default to Projects/
CLIPS_DIR = os.environ.get("CK_CLIPS_DIR", "").strip()


def _resolve_clips_dir() -> str:
    if CLIPS_DIR:
        return os.path.abspath(CLIPS_DIR)
    return projects_root()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: detect device, start worker. Shutdown: stop worker."""
    import time as _time

    global _app_start_time
    _app_start_time = _time.time()
    clips_dir = _resolve_clips_dir()
    os.makedirs(clips_dir, exist_ok=True)

    clips.set_clips_dir(clips_dir)
    preview.set_clips_dir(clips_dir)
    persist.init(clips_dir)

    # Run database migrations if Postgres is configured (CRKY-49)
    from .migrate import run_migrations

    run_migrations()

    # Set base clips dir for org isolation (CRKY-10)
    from .org_isolation import set_base_clips_dir

    set_base_clips_dir(clips_dir)

    # Restore persisted settings before starting workers
    from .worker import restore_settings

    restore_settings()

    service = get_service()
    device = service.detect_device()
    logger.info(f"Device: {device}, Clips dir: {clips_dir}")

    loop = asyncio.get_running_loop()
    manager.set_loop(loop)

    # Start Redis pub/sub subscriber for cross-instance WS fan-out (CRKY-105 Phase 3)
    from .redis_pubsub import start_subscriber, stop_subscriber

    await start_subscriber()

    state = get_state()
    queue = state.jobs

    # Restore job history from storage
    saved_history = get_storage().load_job_history()
    if saved_history:
        restored: list[GPUJob] = []
        for jd in saved_history:
            job = GPUJob(
                job_type=JobType(jd["job_type"]),
                clip_name=jd["clip_name"],
                params=jd.get("params", {}),
            )
            job.id = jd["id"]
            job.status = JobStatus(jd["status"])
            job.error_message = jd.get("error_message")
            job.claimed_by = jd.get("claimed_by")
            job.current_frame = jd.get("current_frame", 0)
            job.total_frames = jd.get("total_frames", 0)
            job.started_at = jd.get("started_at", 0)
            job.completed_at = jd.get("completed_at", 0)
            job.submitted_by = jd.get("submitted_by")
            job.org_id = jd.get("org_id")
            restored.append(job)
        queue.restore_history(restored)
        logger.info(f"Restored {len(saved_history)} jobs from history")

    # Save history, track credits, and fire webhooks on job completion
    def _persist_history(_clip_name: str) -> None:
        _save_history_snapshot(queue)
        _track_consumed_credits(queue)
        # Fire webhook (CRKY-31)
        try:
            history = queue.history_snapshot
            if history:
                job = history[-1]
                if job.org_id:
                    from .webhooks import fire_event

                    fire_event(
                        "job_completed",
                        job.org_id,
                        {
                            "job_id": job.id,
                            "clip_name": job.clip_name,
                            "job_type": job.job_type.value,
                            "frames": job.total_frames,
                        },
                    )
        except Exception:
            pass

    def _persist_history_err(_clip_name: str, _error: str) -> None:
        _save_history_snapshot(queue)
        # Fire webhook for failure (CRKY-31)
        try:
            history = queue.history_snapshot
            if history:
                job = history[-1]
                if job.org_id:
                    from .webhooks import fire_event

                    fire_event(
                        "job_failed",
                        job.org_id,
                        {
                            "job_id": job.id,
                            "clip_name": job.clip_name,
                            "job_type": job.job_type.value,
                            "error": _error,
                        },
                    )
        except Exception:
            pass

    queue.on_completion = _persist_history
    queue.on_error = _persist_history_err

    worker_thread, stop_event = start_worker(service, queue, clips_dir)
    reaper_thread = start_reaper(queue, state.nodes, stop_event)

    # One-shot heal for approved users who lost their personal org
    # (manual prune, earlier dedup pass, etc). Idempotent, cheap.
    try:
        from .orgs import heal_missing_personal_orgs

        heal_missing_personal_orgs()
    except Exception:
        logger.warning("Personal org heal failed on startup", exc_info=True)

    # Start clip retention cleanup daemon (CRKY-115)
    from .clip_retention import start_cleanup

    start_cleanup(clips_dir, stop_event)

    # Start monthly credit grant daemon (CRKY-185)
    from .credit_scheduler import start_grant_scheduler

    start_grant_scheduler(stop_event)

    app.state.clips_dir = clips_dir
    app.state.worker_thread = worker_thread
    app.state.reaper_thread = reaper_thread
    app.state.stop_event = stop_event

    # Start status page sampler (CRKY-51)
    from .status import start_status_sampler

    start_status_sampler(interval=60)

    yield

    # Stop status sampler
    from .status import stop_status_sampler

    stop_status_sampler()

    await stop_subscriber()

    stop_event.set()
    worker_thread.join(timeout=5)
    reaper_thread.join(timeout=5)
    logger.info("Worker and reaper threads joined")


def create_app() -> FastAPI:
    """Application factory — call with uvicorn --factory."""
    # Structured logging (CRKY-50): JSON or text based on CK_LOG_FORMAT
    from .logging_config import configure_logging

    configure_logging()

    # Initialize Sentry error monitoring (no-op if CK_SENTRY_DSN not set)
    from .sentry_setup import init_sentry

    init_sentry()

    # OpenAPI documentation configuration (CRKY-32)
    from .openapi_config import API_DESCRIPTION, API_VERSION, DOCS_PUBLIC, TAG_METADATA

    app = FastAPI(
        title="CorridorKey API",
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=TAG_METADATA,
        docs_url="/docs" if DOCS_PUBLIC else None,
        redoc_url="/redoc" if DOCS_PUBLIC else None,
        openapi_url="/openapi.json" if DOCS_PUBLIC else None,
        lifespan=lifespan,
    )

    # Auth middleware (must be added before GZip so it runs first)
    from .auth import AUTH_ENABLED, JWT_SECRET, AuthMiddleware

    if AUTH_ENABLED and not JWT_SECRET:
        raise RuntimeError(
            "CK_AUTH_ENABLED is true but CK_JWT_SECRET is not set. "
            "An empty JWT secret allows forged tokens. Set CK_JWT_SECRET "
            "to the same value as your Supabase JWT_SECRET."
        )

    # Middleware execution order (Starlette LIFO: last added = outermost):
    # Request → RateLimit → Auth → GZip → CORS → APIVersion → Route
    # RateLimit must be outermost (added last) but needs user context from Auth.
    # Since Auth runs after RateLimit in LIFO, we can't get user in RateLimit.
    #
    # Solution: RateLimit added FIRST (innermost), Auth second, GZip last.
    # Execution: GZip → CORS → APIVersion→ Auth (injects user) → RateLimit (reads user) → Route
    from fastapi.middleware.cors import CORSMiddleware

    from .rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)  # innermost — runs after auth
    app.add_middleware(AuthMiddleware)  # middle — injects user context
    app.add_middleware(GZipMiddleware, minimum_size=1000)  # outermost — compression
    cors_origins = [
        origin.strip()
        for origin in os.environ.get(
            "CK_CORS_ORIGINS",
            "https://corridorkey.cloud,http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    ]
    file_base = os.environ.get("CKWEB_FILE_BASE", "").strip().rstrip("/")
    if file_base:
        file_origin = file_base if "://" in file_base else f"https://{file_base}"
        if file_origin not in cors_origins:
            cors_origins.append(file_origin)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if AUTH_ENABLED:
        logger.info("Auth enabled — JWT validation active on API routes")

    # API version header middleware (CRKY-32)
    from .api_version import APIVersionMiddleware

    app.add_middleware(APIVersionMiddleware)

    # Protected API docs — when DOCS_PUBLIC=false, serve docs behind JWT auth (CRKY-32)
    if not DOCS_PUBLIC:
        from .docs_routes import mount_protected_docs

        mount_protected_docs(app)
        logger.info("API docs behind auth (set CK_DOCS_PUBLIC=true for open access)")
    else:
        logger.info("API docs publicly accessible at /docs and /redoc")

    # Server version (public)
    @app.get("/api/version")
    def get_version():
        """Return the server version string. No auth required."""
        from .version import API_VERSION, BUILD_COMMIT, VERSION_STRING

        return {"version": VERSION_STRING, "api": API_VERSION, "commit": BUILD_COMMIT}

    # Health check (CRKY-21)
    @app.get("/api/health")
    async def health_check(deep: bool = False):
        """Health check for Docker/load balancers.

        Default (fast): checks worker thread + uptime only. Never blocks.
        With ?deep=true: also checks database, GoTrue, and disk space.
        Docker health checks should use the fast path.
        """
        import time

        from starlette.responses import JSONResponse

        checks: dict = {}
        healthy = True

        # Worker thread (instant check)
        worker_thread = getattr(app.state, "worker_thread", None)
        if worker_thread:
            alive = worker_thread.is_alive()
            checks["worker"] = {"status": "ok" if alive else "error"}
            if not alive:
                healthy = False
        else:
            checks["worker"] = {"status": "skipped"}

        checks["uptime_seconds"] = round(time.time() - _app_start_time, 1)

        # Deep checks — only when explicitly requested (monitoring dashboards)
        if deep:
            import shutil

            # Database
            try:
                storage = get_storage()
                from .database import PostgresBackend

                if isinstance(storage, PostgresBackend):
                    storage.get_setting("_health_check")
                    checks["database"] = {"status": "ok", "backend": "postgres", **storage.pool_stats}
                else:
                    checks["database"] = {"status": "ok", "backend": "json"}
            except Exception as e:
                checks["database"] = {"status": "error", "detail": str(e)}
                healthy = False

            # GoTrue
            gotrue_url = os.environ.get("CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "")).strip()
            if gotrue_url:
                try:
                    import urllib.request

                    req = urllib.request.Request(f"{gotrue_url}/health", method="GET")
                    with urllib.request.urlopen(req, timeout=3):
                        checks["gotrue"] = {"status": "ok"}
                except Exception as e:
                    checks["gotrue"] = {"status": "error", "detail": str(e)}
                    healthy = False

            # Disk
            clips_dir = getattr(app.state, "clips_dir", "")
            if clips_dir and os.path.isdir(clips_dir):
                usage = shutil.disk_usage(clips_dir)
                free_gb = round(usage.free / (1024**3), 1)
                checks["disk"] = {"status": "ok" if free_gb > 1.0 else "warning", "free_gb": free_gb}
                if free_gb < 1.0:
                    healthy = False

        status_code = 200 if healthy else 503
        return JSONResponse(content={"healthy": healthy, "checks": checks}, status_code=status_code)

    # Public banner endpoint (no auth required — shown on landing page)
    @app.get("/api/banner")
    def public_banner():
        from .routes.admin import _get_active_banner

        return _get_active_banner()

    # API routes
    app.include_router(auth.router)
    app.include_router(metrics_router)
    app.include_router(clips.router)
    app.include_router(jobs.router)
    app.include_router(system.router)
    app.include_router(system.weights_router)  # No auth — nodes use CK_AUTH_TOKEN
    app.include_router(preview.router)
    app.include_router(projects.router)
    app.include_router(nodes.router)
    app.include_router(nodes_mgmt.router)
    app.include_router(upload.router)
    app.include_router(orgs.router)
    app.include_router(presets.router)
    app.include_router(admin.router)
    app.include_router(status_router)

    # WebSocket
    app.websocket("/ws")(websocket_endpoint)

    # Serve built Svelte SPA from web/frontend/build/
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "build")
    if os.path.isdir(static_dir):
        # Mount static assets (JS, CSS, images) — but NOT as catch-all
        app.mount("/_app", StaticFiles(directory=os.path.join(static_dir, "_app")), name="spa-assets")

        index_html = os.path.join(static_dir, "index.html")

        # SPA catch-all: any non-API, non-asset GET request serves index.html
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(request: Request, path: str):
            # Don't intercept API or WebSocket paths
            if path.startswith("api/") or path == "ws":
                return
            # Serve actual static files if they exist (favicon, etc.)
            # Use realpath to prevent path traversal (e.g., ../../etc/passwd)
            if path:
                file_path = os.path.realpath(os.path.join(static_dir, path))
                static_real = os.path.realpath(static_dir)
                if file_path.startswith(static_real + os.sep) and os.path.isfile(file_path):
                    return FileResponse(file_path)
            return FileResponse(index_html)
    else:
        logger.warning(f"SPA build directory not found at {static_dir} — serving API only")

    return app

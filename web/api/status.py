"""Public status page and monitoring endpoints (CRKY-51).

Provides a public-facing status API and SVG badge for external monitoring.
No authentication required — this is intentionally public.

Endpoints:
- GET /api/status — Platform status summary (JSON)
- GET /api/status/badge — Shields.io-compatible SVG badge
- GET /api/status/history — Recent status snapshots

Status levels: operational, degraded, down
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/status", tags=["status"])

# Status history: ring buffer of snapshots, sampled every 60s by the check loop.
# 1440 entries = 24 hours of per-minute data.
_MAX_HISTORY = 1440
_history: deque[dict] = deque(maxlen=_MAX_HISTORY)
_history_lock = threading.Lock()

# Track incidents (transitions from operational to degraded/down)
_incidents: deque[dict] = deque(maxlen=100)


@dataclass
class StatusSnapshot:
    """A point-in-time platform status sample."""

    timestamp: float = 0.0
    status: str = "operational"  # operational, degraded, down
    api: str = "ok"
    database: str = "ok"
    auth: str = "ok"
    worker: str = "ok"
    disk: str = "ok"
    nodes_online: int = 0
    nodes_total: int = 0
    total_gpus: int = 0
    queue_depth: int = 0
    jobs_running: int = 0
    frames_processed: int = 0
    uptime_seconds: float = 0.0
    avg_job_seconds: float = 0.0
    checks: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "status": self.status,
            "components": {
                "api": self.api,
                "database": self.database,
                "auth": self.auth,
                "worker": self.worker,
                "disk": self.disk,
            },
            "nodes_online": self.nodes_online,
            "nodes_total": self.nodes_total,
            "total_gpus": self.total_gpus,
            "queue_depth": self.queue_depth,
            "jobs_running": self.jobs_running,
            "frames_processed": self.frames_processed,
            "uptime_seconds": self.uptime_seconds,
            "avg_job_seconds": self.avg_job_seconds,
        }


def _compute_status() -> StatusSnapshot:
    """Compute current platform status by aggregating internal state.

    Does NOT call /api/health (which would be circular and slow).
    Instead reads the same underlying state directly.
    """
    snap = StatusSnapshot(timestamp=time.time())

    # Uptime
    from .app import _app_start_time

    snap.uptime_seconds = round(time.time() - _app_start_time, 1) if _app_start_time > 0 else 0.0

    # Database
    try:
        from .database import PostgresBackend, get_storage

        storage = get_storage()
        if isinstance(storage, PostgresBackend):
            storage.get_setting("_status_check")
            snap.database = "ok"
        else:
            snap.database = "ok"
    except Exception:
        snap.database = "error"

    # Auth (GoTrue)
    import os

    gotrue_url = os.environ.get("CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "")).strip()
    if gotrue_url:
        try:
            import urllib.request

            req = urllib.request.Request(f"{gotrue_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                snap.auth = "ok"
        except Exception:
            snap.auth = "error"
    else:
        snap.auth = "skipped"

    # Worker thread — check via app state if available
    try:
        from .deps import _service

        if _service is not None:
            snap.worker = "ok"
        else:
            snap.worker = "skipped"
    except Exception:
        snap.worker = "skipped"

    # Disk
    try:
        import shutil

        from .app import _resolve_clips_dir

        clips_dir = _resolve_clips_dir()
        if os.path.isdir(clips_dir):
            usage = shutil.disk_usage(clips_dir)
            free_gb = usage.free / (1024**3)
            snap.disk = "ok" if free_gb > 1.0 else "warning"
        else:
            snap.disk = "skipped"
    except Exception:
        snap.disk = "skipped"

    # Nodes
    try:
        from .nodes import registry

        nodes = registry.list_nodes()
        snap.nodes_total = len(nodes)
        snap.nodes_online = sum(1 for n in nodes if n.is_alive)
        snap.total_gpus = sum(len(n.gpus) if n.gpus else (1 if n.gpu_name else 0) for n in nodes if n.is_alive)
    except Exception:
        pass

    # Job queue
    try:
        from .deps import get_queue

        queue = get_queue()
        snap.queue_depth = len(queue.queue_snapshot)
        snap.jobs_running = len(queue.running_jobs)

        # Average job time (last hour)
        cutoff = time.time() - 3600
        recent = [
            j
            for j in queue.history_snapshot
            if j.status.value == "completed"
            and j.completed_at > cutoff
            and j.started_at > 0
            and j.completed_at > j.started_at
        ]
        if recent:
            durations = [j.completed_at - j.started_at for j in recent]
            snap.avg_job_seconds = round(sum(durations) / len(durations), 1)

        # Total frames processed (all time)
        snap.frames_processed = sum(
            j.total_frames for j in queue.history_snapshot if j.status.value == "completed" and j.total_frames > 0
        )
    except Exception:
        pass

    # Determine overall status
    components = [snap.database, snap.worker, snap.disk]
    # Auth errors only degrade if auth is configured
    if snap.auth == "error":
        components.append("error")

    error_count = sum(1 for c in components if c == "error")
    warning_count = sum(1 for c in components if c == "warning")

    if error_count >= 2 or (snap.database == "error" and snap.worker == "error"):
        snap.status = "down"
    elif error_count > 0 or warning_count > 0:
        snap.status = "degraded"
    else:
        snap.status = "operational"

    snap.api = "ok"  # If this code is running, the API is up
    return snap


def record_snapshot() -> StatusSnapshot:
    """Take a status snapshot and store it in history."""
    snap = _compute_status()
    with _history_lock:
        # Detect incidents (status transitions)
        if _history:
            prev = _history[-1]
            if prev.get("status") == "operational" and snap.status != "operational":
                _incidents.append(
                    {
                        "timestamp": snap.timestamp,
                        "status": snap.status,
                        "type": "degradation",
                    }
                )
            elif prev.get("status") != "operational" and snap.status == "operational":
                _incidents.append(
                    {
                        "timestamp": snap.timestamp,
                        "status": "resolved",
                        "type": "recovery",
                        "duration_seconds": round(snap.timestamp - prev["timestamp"], 1),
                    }
                )
        _history.append(snap.to_dict())
    return snap


def get_history(limit: int = 60) -> list[dict]:
    """Return recent status snapshots."""
    with _history_lock:
        items = list(_history)
    return items[-limit:]


def get_incidents(limit: int = 20) -> list[dict]:
    """Return recent incidents."""
    return list(_incidents)[-limit:]


# --- Background sampling thread ---

_stop_event: threading.Event | None = None
_sample_thread: threading.Thread | None = None


def start_status_sampler(interval: int = 60) -> None:
    """Start background thread that records status snapshots."""
    global _stop_event, _sample_thread

    if _sample_thread is not None and _sample_thread.is_alive():
        return  # Already running

    _stop_event = threading.Event()

    def _loop():
        # Initial snapshot immediately
        try:
            record_snapshot()
        except Exception as e:
            logger.debug(f"Status snapshot failed: {e}")

        while not _stop_event.is_set():
            _stop_event.wait(interval)
            if _stop_event.is_set():
                break
            try:
                record_snapshot()
            except Exception as e:
                logger.debug(f"Status snapshot failed: {e}")

    _sample_thread = threading.Thread(target=_loop, daemon=True, name="status-sampler")
    _sample_thread.start()
    logger.info(f"Status sampler started (interval={interval}s)")


def stop_status_sampler() -> None:
    """Stop the background sampler."""
    global _stop_event
    if _stop_event:
        _stop_event.set()


# --- Routes ---


@router.get("", summary="Platform status")
def get_status():
    """Public platform status. No authentication required.

    Returns current platform health, component statuses, node counts,
    queue depth, and average job processing time.
    """
    snap = _compute_status()
    result = snap.to_dict()
    # Add incident info
    incidents = get_incidents(5)
    result["recent_incidents"] = incidents
    result["last_incident"] = incidents[-1]["timestamp"] if incidents else None
    return result


@router.get("/history", summary="Status history")
def get_status_history(limit: int = 60):
    """Recent status snapshots (up to 24h at 1-minute resolution).

    No authentication required. Used by status page graphs.
    """
    return {"snapshots": get_history(limit)}


@router.get("/incidents", summary="Recent incidents")
def get_status_incidents(limit: int = 20):
    """Recent status incidents (degradations and recoveries).

    No authentication required.
    """
    return {"incidents": get_incidents(limit)}


@router.get("/badge", summary="Status badge SVG")
def get_status_badge():
    """Shields.io-compatible SVG status badge for README embeds.

    Returns an SVG image showing current platform status with color coding:
    - Green: operational
    - Yellow: degraded
    - Red: down

    Usage in markdown:
    ```
    ![Status](https://your-server.com/api/status/badge)
    ```
    """
    snap = _compute_status()

    colors = {
        "operational": "#4c1",  # Green
        "degraded": "#dfb317",  # Yellow
        "down": "#e05d44",  # Red
    }
    color = colors.get(snap.status, "#9f9f9f")
    label = "status"
    message = snap.status

    # Shields.io-style flat badge SVG
    lw = len(label) * 6.5 + 10
    mw = len(message) * 6.5 + 10
    tw = lw + mw
    lx = lw / 2
    mx = lw + mw / 2
    font = "Verdana,Geneva,DejaVu Sans,sans-serif"

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{tw}" height="20"'
        f' role="img" aria-label="{label}: {message}">'
        f"<title>{label}: {message}</title>"
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/>'
        "</linearGradient>"
        '<clipPath id="r">'
        f'<rect width="{tw}" height="20" rx="3" fill="#fff"/>'
        "</clipPath>"
        '<g clip-path="url(#r)">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{mw}" height="20" fill="{color}"/>'
        f'<rect width="{tw}" height="20" fill="url(#s)"/>'
        "</g>"
        f'<g fill="#fff" text-anchor="middle" font-family="{font}"'
        ' text-rendering="geometricPrecision" font-size="11">'
        f'<text x="{lx}" y="15" fill="#010101" fill-opacity=".3">'
        f"{label}</text>"
        f'<text x="{lx}" y="14">{label}</text>'
        f'<text x="{mx}" y="15" fill="#010101" fill-opacity=".3">'
        f"{message}</text>"
        f'<text x="{mx}" y="14">{message}</text>'
        "</g></svg>"
    )

    from starlette.responses import Response

    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )

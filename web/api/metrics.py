"""Prometheus metrics endpoint for monitoring (CRKY-27).

Exports CorridorKey-specific metrics in Prometheus text format at /metrics.
Enabled via CK_METRICS_ENABLED=true (default false).

No external dependencies — builds the text format manually.
"""

from __future__ import annotations

import os
import shutil
import time

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from .deps import get_queue
from .nodes import registry
from .ws import manager

METRICS_ENABLED = os.environ.get("CK_METRICS_ENABLED", "false").strip().lower() in ("true", "1", "yes")

router = APIRouter(tags=["metrics"])

_start_time = time.time()

# Simple request counter incremented by middleware
_request_count = 0


def increment_request_count() -> None:
    """Called by middleware to count API requests."""
    global _request_count
    _request_count += 1


def _m(name: str, value, help_text: str, mtype: str = "gauge", labels: str = "") -> str:
    """Format a single Prometheus metric with HELP and TYPE."""
    label_str = f"{{{labels}}}" if labels else ""
    return f"# HELP {name} {help_text}\n# TYPE {name} {mtype}\n{name}{label_str} {value}\n"


def _l(name: str, value, labels: str) -> str:
    """Format a labeled metric line (no HELP/TYPE header)."""
    return f"{name}{{{labels}}} {value}\n"


def _header(name: str, help_text: str, mtype: str = "gauge") -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} {mtype}\n"


# Optional bearer token for metrics endpoint security.
_METRICS_TOKEN = os.environ.get("CK_METRICS_TOKEN", "").strip()


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(request: Request):
    """Export metrics in Prometheus text exposition format."""
    if not METRICS_ENABLED:
        return PlainTextResponse("# Metrics disabled. Set CK_METRICS_ENABLED=true\n", status_code=200)

    if _METRICS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_METRICS_TOKEN}":
            return PlainTextResponse("Unauthorized\n", status_code=401)

    lines: list[str] = []
    now = time.time()

    # ── Server ──────────────────────────────────────────────────
    lines.append(_m("corridorkey_uptime_seconds", now - _start_time, "Server uptime in seconds", "counter"))
    lines.append(_m("corridorkey_api_requests_total", _request_count, "Total API requests served", "counter"))

    ws_count = manager.connection_count
    lines.append(_m("corridorkey_ws_connections", ws_count, "Active WebSocket connections"))

    # ── Job Queue ───────────────────────────────────────────────
    queue = get_queue()
    running = queue.running_jobs
    queued = queue.queue_snapshot
    history = queue.history_snapshot

    lines.append(_m("corridorkey_jobs_running", len(running), "Currently running jobs"))
    lines.append(_m("corridorkey_jobs_queued", len(queued), "Jobs waiting in queue"))

    # Totals by status
    completed = failed = cancelled = 0
    for j in history:
        s = j.status.value
        if s == "completed":
            completed += 1
        elif s == "failed":
            failed += 1
        elif s == "cancelled":
            cancelled += 1

    lines.append(_m("corridorkey_jobs_completed_total", completed, "Total completed jobs", "counter"))
    lines.append(_m("corridorkey_jobs_failed_total", failed, "Total failed jobs", "counter"))
    lines.append(_m("corridorkey_jobs_cancelled_total", cancelled, "Total cancelled jobs", "counter"))

    # Per-job-type breakdown
    lines.append(_header("corridorkey_jobs_by_type", "Running jobs by type"))
    type_counts: dict[str, int] = {}
    for j in running:
        type_counts[j.job_type.value] = type_counts.get(j.job_type.value, 0) + 1
    for jtype, count in type_counts.items():
        lines.append(_l("corridorkey_jobs_by_type", count, f'type="{jtype}",status="running"'))
    type_counts_q: dict[str, int] = {}
    for j in queued:
        type_counts_q[j.job_type.value] = type_counts_q.get(j.job_type.value, 0) + 1
    for jtype, count in type_counts_q.items():
        lines.append(_l("corridorkey_jobs_by_type", count, f'type="{jtype}",status="queued"'))

    # Throughput — completed in last hour and 5 minutes
    cutoff_1h = now - 3600
    cutoff_5m = now - 300
    completed_1h = completed_5m = 0
    for j in history:
        if j.status.value == "completed" and j.completed_at:
            if j.completed_at > cutoff_1h:
                completed_1h += 1
            if j.completed_at > cutoff_5m:
                completed_5m += 1

    lines.append(_m("corridorkey_jobs_completed_last_hour", completed_1h, "Jobs completed in last hour"))
    lines.append(_m("corridorkey_jobs_completed_last_5m", completed_5m, "Jobs completed in last 5 minutes"))

    # Frame throughput
    total_frames_processed = 0
    frames_last_hour = 0
    for j in history:
        if j.status.value == "completed" and j.total_frames > 0:
            total_frames_processed += j.total_frames
            if j.completed_at and j.completed_at > cutoff_1h:
                frames_last_hour += j.total_frames

    lines.append(_m("corridorkey_frames_processed_total", total_frames_processed, "Total frames processed", "counter"))
    lines.append(_m("corridorkey_frames_processed_last_hour", frames_last_hour, "Frames processed in last hour"))

    # Average job duration by type
    lines.append(_header("corridorkey_job_avg_duration_seconds", "Average job duration in seconds by type"))
    duration_by_type: dict[str, list[float]] = {}
    for j in history:
        if j.status.value == "completed" and j.started_at > 0 and j.completed_at > j.started_at:
            dur = j.completed_at - j.started_at
            duration_by_type.setdefault(j.job_type.value, []).append(dur)
    for jtype, durations in duration_by_type.items():
        avg = sum(durations) / len(durations)
        lines.append(_l("corridorkey_job_avg_duration_seconds", round(avg, 1), f'type="{jtype}"'))

    # Average FPS by type
    lines.append(_header("corridorkey_job_avg_fps", "Average frames per second by job type"))
    fps_by_type: dict[str, list[float]] = {}
    for j in history:
        if (j.status.value == "completed" and j.total_frames > 0
                and j.started_at > 0 and j.completed_at > j.started_at):
            dur = j.completed_at - j.started_at
            fps = j.total_frames / dur
            if 0 < fps < 100:
                fps_by_type.setdefault(j.job_type.value, []).append(fps)
    for jtype, fps_list in fps_by_type.items():
        avg_fps = sum(fps_list) / len(fps_list)
        lines.append(_l("corridorkey_job_avg_fps", round(avg_fps, 3), f'type="{jtype}"'))

    # Queue wait time (avg seconds from submission to start for recent jobs)
    wait_times: list[float] = []
    for j in history:
        if j.status.value == "completed" and j.started_at > 0 and j.completed_at > cutoff_1h:
            # started_at - queued_at estimate: use started_at as proxy
            pass  # TODO: need queued_at timestamp on GPUJob
    # For now, just track currently queued jobs' wait time
    if queued:
        oldest_queued = min(j.started_at for j in queued if j.started_at > 0) if any(j.started_at > 0 for j in queued) else 0
        if oldest_queued > 0:
            lines.append(_m("corridorkey_queue_oldest_seconds", now - oldest_queued, "Age of oldest queued job in seconds"))

    # ── Nodes ───────────────────────────────────────────────────
    nodes = registry.list_nodes()
    online = sum(1 for n in nodes if n.is_alive and n.status == "online")
    busy = sum(1 for n in nodes if n.is_alive and n.status == "busy")
    offline = sum(1 for n in nodes if not n.is_alive)
    paused = sum(1 for n in nodes if n.paused)
    outdated = sum(1 for n in nodes if not n.version_ok)

    lines.append(_m("corridorkey_nodes_online", online, "Online idle nodes"))
    lines.append(_m("corridorkey_nodes_busy", busy, "Busy nodes (processing)"))
    lines.append(_m("corridorkey_nodes_offline", offline, "Offline nodes"))
    lines.append(_m("corridorkey_nodes_paused", paused, "Paused nodes"))
    lines.append(_m("corridorkey_nodes_outdated", outdated, "Nodes with outdated version"))
    lines.append(_m("corridorkey_nodes_total", len(nodes), "Total registered nodes"))

    # Total GPU count across all nodes
    total_gpus = sum(len(n.gpus) if n.gpus else (1 if n.gpu_name else 0) for n in nodes if n.is_alive)
    lines.append(_m("corridorkey_gpus_total", total_gpus, "Total GPUs across online nodes"))

    # Per-node hardware metrics
    lines.append(_header("corridorkey_node_cpu_percent", "Node CPU usage percent"))
    lines.append(_header("corridorkey_node_ram_used_gb", "Node RAM used in GB"))
    lines.append(_header("corridorkey_node_ram_total_gb", "Node RAM total in GB"))
    lines.append(_header("corridorkey_node_vram_total_gb", "GPU VRAM total in GB"))
    lines.append(_header("corridorkey_node_vram_used_gb", "GPU VRAM used in GB"))
    lines.append(_header("corridorkey_node_online", "Node is alive (1) or offline (0)"))

    for node in nodes:
        nl = f'node="{node.name}",node_id="{node.node_id}"'
        lines.append(_l("corridorkey_node_online", 1 if node.is_alive else 0, nl))

        if node.cpu_stats:
            lines.append(_l("corridorkey_node_cpu_percent", node.cpu_stats.get("cpu_percent", 0), nl))
            lines.append(_l("corridorkey_node_ram_used_gb", round(node.cpu_stats.get("ram_used_gb", 0), 2), nl))
            lines.append(_l("corridorkey_node_ram_total_gb", round(node.cpu_stats.get("ram_total_gb", 0), 2), nl))

        if node.gpus:
            for gpu in node.gpus:
                gl = f'node="{node.name}",gpu="{gpu.name}",gpu_index="{gpu.index}"'
                lines.append(_l("corridorkey_node_vram_total_gb", round(gpu.vram_total_gb, 2), gl))
                lines.append(_l("corridorkey_node_vram_used_gb", round(gpu.vram_total_gb - gpu.vram_free_gb, 2), gl))
        elif node.vram_total_gb > 0:
            gl = f'node="{node.name}",gpu="{node.gpu_name}",gpu_index="0"'
            lines.append(_l("corridorkey_node_vram_total_gb", round(node.vram_total_gb, 2), gl))
            lines.append(_l("corridorkey_node_vram_used_gb", round(node.vram_total_gb - node.vram_free_gb, 2), gl))

    # Per-node reputation
    try:
        from .node_reputation import get_all_reputations

        reps = get_all_reputations()
        if reps:
            lines.append(_header("corridorkey_node_reputation", "Node reputation score 0-100"))
            lines.append(_header("corridorkey_node_success_rate", "Node job success rate 0-1"))
            lines.append(_header("corridorkey_node_completed_jobs", "Node completed job count", "counter"))
            lines.append(_header("corridorkey_node_failed_jobs", "Node failed job count", "counter"))
            lines.append(_header("corridorkey_node_total_frames", "Total frames processed by node", "counter"))
            for rep in reps:
                node = registry.get_node(rep.node_id)
                name = node.name if node else rep.node_id
                nl = f'node="{name}",node_id="{rep.node_id}"'
                lines.append(_l("corridorkey_node_reputation", rep.score, nl))
                lines.append(_l("corridorkey_node_success_rate", round(rep.success_rate, 3), nl))
                lines.append(_l("corridorkey_node_completed_jobs", rep.completed_jobs, nl))
                lines.append(_l("corridorkey_node_failed_jobs", rep.failed_jobs, nl))
                lines.append(_l("corridorkey_node_total_frames", rep.total_frames, nl))
    except Exception:
        pass

    # ── GPU Credits (per-org) ───────────────────────────────────
    try:
        from .gpu_credits import get_all_credits
        from .orgs import get_org_store

        org_store = get_org_store()
        all_credits = get_all_credits()
        if all_credits:
            lines.append(_header("corridorkey_credits_contributed_hours", "GPU hours contributed by org", "counter"))
            lines.append(_header("corridorkey_credits_consumed_hours", "GPU hours consumed by org", "counter"))
            lines.append(_header("corridorkey_credits_balance_hours", "GPU credit balance in hours (contributed - consumed)"))
            for c in all_credits:
                org = org_store.get_org(c.org_id)
                name = org.name if org else c.org_id[:8]
                ol = f'org="{name}",org_id="{c.org_id}"'
                lines.append(_l("corridorkey_credits_contributed_hours", round(c.contributed_seconds / 3600, 2), ol))
                lines.append(_l("corridorkey_credits_consumed_hours", round(c.consumed_seconds / 3600, 2), ol))
                lines.append(_l("corridorkey_credits_balance_hours", round(c.balance / 3600, 2), ol))
    except Exception:
        pass

    # ── Storage ─────────────────────────────────────────────────
    try:
        from .storage_quota import get_org_disk_usage, get_org_quota

        from .orgs import get_org_store as _get_org_store
        os2 = _get_org_store()
        all_orgs = os2.list_all_orgs() if hasattr(os2, 'list_all_orgs') else []
        if all_orgs:
            lines.append(_header("corridorkey_storage_used_gb", "Storage used by org in GB"))
            lines.append(_header("corridorkey_storage_quota_gb", "Storage quota for org in GB"))
            for org in all_orgs:
                used = get_org_disk_usage(org.org_id)
                quota = get_org_quota(org.org_id)
                ol = f'org="{org.name}",org_id="{org.org_id}"'
                lines.append(_l("corridorkey_storage_used_gb", round(used / (1024 ** 3), 2), ol))
                lines.append(_l("corridorkey_storage_quota_gb", round(quota / (1024 ** 3), 1), ol))
    except Exception:
        pass

    # Disk space (server-level)
    try:
        from backend.project import projects_root

        clips_dir = os.environ.get("CK_CLIPS_DIR", "").strip() or projects_root()
        if os.path.isdir(clips_dir):
            usage = shutil.disk_usage(clips_dir)
            lines.append(_m("corridorkey_disk_free_gb", round(usage.free / (1024 ** 3), 2), "Free disk space in GB"))
            lines.append(_m("corridorkey_disk_total_gb", round(usage.total / (1024 ** 3), 2), "Total disk space in GB"))
            lines.append(_m("corridorkey_disk_used_percent", round((usage.used / usage.total) * 100, 1), "Disk usage percent"))
    except Exception:
        pass

    # ── Build info ──────────────────────────────────────────────
    try:
        from .version import BUILD_COMMIT, BUILD_NUMBER, VERSION_STRING

        lines.append(f'# HELP corridorkey_build_info Server build information\n')
        lines.append(f'# TYPE corridorkey_build_info gauge\n')
        lines.append(f'corridorkey_build_info{{version="{VERSION_STRING}",commit="{BUILD_COMMIT}",build_number="{BUILD_NUMBER}"}} 1\n')
    except Exception:
        pass

    return PlainTextResponse("".join(lines))

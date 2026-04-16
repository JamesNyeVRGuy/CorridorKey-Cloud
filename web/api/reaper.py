"""Job reaper -- requeues orphaned and stalled jobs.

Runs as a background thread on the server. Every 30 seconds, checks
all running jobs for two conditions:
1. Dead node: heartbeat timed out, job is requeued immediately.
2. Stalled job: node is alive but no progress for CK_STALL_TIMEOUT
   seconds (default 600). Catches GPU hangs, infinite loops, etc.

In multi-instance mode (Redis), a distributed lock ensures only one
instance runs the reaper at a time (CRKY-105 Phase 4).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from backend.job_queue import JobStatus

from .state import JobState, NodeState
from .ws import manager

logger = logging.getLogger(__name__)

_REAP_INTERVAL = 30  # seconds
_LOCK_KEY = "ck:lock:reaper"
_LOCK_TTL_MS = 25000  # must be < _REAP_INTERVAL * 1000 to prevent deadlock

# Max seconds a running job can go without progress before being reaped.
# Covers download (up to 300s) + model compilation (up to 60s) + buffer.
# Set CK_STALL_TIMEOUT=0 to disable stall detection.
_STALL_TIMEOUT = int(os.environ.get("CK_STALL_TIMEOUT", "600"))

# Lua script: release lock only if we still own it (value matches our UUID)
_LUA_RELEASE_LOCK = """\
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _acquire_lock() -> str | None:
    """Try to acquire the distributed reaper lock via Redis.

    Returns the lock token (UUID) on success, None if another instance holds it.
    No-op (always succeeds) when Redis is not configured (single-instance mode).
    """
    from .redis_client import get_redis, is_redis_configured

    if not is_redis_configured():
        return "local"  # single-instance, always allowed

    r = get_redis()
    if r is None:
        return "local"

    token = uuid.uuid4().hex
    acquired = r.set(_LOCK_KEY, token, nx=True, px=_LOCK_TTL_MS)
    if acquired:
        return token
    return None


def _release_lock(token: str) -> None:
    """Release the distributed reaper lock if we still own it."""
    from .redis_client import get_redis, is_redis_configured

    if not is_redis_configured() or token == "local":
        return

    r = get_redis()
    if r is None:
        return

    try:
        r.eval(_LUA_RELEASE_LOCK, 1, _LOCK_KEY, token)
    except Exception:
        logger.debug("Failed to release reaper lock", exc_info=True)


def _reap_once(queue: JobState, nodes: NodeState) -> None:
    """Check for orphaned and stalled jobs and requeue them.

    Scans ALL running jobs (not just the first) to handle multi-GPU
    nodes that may have multiple jobs in flight when they die.

    Two checks per job:
    1. Dead node: heartbeat timed out (existing behavior)
    2. Stalled job: node is alive but no progress for _STALL_TIMEOUT seconds
    """
    for job in list(queue.running_jobs):
        if job.status != JobStatus.RUNNING or not job.claimed_by:
            continue
        if job.claimed_by == "local":
            continue  # local jobs are managed by the worker thread

        node = nodes.get_node(job.claimed_by)

        # Check 1: Node is dead
        if node is None or not node.is_alive:
            logger.warning(f"Reaping orphan job [{job.id}]: node '{job.claimed_by}' is dead, requeuing")
            queue.requeue_job(job)
            manager.send_job_status(job.id, JobStatus.QUEUED.value, org_id=job.org_id)
            from .node_reputation import record_job_failed

            record_job_failed(job.claimed_by)
            if node:
                nodes.set_idle(job.claimed_by)
            continue

        # Check 2: Node is alive but job is stalled (no progress for too long)
        if _STALL_TIMEOUT > 0 and job.last_progress_at > 0:
            stall_duration = time.time() - job.last_progress_at
            if stall_duration > _STALL_TIMEOUT:
                logger.warning(
                    f"Reaping stalled job [{job.id}]: no progress for {stall_duration:.0f}s "
                    f"(node '{job.claimed_by}' is alive), requeuing"
                )
                queue.requeue_job(job)
                manager.send_job_status(job.id, JobStatus.QUEUED.value, org_id=job.org_id)
                from .node_reputation import record_job_failed

                record_job_failed(job.claimed_by)
                nodes.set_idle(job.claimed_by)


def reaper_loop(queue: JobState, nodes: NodeState, stop_event: threading.Event) -> None:
    """Background thread that periodically checks for orphaned jobs."""
    logger.info(f"Job reaper started (interval: {_REAP_INTERVAL}s)")
    while not stop_event.is_set():
        stop_event.wait(_REAP_INTERVAL)
        if not stop_event.is_set():
            token = _acquire_lock()
            if token is None:
                logger.debug("Reaper lock held by another instance, skipping")
                continue
            try:
                _reap_once(queue, nodes)
            except Exception:
                logger.exception("Reaper error")
            finally:
                _release_lock(token)


def start_reaper(queue: JobState, nodes: NodeState, stop_event: threading.Event) -> threading.Thread:
    """Start the reaper daemon thread."""
    thread = threading.Thread(
        target=reaper_loop,
        args=(queue, nodes, stop_event),
        daemon=True,
        name="job-reaper",
    )
    thread.start()
    return thread

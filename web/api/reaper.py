"""Job orphan reaper — requeues jobs claimed by dead nodes.

Runs as a background thread on the server. Every 30 seconds, checks
all running jobs: if the claiming node's heartbeat has timed out,
the job is returned to the front of the queue.
"""

from __future__ import annotations

import logging
import threading

from backend.job_queue import GPUJobQueue, JobStatus

from .nodes import registry
from .ws import manager

logger = logging.getLogger(__name__)

_REAP_INTERVAL = 30  # seconds


def _reap_once(queue: GPUJobQueue) -> None:
    """Check for orphaned jobs and requeue them."""
    # Check current running job
    current = queue.current_job
    if current and current.status == JobStatus.RUNNING and current.claimed_by:
        claimer = current.claimed_by
        if claimer == "local":
            return  # local jobs are managed by the worker thread

        node = registry.get_node(claimer)
        if node is None or not node.is_alive:
            logger.warning(f"Reaping orphan job [{current.id}]: node '{claimer}' is dead, requeuing")
            queue.requeue_job(current)
            manager.send_job_status(current.id, JobStatus.QUEUED.value)
            if node:
                registry.set_idle(claimer)


def reaper_loop(queue: GPUJobQueue, stop_event: threading.Event) -> None:
    """Background thread that periodically checks for orphaned jobs."""
    logger.info(f"Job reaper started (interval: {_REAP_INTERVAL}s)")
    while not stop_event.is_set():
        stop_event.wait(_REAP_INTERVAL)
        if not stop_event.is_set():
            try:
                _reap_once(queue)
            except Exception:
                logger.exception("Reaper error")


def start_reaper(queue: GPUJobQueue, stop_event: threading.Event) -> threading.Thread:
    """Start the reaper daemon thread."""
    thread = threading.Thread(
        target=reaper_loop,
        args=(queue, stop_event),
        daemon=True,
        name="job-reaper",
    )
    thread.start()
    return thread

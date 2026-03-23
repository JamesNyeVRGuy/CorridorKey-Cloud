"""GPU job queue with mutual exclusion.

Ensures only ONE GPU job runs at a time across all job types
(inference, GVM alpha gen, VideoMaMa alpha gen). This prevents VRAM
contention — CorridorKey alone needs ~22.7GB of 24GB.

Design:
    - Thread-safe queue of GPUJob dataclasses
    - Single consumer loop (designed to be driven by a QThread in the UI,
      or called directly in CLI mode)
    - Jobs carry a cancel flag checked between frames
    - Callbacks for progress, warnings, completion, errors
    - Jobs have stable IDs assigned at creation time
    - Deduplication prevents double-submit of same clip+job_type
    - Job history preserved for UI display (cancelled/completed/failed)
    - Multiple jobs can run simultaneously (local + remote nodes)
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .errors import JobCancelledError

logger = logging.getLogger(__name__)


class JobType(Enum):
    INFERENCE = "inference"
    GVM_ALPHA = "gvm_alpha"
    VIDEOMAMA_ALPHA = "videomama_alpha"
    PREVIEW_REPROCESS = "preview_reprocess"
    VIDEO_EXTRACT = "video_extract"
    VIDEO_STITCH = "video_stitch"


class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class GPUJob:
    """A single GPU job to be executed."""

    job_type: JobType
    clip_name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    params: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    _cancel_requested: bool = field(default=False, repr=False)
    error_message: str | None = None
    claimed_by: str | None = None  # node_id or "local"
    preferred_node: str | None = None  # prefer dispatching to this node (pipeline pinning)
    submitted_by: str | None = None  # user_id of who submitted the job (CRKY-66)
    org_id: str | None = None  # org that owns this job (CRKY-66)
    started_at: float = 0  # timestamp when job started running
    completed_at: float = 0  # timestamp when job finished (for duration calc)
    priority: int = 0  # higher = processed first
    shard_group: str | None = None  # links shards of the same job
    shard_index: int = 0  # which shard this is (0-based)
    shard_total: int = 1  # total number of shards

    # Progress tracking
    current_frame: int = 0
    total_frames: int = 0

    def request_cancel(self) -> None:
        """Signal that this job should stop at the next frame boundary."""
        self._cancel_requested = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def check_cancelled(self) -> None:
        """Raise JobCancelledError if cancel was requested. Call between frames."""
        if self._cancel_requested:
            raise JobCancelledError(self.clip_name, self.current_frame)


# Callback type aliases
ProgressCallback = Callable[[str, int, int], None]  # clip_name, current, total
WarningCallback = Callable[[str], None]  # message
CompletionCallback = Callable[[str], None]  # clip_name
ErrorCallback = Callable[[str, str], None]  # clip_name, error_message


class GPUJobQueue:
    """Thread-safe GPU job queue supporting multiple concurrent running jobs.

    Usage (CLI mode):
        queue = GPUJobQueue()
        queue.submit(GPUJob(JobType.INFERENCE, "shot1", params={...}))
        queue.submit(GPUJob(JobType.GVM_ALPHA, "shot2", params={...}))

        # Process all jobs sequentially
        while queue.has_pending:
            job = queue.next_job()
            if job:
                queue.start_job(job)
                try:
                    run_the_job(job)  # your processing function
                    queue.complete_job(job)
                except Exception as e:
                    queue.fail_job(job, str(e))

    Usage (distributed):
        Multiple workers (local + remote nodes) can claim and run jobs
        simultaneously. All running jobs are tracked and visible in the API.
    """

    _MAX_HISTORY = 1000  # Cap history to prevent unbounded memory growth

    def __init__(self):
        self._queue: deque[GPUJob] = deque()
        self._lock = threading.Lock()
        self._running_jobs: list[GPUJob] = []
        self._history: list[GPUJob] = []  # completed/cancelled/failed jobs for UI display

    def _add_to_history(self, job: GPUJob) -> None:
        """Append a job to history, evicting oldest if over capacity. Must hold _lock."""
        self._history.append(job)
        if len(self._history) > self._MAX_HISTORY:
            self._history = self._history[-self._MAX_HISTORY:]

        # Callbacks (set by UI or CLI)
        self.on_progress: ProgressCallback | None = None
        self.on_warning: WarningCallback | None = None
        self.on_completion: CompletionCallback | None = None
        self.on_error: ErrorCallback | None = None

    def submit(self, job: GPUJob) -> bool:
        """Add a job to the queue. Returns False if duplicate detected.

        PREVIEW_REPROCESS uses replacement semantics — any existing preview
        reprocess in the queue is replaced by the new one (latest-only).
        """
        with self._lock:
            # PREVIEW_REPROCESS: replace existing queued preview jobs (latest-only)
            if job.job_type == JobType.PREVIEW_REPROCESS:
                replaced = [j for j in self._queue if j.job_type == JobType.PREVIEW_REPROCESS]
                for old in replaced:
                    self._queue.remove(old)
                    old.status = JobStatus.CANCELLED
                    logger.debug(f"Preview reprocess [{old.id}] replaced by [{job.id}]")
            else:
                # Deduplication: reject if same clip+job_type already queued or running
                # Sharded jobs (shard_group set) bypass dedup — they're intentional splits
                if not job.shard_group:
                    for existing in self._queue:
                        if existing.clip_name == job.clip_name and existing.job_type == job.job_type:
                            logger.warning(
                                f"Duplicate job rejected: {job.job_type.value} for '{job.clip_name}' "
                                f"(already queued as {existing.id})"
                            )
                            return False
                    for running in self._running_jobs:
                        if (
                            running.clip_name == job.clip_name
                            and running.job_type == job.job_type
                            and running.status == JobStatus.RUNNING
                        ):
                            logger.warning(
                                f"Duplicate job rejected: {job.job_type.value} for '{job.clip_name}' "
                                f"(already running as {running.id})"
                            )
                            return False

            job.status = JobStatus.QUEUED
            # Insert sorted by priority (higher first). Same priority = FIFO.
            inserted = False
            for idx in range(len(self._queue)):
                if self._queue[idx].priority < job.priority:
                    self._queue.insert(idx, job)
                    inserted = True
                    break
            if not inserted:
                self._queue.append(job)
            logger.info(f"Job queued [{job.id}]: {job.job_type.value} for '{job.clip_name}'")
            return True

    def next_job(self) -> GPUJob | None:
        """Get the next pending job without starting it. Returns None if empty."""
        with self._lock:
            if self._queue:
                return self._queue[0]
            return None

    def claim_job(
        self,
        claimer_id: str = "local",
        accepted_types: list[str] | None = None,
        org_id: str | None = None,
    ) -> GPUJob | None:
        """Atomically pop the next job and mark it as running.

        This is the preferred method for distributed workers — it combines
        next_job() + start_job() in a single lock acquisition so two
        workers can't claim the same job.

        Jobs with a preferred_node set will only be claimed by that node
        (or "local"). Other claimers skip them.

        Args:
            claimer_id: Identifier of the worker claiming the job (node_id or "local").
            accepted_types: Job types this claimer can handle. None or empty = all types.
            org_id: Org filter — only claim jobs from this org. None = any org (CRKY-19).

        Returns:
            The claimed job, or None if no claimable job is available.
        """
        # CPU-bound jobs that need the local filesystem — never dispatch to nodes
        _LOCAL_ONLY = {JobType.VIDEO_EXTRACT, JobType.VIDEO_STITCH}

        with self._lock:
            for i, job in enumerate(self._queue):
                # Skip jobs pinned to a different node
                if job.preferred_node and job.preferred_node != claimer_id:
                    continue
                # Extract/stitch must run locally (need source video on disk)
                if claimer_id != "local" and job.job_type in _LOCAL_ONLY:
                    continue
                # Skip jobs this claimer can't handle
                if accepted_types and job.job_type.value not in accepted_types:
                    continue
                # Skip jobs from other orgs (CRKY-19)
                if org_id and job.org_id and job.org_id != org_id:
                    continue
                import time

                del self._queue[i]
                job.status = JobStatus.RUNNING
                job.claimed_by = claimer_id
                job.started_at = time.time()
                self._running_jobs.append(job)
                logger.info(f"Job claimed [{job.id}] by {claimer_id}: {job.job_type.value} for '{job.clip_name}'")
                return job
            return None

    def start_job(self, job: GPUJob) -> None:
        """Mark a job as running. Must be called before processing."""
        with self._lock:
            if job in self._queue:
                self._queue.remove(job)
            job.status = JobStatus.RUNNING
            if job not in self._running_jobs:
                self._running_jobs.append(job)
            logger.info(f"Job started [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def complete_job(self, job: GPUJob) -> None:
        """Mark a job as successfully completed."""
        import time

        with self._lock:
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
            if job in self._running_jobs:
                self._running_jobs.remove(job)
            self._add_to_history(job)
            logger.info(f"Job completed [{job.id}]: {job.job_type.value} for '{job.clip_name}'")
        # Emit AFTER lock release (Codex: no deadlock risk)
        if self.on_completion:
            self.on_completion(job.clip_name)

    def fail_job(self, job: GPUJob, error: str) -> None:
        """Mark a job as failed."""
        with self._lock:
            job.status = JobStatus.FAILED
            job.error_message = error
            if job in self._running_jobs:
                self._running_jobs.remove(job)
            self._add_to_history(job)
            logger.error(f"Job failed [{job.id}]: {job.job_type.value} for '{job.clip_name}': {error}")
        # Emit AFTER lock release
        if self.on_error:
            self.on_error(job.clip_name, error)

    def move_job(self, job_id: str, position: int) -> bool:
        """Move a queued job to a specific position (0 = front). Returns False if not found."""
        with self._lock:
            for i, job in enumerate(self._queue):
                if job.id == job_id:
                    del self._queue[i]
                    pos = max(0, min(position, len(self._queue)))
                    self._queue.insert(pos, job)
                    logger.info(f"Job [{job_id}] moved to position {pos}")
                    return True
            return False

    def requeue_job(self, job: GPUJob) -> None:
        """Return a running job to the front of the queue (e.g. orphan reaper)."""
        with self._lock:
            job.status = JobStatus.QUEUED
            job.claimed_by = None
            job.current_frame = 0
            job.total_frames = 0
            if job in self._running_jobs:
                self._running_jobs.remove(job)
            self._queue.appendleft(job)
            logger.info(f"Job requeued [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def mark_cancelled(self, job: GPUJob) -> None:
        """Mark a running job as cancelled AND remove from running list."""
        with self._lock:
            job.status = JobStatus.CANCELLED
            if job in self._running_jobs:
                self._running_jobs.remove(job)
            self._add_to_history(job)
            logger.info(f"Job cancelled [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def cancel_job(self, job: GPUJob) -> None:
        """Request cancellation of a specific job."""
        with self._lock:
            if job.status == JobStatus.QUEUED:
                if job in self._queue:
                    self._queue.remove(job)
                job.status = JobStatus.CANCELLED
                self._add_to_history(job)
                logger.info(f"Job removed from queue [{job.id}]: {job.job_type.value} for '{job.clip_name}'")
            elif job.status == JobStatus.RUNNING:
                # Signal cancel — worker calls mark_cancelled() after catching JobCancelledError
                job.request_cancel()
                logger.info(f"Job cancel requested [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def cancel_current(self) -> None:
        """Cancel all currently running jobs."""
        with self._lock:
            for job in self._running_jobs:
                if job.status == JobStatus.RUNNING:
                    job.request_cancel()

    def cancel_all(self) -> None:
        """Cancel all running jobs and clear the queue."""
        with self._lock:
            # Cancel running
            for job in self._running_jobs:
                if job.status == JobStatus.RUNNING:
                    job.request_cancel()
            # Clear queue — preserve in history
            for job in self._queue:
                job.status = JobStatus.CANCELLED
                self._add_to_history(job)
            self._queue.clear()
            logger.info("All jobs cancelled")

    def report_progress(self, clip_name: str, current: int, total: int) -> None:
        """Report progress for a job by clip name. Called by processing code."""
        with self._lock:
            for job in self._running_jobs:
                if job.clip_name == clip_name and job.status == JobStatus.RUNNING:
                    job.current_frame = current
                    job.total_frames = total
                    break
        if self.on_progress:
            self.on_progress(clip_name, current, total)

    def report_warning(self, message: str) -> None:
        """Report a non-fatal warning. Called by processing code."""
        logger.warning(message)
        if self.on_warning:
            self.on_warning(message)

    def find_job_by_id(self, job_id: str) -> GPUJob | None:
        """Find a job by ID in running, queue, or history."""
        with self._lock:
            for job in self._running_jobs:
                if job.id == job_id:
                    return job
            for job in self._queue:
                if job.id == job_id:
                    return job
            for job in self._history:
                if job.id == job_id:
                    return job
        return None

    def shard_group_progress(self, shard_group: str) -> dict:
        """Get combined progress for all shards in a group."""
        with self._lock:
            all_jobs = list(self._running_jobs) + list(self._queue) + self._history
            shards = [j for j in all_jobs if j.shard_group == shard_group]
            if not shards:
                return {
                    "total_shards": 0,
                    "completed": 0,
                    "running": 0,
                    "failed": 0,
                    "current_frame": 0,
                    "total_frames": 0,
                }

            completed = sum(1 for s in shards if s.status == JobStatus.COMPLETED)
            running = sum(1 for s in shards if s.status == JobStatus.RUNNING)
            failed = sum(1 for s in shards if s.status == JobStatus.FAILED)
            current = sum(s.current_frame for s in shards)
            total = sum(s.total_frames for s in shards)

            return {
                "shard_group": shard_group,
                "total_shards": len(shards),
                "completed": completed,
                "running": running,
                "failed": failed,
                "current_frame": current,
                "total_frames": total,
            }

    def cancel_shard_group(self, shard_group: str) -> int:
        """Cancel all shards in a group. Returns the number cancelled."""
        cancelled = 0
        with self._lock:
            # Cancel queued shards
            to_remove = [j for j in self._queue if j.shard_group == shard_group]
            for job in to_remove:
                self._queue.remove(job)
                job.status = JobStatus.CANCELLED
                self._add_to_history(job)
                cancelled += 1
            # Request cancel on running shards
            for job in self._running_jobs:
                if job.shard_group == shard_group and job.status == JobStatus.RUNNING:
                    job.request_cancel()
                    cancelled += 1
        if cancelled:
            logger.info(f"Cancelled {cancelled} shards in group {shard_group}")
        return cancelled

    def retry_failed_shards(self, shard_group: str) -> list["GPUJob"]:
        """Re-submit failed shards from a group. Returns new jobs."""
        new_jobs = []
        with self._lock:
            failed = [j for j in self._history if j.shard_group == shard_group and j.status == JobStatus.FAILED]
            for old in failed:
                job = GPUJob(
                    job_type=old.job_type,
                    clip_name=old.clip_name,
                    params=dict(old.params),
                    shard_group=old.shard_group,
                    shard_index=old.shard_index,
                    shard_total=old.shard_total,
                )
                new_jobs.append(job)
        # Submit outside lock
        submitted = []
        for job in new_jobs:
            if self.submit(job):
                submitted.append(job)
        return submitted

    def clear_history(self) -> None:
        """Clear job history (for UI reset)."""
        with self._lock:
            self._history.clear()

    def remove_job(self, job_id: str) -> None:
        """Remove a single finished job from history."""
        with self._lock:
            self._history = [j for j in self._history if j.id != job_id]

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return len(self._queue) > 0

    @property
    def current_job(self) -> GPUJob | None:
        """Return the first running job (backward compat). Use running_jobs for all."""
        with self._lock:
            return self._running_jobs[0] if self._running_jobs else None

    @property
    def running_jobs(self) -> list[GPUJob]:
        """Return a copy of all currently running jobs."""
        with self._lock:
            return list(self._running_jobs)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def queue_snapshot(self) -> list[GPUJob]:
        """Return a copy of the current queue for display purposes."""
        with self._lock:
            return list(self._queue)

    @property
    def history_snapshot(self) -> list[GPUJob]:
        """Return a copy of job history for display purposes."""
        with self._lock:
            return list(self._history)

    @property
    def all_jobs_snapshot(self) -> list[GPUJob]:
        """Return running + queued + history for full queue panel display."""
        with self._lock:
            result = list(self._running_jobs)
            result.extend(self._queue)
            result.extend(self._history)
            return result

"""Worker pool — CPU jobs run in parallel, GPU jobs check VRAM before starting."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from backend.clip_state import ClipAsset, ClipState
from backend.errors import CorridorKeyError, JobCancelledError
from backend.ffmpeg_tools import extract_frames
from backend.job_queue import GPUJob, GPUJobQueue, JobStatus, JobType
from backend.project import is_video_file
from backend.service import CorridorKeyService, InferenceParams, OutputConfig

from .ws import manager

logger = logging.getLogger(__name__)

# CPU-only job types that don't need VRAM
_CPU_JOB_TYPES = {JobType.VIDEO_EXTRACT, JobType.VIDEO_STITCH}

# Configurable VRAM limit (GB). Jobs won't start if free VRAM is below this.
# Set to 0 to disable the check (always allow).
_vram_limit_gb: float = 0.0
_vram_lock = threading.Lock()

# Local GPU processing toggle. When False, only CPU jobs run locally;
# GPU jobs stay in the queue for remote nodes to claim.
_local_gpu_enabled: bool = True

# Claim delay: local worker waits this many seconds before claiming a GPU job,
# giving remote nodes a head start. 0 = no delay (claim immediately).
_local_claim_delay: float = 0.0


def set_local_gpu_enabled(enabled: bool) -> None:
    global _local_gpu_enabled
    _local_gpu_enabled = enabled
    logger.info(f"Local GPU processing {'enabled' if enabled else 'disabled (remote-only)'}")
    from .database import get_storage

    get_storage().set_setting("local_gpu_enabled", enabled)


def get_local_gpu_enabled() -> bool:
    return _local_gpu_enabled


def set_local_claim_delay(seconds: float) -> None:
    global _local_claim_delay
    _local_claim_delay = max(0.0, seconds)
    logger.info(f"Local claim delay set to {_local_claim_delay:.1f}s")
    from .database import get_storage

    get_storage().set_setting("local_claim_delay", seconds)


def get_local_claim_delay() -> float:
    return _local_claim_delay


def set_vram_limit(gb: float) -> None:
    global _vram_limit_gb
    _vram_limit_gb = max(0.0, gb)
    logger.info(f"VRAM limit set to {_vram_limit_gb:.1f} GB")
    from .database import get_storage

    get_storage().set_setting("vram_limit_gb", gb)


def restore_settings() -> None:
    """Restore persisted settings on startup."""
    global _local_gpu_enabled, _vram_limit_gb, _local_claim_delay
    from .database import get_storage

    storage = get_storage()
    _local_gpu_enabled = storage.get_setting("local_gpu_enabled", True)
    _vram_limit_gb = storage.get_setting("vram_limit_gb", 0.0)
    if not _local_gpu_enabled:
        logger.info("Restored setting: local GPU processing disabled (remote-only)")
    if _vram_limit_gb > 0:
        logger.info(f"Restored setting: VRAM limit {_vram_limit_gb:.1f} GB")
    _local_claim_delay = storage.get_setting("local_claim_delay", 0.0)
    if _local_claim_delay > 0:
        logger.info(f"Restored setting: local claim delay {_local_claim_delay:.1f}s")


def get_vram_limit() -> float:
    return _vram_limit_gb


def _get_free_vram_gb() -> float | None:
    """Return free VRAM in GB, or None if unavailable."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        total = torch.cuda.get_device_properties(0).total_memory
        reserved = torch.cuda.memory_reserved(0)
        return (total - reserved) / (1024**3)
    except Exception:
        return None


def _can_start_gpu_job() -> bool:
    """Check if there's enough free VRAM to start another GPU job.

    Note: VRAM checking only works on CUDA. On Mac/MLX, _get_free_vram_gb()
    returns None and this gate is effectively disabled — multiple GPU jobs
    can run simultaneously with no throttle. MLX unified memory checking
    is not yet implemented.
    """
    if _vram_limit_gb <= 0:
        return True  # no limit set
    free = _get_free_vram_gb()
    if free is None:
        return True  # non-CUDA (e.g. MLX) — can't check, allow it
    can = free >= _vram_limit_gb
    if not can:
        logger.debug(f"VRAM check: {free:.1f} GB free < {_vram_limit_gb:.1f} GB limit, waiting")
    return can


def _find_clip(service: CorridorKeyService, clips_dir: str, clip_name: str):
    """Find a clip by name from the clips directory."""
    clips = service.scan_clips(clips_dir)
    for clip in clips:
        if clip.name == clip_name:
            return clip
    return None


def _execute_extraction(job: GPUJob, clip, clips_dir: str) -> None:
    """Extract frames from a video clip."""
    video_path = None

    if clip.input_asset and clip.input_asset.asset_type == "video" and os.path.isfile(clip.input_asset.path):
        video_path = clip.input_asset.path
    else:
        source_dir = os.path.join(clip.root_path, "Source")
        if os.path.isdir(source_dir):
            videos = [f for f in os.listdir(source_dir) if is_video_file(f)]
            if videos:
                video_path = os.path.join(source_dir, videos[0])

    if not video_path:
        raise CorridorKeyError(f"No video file found for clip '{clip.name}'")
    frames_dir = os.path.join(clip.root_path, "Frames")

    cancel_event = threading.Event()

    def on_progress(current: int, total: int) -> None:
        job.current_frame = current
        job.total_frames = total
        manager.send_job_progress(job.id, clip.name, current, total, org_id=job.org_id)
        if job.is_cancelled:
            cancel_event.set()

    count = extract_frames(
        video_path,
        frames_dir,
        on_progress=on_progress,
        cancel_event=cancel_event,
    )
    logger.info(f"Extracted {count} frames for clip '{clip.name}'")

    clip.input_asset = ClipAsset(frames_dir, "sequence")
    try:
        clip.transition_to(ClipState.RAW)
    except Exception:
        pass

    manager.send_clip_state_changed(clip.name, "RAW", org_id=job.org_id)


def _execute_gpu_job(service: CorridorKeyService, job: GPUJob, clips_dir: str) -> None:
    """Execute a GPU job (inference, GVM, VideoMaMa)."""
    clip = _find_clip(service, clips_dir, job.clip_name)
    if clip is None:
        raise CorridorKeyError(f"Clip '{job.clip_name}' not found")

    def on_progress(clip_name: str, current: int, total: int) -> None:
        job.current_frame = current
        job.total_frames = total
        manager.send_job_progress(job.id, clip_name, current, total, org_id=job.org_id)
        if current % 10 == 0:
            vram = service.get_vram_info()
            if vram:
                manager.send_vram_update(vram)

    def on_warning(message: str) -> None:
        manager.send_job_warning(job.id, message, org_id=job.org_id)

    if job.job_type == JobType.INFERENCE:
        params = InferenceParams.from_dict(job.params.get("inference_params", {}))
        output_config = OutputConfig.from_dict(job.params.get("output_config", {}))
        frame_range = job.params.get("frame_range")
        service.run_inference(
            clip,
            params,
            job=job,
            on_progress=on_progress,
            on_warning=on_warning,
            output_config=output_config,
            frame_range=tuple(frame_range) if frame_range else None,
        )
    elif job.job_type == JobType.GVM_ALPHA:
        gvm_frame_range = job.params.get("frame_range")
        service.run_gvm(
            clip, job=job, on_progress=on_progress, on_warning=on_warning,
            frame_range=tuple(gvm_frame_range) if gvm_frame_range else None,
        )
    elif job.job_type == JobType.VIDEOMAMA_ALPHA:
        chunk_size = job.params.get("chunk_size", 50)
        service.run_videomama(clip, job=job, on_progress=on_progress, on_warning=on_warning, chunk_size=chunk_size)

    manager.send_clip_state_changed(job.clip_name, clip.state.value, org_id=job.org_id)


def _chain_next_pipeline_step(job: GPUJob, queue: GPUJobQueue, clips_dir: str, service: CorridorKeyService) -> None:
    """If this was a pipeline job, submit the next step."""
    if not job.params.get("pipeline"):
        return

    # Re-scan the clip to get its current state after this step completed
    clip = _find_clip(service, clips_dir, job.clip_name)
    if clip is None:
        return

    state = clip.state.value
    params = job.params  # carries pipeline config forward

    # If this job is part of a shard group, wait until ALL shards are done
    # before chaining the next step. Otherwise each shard would independently
    # try to chain, causing duplicate submissions or partial processing.
    if job.shard_group and not queue.shard_group_all_done(job.shard_group):
        logger.debug(
            f"Pipeline chain: waiting for shard group {job.shard_group} to complete "
            f"before chaining next step for '{job.clip_name}'"
        )
        return

    next_jobs: list[GPUJob] = []
    frame_count = clip.input_asset.frame_count if clip.input_asset else 0

    if state == "RAW":
        # Extraction done → need alpha generation
        alpha_method = params.get("alpha_method", "gvm")
        if alpha_method == "videomama":
            next_jobs = [GPUJob(
                job_type=JobType.VIDEOMAMA_ALPHA,
                clip_name=job.clip_name,
                params={**params, "chunk_size": 50},
            )]
        else:
            from .routes.jobs import _build_gvm_jobs

            next_jobs = _build_gvm_jobs(job.clip_name, frame_count, extra_params=params)
    elif state == "READY":
        # Alpha done → need inference (shard across available GPUs)
        from .routes.jobs import _build_inference_shards

        next_jobs = _build_inference_shards(job.clip_name, frame_count, params)

    for next_job in next_jobs:
        next_job.org_id = job.org_id
        next_job.submitted_by = job.submitted_by
        # Pin to the same node for single (non-sharded) jobs only
        if len(next_jobs) == 1 and job.claimed_by and job.claimed_by != "local":
            next_job.preferred_node = job.claimed_by
        if queue.submit(next_job):
            logger.info(
                f"Pipeline chain: {job.job_type.value} → {next_job.job_type.value} "
                f"for '{job.clip_name}' (pinned to {next_job.preferred_node or 'any'})"
            )


def _run_job(service: CorridorKeyService, job: GPUJob, queue: GPUJobQueue, clips_dir: str) -> None:
    """Run a single job (called from thread pool). Job must already be claimed."""
    manager.send_job_status(job.id, JobStatus.RUNNING.value, org_id=job.org_id)

    # Use org-scoped clips dir if the job has an org_id
    job_clips_dir = clips_dir
    if job.org_id:
        from .org_isolation import resolve_node_clips_dir

        scoped = resolve_node_clips_dir(job.org_id)
        if scoped:
            job_clips_dir = scoped

    try:
        if job.job_type == JobType.VIDEO_EXTRACT:
            clip = _find_clip(service, job_clips_dir, job.clip_name)
            if clip is None:
                raise CorridorKeyError(f"Clip '{job.clip_name}' not found")
            _execute_extraction(job, clip, job_clips_dir)
        elif job.job_type in _CPU_JOB_TYPES:
            raise CorridorKeyError(f"CPU job type '{job.job_type.value}' not yet implemented")
        else:
            _execute_gpu_job(service, job, job_clips_dir)

        queue.complete_job(job)
        manager.send_job_status(job.id, JobStatus.COMPLETED.value, org_id=job.org_id)

        # Auto-chain next pipeline step
        _chain_next_pipeline_step(job, queue, job_clips_dir, service)
    except JobCancelledError:
        queue.mark_cancelled(job)
        manager.send_job_status(job.id, JobStatus.CANCELLED.value, org_id=job.org_id)
        # Credit partial GPU time consumed before cancellation
        if job.started_at > 0 and job.org_id:
            import time as _t

            elapsed = _t.time() - job.started_at
            if elapsed > 0:
                from .gpu_credits import add_consumed

                add_consumed(job.org_id, elapsed)
                logger.info(f"Credit tracking (cancelled): {elapsed:.1f}s consumed by org {job.org_id}")
        from .app import _save_history_snapshot

        _save_history_snapshot(queue)
    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Job {job.id} failed: {error_msg}")
        # Sanitize: only expose CorridorKeyError messages to clients
        client_msg = str(e) if isinstance(e, CorridorKeyError) else "Internal processing error"
        queue.fail_job(job, error_msg)  # full detail in server-side history
        manager.send_job_status(job.id, JobStatus.FAILED.value, error=client_msg, org_id=job.org_id)


# Track running GPU jobs
_running_gpu_count = 0
_running_gpu_lock = threading.Lock()


def _detect_local_gpu_count() -> int:
    """Detect number of local GPUs for worker concurrency."""
    try:
        import torch

        if torch.cuda.is_available():
            return max(1, torch.cuda.device_count())
    except Exception:
        pass
    return 1


def worker_loop(
    service: CorridorKeyService,
    queue: GPUJobQueue,
    clips_dir: str,
    stop_event: threading.Event,
    max_gpu_workers: int = 0,  # 0 = auto-detect (1 per GPU)
    max_cpu_workers: int = 4,
) -> None:
    """Main worker loop with parallel execution.

    CPU jobs (extraction) run in a separate thread pool and never block GPU jobs.
    GPU jobs are dispatched via GPUWorkerPool (one subprocess per GPU) when
    multiple GPUs are available, or via a thread pool for single-GPU systems.
    """
    global _running_gpu_count

    if max_gpu_workers <= 0:
        max_gpu_workers = _detect_local_gpu_count()

    cpu_pool = ThreadPoolExecutor(max_workers=max_cpu_workers, thread_name_prefix="cpu-worker")

    # Use subprocess pool for multi-GPU, thread pool for single-GPU
    use_subprocess_pool = max_gpu_workers > 1
    gpu_subprocess_pool = None
    gpu_thread_pool = None

    if use_subprocess_pool:
        from .gpu_pool import GPUWorkerPool

        gpu_subprocess_pool = GPUWorkerPool()

        def _on_sp_progress(job_id, clip_name, current, total):
            job = queue.find_job_by_id(job_id)
            oid = job.org_id if job else None
            if job:
                job.current_frame = current
                job.total_frames = total
            manager.send_job_progress(job_id, clip_name, current, total, org_id=oid)

        def _on_sp_warning(job_id, message):
            job = queue.find_job_by_id(job_id)
            oid = job.org_id if job else None
            manager.send_job_warning(job_id, message, org_id=oid)

        def _on_sp_completed(job_id, clip_name, clip_state):
            job = queue.find_job_by_id(job_id)
            if job:
                oid = job.org_id
                queue.complete_job(job)
                manager.send_job_status(job_id, JobStatus.COMPLETED.value, org_id=oid)
                manager.send_clip_state_changed(clip_name, clip_state, org_id=oid)
                # Use org-scoped clips dir for pipeline chaining
                sp_clips_dir = clips_dir
                if oid:
                    from .org_isolation import resolve_node_clips_dir

                    scoped = resolve_node_clips_dir(oid)
                    if scoped:
                        sp_clips_dir = scoped
                _chain_next_pipeline_step(job, queue, sp_clips_dir, service)
            else:
                logger.error(f"Subprocess completed job {job_id} but job not found in queue")
                manager.send_job_status(job_id, JobStatus.COMPLETED.value)

        def _on_sp_failed(job_id, error):
            job = queue.find_job_by_id(job_id)
            if job:
                oid = job.org_id
                queue.fail_job(job, error)  # full detail in server-side history
                manager.send_job_status(job_id, JobStatus.FAILED.value, error="Internal processing error", org_id=oid)
            else:
                logger.error(f"Subprocess failed job {job_id} but job not found in queue: {error}")

        gpu_subprocess_pool.set_callbacks(
            on_progress=_on_sp_progress,
            on_warning=_on_sp_warning,
            on_completed=_on_sp_completed,
            on_failed=_on_sp_failed,
        )
        gpu_subprocess_pool.start()
        logger.info(
            f"Worker pool started (GPU subprocesses: {gpu_subprocess_pool.gpu_count}, CPU workers: {max_cpu_workers})"
        )
    else:
        gpu_thread_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-worker")
        logger.info(f"Worker pool started (GPU workers: 1, CPU workers: {max_cpu_workers})")

    def _on_gpu_done(future, job=None):
        global _running_gpu_count
        with _running_gpu_lock:
            _running_gpu_count -= 1

    _cpu_types = [t.value for t in _CPU_JOB_TYPES]
    _gpu_types = [t.value for t in JobType if t not in _CPU_JOB_TYPES]

    while not stop_event.is_set():
        # Always try CPU jobs first — they never block on GPU availability
        cpu_job = queue.claim_job("local", accepted_types=_cpu_types)
        if cpu_job:
            cpu_pool.submit(_run_job, service, cpu_job, queue, clips_dir)
            continue

        # Check if there are GPU jobs to process
        peeked = queue.next_job()
        if peeked is None:
            stop_event.wait(0.5)
            continue

        if peeked.job_type in _CPU_JOB_TYPES:
            # CPU job that wasn't claimable (e.g. preferred_node mismatch) — wait
            stop_event.wait(0.5)
            continue

        if not _local_gpu_enabled:
            stop_event.wait(1.0)
            continue

        # Claim delay: give remote nodes a head start on GPU jobs
        if _local_claim_delay > 0:
            stop_event.wait(_local_claim_delay)
            # Re-check if job was claimed by a node during the delay
            if queue.next_job() is not peeked:
                continue

        if use_subprocess_pool and gpu_subprocess_pool:
            # Multi-GPU: dispatch to subprocess pool
            if not gpu_subprocess_pool.has_idle_gpu():
                stop_event.wait(0.5)
                continue
            job = queue.claim_job("local", accepted_types=_gpu_types)
            if job is None:
                continue
            if not gpu_subprocess_pool.submit(job, clips_dir):
                # All GPUs busy (race), requeue
                queue.requeue_job(job)
                stop_event.wait(0.5)
        else:
            # Single GPU: use thread pool (never sleep inside the lock)
            claimed_job = None
            with _running_gpu_lock:
                if _running_gpu_count < 1 and _can_start_gpu_job():
                    claimed_job = queue.claim_job("local", accepted_types=_gpu_types)
                    if claimed_job is not None:
                        _running_gpu_count += 1

            if claimed_job is None:
                stop_event.wait(0.5)
                continue

            future = gpu_thread_pool.submit(_run_job, service, claimed_job, queue, clips_dir)
            future.add_done_callback(lambda f, j=claimed_job: _on_gpu_done(f, j))

    logger.info("Shutting down worker pools")
    if gpu_subprocess_pool:
        gpu_subprocess_pool.stop()
    if gpu_thread_pool:
        gpu_thread_pool.shutdown(wait=True, cancel_futures=True)
    cpu_pool.shutdown(wait=True, cancel_futures=True)
    logger.info("Worker pools stopped")


def start_worker(
    service: CorridorKeyService,
    queue: GPUJobQueue,
    clips_dir: str,
) -> tuple[threading.Thread, threading.Event]:
    """Start the worker daemon thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=worker_loop,
        args=(service, queue, clips_dir, stop_event),
        daemon=True,
        name="worker-dispatcher",
    )
    thread.start()
    return thread, stop_event

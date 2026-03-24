"""Job submission, listing, and cancellation endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.job_queue import GPUJob, JobType

from ..auth import get_current_user
from ..credit_guard import check_credit_balance, estimate_gpu_seconds
from ..deps import get_queue, get_service
from ..nodes import registry
from ..schemas import (
    ExtractJobRequest,
    GVMJobRequest,
    InferenceJobRequest,
    InferenceParamsSchema,
    JobListResponse,
    JobSchema,
    OutputConfigSchema,
    PipelineJobRequest,
    VideoMaMaJobRequest,
)
from ..tier_guard import require_admin, require_member

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_member)])


def _stamp_job(job: GPUJob, request: Request | None, estimated_seconds: float = 0) -> GPUJob:
    """Set submitted_by and org_id from the authenticated user (CRKY-66).

    Also checks GPU credit balance before allowing submission (CRKY-37).
    Estimated_seconds is the projected GPU cost — if submitting this job
    would push the org over the credit ratio, it's rejected.
    """
    if request is None:
        return job
    # Check credit balance with projected cost (CRKY-37)
    check_credit_balance(request, estimated_seconds=estimated_seconds)
    user = get_current_user(request)
    if user:
        job.submitted_by = user.user_id
        # Use active org from X-Org-Id header, fall back to first org
        from ..orgs import get_org_store

        active_org = request.headers.get("X-Org-Id", "").strip()
        store = get_org_store()
        if active_org and (user.is_admin or store.is_member(active_org, user.user_id)):
            job.org_id = active_org
        else:
            user_orgs = store.list_user_orgs(user.user_id)
            job.org_id = user_orgs[0].org_id if user_orgs else None
    return job


def _submit_jobs(jobs: list[GPUJob], request: Request) -> list[GPUJob]:
    """Stamp and submit a list of jobs to the queue."""
    queue = get_queue()
    for job in jobs:
        _stamp_job(job, request)
        queue.submit(job)
    return jobs


def _job_to_schema(job: GPUJob, is_admin: bool = False) -> JobSchema:
    # Resolve node ID to display name
    claimed = job.claimed_by
    if claimed and claimed != "local":
        node = registry.get_node(claimed)
        claimed = node.name if node else claimed

    # Compute duration and fps
    duration = 0.0
    fps = 0.0
    if job.started_at > 0:
        import time

        end = job.completed_at if job.completed_at > job.started_at else time.time()
        duration = round(end - job.started_at, 1)
        if job.current_frame > 0 and duration > 0:
            fps = round(job.current_frame / duration, 2)

    return JobSchema(
        id=job.id,
        job_type=job.job_type.value,
        clip_name=job.clip_name,
        status=job.status.value,
        current_frame=job.current_frame,
        total_frames=job.total_frames,
        error_message=job.error_message,
        claimed_by=claimed,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration_seconds=duration,
        fps=fps,
        priority=job.priority,
        shard_group=job.shard_group,
        shard_index=job.shard_index,
        shard_total=job.shard_total,
        org_id=job.org_id if is_admin else None,
        submitted_by=job.submitted_by if is_admin else None,
    )


@router.get("/estimate", summary="Estimate GPU cost")
def estimate_job_cost(job_type: str = "inference", frame_count: int = 0, num_shards: int = 1):
    """Estimate GPU cost for a job based on historical data (CRKY-34)."""
    from ..credit_guard import _DEFAULT_SPF

    if job_type not in _DEFAULT_SPF:
        raise HTTPException(status_code=400, detail=f"Unknown job type: {job_type}")

    estimated_seconds = estimate_gpu_seconds(job_type, frame_count)
    avg_spf = estimated_seconds / max(1, frame_count)
    wall_clock = estimated_seconds / max(1, num_shards)

    return {
        "job_type": job_type,
        "frame_count": frame_count,
        "num_shards": num_shards,
        "avg_seconds_per_frame": round(avg_spf, 3),
        "estimated_gpu_seconds": round(estimated_seconds, 1),
        "estimated_gpu_minutes": round(estimated_seconds / 60, 1),
        "estimated_wall_clock_seconds": round(wall_clock, 1),
    }


@router.get("", response_model=JobListResponse, summary="List all jobs")
def list_jobs(request: Request):
    """Return running, queued, and completed jobs filtered by the user's org membership."""
    queue = get_queue()
    user = get_current_user(request)

    # Look up user's org_ids from org store (JWT org_ids may be empty)
    user_org_ids: set[str] = set()
    if user and not user.is_admin:
        from ..orgs import get_org_store

        user_org_ids = {o.org_id for o in get_org_store().list_user_orgs(user.user_id)}

    def _visible(job):
        """Filter jobs by org — admins see all, members see their org's jobs only."""
        if not user or user.is_admin:
            return True
        if not job.org_id:
            return False  # Jobs without an org are hidden from non-admins
        return job.org_id in user_org_ids

    admin = user.is_admin if user else False
    running = [j for j in queue.running_jobs if _visible(j)]
    queued = [j for j in queue.queue_snapshot if _visible(j)]
    history = [j for j in queue.history_snapshot if _visible(j)]
    return JobListResponse(
        current=_job_to_schema(running[0], is_admin=admin) if running else None,
        running=[_job_to_schema(j, is_admin=admin) for j in running],
        queued=[_job_to_schema(j, is_admin=admin) for j in queued],
        history=[_job_to_schema(j, is_admin=admin) for j in history],
    )


@router.post("/inference", response_model=list[JobSchema], summary="Submit inference job")
def submit_inference(req: InferenceJobRequest, request: Request):
    """Submit CorridorKey inference for one or more clips. Requires alpha hints to be ready."""
    from ..org_isolation import resolve_clips_dir

    queue = get_queue()
    service = get_service()
    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    submitted = []
    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        frame_count = clip.input_asset.frame_count if clip and clip.input_asset else 0
        est = estimate_gpu_seconds("inference", frame_count)

        job = GPUJob(
            job_type=JobType.INFERENCE,
            clip_name=clip_name,
            params={
                "inference_params": req.params.model_dump(),
                "output_config": req.output_config.model_dump(),
                "frame_range": list(req.frame_range) if req.frame_range else None,
            },
        )
        _stamp_job(job, request, estimated_seconds=est)
        if queue.submit(job):
            submitted.append(_job_to_schema(job))
    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


class ShardedInferenceRequest(BaseModel):
    clip_names: list[str] = Field(max_length=100)
    params: InferenceParamsSchema = InferenceParamsSchema()
    output_config: OutputConfigSchema = OutputConfigSchema()
    num_shards: int = Field(0, ge=0, le=64)
    min_shard_size: int = Field(50, ge=1)


@router.post("/inference/sharded", response_model=list[JobSchema])
def submit_sharded_inference(req: ShardedInferenceRequest, request: Request):
    """Submit inference split across multiple GPUs/nodes.

    Each shard processes a frame range independently. Only works for
    inference (GVM/VideoMaMa have temporal dependencies).
    """
    queue = get_queue()
    service = get_service()
    submitted = []

    # Count ACTUALLY available GPU slots — only free GPUs that can accept work
    from ..worker import get_local_gpu_enabled

    gpu_weights: list[float] = []

    # Local GPUs — only if local GPU processing is enabled
    if get_local_gpu_enabled():
        try:
            from device_utils import enumerate_gpus

            for g in enumerate_gpus():
                gpu_weights.append(max(1.0, g.vram_total_gb))
        except Exception:
            gpu_weights.append(1.0)

    # Remote nodes — only online, not busy, not paused, accepting inference
    online_nodes = [
        n for n in registry.list_nodes()
        if n.can_accept_jobs and n.accepts_job_type("inference") and n.status != "busy"
    ]
    for node in online_nodes:
        if node.gpus:
            for g in node.gpus:
                if g.status != "busy":
                    gpu_weights.append(max(1.0, g.vram_total_gb))
        else:
            gpu_weights.append(max(1.0, node.vram_total_gb))

    available_gpus = len(gpu_weights)

    from ..org_isolation import resolve_clips_dir

    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        if clip is None:
            continue

        frame_count = clip.input_asset.frame_count if clip.input_asset else 0
        if frame_count == 0:
            continue

        # Determine shard count
        num_shards = req.num_shards if req.num_shards > 0 else available_gpus
        # Don't create shards smaller than min_shard_size
        max_shards = max(1, frame_count // req.min_shard_size)
        num_shards = min(num_shards, max_shards)

        if num_shards <= 1:
            # Not worth sharding — submit as single job
            job = GPUJob(
                job_type=JobType.INFERENCE,
                clip_name=clip_name,
                params={
                    "inference_params": req.params.model_dump(),
                    "output_config": req.output_config.model_dump(),
                },
            )
            _stamp_job(job, request)
            if queue.submit(job):
                submitted.append(_job_to_schema(job))
            continue

        # Create shard group with proportional frame distribution
        group_id = uuid.uuid4().hex[:8]
        # Distribute frames evenly — inference is compute-bound, not VRAM-bound.
        # A 24GB card and a 96GB card process at the same speed per frame.
        shard_ranges = []
        base = frame_count // num_shards
        remainder = frame_count % num_shards
        cursor = 0
        for i in range(num_shards):
            # Spread remainder across first N shards (each gets +1 frame)
            size = base + (1 if i < remainder else 0)
            shard_ranges.append((cursor, cursor + size - 1))  # inclusive end
            cursor += size

        for i, (start, end) in enumerate(shard_ranges):
            job = GPUJob(
                job_type=JobType.INFERENCE,
                clip_name=clip_name,
                params={
                    "inference_params": req.params.model_dump(),
                    "output_config": req.output_config.model_dump(),
                    "frame_range": [start, end],  # inclusive range for run_inference
                },
                shard_group=group_id,
                shard_index=i,
                shard_total=num_shards,
            )
            _stamp_job(job, request)
            if queue.submit(job):
                submitted.append(_job_to_schema(job))

    if not submitted:
        raise HTTPException(status_code=409, detail="No jobs submitted")
    return submitted


@router.get("/shard-group/{group_id}")
def get_shard_group_progress(group_id: str, request: Request):
    """Get combined progress for all shards in a group."""
    queue = get_queue()
    # Verify the requesting user owns at least one shard in the group
    _check_shard_group_ownership(queue, group_id, request)
    return queue.shard_group_progress(group_id)


@router.delete("/shard-group/{group_id}")
def cancel_shard_group(group_id: str, request: Request):
    """Cancel all shards in a group. Only the submitter or platform admin."""
    queue = get_queue()
    _check_shard_group_ownership(queue, group_id, request)
    count = queue.cancel_shard_group(group_id)
    return {"status": "cancelled", "shard_group": group_id, "cancelled": count}


@router.post("/shard-group/{group_id}/retry")
def retry_shard_group(group_id: str, request: Request):
    """Re-submit failed shards from a group. Only the submitter or platform admin."""
    queue = get_queue()
    _check_shard_group_ownership(queue, group_id, request)
    check_credit_balance(request)
    new_jobs = queue.retry_failed_shards(group_id)
    return {
        "status": "retried",
        "shard_group": group_id,
        "resubmitted": len(new_jobs),
        "jobs": [_job_to_schema(j) for j in new_jobs],
    }


def _gpu_speed_weights(job_type: str) -> dict[str, float]:
    """Build GPU speed weights from job history.

    Returns {gpu_name: median_fps}. Only uses local GPU jobs (claimed_by="local")
    to avoid polluting speed data with HTTP transfer overhead from remote nodes.
    Falls back to VRAM-based heuristic for GPUs with no history.
    """
    queue = get_queue()
    history = queue.history_snapshot

    # Collect fps per GPU model from completed LOCAL jobs only
    fps_by_gpu: dict[str, list[float]] = {}
    for j in history:
        if (
            j.status.value == "completed"
            and j.job_type.value == job_type
            and j.total_frames > 0
            and j.started_at > 0
            and j.completed_at > j.started_at
            and j.claimed_by == "local"
        ):
            duration = j.completed_at - j.started_at
            fps = j.total_frames / duration
            if fps > 0 and fps < 100:  # sanity bound
                # Look up the GPU name from the device (local GPU)
                try:
                    import torch
                    if torch.cuda.is_available():
                        gpu_name = torch.cuda.get_device_name(0)
                        fps_by_gpu.setdefault(gpu_name, []).append(fps)
                except Exception:
                    pass

    # Also collect from remote nodes — but only if they used shared storage
    # (no HTTP transfer overhead). We can identify this by fast completion times.
    for j in history:
        if (
            j.status.value == "completed"
            and j.job_type.value == job_type
            and j.total_frames > 0
            and j.started_at > 0
            and j.completed_at > j.started_at
            and j.claimed_by
            and j.claimed_by != "local"
        ):
            node = registry.get_node(j.claimed_by)
            if node and node.shared_storage:
                # Shared storage = no transfer overhead, safe to use
                duration = j.completed_at - j.started_at
                fps = j.total_frames / duration
                if fps > 0 and fps < 100:
                    gpu_name = node.gpus[0].name if node.gpus else node.gpu_name
                    if gpu_name:
                        fps_by_gpu.setdefault(gpu_name, []).append(fps)

    # Compute median fps per GPU
    result: dict[str, float] = {}
    for gpu_name, fps_list in fps_by_gpu.items():
        fps_list.sort()
        result[gpu_name] = fps_list[len(fps_list) // 2]

    return result


def _weighted_shard_sizes(frame_count: int, gpu_names: list[str], speed_map: dict[str, float]) -> list[int]:
    """Distribute frames proportionally to GPU speed.

    GPUs with known speed get proportional shares. GPUs with unknown speed
    get the median share (assume average performance).
    """
    if not gpu_names:
        return []

    # Get speed for each GPU, defaulting unknowns to the median known speed
    known_speeds = [speed_map[g] for g in gpu_names if g in speed_map]
    default_speed = sorted(known_speeds)[len(known_speeds) // 2] if known_speeds else 1.0

    weights = [speed_map.get(g, default_speed) for g in gpu_names]
    total_weight = sum(weights)

    # Distribute frames proportionally
    sizes = []
    remaining = frame_count
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            sizes.append(remaining)  # last GPU gets whatever's left
        else:
            share = max(1, round(frame_count * w / total_weight))
            share = min(share, remaining)
            sizes.append(share)
            remaining -= share

    return sizes


def _get_available_gpus(job_type: str) -> list[str]:
    """Get list of GPU model names for available workers (local + remote nodes)."""
    from ..worker import get_local_gpu_enabled

    gpu_names: list[str] = []

    if get_local_gpu_enabled():
        try:
            import torch
            if torch.cuda.is_available():
                gpu_names.append(torch.cuda.get_device_name(0))
            else:
                gpu_names.append("unknown")
        except Exception:
            gpu_names.append("unknown")

    online_nodes = [
        n for n in registry.list_nodes()
        if n.can_accept_jobs and n.accepts_job_type(job_type) and n.status != "busy"
    ]
    for node in online_nodes:
        if node.gpus:
            for g in node.gpus:
                if g.status != "busy":
                    gpu_names.append(g.name or "unknown")
        else:
            gpu_names.append(node.gpu_name or "unknown")

    return gpu_names


def _build_gvm_jobs(clip_name: str, frame_count: int, extra_params: dict | None = None) -> list[GPUJob]:
    """Build GVM jobs for a clip, auto-sharding across available GPUs.

    Distributes frames proportionally to GPU speed (from job history).
    Falls back to even distribution when no history is available.
    """
    gpu_names = _get_available_gpus("gvm_alpha")
    min_shard = 20
    params = dict(extra_params) if extra_params else {}

    num_shards = min(len(gpu_names), frame_count // min_shard) if gpu_names else 0
    if num_shards > 1:
        speed_map = _gpu_speed_weights("gvm_alpha")
        sizes = _weighted_shard_sizes(frame_count, gpu_names[:num_shards], speed_map)
        group_id = uuid.uuid4().hex[:8]
        cursor = 0
        jobs = []
        for i, size in enumerate(sizes):
            job = GPUJob(
                job_type=JobType.GVM_ALPHA,
                clip_name=clip_name,
                params={**params, "frame_range": [cursor, cursor + size]},
                shard_group=group_id,
                shard_index=i,
                shard_total=len(sizes),
            )
            jobs.append(job)
            cursor += size
        if speed_map:
            logger.info(f"GVM sharding for '{clip_name}': {sizes} frames across {gpu_names[:num_shards]} (speed-weighted)")
        return jobs

    return [GPUJob(job_type=JobType.GVM_ALPHA, clip_name=clip_name, params=params)]


def _build_inference_shards(clip_name: str, frame_count: int, extra_params: dict | None = None) -> list[GPUJob]:
    """Build inference jobs for a clip, auto-sharding across available GPUs.

    Distributes frames proportionally to GPU speed (from job history).
    Uses inclusive frame ranges (run_inference treats end as inclusive).
    """
    gpu_names = _get_available_gpus("inference")
    min_shard = 50
    params = dict(extra_params) if extra_params else {}

    num_shards = min(len(gpu_names), frame_count // min_shard) if gpu_names else 0
    if num_shards > 1:
        speed_map = _gpu_speed_weights("inference")
        sizes = _weighted_shard_sizes(frame_count, gpu_names[:num_shards], speed_map)
        group_id = uuid.uuid4().hex[:8]
        cursor = 0
        jobs = []
        for i, size in enumerate(sizes):
            job = GPUJob(
                job_type=JobType.INFERENCE,
                clip_name=clip_name,
                params={**params, "frame_range": [cursor, cursor + size - 1]},  # inclusive end
                shard_group=group_id,
                shard_index=i,
                shard_total=len(sizes),
            )
            jobs.append(job)
            cursor += size
        if speed_map:
            logger.info(f"Inference sharding for '{clip_name}': {sizes} frames across {gpu_names[:num_shards]} (speed-weighted)")
        return jobs

    return [GPUJob(job_type=JobType.INFERENCE, clip_name=clip_name, params=params)]


@router.post("/gvm", response_model=list[JobSchema], summary="Submit GVM alpha generation")
def submit_gvm(req: GVMJobRequest, request: Request):
    """Generate alpha hints using Generative Video Matting.

    Automatically shards across available nodes when multiple GPUs are
    online. Each node processes a subset of frames independently (batch=1).
    Falls back to single-node when no other GPUs are available.
    """
    queue = get_queue()
    service = get_service()
    submitted = []

    from ..org_isolation import resolve_clips_dir

    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        if clip is None:
            continue
        frame_count = clip.input_asset.frame_count if clip.input_asset else 0
        est = estimate_gpu_seconds("gvm_alpha", frame_count)
        for job in _build_gvm_jobs(clip_name, frame_count):
            _stamp_job(job, request, estimated_seconds=est)
            if queue.submit(job):
                submitted.append(_job_to_schema(job))

    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


@router.post("/videomama", response_model=list[JobSchema], summary="Submit VideoMaMa alpha generation")
def submit_videomama(req: VideoMaMaJobRequest, request: Request):
    """Generate alpha hints using VideoMaMa mask-driven matting for one or more clips."""
    from ..org_isolation import resolve_clips_dir

    queue = get_queue()
    service = get_service()
    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    submitted = []
    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        frame_count = clip.input_asset.frame_count if clip and clip.input_asset else 0
        est = estimate_gpu_seconds("videomama_alpha", frame_count)

        job = GPUJob(
            job_type=JobType.VIDEOMAMA_ALPHA,
            clip_name=clip_name,
            params={"chunk_size": req.chunk_size},
        )
        _stamp_job(job, request, estimated_seconds=est)
        if queue.submit(job):
            submitted.append(_job_to_schema(job))
    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


@router.post("/pipeline", response_model=list[JobSchema])
def submit_pipeline(req: PipelineJobRequest, request: Request):
    """Submit the first step of a full pipeline.

    Only queues the NEXT needed step for each clip. When that step
    completes, the worker auto-chains the following step (via the
    pipeline params stored on the job). This ensures each step finishes
    before the next begins.
    """
    from ..org_isolation import resolve_clips_dir

    queue = get_queue()
    service = get_service()
    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    # Pipeline params stored on each job so the worker can chain the next step
    pipeline_params = {
        "pipeline": True,
        "alpha_method": req.alpha_method,
        "inference_params": req.params.model_dump(),
        "output_config": req.output_config.model_dump(),
    }

    submitted = []
    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        if not clip:
            continue

        state = clip.state.value

        if state == "EXTRACTING":
            jobs = [GPUJob(job_type=JobType.VIDEO_EXTRACT, clip_name=clip_name, params=pipeline_params)]
        elif state == "RAW":
            if req.alpha_method == "videomama":
                jobs = [GPUJob(
                    job_type=JobType.VIDEOMAMA_ALPHA,
                    clip_name=clip_name,
                    params={**pipeline_params, "chunk_size": 50},
                )]
            else:
                frame_count = clip.input_asset.frame_count if clip.input_asset else 0
                jobs = _build_gvm_jobs(clip_name, frame_count, extra_params=pipeline_params)
        elif state == "MASKED":
            jobs = [GPUJob(
                job_type=JobType.VIDEOMAMA_ALPHA,
                clip_name=clip_name,
                params={**pipeline_params, "chunk_size": 50},
            )]
        elif state in ("READY", "COMPLETE"):
            jobs = [GPUJob(job_type=JobType.INFERENCE, clip_name=clip_name, params=pipeline_params)]
        else:
            continue

        # Estimate the FULL remaining pipeline cost, not just this step.
        # The pipeline auto-chains, so we need to project the total.
        frame_count = clip.input_asset.frame_count if clip.input_asset else 0
        est = 0.0
        if state in ("EXTRACTING", "RAW"):
            est += estimate_gpu_seconds("gvm_alpha", frame_count)
            est += estimate_gpu_seconds("inference", frame_count)
        elif state in ("MASKED",):
            est += estimate_gpu_seconds("videomama_alpha", frame_count)
            est += estimate_gpu_seconds("inference", frame_count)
        elif state in ("READY", "COMPLETE"):
            est += estimate_gpu_seconds("inference", frame_count)

        for job in jobs:
            _stamp_job(job, request, estimated_seconds=est)
            est = 0  # only charge once for the whole pipeline
            if queue.submit(job):
                submitted.append(_job_to_schema(job))

    if not submitted:
        raise HTTPException(status_code=409, detail="No jobs submitted (clips may already be complete or duplicates)")
    return submitted


@router.post("/extract", response_model=list[JobSchema], summary="Submit frame extraction")
def submit_extract(req: ExtractJobRequest, request: Request):
    """Extract frames from video source files for one or more clips."""
    queue = get_queue()
    submitted = []
    for clip_name in req.clip_names:
        job = GPUJob(job_type=JobType.VIDEO_EXTRACT, clip_name=clip_name)
        _stamp_job(job, request)
        if queue.submit(job):
            submitted.append(_job_to_schema(job))
    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


def _check_job_ownership(job: GPUJob, request: Request) -> None:
    """Verify the requesting user owns the job or is a platform admin (CRKY-66)."""
    user = get_current_user(request)
    if not user:
        return  # Auth disabled
    if user.is_admin:
        return  # Platform admins bypass
    # Deny by default — user must be the submitter
    if not job.submitted_by or job.submitted_by != user.user_id:
        raise HTTPException(status_code=403, detail="You can only manage your own jobs")


def _check_shard_group_ownership(queue, group_id: str, request: Request) -> None:
    """Verify the requesting user owns the shard group or is a platform admin."""
    user = get_current_user(request)
    if not user:
        return  # Auth disabled
    if user.is_admin:
        return
    all_jobs = list(queue.queue_snapshot) + queue.running_jobs + list(queue.history_snapshot)
    group_jobs = [j for j in all_jobs if j.shard_group == group_id]
    if not group_jobs:
        raise HTTPException(status_code=404, detail="Shard group not found")
    # Deny if ANY shard in the group belongs to another user
    for j in group_jobs:
        if not j.submitted_by or j.submitted_by != user.user_id:
            raise HTTPException(status_code=403, detail="You can only manage your own shard groups")


@router.delete("/{job_id}", summary="Cancel a job")
def cancel_job(job_id: str, request: Request):
    """Cancel a running or queued job. Only the submitter or platform admin can cancel."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    _check_job_ownership(job, request)
    queue.cancel_job(job)
    return {"status": "cancelled", "job_id": job_id}


@router.post("/{job_id}/move")
def move_job(job_id: str, position: int, request: Request):
    """Move a queued job to a specific position (0 = front of queue)."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found in queue")
    _check_job_ownership(job, request)
    if not queue.move_job(job_id, position):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found in queue")
    return {"status": "moved", "job_id": job_id, "position": position}


@router.post("/{job_id}/priority")
def set_job_priority(job_id: str, priority: int, request: Request):
    """Set priority for a queued job. Higher = processed first."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    _check_job_ownership(job, request)
    if job.status.value != "queued":
        raise HTTPException(status_code=409, detail="Can only set priority on queued jobs")
    job.priority = priority
    return {"status": "ok", "job_id": job_id, "priority": priority}


@router.get("/{job_id}/log")
def get_job_log(job_id: str, request: Request):
    """Get detailed error/log info for a job. Only the submitter or platform admin."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    _check_job_ownership(job, request)
    return {
        "id": job.id,
        "job_type": job.job_type.value,
        "clip_name": job.clip_name,
        "status": job.status.value,
        "error_message": job.error_message,
        "current_frame": job.current_frame,
        "total_frames": job.total_frames,
    }


@router.delete("", summary="Cancel all jobs", dependencies=[Depends(require_admin)])
def cancel_all():
    """Cancel all running and queued jobs. Requires platform_admin."""
    queue = get_queue()
    queue.cancel_all()
    return {"status": "all_cancelled"}

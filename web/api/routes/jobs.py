"""Job submission, listing, and cancellation endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.job_queue import GPUJob, JobType

from ..auth import get_current_user
from ..credit_guard import check_credit_balance
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


def _stamp_job(job: GPUJob, request: Request | None) -> GPUJob:
    """Set submitted_by and org_id from the authenticated user (CRKY-66).

    Also checks GPU credit balance before allowing submission (CRKY-37).
    """
    if request is None:
        return job
    # Check credit balance before allowing job (CRKY-37)
    check_credit_balance(request)
    user = get_current_user(request)
    if user:
        job.submitted_by = user.user_id
        # Look up org from org store (JWT org_ids may be empty)
        from ..orgs import get_org_store

        user_orgs = get_org_store().list_user_orgs(user.user_id)
        job.org_id = user_orgs[0].org_id if user_orgs else None
    return job


def _submit_jobs(jobs: list[GPUJob], request: Request) -> list[GPUJob]:
    """Stamp and submit a list of jobs to the queue."""
    queue = get_queue()
    for job in jobs:
        _stamp_job(job, request)
        queue.submit(job)
    return jobs


def _job_to_schema(job: GPUJob) -> JobSchema:
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
    )


@router.get("/estimate", summary="Estimate GPU cost")
def estimate_job_cost(job_type: str = "inference", frame_count: int = 0, num_shards: int = 1):
    """Estimate GPU cost for a job based on historical data (CRKY-34).

    Returns estimated GPU-seconds, GPU-minutes, and wall-clock time.
    """
    # Default estimates per job type (used when no history available)
    defaults = {
        "inference": 1.5,   # ~1.5s per frame on RTX 3090, ~0.5s on RTX 4090
        "gvm_alpha": 2.5,   # ~2.5s per frame (heavy diffusion model)
        "videomama_alpha": 1.5,
        "video_extract": 0.05,
        "video_stitch": 0.02,
    }

    if job_type not in defaults:
        raise HTTPException(status_code=400, detail=f"Unknown job type: {job_type}")

    queue = get_queue()
    history = queue.history_snapshot

    # Compute median seconds-per-frame from completed jobs with valid timing
    completed = [
        j for j in history
        if j.status.value == "completed"
        and j.job_type.value == job_type
        and j.total_frames > 0
        and j.started_at > 0
        and j.completed_at > j.started_at
    ]

    if completed:
        # Per-job seconds-per-frame, capped at 60s/frame to filter outliers
        spf_values = []
        for j in completed:
            duration = j.completed_at - j.started_at
            spf = duration / j.total_frames
            if spf < 60:  # ignore jobs where download/upload dominated
                spf_values.append(spf)
        if spf_values:
            spf_values.sort()
            avg_spf = spf_values[len(spf_values) // 2]  # median
        else:
            avg_spf = defaults.get(job_type, 1.0)
    else:
        avg_spf = defaults.get(job_type, 1.0)

    estimated_seconds = avg_spf * frame_count
    wall_clock = estimated_seconds / max(1, num_shards)

    return {
        "job_type": job_type,
        "frame_count": frame_count,
        "num_shards": num_shards,
        "avg_seconds_per_frame": round(avg_spf, 3),
        "estimated_gpu_seconds": round(estimated_seconds, 1),
        "estimated_gpu_minutes": round(estimated_seconds / 60, 1),
        "estimated_wall_clock_seconds": round(wall_clock, 1),
        "based_on_history": len(completed) if completed else 0,
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

    running = [j for j in queue.running_jobs if _visible(j)]
    queued = [j for j in queue.queue_snapshot if _visible(j)]
    history = [j for j in queue.history_snapshot if _visible(j)]
    return JobListResponse(
        current=_job_to_schema(running[0]) if running else None,
        running=[_job_to_schema(j) for j in running],
        queued=[_job_to_schema(j) for j in queued],
        history=[_job_to_schema(j) for j in history],
    )


@router.post("/inference", response_model=list[JobSchema], summary="Submit inference job")
def submit_inference(req: InferenceJobRequest, request: Request):
    """Submit CorridorKey inference for one or more clips. Requires alpha hints to be ready."""
    queue = get_queue()
    submitted = []
    for clip_name in req.clip_names:
        job = GPUJob(
            job_type=JobType.INFERENCE,
            clip_name=clip_name,
            params={
                "inference_params": req.params.model_dump(),
                "output_config": req.output_config.model_dump(),
                "frame_range": list(req.frame_range) if req.frame_range else None,
            },
        )
        _stamp_job(job, request)
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
    from ..worker import get_local_gpu_enabled

    clips = service.scan_clips(resolve_clips_dir(request))
    clip_map = {c.name: c for c in clips}

    available = 0
    if get_local_gpu_enabled():
        available += 1
    online_nodes = [
        n for n in registry.list_nodes()
        if n.can_accept_jobs and n.accepts_job_type("gvm_alpha") and n.status != "busy"
    ]
    available += len(online_nodes)

    for clip_name in req.clip_names:
        clip = clip_map.get(clip_name)
        if clip is None:
            continue
        frame_count = clip.input_asset.frame_count if clip.input_asset else 0

        min_shard = 20

        if available > 1 and frame_count > min_shard:
            num_shards = min(available, frame_count // min_shard)
            if num_shards > 1:
                group_id = uuid.uuid4().hex[:8]
                base = frame_count // num_shards
                remainder = frame_count % num_shards
                cursor = 0
                for i in range(num_shards):
                    size = base + (1 if i < remainder else 0)
                    job = GPUJob(
                        job_type=JobType.GVM_ALPHA,
                        clip_name=clip_name,
                        params={"frame_range": [cursor, cursor + size]},
                        shard_group=group_id,
                        shard_index=i,
                        shard_total=num_shards,
                    )
                    _stamp_job(job, request)
                    if queue.submit(job):
                        submitted.append(_job_to_schema(job))
                    cursor += size
                continue

        # Single node — not enough GPUs to shard
        job = GPUJob(job_type=JobType.GVM_ALPHA, clip_name=clip_name)
        _stamp_job(job, request)
        if queue.submit(job):
            submitted.append(_job_to_schema(job))

    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


@router.post("/videomama", response_model=list[JobSchema], summary="Submit VideoMaMa alpha generation")
def submit_videomama(req: VideoMaMaJobRequest, request: Request):
    """Generate alpha hints using VideoMaMa mask-driven matting for one or more clips."""
    queue = get_queue()
    submitted = []
    for clip_name in req.clip_names:
        job = GPUJob(
            job_type=JobType.VIDEOMAMA_ALPHA,
            clip_name=clip_name,
            params={"chunk_size": req.chunk_size},
        )
        _stamp_job(job, request)
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
            job = GPUJob(job_type=JobType.VIDEO_EXTRACT, clip_name=clip_name, params=pipeline_params)
        elif state == "RAW":
            if req.alpha_method == "videomama":
                job = GPUJob(
                    job_type=JobType.VIDEOMAMA_ALPHA,
                    clip_name=clip_name,
                    params={**pipeline_params, "chunk_size": 50},
                )
            else:
                job = GPUJob(job_type=JobType.GVM_ALPHA, clip_name=clip_name, params=pipeline_params)
        elif state == "MASKED":
            # MASKED clips already have a mask — run VideoMaMa to generate alpha, then inference
            job = GPUJob(
                job_type=JobType.VIDEOMAMA_ALPHA,
                clip_name=clip_name,
                params={**pipeline_params, "chunk_size": 50},
            )
        elif state in ("READY", "COMPLETE"):
            job = GPUJob(job_type=JobType.INFERENCE, clip_name=clip_name, params=pipeline_params)
        else:
            continue

        _stamp_job(job, request)
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

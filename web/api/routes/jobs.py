"""Job submission, listing, and cancellation endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

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
from ..tier_guard import require_member

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
        job.org_id = user.org_ids[0] if user.org_ids else None
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
        priority=job.priority,
        shard_group=job.shard_group,
        shard_index=job.shard_index,
        shard_total=job.shard_total,
    )


@router.get("/estimate")
def estimate_job_cost(job_type: str = "inference", frame_count: int = 0, num_shards: int = 1):
    """Estimate GPU cost for a job based on historical data (CRKY-34).

    Returns estimated GPU-seconds, GPU-minutes, and wall-clock time.
    """
    import time

    queue = get_queue()
    history = queue.history_snapshot

    # Compute average seconds-per-frame from completed jobs of this type
    completed = [
        j for j in history
        if j.status.value == "completed"
        and j.job_type.value == job_type
        and j.total_frames > 0
        and j.started_at > 0
    ]

    if completed:
        total_time = sum(time.time() - j.started_at for j in completed if j.started_at > 0)
        total_frames = sum(j.total_frames for j in completed)
        avg_spf = total_time / total_frames if total_frames > 0 else 0
    else:
        # Default estimates per job type (rough, based on typical hardware)
        defaults = {
            "inference": 0.5,  # ~0.5s per frame on RTX 4090
            "gvm_alpha": 2.0,
            "videomama_alpha": 1.5,
            "video_extract": 0.05,
            "video_stitch": 0.02,
        }
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


@router.get("", response_model=JobListResponse)
def list_jobs():
    queue = get_queue()
    running = queue.running_jobs
    return JobListResponse(
        current=_job_to_schema(running[0]) if running else None,
        running=[_job_to_schema(j) for j in running],
        queued=[_job_to_schema(j) for j in queue.queue_snapshot],
        history=[_job_to_schema(j) for j in queue.history_snapshot],
    )


@router.post("/inference", response_model=list[JobSchema])
def submit_inference(req: InferenceJobRequest, request: Request):
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
    clip_names: list[str]
    params: InferenceParamsSchema = InferenceParamsSchema()
    output_config: OutputConfigSchema = OutputConfigSchema()
    num_shards: int = 0  # 0 = auto (based on available GPUs/nodes)
    min_shard_size: int = 50  # don't shard below this frame count


@router.post("/inference/sharded", response_model=list[JobSchema])
def submit_sharded_inference(req: ShardedInferenceRequest, request: Request):
    """Submit inference split across multiple GPUs/nodes.

    Each shard processes a frame range independently. Only works for
    inference (GVM/VideoMaMa have temporal dependencies).
    """
    queue = get_queue()
    service = get_service()
    submitted = []

    # Count available GPU slots with VRAM weights for proportional sharding
    gpu_weights: list[float] = []
    try:
        from device_utils import enumerate_gpus

        for g in enumerate_gpus():
            gpu_weights.append(max(1.0, g.vram_total_gb))
    except Exception:
        gpu_weights.append(1.0)

    online_nodes = [n for n in registry.list_nodes() if n.can_accept_jobs and n.accepts_job_type("inference")]
    for node in online_nodes:
        if node.gpus:
            for g in node.gpus:
                gpu_weights.append(max(1.0, g.vram_total_gb))
        else:
            gpu_weights.append(max(1.0, node.vram_total_gb))

    available_gpus = len(gpu_weights)

    for clip_name in req.clip_names:
        # Get frame count
        from ..org_isolation import resolve_clips_dir

        clips = service.scan_clips(resolve_clips_dir(request))
        clip = next((c for c in clips if c.name == clip_name), None)
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
        weights = gpu_weights[:num_shards]
        total_weight = sum(weights)
        # Distribute frames proportionally to GPU VRAM
        shard_ranges = []
        cursor = 0
        for i, w in enumerate(weights):
            if i == num_shards - 1:
                shard_ranges.append((cursor, frame_count))
            else:
                frames = max(1, round(frame_count * w / total_weight))
                shard_ranges.append((cursor, min(cursor + frames, frame_count)))
                cursor += frames

        for i, (start, end) in enumerate(shard_ranges):
            job = GPUJob(
                job_type=JobType.INFERENCE,
                clip_name=clip_name,
                params={
                    "inference_params": req.params.model_dump(),
                    "output_config": req.output_config.model_dump(),
                    "frame_range": [start, end],
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
def get_shard_group_progress(group_id: str):
    """Get combined progress for all shards in a group."""
    queue = get_queue()
    return queue.shard_group_progress(group_id)


@router.delete("/shard-group/{group_id}")
def cancel_shard_group(group_id: str):
    """Cancel all shards in a group."""
    queue = get_queue()
    count = queue.cancel_shard_group(group_id)
    return {"status": "cancelled", "shard_group": group_id, "cancelled": count}


@router.post("/shard-group/{group_id}/retry")
def retry_shard_group(group_id: str):
    """Re-submit failed shards from a group."""
    queue = get_queue()
    new_jobs = queue.retry_failed_shards(group_id)
    return {
        "status": "retried",
        "shard_group": group_id,
        "resubmitted": len(new_jobs),
        "jobs": [_job_to_schema(j) for j in new_jobs],
    }


@router.post("/gvm", response_model=list[JobSchema])
def submit_gvm(req: GVMJobRequest, request: Request):
    queue = get_queue()
    submitted = []
    for clip_name in req.clip_names:
        job = GPUJob(job_type=JobType.GVM_ALPHA, clip_name=clip_name)
        _stamp_job(job, request)
        if queue.submit(job):
            submitted.append(_job_to_schema(job))
    if not submitted:
        raise HTTPException(status_code=409, detail="All jobs rejected (duplicates)")
    return submitted


@router.post("/videomama", response_model=list[JobSchema])
def submit_videomama(req: VideoMaMaJobRequest, request: Request):
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


@router.post("/extract", response_model=list[JobSchema])
def submit_extract(req: ExtractJobRequest, request: Request):
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
    if job.submitted_by and job.submitted_by != user.user_id:
        raise HTTPException(status_code=403, detail="You can only manage your own jobs")


@router.delete("/{job_id}")
def cancel_job(job_id: str, request: Request):
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
def get_job_log(job_id: str):
    """Get detailed error/log info for a job."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {
        "id": job.id,
        "job_type": job.job_type.value,
        "clip_name": job.clip_name,
        "status": job.status.value,
        "error_message": job.error_message,
        "current_frame": job.current_frame,
        "total_frames": job.total_frames,
        "params": job.params,
    }


@router.delete("")
def cancel_all():
    queue = get_queue()
    queue.cancel_all()
    return {"status": "all_cancelled"}

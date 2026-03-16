"""Node management endpoints — registration, heartbeat, job dispatch, file transfer."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.job_queue import JobStatus
from backend.natural_sort import natsorted

from ..deps import get_queue, get_service
from ..nodes import GPUSlot, NodeInfo, NodeSchedule, registry
from ..routes import clips as _clips_mod
from ..ws import manager

logger = logging.getLogger(__name__)

# Shared secret auth — set CK_AUTH_TOKEN on the server to require it
_AUTH_TOKEN = os.environ.get("CK_AUTH_TOKEN", "")


def _check_node_auth(request: Request) -> None:
    """Verify Bearer token if CK_AUTH_TOKEN is set on the server."""
    if not _AUTH_TOKEN:
        return  # no token configured, allow all
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid or missing auth token")


router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(_check_node_auth)])


# --- Schemas ---


class GPUSlotSchema(BaseModel):
    index: int
    name: str
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0


class NodeRegisterRequest(BaseModel):
    node_id: str
    name: str
    host: str
    gpus: list[GPUSlotSchema] = []
    gpu_name: str = ""
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    capabilities: list[str] = []
    shared_storage: str | None = None


class NodeHeartbeatRequest(BaseModel):
    vram_free_gb: float = 0.0
    status: str = "online"


class JobResultRequest(BaseModel):
    job_id: str
    status: str  # "completed" or "failed"
    error_message: str | None = None


# --- Registration ---


@router.post("/register")
def register_node(req: NodeRegisterRequest):
    """Register a new worker node or update an existing one."""
    gpu_slots = [
        GPUSlot(index=g.index, name=g.name, vram_total_gb=g.vram_total_gb, vram_free_gb=g.vram_free_gb)
        for g in req.gpus
    ]
    info = NodeInfo(
        node_id=req.node_id,
        name=req.name,
        host=req.host,
        gpus=gpu_slots,
        gpu_name=req.gpu_name,
        vram_total_gb=req.vram_total_gb,
        vram_free_gb=req.vram_free_gb,
        capabilities=req.capabilities,
        shared_storage=req.shared_storage,
    )
    registry.register(info)
    manager.send_node_update(info.to_dict())
    return {"status": "registered", "node_id": req.node_id}


@router.post("/{node_id}/heartbeat")
def node_heartbeat(node_id: str, req: NodeHeartbeatRequest):
    """Update node heartbeat and VRAM status."""
    if not registry.heartbeat(node_id, req.vram_free_gb, req.status):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not registered")
    node = registry.get_node(node_id)
    if node:
        manager.send_node_update(node.to_dict())
    return {"status": "ok"}


@router.delete("/{node_id}")
def unregister_node(node_id: str):
    registry.unregister(node_id)
    manager.send_node_offline(node_id)
    return {"status": "unregistered"}


@router.get("")
def list_nodes():
    """List all registered nodes."""
    return [n.to_dict() for n in registry.list_nodes()]


# --- Pause / Schedule ---


class NodeScheduleRequest(BaseModel):
    enabled: bool = False
    start: str = "00:00"
    end: str = "23:59"


@router.post("/{node_id}/pause")
def pause_node(node_id: str):
    """Pause a node — it won't receive new jobs until resumed."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.paused = True
    manager.send_node_update(node.to_dict())
    return {"status": "paused"}


@router.post("/{node_id}/resume")
def resume_node(node_id: str):
    """Resume a paused node."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.paused = False
    manager.send_node_update(node.to_dict())
    return {"status": "resumed"}


@router.get("/{node_id}/schedule")
def get_node_schedule(node_id: str):
    """Get a node's active hours schedule."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return node.schedule.to_dict()


@router.put("/{node_id}/schedule")
def set_node_schedule(node_id: str, req: NodeScheduleRequest):
    """Set a node's active hours schedule."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.schedule = NodeSchedule(enabled=req.enabled, start=req.start, end=req.end)
    manager.send_node_update(node.to_dict())
    return node.schedule.to_dict()


# --- Job dispatch ---


@router.get("/{node_id}/next-job")
def get_next_job(node_id: str):
    """Get the next available job for a node to process.

    The main machine assigns jobs from its queue to requesting nodes.
    Returns null if no jobs are available.
    """
    node = registry.get_node(node_id)
    if not node or not node.is_alive:
        raise HTTPException(status_code=404, detail="Node not registered or offline")

    if not node.can_accept_jobs:
        return {"job": None, "reason": "paused" if node.paused else "outside_schedule"}

    queue = get_queue()
    job = queue.claim_job(node_id)
    if job is None:
        return {"job": None}

    # Assign the job to this node
    registry.set_busy(node_id, job.id)
    manager.send_job_status(job.id, JobStatus.RUNNING.value)

    # Build job payload with file info
    clip = None
    service = get_service()
    clips = service.scan_clips(_clips_mod._clips_dir)
    for c in clips:
        if c.name == job.clip_name:
            clip = c
            break

    # Determine if node has shared storage (skip file transfer)
    use_shared = node.shared_storage is not None

    return {
        "job": {
            "id": job.id,
            "job_type": job.job_type.value,
            "clip_name": job.clip_name,
            "params": job.params,
            "use_shared_storage": use_shared,
            "shared_clip_root": clip.root_path if clip and use_shared else None,
            "clip": _clips_mod._clip_to_schema(clip).__dict__ if clip else None,
        }
    }


@router.post("/{node_id}/job-progress")
def report_job_progress(node_id: str, job_id: str, current: int, total: int):
    """Node reports job progress."""
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job:
        job.current_frame = current
        job.total_frames = total
    manager.send_job_progress(job_id, "", current, total)
    return {"status": "ok"}


@router.post("/{node_id}/job-result")
def report_job_result(node_id: str, req: JobResultRequest):
    """Node reports job completion or failure."""
    queue = get_queue()
    job = queue.find_job_by_id(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{req.job_id}' not found")

    if req.status == "completed":
        queue.complete_job(job)
        manager.send_job_status(job.id, JobStatus.COMPLETED.value)
    else:
        queue.fail_job(job, req.error_message or "Unknown error")
        manager.send_job_status(job.id, JobStatus.FAILED.value, error=req.error_message)

    registry.set_idle(node_id)

    # Trigger pipeline chaining if applicable
    from ..worker import _chain_next_pipeline_step

    if req.status == "completed":
        service = get_service()
        _chain_next_pipeline_step(job, queue, _clips_mod._clips_dir, service)

    return {"status": "ok"}


# --- File transfer (for nodes without shared storage) ---


@router.get("/{node_id}/files/{clip_name}/{pass_name}")
def list_clip_files(node_id: str, clip_name: str, pass_name: str):
    """List files available for download for a specific clip pass."""
    _PASS_MAP = {
        "input": ["Frames", "Input"],
        "alpha": ["AlphaHint"],
        "mask": ["VideoMamaMaskHint"],
        "source": ["Source"],
    }

    dirs = _PASS_MAP.get(pass_name)
    if not dirs:
        raise HTTPException(status_code=400, detail=f"Unknown pass: {pass_name}")

    service = get_service()
    clips = service.scan_clips(_clips_mod._clips_dir)
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    for d in dirs:
        target = os.path.join(clip.root_path, d)
        if os.path.isdir(target):
            files = natsorted(os.listdir(target))
            return {"directory": d, "files": files, "clip_root": clip.root_path}

    return {"directory": None, "files": [], "clip_root": clip.root_path}


@router.get("/{node_id}/files/{clip_name}/{pass_name}/{filename}")
def download_clip_file(node_id: str, clip_name: str, pass_name: str, filename: str):
    """Download a single file from a clip pass. Used by nodes without shared storage."""
    _PASS_MAP = {
        "input": ["Frames", "Input"],
        "alpha": ["AlphaHint"],
        "mask": ["VideoMamaMaskHint"],
        "source": ["Source"],
    }

    dirs = _PASS_MAP.get(pass_name)
    if not dirs:
        raise HTTPException(status_code=400, detail=f"Unknown pass: {pass_name}")

    service = get_service()
    clips = service.scan_clips(_clips_mod._clips_dir)
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    for d in dirs:
        fpath = os.path.join(clip.root_path, d, filename)
        if os.path.isfile(fpath):
            return FileResponse(fpath)

    raise HTTPException(status_code=404, detail=f"File not found: {filename}")


@router.post("/{node_id}/files/{clip_name}/{pass_name}/{filename}")
async def upload_result_file(node_id: str, clip_name: str, pass_name: str, filename: str, file: UploadFile):
    """Upload a result file from a node. Used by nodes without shared storage."""
    _OUTPUT_MAP = {
        "fg": "Output/FG",
        "matte": "Output/Matte",
        "comp": "Output/Comp",
        "processed": "Output/Processed",
        "alpha": "AlphaHint",
    }

    subdir = _OUTPUT_MAP.get(pass_name)
    if not subdir:
        raise HTTPException(status_code=400, detail=f"Unknown output pass: {pass_name}")

    service = get_service()
    clips = service.scan_clips(_clips_mod._clips_dir)
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = os.path.join(clip.root_path, subdir)
    os.makedirs(target_dir, exist_ok=True)

    fpath = os.path.join(target_dir, filename)
    try:
        with open(fpath, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}") from e

    return {"status": "ok", "path": fpath}

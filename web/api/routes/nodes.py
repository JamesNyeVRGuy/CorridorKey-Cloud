"""Node management endpoints — registration, heartbeat, job dispatch, file transfer."""

from __future__ import annotations

import io
import logging
import os
import tarfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from backend.job_queue import JobStatus
from backend.natural_sort import natsorted

from ..database import get_storage
from ..deps import get_queue, get_service
from ..nodes import GPUSlot, NodeInfo, NodeSchedule, registry
from ..path_security import safe_join
from ..routes import clips as _clips_mod
from ..ws import manager

logger = logging.getLogger(__name__)

# Legacy shared secret — set CK_AUTH_TOKEN for backward compatibility
_AUTH_TOKEN = os.environ.get("CK_AUTH_TOKEN", "")


def _check_node_auth(request: Request) -> None:
    """Verify node auth via per-node token or legacy shared secret.

    Checks in order:
    1. Per-node token (from node_tokens store) — sets request.state.node_org_id
    2. Legacy CK_AUTH_TOKEN shared secret
    3. If neither is configured, allow all (backward compat)
    """
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""

    if bearer:
        # Check per-node tokens first
        from ..node_tokens import get_node_token_store

        store = get_node_token_store()
        node_token = store.validate(bearer)
        if node_token:
            # Valid per-node token — store org_id for registration
            request.state.node_org_id = node_token.org_id
            request.state.node_token = bearer
            return

        # Check legacy shared secret
        if _AUTH_TOKEN and bearer == _AUTH_TOKEN:
            request.state.node_org_id = None
            request.state.node_token = None
            return

    # No bearer token at all
    if not _AUTH_TOKEN:
        request.state.node_org_id = None
        request.state.node_token = None
        return  # no auth configured, allow all

    raise HTTPException(status_code=401, detail="Invalid or missing node auth token")


router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(_check_node_auth)])


def _save_node_config(node_id: str, node: NodeInfo) -> None:
    """Persist UI-configurable node settings."""
    storage = get_storage()
    configs = storage.get_setting("node_configs", {})
    configs[node_id] = {
        "paused": node.paused,
        "schedule": node.schedule.to_dict(),
        "accepted_types": node.accepted_types,
    }
    storage.set_setting("node_configs", configs)


def _restore_node_config(node: NodeInfo) -> None:
    """Restore persisted settings when a node re-registers."""
    storage = get_storage()
    configs = storage.get_setting("node_configs", {})
    cfg = configs.get(node.node_id)
    if cfg:
        node.paused = cfg.get("paused", False)
        node.visibility = cfg.get("visibility", "private")
        sched = cfg.get("schedule", {})
        if sched:
            node.schedule = NodeSchedule(
                enabled=sched.get("enabled", False),
                start=sched.get("start", "00:00"),
                end=sched.get("end", "23:59"),
            )
        node.accepted_types = cfg.get("accepted_types", [])


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
    org_id: str | None = None
    accepted_types: list[str] = []


class NodeHeartbeatRequest(BaseModel):
    vram_free_gb: float = 0.0
    status: str = "online"
    logs: list[str] = []
    cpu_stats: dict = {}


class JobResultRequest(BaseModel):
    job_id: str
    status: str  # "completed" or "failed"
    error_message: str | None = None


# --- Registration ---


@router.post("/register")
def register_node(req: NodeRegisterRequest, request: Request):
    """Register a new worker node or update an existing one."""
    # Resolve org_id: per-node token takes priority, then request body
    org_id = getattr(request.state, "node_org_id", None) or req.org_id
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
        org_id=org_id,
        accepted_types=req.accepted_types,
    )
    registry.register(info)
    # Associate the per-node token with this node_id
    node_token = getattr(request.state, "node_token", None)
    if node_token:
        from ..node_tokens import get_node_token_store
        get_node_token_store().mark_used_by_node(node_token, req.node_id)
    # Re-fetch to get the merged state (register preserves UI-set fields on re-register)
    node = registry.get_node(req.node_id)
    if node:
        _restore_node_config(node)
        manager.send_node_update(node.to_dict())
    return {"status": "registered", "node_id": req.node_id}


@router.post("/{node_id}/heartbeat")
def node_heartbeat(node_id: str, req: NodeHeartbeatRequest):
    """Update node heartbeat and VRAM status."""
    if not registry.heartbeat(node_id, req.vram_free_gb, req.status):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not registered")
    node = registry.get_node(node_id)
    if node:
        if req.logs:
            node.append_logs(req.logs)
        if req.cpu_stats:
            node.cpu_stats = req.cpu_stats
            node.record_health()
        # Note: GPU credit contribution is tracked on job COMPLETION,
        # not heartbeat. See report_job_result below. This prevents
        # nodes from faking "busy" status to earn credits.
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


@router.get("/{node_id}/health")
def get_node_health(node_id: str):
    """Get health history for a node (last 60 snapshots, ~10 min at 10s intervals)."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return {"history": node.health_history}


@router.get("/{node_id}/logs")
def get_node_logs(node_id: str):
    """Get recent log lines from a node."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return {"logs": node.recent_logs}


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
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict())
    return {"status": "paused"}


@router.post("/{node_id}/resume")
def resume_node(node_id: str):
    """Resume a paused node."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.paused = False
    _save_node_config(node_id, node)
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
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict())
    return node.schedule.to_dict()


class AcceptedTypesRequest(BaseModel):
    accepted_types: list[str] = []  # empty = all types


@router.put("/{node_id}/accepted-types")
def set_accepted_types(node_id: str, req: AcceptedTypesRequest):
    """Set which job types a node will accept. Empty list = all types."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.accepted_types = req.accepted_types
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict())
    return {"accepted_types": node.accepted_types}


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
    # Org isolation (CRKY-19): private nodes only claim jobs from their org.
    # Shared nodes (visibility=shared) can claim from any org.
    claim_org = node.org_id if node.visibility != "shared" else None
    job = queue.claim_job(node_id, accepted_types=node.accepted_types or None, org_id=claim_org)
    if job is None:
        return {"job": None}

    # Assign the job to this node
    registry.set_busy(node_id, job.id)
    manager.send_job_status(job.id, JobStatus.RUNNING.value, org_id=job.org_id)

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
    oid = job.org_id if job else None
    manager.send_job_progress(job_id, "", current, total, org_id=oid)
    return {"status": "ok"}


@router.post("/{node_id}/job-result")
def report_job_result(node_id: str, req: JobResultRequest):
    """Node reports job completion or failure."""
    queue = get_queue()
    job = queue.find_job_by_id(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{req.job_id}' not found")

    oid = job.org_id
    if req.status == "completed":
        queue.complete_job(job)
        manager.send_job_status(job.id, JobStatus.COMPLETED.value, org_id=oid)

        # Credit the node's org for contributed GPU time (verified server-side)
        import time

        node = registry.get_node(node_id)
        if job.started_at and node and node.org_id:
            elapsed = time.time() - job.started_at
            if elapsed > 0:
                from ..gpu_credits import add_contributed

                add_contributed(node.org_id, elapsed)
                logger.info(f"Credit: +{elapsed:.1f}s by node {node_id} -> org {node.org_id}")
    else:
        queue.fail_job(job, req.error_message or "Unknown error")
        manager.send_job_status(job.id, JobStatus.FAILED.value, error=req.error_message, org_id=oid)

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


@router.get("/{node_id}/files/{clip_name}/{pass_name}/bundle")
def download_clip_bundle(
    node_id: str,
    clip_name: str,
    pass_name: str,
    start: int = Query(0),
    end: int = Query(-1),
):
    """Download multiple files as a tar stream. Much faster than one-per-file."""
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

    target_dir = None
    directory = None
    for d in dirs:
        candidate = os.path.join(clip.root_path, d)
        if os.path.isdir(candidate):
            target_dir = candidate
            directory = d
            break

    if not target_dir:
        raise HTTPException(status_code=404, detail=f"No files for pass {pass_name}")

    files = natsorted(os.listdir(target_dir))
    if end > 0:
        files = files[start:end]
    elif start > 0:
        files = files[start:]

    def generate_tar():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|") as tar:
            for fname in files:
                fpath = os.path.join(target_dir, fname)
                if os.path.isfile(fpath):
                    tar.add(fpath, arcname=os.path.join(directory, fname))
                    buf.seek(0)
                    data = buf.read()
                    buf.seek(0)
                    buf.truncate()
                    if data:
                        yield data
        buf.seek(0)
        remaining = buf.read()
        if remaining:
            yield remaining

    return StreamingResponse(
        generate_tar(),
        media_type="application/x-tar",
        headers={"X-Tar-Directory": directory, "X-Tar-Count": str(len(files))},
    )


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
        fpath = safe_join(clip.root_path, d, filename)
        if os.path.isfile(fpath):
            return FileResponse(fpath)

    raise HTTPException(status_code=404, detail=f"File not found: {filename}")


@router.post("/{node_id}/files/{clip_name}/{pass_name}/bundle")
async def upload_result_bundle(node_id: str, clip_name: str, pass_name: str, request: Request):
    """Upload multiple result files as a tar stream. Much faster than one-per-file."""
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

    # Read the tar stream from the request body
    body = await request.body()
    buf = io.BytesIO(body)
    count = 0
    try:
        with tarfile.open(fileobj=buf, mode="r|") as tar:
            for member in tar:
                if member.isfile():
                    # Validate path stays within target_dir
                    dest = safe_join(target_dir, os.path.basename(member.name))
                    extracted = tar.extractfile(member)
                    if extracted:
                        with open(dest, "wb") as f:
                            f.write(extracted.read())
                        count += 1
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid tar stream: {e}") from e

    return {"status": "ok", "count": count}


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

    fpath = safe_join(target_dir, filename)
    try:
        with open(fpath, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}") from e

    return {"status": "ok", "path": fpath}

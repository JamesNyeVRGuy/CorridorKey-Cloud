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
from ..deps import get_node_state, get_queue, get_service
from ..nodes import GPUSlot, NodeInfo, NodeSchedule
from ..org_isolation import resolve_node_clips_dir
from ..path_security import safe_join
from ..routes import clips as _clips_mod
from ..ws import manager

logger = logging.getLogger(__name__)

# Legacy shared secret — set CK_AUTH_TOKEN for backward compatibility
_AUTH_TOKEN = os.environ.get("CK_AUTH_TOKEN", "").strip()

# Max upload size for node file transfers (default 10GB)
_MAX_UPLOAD_BYTES = int(os.environ.get("CK_MAX_NODE_UPLOAD_BYTES", str(10 * 1024**3)))


def _check_node_auth(request: Request) -> None:
    """Verify node auth via per-node token or legacy shared secret.

    Checks in order:
    1. Per-node token (from node_tokens store) — sets request.state.node_org_id
    2. Legacy CK_AUTH_TOKEN shared secret
    3. If neither is configured, allow all (backward compat)

    When a bearer token is provided but is invalid, auth always fails —
    even if no auth is configured. This prevents revoked tokens from
    granting access.
    """
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""

    if bearer:
        # Check per-node tokens first
        from ..node_tokens import get_node_token_store

        store = get_node_token_store()
        node_token = store.validate(bearer)
        if node_token:
            # Valid per-node token — store org_id and bound node_id
            request.state.node_org_id = node_token.org_id
            request.state.node_token = bearer
            request.state.node_token_node_id = node_token.node_id  # may be None before registration
            return

        # Check legacy shared secret
        if _AUTH_TOKEN and bearer == _AUTH_TOKEN:
            request.state.node_org_id = None
            request.state.node_token = None
            request.state.node_token_node_id = None
            return

        # Token was provided but didn't match anything — always reject
        raise HTTPException(status_code=401, detail="Invalid or revoked node auth token")

    # No bearer token at all
    if not _AUTH_TOKEN:
        # Check if per-node tokens have been created — if so, require auth
        from ..node_tokens import get_node_token_store

        store = get_node_token_store()
        if store.list_all():
            raise HTTPException(status_code=401, detail="Authentication required")
        request.state.node_org_id = None
        request.state.node_token = None
        request.state.node_token_node_id = None
        return  # no auth configured, allow all

    raise HTTPException(status_code=401, detail="Invalid or missing node auth token")


def _check_node_identity(request: Request, node_id: str) -> None:
    """Verify the authenticated token is bound to this node_id.

    Skipped for legacy shared-secret auth and no-auth mode (node_token is None).
    Registration binds the token to the node, so this check runs on all
    subsequent requests (heartbeat, job dispatch, file transfer, etc.).
    """
    bound_node_id = getattr(request.state, "node_token_node_id", None)
    if bound_node_id is not None and bound_node_id != node_id:
        raise HTTPException(status_code=403, detail="Token not authorized for this node")


router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(_check_node_auth)])


def _node_clips_dir(node_id: str, org_id: str | None = None, job_id: str | None = None) -> str:
    """Resolve org-scoped clips dir. Tries: explicit org_id > job_id lookup > node's current job > node's org."""
    if not org_id and job_id:
        queue = get_queue()
        job = queue.find_job_by_id(job_id)
        if job and job.org_id:
            return resolve_node_clips_dir(job.org_id)
    if not org_id:
        node = get_node_state().get_node(node_id)
        if node and node.current_job_id:
            queue = get_queue()
            job = queue.find_job_by_id(node.current_job_id)
            if job and job.org_id:
                return resolve_node_clips_dir(job.org_id)
        org_id = node.org_id if node else None
    return resolve_node_clips_dir(org_id)


def _save_node_config(node_id: str, node: NodeInfo) -> None:
    """Persist UI-configurable node settings and write back to state backend."""
    storage = get_storage()
    configs = storage.get_setting("node_configs", {})
    configs[node_id] = {
        "paused": node.paused,
        "visibility": node.visibility,
        "schedule": node.schedule.to_dict(),
        "accepted_types": node.accepted_types,
    }
    storage.set_setting("node_configs", configs)
    get_node_state().update_node(node_id, node)


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


class NodeSecurityInfo(BaseModel):
    """Self-reported security posture from the node agent."""

    running_as_root: bool = True  # default unsafe until proven otherwise
    hardened: bool = False
    uid: int = 0
    read_only_fs: bool = False
    agent_version: str = ""
    build_number: int = 0  # Unix timestamp of the git commit — higher = newer


class NodeRegisterRequest(BaseModel):
    node_id: str
    name: str
    host: str
    gpus: list[GPUSlotSchema] = []
    gpu_name: str = ""
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    capabilities: list[str] = []
    model_compiled: bool = False
    shared_storage: str | None = None
    org_id: str | None = None
    accepted_types: list[str] = []
    security: NodeSecurityInfo = NodeSecurityInfo()


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
    import os as _os

    # --- Security posture checks ---
    security_warnings: list[str] = []
    require_hardened = _os.environ.get("CK_REQUIRE_HARDENED", "").strip().lower() in ("true", "1", "yes")

    # Server-verifiable: is the node using a per-node token (not legacy shared secret)?
    has_per_node_token = getattr(request.state, "node_token", None) is not None
    if not has_per_node_token:
        security_warnings.append("using legacy shared secret instead of per-node token")

    # Server-verifiable: is the connection over HTTPS?
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if forwarded_proto != "https" and request.url.hostname not in ("localhost", "127.0.0.1"):
        security_warnings.append("connecting over plain HTTP (tokens in cleartext)")

    # Self-reported: running as root?
    if req.security.running_as_root:
        security_warnings.append("running as root (uid 0)")

    # Note: read_only_fs and hardened mode are informational only.
    # The default Docker image runs as non-root (nodeuser, uid 1000)
    # which is sufficient for the default security posture.

    # Reject if CK_REQUIRE_HARDENED and node doesn't meet requirements
    if require_hardened:
        if req.security.running_as_root or not has_per_node_token:
            raise HTTPException(
                status_code=403,
                detail=f"Node rejected: server requires hardened nodes. Issues: {', '.join(security_warnings)}",
            )

    if security_warnings:
        logger.warning(f"Node {req.name} ({req.node_id}) security: {', '.join(security_warnings)}")

    # Update security reputation (clears penalty if no warnings)
    from ..node_reputation import record_security_warning

    record_security_warning(req.node_id, security_warnings)

    # --- VRAM check ---
    # Minimum 10GB VRAM required (GVM needs ~8GB + headroom for frames/OS).
    # Nodes below this will fail every job and waste everyone's time.
    _MIN_NODE_VRAM_GB = float(_os.environ.get("CK_MIN_NODE_VRAM_GB", "10.0").strip())
    node_vram = req.vram_total_gb
    if req.gpus:
        node_vram = max(g.vram_total_gb for g in req.gpus)
    if _MIN_NODE_VRAM_GB > 0 and node_vram < _MIN_NODE_VRAM_GB:
        raise HTTPException(
            status_code=403,
            detail=f"Node rejected: GPU has {node_vram:.1f}GB VRAM, minimum {_MIN_NODE_VRAM_GB:.0f}GB required. "
            f"GVM alpha generation needs ~8GB and inference needs ~4GB plus headroom.",
        )

    # --- Registration ---
    # Org from per-node token takes precedence; legacy auth cannot self-assign org
    org_id = getattr(request.state, "node_org_id", None)
    if not org_id and req.org_id:
        logger.warning(
            f"Node {req.name} ({req.node_id}) attempted org_id override ({req.org_id}) without per-node token — ignored"
        )
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
        model_compiled=req.model_compiled,
        shared_storage=req.shared_storage,
        org_id=org_id,
        accepted_types=req.accepted_types,
        agent_version=req.security.agent_version,
        build_number=req.security.build_number,
    )
    # Bind per-node token to this node_id (reject if already bound to different node)
    node_token = getattr(request.state, "node_token", None)
    bound_node_id = getattr(request.state, "node_token_node_id", None)
    if node_token and bound_node_id and bound_node_id != req.node_id:
        raise HTTPException(status_code=403, detail="This token is already bound to a different node")

    get_node_state().register(info)
    # Associate the per-node token with this node_id
    if node_token:
        from ..node_tokens import get_node_token_store

        get_node_token_store().mark_used_by_node(node_token, req.node_id)
    # Re-fetch to get the merged state (register preserves UI-set fields on re-register)
    node = get_node_state().get_node(req.node_id)
    if node:
        _restore_node_config(node)
        manager.send_node_update(node.to_dict(), org_id=node.org_id)
    # Version comparison — use build_number for proper ordering
    from ..version import BUILD_NUMBER, MIN_NODE_BUILD, VERSION_STRING

    server_version = VERSION_STRING
    node_build = req.security.build_number

    # Version check: node must meet the minimum build requirement.
    # MIN_NODE_BUILD defaults to the server's own BUILD_NUMBER (require latest),
    # but can be set lower via CK_MIN_NODE_BUILD to accept older nodes.
    if node_build > 0 and MIN_NODE_BUILD > 0:
        version_ok = node_build >= MIN_NODE_BUILD
    elif req.security.agent_version:
        version_ok = req.security.agent_version == VERSION_STRING
    else:
        version_ok = False  # Unknown version — flag as outdated

    if node:
        node.version_ok = version_ok

    if not version_ok:
        logger.info(
            f"Node {req.name} outdated: build {node_build} < server {BUILD_NUMBER} "
            f"(node: {req.security.agent_version}, server: {VERSION_STRING})"
        )

    return {
        "status": "registered",
        "node_id": req.node_id,
        "security_warnings": security_warnings,
        "server_version": server_version,
        "version_match": version_ok,
    }


@router.post("/{node_id}/heartbeat")
def node_heartbeat(node_id: str, req: NodeHeartbeatRequest, request: Request):
    """Update node heartbeat and VRAM status."""
    _check_node_identity(request, node_id)
    # 410 Gone = node was explicitly removed via UI, agent should shut down
    if get_node_state().is_dismissed(node_id):
        raise HTTPException(status_code=410, detail="Node was removed — shutting down")
    if not get_node_state().heartbeat(node_id, req.vram_free_gb, req.status):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not registered")
    node = get_node_state().get_node(node_id)
    if node:
        if req.logs:
            node.append_logs(req.logs)
        if req.cpu_stats:
            node.cpu_stats = req.cpu_stats
            node.record_health()
        # Write mutations back to state backend (required for Redis)
        if req.logs or req.cpu_stats:
            get_node_state().update_node(node_id, node)
        # Note: GPU credit contribution is tracked on job COMPLETION,
        # not heartbeat. See report_job_result below. This prevents
        # nodes from faking "busy" status to earn credits.
        manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"status": "ok"}


@router.delete("/{node_id}")
def unregister_node(node_id: str, request: Request):
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    get_node_state().unregister(node_id, dismiss=True)
    manager.send_node_offline(node_id, org_id=node.org_id if node else None)
    return {"status": "unregistered"}


@router.get("")
def list_nodes(request: Request):
    """List registered nodes, filtered to the caller's org."""
    caller_org = getattr(request.state, "node_org_id", None)
    nodes = get_node_state().list_nodes()
    if caller_org:
        nodes = [n for n in nodes if n.org_id == caller_org or n.visibility == "shared"]
    return [n.to_dict() for n in nodes]


@router.get("/{node_id}/health")
def get_node_health(node_id: str, request: Request):
    """Get health history for a node (last 60 snapshots, ~10 min at 10s intervals)."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return {"history": node.health_history}


@router.get("/{node_id}/logs")
def get_node_logs(node_id: str, request: Request):
    """Get recent log lines from a node."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return {"logs": node.recent_logs}


# --- Pause / Schedule ---


class NodeScheduleRequest(BaseModel):
    enabled: bool = False
    start: str = "00:00"
    end: str = "23:59"


@router.post("/{node_id}/pause")
def pause_node(node_id: str, request: Request):
    """Pause a node — it won't receive new jobs until resumed."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.paused = True
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"status": "paused"}


@router.post("/{node_id}/resume")
def resume_node(node_id: str, request: Request):
    """Resume a paused node."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.paused = False
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"status": "resumed"}


@router.get("/{node_id}/schedule")
def get_node_schedule(node_id: str, request: Request):
    """Get a node's active hours schedule."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return node.schedule.to_dict()


@router.put("/{node_id}/schedule")
def set_node_schedule(node_id: str, req: NodeScheduleRequest, request: Request):
    """Set a node's active hours schedule."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.schedule = NodeSchedule(enabled=req.enabled, start=req.start, end=req.end)
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return node.schedule.to_dict()


class AcceptedTypesRequest(BaseModel):
    accepted_types: list[str] = []  # empty = all types


@router.put("/{node_id}/accepted-types")
def set_accepted_types(node_id: str, req: AcceptedTypesRequest, request: Request):
    """Set which job types a node will accept. Empty list = all types."""
    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    node.accepted_types = req.accepted_types
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"accepted_types": node.accepted_types}


# --- Job dispatch ---


@router.get("/{node_id}/next-job")
async def get_next_job(node_id: str, request: Request):
    """Get the next available job for a node to process.

    Uses reputation-weighted dispatch: low-reputation nodes are delayed
    slightly so higher-reputation nodes claim jobs first. This naturally
    routes work to faster, more reliable nodes without a complex scheduler.
    """
    import asyncio as _asyncio

    _check_node_identity(request, node_id)
    node = get_node_state().get_node(node_id)
    if not node or not node.is_alive:
        raise HTTPException(status_code=404, detail="Node not registered or offline")

    if not node.can_accept_jobs:
        return {"job": None, "reason": "paused" if node.paused else "outside_schedule"}

    # Block during maintenance mode (CRKY-149)
    from .admin import is_maintenance_active

    if is_maintenance_active():
        return {"job": None, "reason": "maintenance"}

    # Block outdated nodes from picking up jobs
    if not node.version_ok:
        return {"job": None, "reason": "outdated"}

    # Reputation-weighted delay: high-rep nodes claim immediately,
    # low-rep nodes wait up to 1 second. Score 100 = 0s, score 0 = 1s.
    from ..node_reputation import get_reputation

    rep = get_reputation(node_id)
    if rep.completed_jobs + rep.failed_jobs >= 3:
        # Only apply delay after enough history to judge
        score = rep.score
        # Compiled nodes get a dispatch boost (claim jobs faster)
        if node.model_compiled:
            score = min(100, score + 10)
        delay = max(0.0, (100 - score) / 100.0)  # 0-1 second
        if delay > 0.05:
            await _asyncio.sleep(delay)

    queue = get_queue()
    # Org isolation (CRKY-19): private nodes only claim jobs from their org.
    # Shared nodes (visibility=shared) can claim from any org.
    # Org-less nodes in multi-tenant mode cannot claim any jobs.
    from ..auth import AUTH_ENABLED

    if AUTH_ENABLED and not node.org_id and node.visibility != "shared":
        return {"job": None, "reason": "no_org"}
    # Shared nodes: build exclusion list of orgs that opted out
    exclude_orgs: set[str] = set()
    if node.visibility == "shared":
        from ..database import get_storage

        org_prefs = get_storage().get_setting("org_preferences", {})
        for oid, prefs in org_prefs.items():
            if not prefs.get("allow_shared_nodes", True):
                exclude_orgs.add(oid)

    claim_org = node.org_id if node.visibility != "shared" else None
    job = queue.claim_job(
        node_id, accepted_types=node.accepted_types or None, org_id=claim_org, exclude_orgs=exclude_orgs or None
    )
    if job is None:
        return {"job": None}

    # Assign the job to this node
    get_node_state().set_busy(node_id, job.id)
    manager.send_job_status(job.id, JobStatus.RUNNING.value, org_id=job.org_id)

    # Build job payload with file info
    clip = None
    service = get_service()
    clips = service.scan_clips(_node_clips_dir(node_id, org_id=job.org_id))
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
def report_job_progress(node_id: str, job_id: str, current: int, total: int, request: Request):
    """Node reports job progress."""
    _check_node_identity(request, node_id)
    queue = get_queue()
    job = queue.find_job_by_id(job_id)
    if job:
        # Verify this node is assigned to the job
        if job.claimed_by and job.claimed_by != node_id:
            raise HTTPException(status_code=403, detail="Job is assigned to a different node")
        # Don't overwrite real frame counts with zeros (cancel check sends 0,0)
        if current > 0 or total > 0:
            job.current_frame = current
            job.total_frames = total
    oid = job.org_id if job else None
    cancelled = job.status.value == "cancelled" if job else False
    if current > 0 or total > 0:
        manager.send_job_progress(job_id, "", current, total, org_id=oid)
    return {"status": "cancelled" if cancelled else "ok"}


@router.post("/{node_id}/job-result")
def report_job_result(node_id: str, req: JobResultRequest, request: Request):
    """Node reports job completion or failure."""
    _check_node_identity(request, node_id)
    queue = get_queue()
    job = queue.find_job_by_id(req.job_id)
    if not job:
        # Job may have been reaped and re-completed by another worker.
        # The node still did valid work (files already uploaded), so accept gracefully.
        logger.warning(f"Node {node_id} reported result for unknown job {req.job_id} (may have been reaped)")
        get_node_state().set_idle(node_id)
        return {"status": "ok"}

    # Verify this node is the one assigned to the job (prevents credit fraud).
    # Allow if claimed_by is None (job was reaped but node finished the work).
    if job.claimed_by and job.claimed_by != node_id and job.claimed_by != "local":
        raise HTTPException(status_code=403, detail="Job is assigned to a different node")

    oid = job.org_id

    import time

    node = get_node_state().get_node(node_id)
    elapsed = time.time() - job.started_at if job.started_at else 0

    if req.status == "completed" or req.status == "cancelled":
        queue.complete_job(job) if req.status == "completed" else queue.mark_cancelled(job)
        status_val = JobStatus.COMPLETED.value if req.status == "completed" else JobStatus.CANCELLED.value
        manager.send_job_status(job.id, status_val, org_id=oid)

        # Credit/charge GPU time for completed AND cancelled (user got partial output).
        # Failed jobs are not charged — system fault, no usable output.
        if elapsed > 0:
            if job.org_id:
                from ..gpu_credits import add_consumed

                add_consumed(job.org_id, elapsed)
                logger.info(f"Credit: {elapsed:.1f}s consumed by org {job.org_id} ({req.status})")
            if node and node.org_id:
                if not job.org_id or job.org_id == node.org_id or node.visibility == "shared":
                    from ..gpu_credits import add_contributed

                    add_contributed(node.org_id, elapsed)
                    logger.info(f"Credit: +{elapsed:.1f}s by node {node_id} -> org {node.org_id}")

        if req.status == "completed":
            from ..node_reputation import record_job_completed

            record_job_completed(node_id, job.total_frames, elapsed)
    else:
        # Failed — no credit charge (system fault)
        error_detail = req.error_message or "Unknown error"
        queue.fail_job(job, error_detail)
        manager.send_job_status(job.id, JobStatus.FAILED.value, error="Remote processing error", org_id=oid)
        from ..node_reputation import record_job_failed

        record_job_failed(node_id)

    get_node_state().set_idle(node_id)

    # Trigger pipeline chaining if applicable
    from ..worker import _chain_next_pipeline_step

    if req.status == "completed":
        service = get_service()
        _chain_next_pipeline_step(job, queue, _node_clips_dir(node_id, org_id=job.org_id), service)

    return {"status": "ok"}


# --- File transfer (for nodes without shared storage) ---


@router.get("/{node_id}/files/{clip_name}/{pass_name}")
def list_clip_files(node_id: str, clip_name: str, pass_name: str, request: Request, job_id: str | None = Query(None)):
    """List files available for download for a specific clip pass."""
    _check_node_identity(request, node_id)
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
    clips = service.scan_clips(_node_clips_dir(node_id, job_id=job_id))
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    for d in dirs:
        target = os.path.join(clip.root_path, d)
        if os.path.isdir(target):
            files = natsorted(os.listdir(target))
            return {"directory": d, "files": files}

    return {"directory": None, "files": []}


@router.get("/{node_id}/files/{clip_name}/{pass_name}/bundle")
def download_clip_bundle(
    node_id: str,
    clip_name: str,
    pass_name: str,
    request: Request,
    start: int = Query(0),
    end: int = Query(-1),
    job_id: str | None = Query(None),
):
    """Download multiple files as a tar stream. Much faster than one-per-file."""
    _check_node_identity(request, node_id)
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
    clips = service.scan_clips(_node_clips_dir(node_id, job_id=job_id))
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
def download_clip_file(
    node_id: str, clip_name: str, pass_name: str, filename: str, request: Request, job_id: str | None = Query(None)
):
    """Download a single file from a clip pass. Used by nodes without shared storage."""
    _check_node_identity(request, node_id)
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
    clips = service.scan_clips(_node_clips_dir(node_id, job_id=job_id))
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    for d in dirs:
        fpath = safe_join(clip.root_path, d, filename)
        if os.path.isfile(fpath):
            return FileResponse(fpath)

    raise HTTPException(status_code=404, detail=f"File not found: {filename}")


@router.post("/{node_id}/files/{clip_name}/{pass_name}/bundle")
async def upload_result_bundle(
    node_id: str, clip_name: str, pass_name: str, request: Request, job_id: str | None = Query(None)
):
    """Upload multiple result files as a tar stream. Much faster than one-per-file."""
    _check_node_identity(request, node_id)
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
    clips = service.scan_clips(_node_clips_dir(node_id, job_id=job_id))
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = os.path.join(clip.root_path, subdir)
    os.makedirs(target_dir, exist_ok=True)

    # Stream the request body to a temp file to avoid holding the entire tar in memory.
    import tempfile as _tempfile

    _MAX_TAR_MEMBER = 500 * 1024 * 1024  # 500MB per extracted file

    count = 0
    is_gzip = request.headers.get("Content-Encoding", "").strip().lower() == "gzip"
    try:
        with _tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as tmp:
            total_bytes = 0
            async for chunk in request.stream():
                total_bytes += len(chunk)
                if total_bytes > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds maximum size")
                tmp.write(chunk)
            tmp.seek(0)

            # Decompress gzip if node sent compressed bundle
            if is_gzip:
                import gzip as _gzip

                decompressed = _tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
                with _gzip.open(tmp, "rb") as gz:
                    while True:
                        block = gz.read(8 * 1024 * 1024)
                        if not block:
                            break
                        decompressed.write(block)
                decompressed.seek(0)
                tmp = decompressed

            with tarfile.open(fileobj=tmp, mode="r|") as tar:
                for member in tar:
                    if member.isfile():
                        if member.size > _MAX_TAR_MEMBER:
                            logger.warning(f"Skipping oversized tar member: {member.name} ({member.size} bytes)")
                            continue
                        dest = safe_join(target_dir, os.path.basename(member.name))
                        extracted = tar.extractfile(member)
                        if extracted:
                            with open(dest, "wb") as f:
                                f.write(extracted.read())
                            count += 1
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Invalid tar upload from node {node_id}: {e}")
        raise HTTPException(status_code=400, detail="Invalid tar stream") from e

    return {"status": "ok", "count": count}


@router.post("/{node_id}/files/{clip_name}/{pass_name}/{filename}")
async def upload_result_file(
    node_id: str,
    clip_name: str,
    pass_name: str,
    filename: str,
    file: UploadFile,
    request: Request,
    job_id: str | None = Query(None),
):
    """Upload a result file from a node. Used by nodes without shared storage."""
    _check_node_identity(request, node_id)
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
    clips = service.scan_clips(_node_clips_dir(node_id, job_id=job_id))
    clip = next((c for c in clips if c.name == clip_name), None)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = os.path.join(clip.root_path, subdir)
    os.makedirs(target_dir, exist_ok=True)

    fpath = safe_join(target_dir, filename)
    try:
        total_bytes = 0
        with open(fpath, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds maximum size")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to save file") from e

    return {"status": "ok"}

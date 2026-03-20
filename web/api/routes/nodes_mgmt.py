"""Node management endpoints for the frontend (CRKY-53).

Org-scoped node visibility with role-based management actions.
Uses JWT auth (via middleware). Separate from the node agent endpoints
in nodes.py which use CK_AUTH_TOKEN.

- Members see nodes belonging to their org (read-only)
- Org admins can manage nodes (pause, schedule, remove, accepted types)
- Platform admins see and manage all nodes
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import AUTH_ENABLED, UserContext, get_current_user
from ..database import get_storage
from ..node_tokens import get_node_token_store
from ..nodes import NodeInfo, NodeSchedule, registry
from ..orgs import get_org_store
from ..tier_guard import require_member
from ..ws import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/farm", tags=["farm"], dependencies=[Depends(require_member)])


def _get_user(request: Request) -> UserContext | None:
    return get_current_user(request)


def _user_can_see_node(user: UserContext | None, node: NodeInfo) -> bool:
    """Check if a user can see this node based on org membership and visibility."""
    if not AUTH_ENABLED or user is None:
        return True
    if user.is_admin:
        return True
    if not node.org_id:
        return False  # Unscoped nodes only visible to platform admin
    # Shared nodes are visible to all authenticated users
    if node.visibility == "shared":
        return True
    # Private nodes require org membership
    store = get_org_store()
    return store.is_member(node.org_id, user.user_id)


def _user_can_manage_node(user: UserContext | None, node: NodeInfo) -> bool:
    """Check if a user can manage (pause/schedule/remove) this node."""
    if not AUTH_ENABLED or user is None:
        return True
    if user.is_admin:
        return True
    if not node.org_id:
        return user.is_admin  # Unscoped nodes only manageable by platform admin
    store = get_org_store()
    return store.is_org_admin(node.org_id, user.user_id)


def _require_node_access(request: Request, node_id: str, manage: bool = False) -> NodeInfo:
    """Get a node and verify user access. Raises 404/403."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    user = _get_user(request)
    if not _user_can_see_node(user, node):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    if manage and not _user_can_manage_node(user, node):
        raise HTTPException(status_code=403, detail="Only org admins can manage this node")
    return node


def _save_node_config(node_id: str, node: NodeInfo) -> None:
    """Persist UI-configurable node settings."""
    storage = get_storage()
    configs = storage.get_setting("node_configs", {})
    configs[node_id] = {
        "paused": node.paused,
        "visibility": node.visibility,
        "schedule": node.schedule.to_dict(),
        "accepted_types": node.accepted_types,
    }
    storage.set_setting("node_configs", configs)


# --- Listing ---


@router.get("")
def list_managed_nodes(request: Request):
    """List nodes visible to the current user, filtered by org membership.

    Sensitive fields (host IP) are redacted for members who can't manage
    the node. Only org admins/owners and platform admins see full details.
    """
    user = _get_user(request)
    all_nodes = registry.list_nodes()
    visible = [n for n in all_nodes if _user_can_see_node(user, n)]
    manageable = {n.node_id for n in all_nodes if _user_can_manage_node(user, n)}
    # Build org name lookup (CRKY-72)
    org_store = get_org_store()
    org_names: dict[str, str] = {}
    for n in visible:
        if n.org_id and n.org_id not in org_names:
            org = org_store.get_org(n.org_id)
            org_names[n.org_id] = org.name if org else ""

    # Load reputations (CRKY-30)
    from ..node_reputation import get_all_reputations

    rep_map = {r.node_id: r for r in get_all_reputations()}

    result = []
    for n in visible:
        data = n.to_dict()
        can_manage = n.node_id in manageable
        data["can_manage"] = can_manage
        data["org_name"] = org_names.get(n.org_id or "", "")
        rep = rep_map.get(n.node_id)
        data["reputation"] = rep.to_dict() if rep else None
        if not can_manage:
            # Redact infrastructure/operational details for read-only members.
            # Members see: name, status, GPU names, busy state — enough to
            # understand queue behavior. Not IPs, logs, or config.
            data["host"] = "***"
            data["shared_storage"] = None
            data["capabilities"] = []
            data["cpu_stats"] = {}
            data["accepted_types"] = []
        result.append(data)
    return result


# --- Operational info (require org admin) ---


@router.get("/{node_id}/health")
def get_node_health(node_id: str, request: Request):
    """Get health history for a node. Requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    return {"history": node.health_history}


@router.get("/{node_id}/logs")
def get_node_logs(node_id: str, request: Request):
    """Get recent log lines from a node. Requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    return {"logs": node.recent_logs}


# --- Management actions (require org admin) ---


class NodeScheduleRequest(BaseModel):
    enabled: bool = False
    start: str = "00:00"
    end: str = "23:59"


class AcceptedTypesRequest(BaseModel):
    accepted_types: list[str] = []


@router.post("/{node_id}/pause")
def pause_node(node_id: str, request: Request):
    """Pause a node — requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    node.paused = True
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"status": "paused"}


@router.post("/{node_id}/resume")
def resume_node(node_id: str, request: Request):
    """Resume a paused node — requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    node.paused = False
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"status": "resumed"}


@router.get("/{node_id}/schedule")
def get_node_schedule(node_id: str, request: Request):
    node = _require_node_access(request, node_id)
    return node.schedule.to_dict()


@router.put("/{node_id}/schedule")
def set_node_schedule(node_id: str, req: NodeScheduleRequest, request: Request):
    """Set a node's active hours schedule — requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    node.schedule = NodeSchedule(enabled=req.enabled, start=req.start, end=req.end)
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return node.schedule.to_dict()


@router.put("/{node_id}/accepted-types")
def set_accepted_types(node_id: str, req: AcceptedTypesRequest, request: Request):
    """Set which job types a node will accept — requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    node.accepted_types = req.accepted_types
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"accepted_types": node.accepted_types}


class SetVisibilityRequest(BaseModel):
    visibility: str  # "private" or "shared"


@router.put("/{node_id}/visibility")
def set_node_visibility(node_id: str, req: SetVisibilityRequest, request: Request):
    """Set whether a node is private (org-only) or shared (all users).

    Requires org admin. Shared nodes are visible and usable by all
    authenticated users, not just the owning org.
    """
    if req.visibility not in ("private", "shared"):
        raise HTTPException(status_code=400, detail="Visibility must be 'private' or 'shared'")
    node = _require_node_access(request, node_id, manage=True)
    node.visibility = req.visibility
    _save_node_config(node_id, node)
    manager.send_node_update(node.to_dict(), org_id=node.org_id)
    return {"visibility": node.visibility}


@router.delete("/{node_id}")
def unregister_node(node_id: str, request: Request):
    """Remove a node — requires org admin."""
    node = _require_node_access(request, node_id, manage=True)
    oid = node.org_id
    registry.unregister(node_id)
    manager.send_node_offline(node_id, org_id=oid)
    return {"status": "unregistered"}


# --- Node Tokens (per-node auth) ---


class GenerateTokenRequest(BaseModel):
    org_id: str
    label: str


@router.post("/tokens")
def generate_node_token(req: GenerateTokenRequest, request: Request):
    """Generate a per-node auth token. Org admins for any org, members for their personal org."""
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    org_store = get_org_store()
    org = org_store.get_org(req.org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    # Allow: platform admin, org admin/owner, or owner of personal org
    is_personal_owner = org.personal and org.owner_id == user.user_id
    if not user.is_admin and not org_store.is_org_admin(req.org_id, user.user_id) and not is_personal_owner:
        raise HTTPException(status_code=403, detail="Only org admins can generate node tokens")
    token_store = get_node_token_store()
    token = token_store.generate(org_id=req.org_id, label=req.label, created_by=user.user_id)
    # Return the full token only on creation — never again
    return token.to_dict()


@router.get("/tokens")
def list_node_tokens(request: Request, org_id: str | None = None):
    """List node tokens. Org admins see their org's tokens. Platform admins see all."""
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    token_store = get_node_token_store()
    if user.is_admin and not org_id:
        tokens = token_store.list_all()
    elif org_id:
        org_store = get_org_store()
        if not user.is_admin and not org_store.is_org_admin(org_id, user.user_id):
            raise HTTPException(status_code=403, detail="Only org admins can view tokens")
        tokens = token_store.list_for_org(org_id)
    else:
        # List tokens for orgs the user can manage (admin of, or personal owner)
        org_store = get_org_store()
        user_orgs = org_store.list_user_orgs(user.user_id)
        tokens = []
        for o in user_orgs:
            if org_store.is_org_admin(o.org_id, user.user_id) or (o.personal and o.owner_id == user.user_id):
                tokens.extend(token_store.list_for_org(o.org_id))
    return {"tokens": [t.to_safe_dict() for t in tokens]}


@router.delete("/tokens/{token_preview}")
def revoke_node_token(token_preview: str, request: Request):
    """Revoke a node token by its preview (first 8 chars). Requires org admin."""
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    token_store = get_node_token_store()
    # Find the full token by preview
    all_tokens = token_store.list_all()
    target = next((t for t in all_tokens if t.token[:8] == token_preview), None)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    # Check permission: org admin, platform admin, or personal org owner
    if not user.is_admin:
        org_store = get_org_store()
        org = org_store.get_org(target.org_id)
        is_personal_owner = org and org.personal and org.owner_id == user.user_id
        if not org_store.is_org_admin(target.org_id, user.user_id) and not is_personal_owner:
            raise HTTPException(status_code=403, detail="Only org admins can revoke tokens")
    token_store.revoke(target.token)
    return {"status": "revoked"}


@router.get("/setup")
def get_node_setup_info(request: Request):
    """Return info needed for the node setup guide.

    Returns the server URL and image tag. The actual auth token
    is generated separately via POST /tokens.
    """
    return {
        "main_url": request.url.scheme + "://" + request.url.netloc,
        "image": "ghcr.io/jamesnyevrguy/corridorkey-node:stable",
        "compose_template": "docker-compose.node.yml",
        "env_vars": {
            "CK_MAIN_URL": {"required": True, "desc": "Main server URL"},
            "CK_AUTH_TOKEN": {"required": True, "desc": "Node auth token (generate above)"},
            "CK_NODE_NAME": {"required": False, "desc": "Display name for this node"},
            "CK_NODE_GPUS": {"required": False, "desc": "'auto', '0', or '0,1'"},
            "CK_SHARED_STORAGE": {"required": False, "desc": "Shared mount path (skip HTTP transfer)"},
        },
    }

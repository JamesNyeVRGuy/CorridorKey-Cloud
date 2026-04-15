"""Organization management endpoints (CRKY-4).

Provides CRUD for orgs, membership management, and org invites.
All endpoints require authentication. Org-level operations check
that the requesting user has the appropriate org role.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import AUTH_ENABLED, UserContext, get_current_user
from ..orgs import get_org_store
from ..tier_guard import require_authenticated, require_member
from ..users import get_user_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class CreateOrgRequest(BaseModel):
    name: str


class AddMemberRequest(BaseModel):
    user_id: str = ""
    email: str = ""  # Can add by email instead of user_id
    role: str = "member"


class UpdateRoleRequest(BaseModel):
    role: str


def _get_user(request: Request) -> UserContext:
    """Extract authenticated user from request state. Raises 401 if missing."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# --- Org CRUD ---


@router.get("", dependencies=[Depends(require_authenticated)])
def list_my_orgs(request: Request):
    """List orgs the current user belongs to."""
    if not AUTH_ENABLED:
        return {"orgs": []}
    user = _get_user(request)
    store = get_org_store()
    orgs = store.list_user_orgs(user.user_id)
    return {"orgs": [o.to_dict() for o in orgs]}


@router.post("", dependencies=[Depends(require_member)])
def create_org(req: CreateOrgRequest, request: Request):
    """Create a new org. Requires at least member tier."""
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="Auth is not enabled")
    user = _get_user(request)
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Org name is required")
    store = get_org_store()
    org = store.create_org(name=req.name.strip(), owner_id=user.user_id)
    return org.to_dict()


@router.get("/{org_id}", dependencies=[Depends(require_authenticated)])
def get_org(org_id: str, request: Request):
    """Get org details. Must be a member of the org (or platform admin)."""
    user = _get_user(request)
    store = get_org_store()
    org = store.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_member(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not a member of this org")
    return org.to_dict()


@router.delete("/{org_id}", dependencies=[Depends(require_authenticated)])
def delete_org(org_id: str, request: Request):
    """Delete an org. Must be the org owner or platform admin."""
    user = _get_user(request)
    store = get_org_store()
    org = store.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.personal:
        raise HTTPException(status_code=400, detail="Cannot delete personal org")
    if not user.is_admin and org.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="Only the org owner can delete this org")
    store.delete_org(org_id)
    return {"status": "deleted"}


# --- Membership ---


@router.get("/{org_id}/members", dependencies=[Depends(require_authenticated)])
def list_members(org_id: str, request: Request):
    """List org members. Must be a member of the org."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_member(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not a member of this org")
    members = store.list_members(org_id)
    # Enrich with emails from user store (CRKY-68)
    user_store = get_user_store()
    enriched = []
    for m in members:
        data = m.to_dict()
        user_record = user_store.get_user(m.user_id)
        data["email"] = user_record.email if user_record else ""
        enriched.append(data)
    return {"members": enriched}


@router.post("/{org_id}/members", dependencies=[Depends(require_authenticated)])
def add_member(org_id: str, req: AddMemberRequest, request: Request):
    """Add a user to an org. Must be an org admin/owner or platform admin."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can add members")
    if req.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'member' or 'admin'")
    # Resolve email to user_id if provided (CRKY-68)
    target_user_id = req.user_id
    if not target_user_id and req.email:
        user_store = get_user_store()
        found = user_store.get_user_by_email(req.email)
        if not found:
            raise HTTPException(status_code=404, detail=f"No user found with email '{req.email}'")
        target_user_id = found.user_id
    if not target_user_id:
        raise HTTPException(status_code=400, detail="Either user_id or email is required")
    member = store.add_member(org_id, target_user_id, role=req.role)
    return member.to_dict()


@router.delete("/{org_id}/members/{member_user_id}", dependencies=[Depends(require_authenticated)])
def remove_member(org_id: str, member_user_id: str, request: Request):
    """Remove a user from an org. Org admin/owner, platform admin, or self."""
    user = _get_user(request)
    store = get_org_store()
    org = store.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if member_user_id == org.owner_id:
        raise HTTPException(status_code=400, detail="Cannot remove the org owner")
    is_self = member_user_id == user.user_id
    if not is_self and not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not authorized to remove this member")
    if not store.remove_member(org_id, member_user_id):
        raise HTTPException(status_code=404, detail="User is not a member of this org")
    return {"status": "removed"}


@router.patch("/{org_id}/members/{member_user_id}/role", dependencies=[Depends(require_authenticated)])
def update_member_role(org_id: str, member_user_id: str, req: UpdateRoleRequest, request: Request):
    """Change a member's role. Must be org owner or platform admin."""
    user = _get_user(request)
    store = get_org_store()
    org = store.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if req.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'member' or 'admin'")
    if not user.is_admin and org.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="Only the org owner can change roles")
    if member_user_id == org.owner_id:
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")
    result = store.update_member_role(org_id, member_user_id, req.role)
    if not result:
        raise HTTPException(status_code=404, detail="User is not a member of this org")
    return result.to_dict()


# --- GPU Credits (CRKY-6) ---


@router.get("/{org_id}/credits", dependencies=[Depends(require_authenticated)])
def get_credits(org_id: str, request: Request):
    """Get GPU credit balance for an org. Must be a member."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_member(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not a member of this org")
    from ..gpu_credits import get_org_credits

    credits = get_org_credits(org_id)
    return credits.to_dict()


@router.get("/{org_id}/storage", dependencies=[Depends(require_authenticated)])
def get_storage_info(org_id: str, request: Request):
    """Get storage usage and quota for an org. Must be a member."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_member(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not a member of this org")
    from ..storage_quota import get_org_storage_info

    return get_org_storage_info(org_id)


# --- IP Allowlisting (CRKY-25) ---


class IPAllowlistRequest(BaseModel):
    cidrs: list[str] = []  # Empty = remove allowlist (no restriction)


@router.get("/{org_id}/ip-allowlist", dependencies=[Depends(require_authenticated)])
def get_ip_allowlist(org_id: str, request: Request):
    """Get the IP allowlist for an org. Org admin or platform admin."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can view the allowlist")
    from ..ip_allowlist import _load_allowlists

    allowlists = _load_allowlists()
    return {"org_id": org_id, "cidrs": allowlists.get(org_id, [])}


@router.put("/{org_id}/ip-allowlist", dependencies=[Depends(require_authenticated)])
def set_ip_allowlist(org_id: str, req: IPAllowlistRequest, request: Request):
    """Set the IP allowlist for an org. Org admin or platform admin. Empty list = remove."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can set the allowlist")
    from ..ip_allowlist import save_allowlist

    save_allowlist(org_id, req.cidrs)
    from ..audit import audit_from_request

    audit_from_request(
        "org.ip_allowlist_updated", request, target_type="org", target_id=org_id, details={"cidrs": req.cidrs}
    )
    return {"org_id": org_id, "cidrs": req.cidrs}


# --- Webhooks (CRKY-31) ---


class WebhookRequest(BaseModel):
    url: str
    events: list[str]
    format: str = "json"  # "json", "discord", "slack"


@router.get("/{org_id}/webhooks", dependencies=[Depends(require_authenticated)])
def list_org_webhooks(org_id: str, request: Request):
    """List webhooks for an org. Org admin only."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can view webhooks")
    from ..webhooks import list_webhooks

    return {"webhooks": [h.to_dict() for h in list_webhooks(org_id)]}


@router.post("/{org_id}/webhooks", dependencies=[Depends(require_authenticated)])
def create_org_webhook(org_id: str, req: WebhookRequest, request: Request):
    """Create a webhook for an org. Org admin only."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can create webhooks")
    valid_events = {"job_started", "job_completed", "job_failed", "node_offline", "node_online"}
    invalid = set(req.events) - valid_events
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid events: {invalid}. Valid: {valid_events}")
    if req.format not in ("json", "discord", "slack"):
        raise HTTPException(status_code=400, detail="Format must be 'json', 'discord', or 'slack'")
    from ..webhooks import create_webhook

    hook = create_webhook(org_id, req.url, req.events, req.format, user.user_id)
    return hook.to_dict()


@router.delete("/{org_id}/webhooks/{hook_id}", dependencies=[Depends(require_authenticated)])
def delete_org_webhook(org_id: str, hook_id: str, request: Request):
    """Delete a webhook. Org admin only. Verifies hook belongs to the specified org."""
    user = _get_user(request)
    store = get_org_store()
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can delete webhooks")
    from ..webhooks import delete_webhook, list_webhooks

    # Verify the webhook belongs to this org (prevent cross-org deletion)
    org_hooks = {h.id for h in list_webhooks(org_id)}
    if hook_id not in org_hooks:
        raise HTTPException(status_code=404, detail="Webhook not found in this org")
    if not delete_webhook(hook_id):
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"status": "deleted"}


@router.post("/{org_id}/webhooks/{hook_id}/test", dependencies=[Depends(require_authenticated)])
def test_org_webhook(org_id: str, hook_id: str, request: Request):
    """Send a test event to a webhook. Org admin only."""
    user = _get_user(request)
    store = get_org_store()
    if not user.is_admin and not store.is_org_admin(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Only org admins can test webhooks")
    from ..webhooks import list_webhooks

    hook = next((h for h in list_webhooks(org_id) if h.id == hook_id), None)
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found in this org")

    # Fire a test event
    from ..webhooks import _deliver

    test_data = {
        "job_id": "test-0000",
        "clip_name": "test_clip",
        "job_type": "inference",
        "frames": 100,
        "test": True,
    }
    try:
        _deliver(hook, "test", test_data)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Delivery failed: {e}") from e


# --- Org preferences ---


class OrgPreferencesUpdate(BaseModel):
    allow_shared_nodes: bool | None = None


@router.get("/{org_id}/preferences", dependencies=[Depends(require_authenticated)])
def get_org_preferences(org_id: str, request: Request):
    """Get org processing preferences."""
    user = _get_user(request)
    store = get_org_store()
    if not store.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if not user.is_admin and not store.is_member(org_id, user.user_id):
        raise HTTPException(status_code=403, detail="Not a member of this org")
    from ..org_prefs import get_preferences

    return get_preferences(org_id)


@router.put("/{org_id}/preferences", dependencies=[Depends(require_authenticated)])
def update_org_preferences(org_id: str, req: OrgPreferencesUpdate, request: Request):
    """Update org processing preferences. Requires org membership."""
    user = _get_user(request)
    store = get_org_store()
    org = store.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    # Only org owner or admin can change preferences
    if not user.is_admin and org.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="Only the org owner can change preferences")
    from ..org_prefs import update_preferences

    return update_preferences(org_id, req.model_dump(exclude_none=True))

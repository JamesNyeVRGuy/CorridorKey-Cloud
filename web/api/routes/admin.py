"""Admin endpoints — user management, approval workflow (CRKY-2, CRKY-20).

All endpoints require platform_admin tier. Provides user listing,
tier management (approve/reject), and user deletion.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..audit import audit_from_request
from ..auth import TIER_HIERARCHY, UserContext, get_current_user
from ..orgs import get_org_store
from ..tier_guard import require_admin
from ..users import get_user_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class SetTierRequest(BaseModel):
    tier: str


def _get_admin(request: Request) -> UserContext:
    """Extract admin user from request state."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# --- User management ---


@router.get("/users")
def list_users(tier: str | None = None):
    """List all users, optionally filtered by tier. Includes org memberships."""
    store = get_user_store()
    org_store = get_org_store()
    users = store.list_users(tier_filter=tier)
    enriched = []
    for u in users:
        data = u.to_dict()
        user_orgs = org_store.list_user_orgs(u.user_id)
        data["orgs"] = [{"org_id": o.org_id, "name": o.name} for o in user_orgs]
        enriched.append(data)
    return {"users": enriched}


@router.get("/users/pending")
def list_pending_users():
    """List users awaiting approval."""
    store = get_user_store()
    pending = store.list_users(tier_filter="pending")
    return {"users": [u.to_dict() for u in pending]}


@router.post("/users/{user_id}/approve")
def approve_user(user_id: str, request: Request):
    """Approve a pending user — sets tier to 'member' and creates personal org."""
    admin = _get_admin(request)
    user_store = get_user_store()
    user = user_store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.tier != "pending":
        raise HTTPException(status_code=400, detail=f"User is already {user.tier}, not pending")

    # Set tier to member
    updated = user_store.set_tier(user_id, "member", approved_by=admin.user_id)

    # Create personal org and grant starter credits
    org_store = get_org_store()
    personal_org = org_store.ensure_personal_org(user_id, user.email, display_name=user.name)

    from ..gpu_credits import STARTER_CREDITS, add_contributed

    if STARTER_CREDITS > 0:
        add_contributed(personal_org.org_id, STARTER_CREDITS)

    audit_from_request("user.approved", request, target_type="user", target_id=user_id, details={"email": user.email})
    return {"status": "approved", "user": updated.to_dict() if updated else None}


@router.post("/users/{user_id}/reject")
def reject_user(user_id: str, request: Request):
    """Reject a pending user — marks as rejected."""
    admin = _get_admin(request)
    user_store = get_user_store()
    user = user_store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.tier != "pending":
        raise HTTPException(status_code=400, detail=f"User is already {user.tier}, not pending")

    updated = user_store.set_tier(user_id, "rejected", approved_by=admin.user_id)
    audit_from_request("user.rejected", request, target_type="user", target_id=user_id, details={"email": user.email})
    return {"status": "rejected", "user": updated.to_dict() if updated else None}


@router.post("/users/{user_id}/tier")
def set_user_tier(user_id: str, req: SetTierRequest, request: Request):
    """Set a user's trust tier directly. Platform admin only."""
    _get_admin(request)
    valid_tiers = set(TIER_HIERARCHY)
    if req.tier not in valid_tiers:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {', '.join(TIER_HIERARCHY)}")

    user_store = get_user_store()
    user = user_store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    old_tier = user.tier
    updated = user_store.set_tier(user_id, req.tier)

    # Ensure personal org + starter credits when promoting from pending
    if old_tier == "pending" and req.tier != "pending":
        personal_org = get_org_store().ensure_personal_org(user_id, user.email, display_name=user.name)
        from ..gpu_credits import STARTER_CREDITS, add_contributed

        if STARTER_CREDITS > 0:
            add_contributed(personal_org.org_id, STARTER_CREDITS)

    audit_from_request(
        "user.tier_changed",
        request,
        target_type="user",
        target_id=user_id,
        details={"old_tier": old_tier, "new_tier": req.tier},
    )
    return {"status": "updated", "user": updated.to_dict() if updated else None}


@router.delete("/users/{user_id}")
def delete_user(user_id: str):
    """Delete a user record. Does not delete the Supabase auth account."""
    user_store = get_user_store()
    if not user_store.delete_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted"}


# --- Org listing (admin view) ---


@router.get("/orgs")
def list_all_orgs():
    """List all organizations. Platform admin only."""
    org_store = get_org_store()
    orgs = org_store.list_orgs()
    result = []
    for org in orgs:
        members = org_store.list_members(org.org_id)
        result.append({**org.to_dict(), "member_count": len(members)})
    return {"orgs": result}


# --- GPU Credits (admin) ---


class GrantCreditsRequest(BaseModel):
    org_id: str
    hours: float


@router.post("/credits/grant")
def grant_credits(req: GrantCreditsRequest, request: Request):
    """Grant or revoke GPU credit hours for an org. Platform admin only.

    Positive hours = grant credits. Negative hours = revoke credits.
    """
    from ..gpu_credits import add_contributed

    if req.hours == 0:
        raise HTTPException(status_code=400, detail="Hours must be non-zero")
    org_store = get_org_store()
    org = org_store.get_org(req.org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    seconds = req.hours * 3600
    add_contributed(req.org_id, seconds)

    action = "credits.granted" if req.hours > 0 else "credits.revoked"
    audit_from_request(
        action, request, target_type="org", target_id=req.org_id, details={"hours": req.hours, "seconds": seconds}
    )

    from ..gpu_credits import get_org_credits

    return get_org_credits(req.org_id).to_dict()


@router.get("/credits")
def list_all_credits():
    """Get GPU credit balances for all orgs. Platform admin only."""
    from ..gpu_credits import get_all_credits

    org_store = get_org_store()
    credits = get_all_credits()
    result = []
    for c in credits:
        org = org_store.get_org(c.org_id)
        data = c.to_dict()
        data["org_name"] = org.name if org else ""
        result.append(data)
    return {"credits": result}


# --- Usage stats (admin dashboard) ---


@router.get("/stats")
def get_platform_stats():
    """Platform-wide usage statistics. Platform admin only."""
    user_store = get_user_store()
    org_store = get_org_store()

    all_users = user_store.list_users()
    all_orgs = org_store.list_orgs()

    from ..deps import get_node_state, get_queue
    from ..gpu_credits import get_all_credits

    all_credits = get_all_credits()
    total_contributed = sum(c.contributed_seconds for c in all_credits)
    total_consumed = sum(c.consumed_seconds for c in all_credits)

    queue = get_queue()
    nodes = get_node_state().list_nodes()

    return {
        "users": {
            "total": len(all_users),
            "by_tier": {
                tier: len([u for u in all_users if u.tier == tier])
                for tier in ["pending", "member", "contributor", "org_admin", "platform_admin", "rejected"]
                if any(u.tier == tier for u in all_users)
            },
        },
        "orgs": {
            "total": len(all_orgs),
            "personal": len([o for o in all_orgs if o.personal]),
            "team": len([o for o in all_orgs if not o.personal]),
        },
        "gpu": {
            "total_contributed_hours": round(total_contributed / 3600, 2),
            "total_consumed_hours": round(total_consumed / 3600, 2),
            "balance_hours": round((total_contributed - total_consumed) / 3600, 2),
        },
        "jobs": {
            "running": len(queue.running_jobs),
            "queued": len(queue.queue_snapshot),
            "history": len(queue.history_snapshot),
        },
        "nodes": {
            "total": len(nodes),
            "online": len([n for n in nodes if n.status != "offline"]),
            "busy": len([n for n in nodes if n.status == "busy"]),
        },
    }


@router.get("/users/{user_id}/activity")
def get_user_activity(user_id: str):
    """Get activity summary for a specific user. Platform admin only."""
    user_store = get_user_store()
    user = user_store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    org_store = get_org_store()
    user_orgs = org_store.list_user_orgs(user_id)

    from ..deps import get_queue
    from ..gpu_credits import get_org_credits

    # Aggregate credits across all user's orgs
    total_contributed = 0.0
    total_consumed = 0.0
    org_credits = []
    for org in user_orgs:
        credits = get_org_credits(org.org_id)
        total_contributed += credits.contributed_seconds
        total_consumed += credits.consumed_seconds
        org_credits.append({**credits.to_dict(), "org_name": org.name})

    # Count user's jobs
    queue = get_queue()
    all_jobs = queue.running_jobs + list(queue.queue_snapshot) + list(queue.history_snapshot)
    user_jobs = [j for j in all_jobs if j.submitted_by == user_id]

    return {
        "user": user.to_dict(),
        "orgs": [
            {"org_id": o.org_id, "name": o.name, "role": "owner" if o.owner_id == user_id else "member"}
            for o in user_orgs
        ],
        "credits": org_credits,
        "totals": {
            "contributed_hours": round(total_contributed / 3600, 2),
            "consumed_hours": round(total_consumed / 3600, 2),
        },
        "jobs": {
            "total": len(user_jobs),
            "completed": len([j for j in user_jobs if j.status.value == "completed"]),
            "failed": len([j for j in user_jobs if j.status.value == "failed"]),
            "running": len([j for j in user_jobs if j.status.value == "running"]),
        },
    }


# --- Server version ---


@router.get("/version")
def get_server_version():
    """Server version and build info. Platform admin only."""
    from ..version import API_VERSION, BUILD_COMMIT, VERSION_STRING

    return {
        "version": VERSION_STRING,
        "api_version": API_VERSION,
        "build_commit": BUILD_COMMIT,
    }


# --- Server logs ---


@router.get("/logs")
def get_server_logs(lines: int = 200):
    """View recent server log output. Platform admin only.

    Reads from the structured log buffer if JSON logging is enabled,
    otherwise reads the last N lines from stderr/stdout capture.
    Safe for remote admin access — no shell, no file paths exposed.
    """
    from ..logging_config import LOG_FORMAT

    if LOG_FORMAT == "json":
        # Read from Python's root logger handlers
        import json as _json

        entries = []
        for handler in logging.root.handlers:
            if hasattr(handler, "stream") and hasattr(handler.stream, "getvalue"):
                # StringIO-backed handler (unlikely in production)
                raw = handler.stream.getvalue()
                for line in raw.strip().split("\n")[-lines:]:
                    try:
                        entries.append(_json.loads(line))
                    except Exception:
                        entries.append({"msg": line})
        if entries:
            return {"format": "json", "entries": entries[-lines:]}

    # Fallback: read from the in-memory log ring buffer
    from ..log_buffer import get_recent_logs

    return {"format": "text", "entries": get_recent_logs(lines)}


# --- Audit log ---


@router.get("/audit")
def get_audit_log(limit: int = 100, offset: int = 0, action: str | None = None):
    """Query the audit log. Platform admin only. Paginated."""
    from ..database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is None:
            return {"entries": [], "total": 0}
        cur = conn.cursor()
        where = ""
        params: list = []
        if action:
            where = "WHERE action = %s"
            params.append(action)

        cur.execute(f"SELECT COUNT(*) FROM ck.audit_log {where}", params)
        total = cur.fetchone()[0]

        cur.execute(
            f"""SELECT id, timestamp, actor_user_id, action, target_type,
                       target_id, details, ip_address
                FROM ck.audit_log {where}
                ORDER BY timestamp DESC LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
        entries = []
        for row in cur.fetchall():
            entries.append(
                {
                    "id": row[0],
                    "timestamp": row[1].isoformat() if row[1] else None,
                    "actor_user_id": row[2],
                    "action": row[3],
                    "target_type": row[4],
                    "target_id": row[5],
                    "details": row[6],
                    "ip_address": row[7],
                }
            )
        cur.close()
        return {"entries": entries, "total": total}


# ---------------------------------------------------------------------------
# Clip retention policy (CRKY-115)
# ---------------------------------------------------------------------------


class RetentionPolicyUpdate(BaseModel):
    enabled: bool | None = None
    retention_days: dict[str, int] | None = None
    delete_mode: str | None = None
    check_interval: int | None = None


@router.get("/retention")
def get_retention():
    """Get current clip retention policy."""
    from ..clip_retention import get_retention_policy

    policy = get_retention_policy()
    return {
        "enabled": policy.enabled,
        "retention_days": policy.retention_days,
        "delete_mode": policy.delete_mode,
        "check_interval": policy.check_interval,
    }


@router.put("/retention")
def update_retention(req: RetentionPolicyUpdate):
    """Update clip retention policy."""
    from ..clip_retention import get_retention_policy, set_retention_policy

    policy = get_retention_policy()
    if req.enabled is not None:
        policy.enabled = req.enabled
    if req.retention_days is not None:
        policy.retention_days = req.retention_days
    if req.delete_mode is not None:
        if req.delete_mode not in ("outputs_only", "full"):
            raise HTTPException(status_code=400, detail="delete_mode must be 'outputs_only' or 'full'")
        policy.delete_mode = req.delete_mode
    if req.check_interval is not None:
        if req.check_interval < 60:
            raise HTTPException(status_code=400, detail="check_interval must be >= 60 seconds")
        policy.check_interval = req.check_interval
    set_retention_policy(policy)
    return {
        "enabled": policy.enabled,
        "retention_days": policy.retention_days,
        "delete_mode": policy.delete_mode,
        "check_interval": policy.check_interval,
    }

"""Authentication endpoints — login, signup, invite tokens.

These routes are always public (no JWT required). They handle
the auth flow between the frontend and Supabase GoTrue.

MVP flow: invite-link based signup. Admin generates tokens,
shares via Discord/DM. Users sign up at /signup?invite=TOKEN.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import AUTH_ENABLED
from ..database import get_storage
from ..tier_guard import require_admin
from ..users import get_user_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Default admin credentials (first-time setup)
_DEFAULT_ADMIN_EMAIL = "admin@corridorkey.local"
_DEFAULT_ADMIN_PASSWORD = "admin"


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    invite_token: str = ""


class InviteTokenResponse(BaseModel):
    token: str
    created_at: float
    used: bool = False


@router.get("/me")
def get_current_user_info(request: Request):
    """Return the current user's tier from their JWT.

    Used by the /pending page to poll for approval. Decodes the JWT
    from the Authorization header without requiring it to go through
    the full middleware (this path is in PUBLIC_PREFIXES).
    """
    from ..auth import _decode_jwt

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"authenticated": False, "tier": None}
    try:
        claims = _decode_jwt(auth_header[7:])
        app_metadata = claims.get("app_metadata", {})
        return {
            "authenticated": True,
            "tier": app_metadata.get("tier", "pending"),
            "email": claims.get("email", ""),
            "user_id": claims.get("sub", ""),
        }
    except Exception:
        return {"authenticated": False, "tier": None}


@router.get("/status")
def auth_status():
    """Check if auth is enabled and return configuration hints."""
    return {
        "auth_enabled": AUTH_ENABLED,
        "supabase_url": os.environ.get("CK_SUPABASE_URL", ""),
        "gotrue_url": os.environ.get("CK_GOTRUE_URL", "http://localhost:54324"),
    }


@router.post("/invite/generate", dependencies=[Depends(require_admin)])
def generate_invite_token():
    """Generate an invite token for sharing. Admin only."""
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="Auth is not enabled")

    token = secrets.token_urlsafe(32)
    storage = get_storage()
    invites = storage.get_invite_tokens()
    invites[token] = {
        "created_at": time.time(),
        "used": False,
        "used_by": None,
    }
    storage.save_invite_token(token, invites[token])
    return {"token": token, "signup_url": f"/signup?invite={token}"}


@router.post("/invite/validate")
def validate_invite_token(token: str):
    """Check if an invite token is valid and unused."""
    storage = get_storage()
    invites = storage.get_invite_tokens()
    invite = invites.get(token)
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite token")
    if invite.get("used"):
        raise HTTPException(status_code=409, detail="Invite token already used")
    return {"valid": True}


@router.post("/invite/consume")
def consume_invite_token(token: str, email: str):
    """Mark an invite token as used after successful signup.

    Re-checks the used flag to narrow the TOCTOU window between
    validate and consume. The frontend should treat a 409 here
    as a failed signup and show an error.
    """
    storage = get_storage()
    invites = storage.get_invite_tokens()
    invite = invites.get(token)
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite token")
    if invite.get("used"):
        raise HTTPException(status_code=409, detail="Invite token already used")
    invite["used"] = True
    invite["used_by"] = email
    invite["used_at"] = time.time()
    storage.save_invite_token(token, invite)

    # Record the user for the approval workflow (CRKY-2)
    # user_id comes from GoTrue signup — the frontend passes email here,
    # and the actual user_id will be populated when they first authenticate.
    # For now, track by email so admins can see who signed up.
    user_store = get_user_store()
    user_store.record_signup(user_id=email, email=email)

    return {"status": "consumed"}


@router.post("/signup")
def signup_with_invite(req: SignupRequest):
    """Server-side signup: validate invite, create GoTrue user, consume invite.

    This replaces the frontend's direct GoTrue signup call so that
    GOTRUE_DISABLE_SIGNUP=true can be enforced. The server uses the
    GoTrue admin API (service role key) to create the user.
    """
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="Auth is not enabled")
    if not req.email or not req.password:
        raise HTTPException(status_code=400, detail="Email and password required")
    if not req.invite_token:
        raise HTTPException(status_code=400, detail="Invite token required")

    # Validate invite
    storage = get_storage()
    invites = storage.get_invite_tokens()
    invite = invites.get(req.invite_token)
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite token")
    if invite.get("used"):
        raise HTTPException(status_code=409, detail="Invite token already used")

    # Create user via GoTrue admin API (use internal URL for server-to-server)
    gotrue_url = os.environ.get("CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324"))
    service_key = os.environ.get("CK_SUPABASE_SERVICE_KEY", "")

    if service_key:
        # Use admin API (bypasses DISABLE_SIGNUP)
        import urllib.request

        admin_body = json.dumps({
            "email": req.email,
            "password": req.password,
            "email_confirm": True,
            "app_metadata": {"tier": "pending"},
            "user_metadata": {"name": req.name},
        }).encode()
        admin_req = urllib.request.Request(
            f"{gotrue_url}/admin/users",
            data=admin_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {service_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(admin_req, timeout=10) as resp:
                user_data = json.loads(resp.read())
                user_id = user_data.get("id", req.email)
        except Exception as e:
            logger.error(f"GoTrue admin API error: {e}")
            raise HTTPException(status_code=502, detail="Failed to create user account") from e
    else:
        # Fallback: direct signup (only works if DISABLE_SIGNUP=false)
        import urllib.request

        signup_body = json.dumps({"email": req.email, "password": req.password}).encode()
        signup_req = urllib.request.Request(
            f"{gotrue_url}/signup",
            data=signup_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(signup_req, timeout=10) as resp:
                user_data = json.loads(resp.read())
                user_id = user_data.get("user", {}).get("id", req.email)
        except Exception as e:
            logger.error(f"GoTrue signup error: {e}")
            raise HTTPException(status_code=502, detail="Failed to create user account") from e

    # Consume invite
    invite["used"] = True
    invite["used_by"] = req.email
    invite["used_at"] = time.time()
    storage.save_invite_token(req.invite_token, invite)

    # Record for approval workflow
    user_store = get_user_store()
    user_store.record_signup(user_id=user_id, email=req.email, name=req.name)

    return {"status": "created", "user_id": user_id}


@router.get("/invites", dependencies=[Depends(require_admin)])
def list_invites():
    """List all invite tokens. Admin only."""
    storage = get_storage()
    invites = storage.get_invite_tokens()
    return {
        "invites": [
            {
                "token": t[:8] + "...",
                "created_at": v["created_at"],
                "used": v.get("used", False),
                "used_by": v.get("used_by"),
            }
            for t, v in invites.items()
        ]
    }

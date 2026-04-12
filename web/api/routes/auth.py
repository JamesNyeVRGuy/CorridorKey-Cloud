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


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    invite_token: str = ""


class RegisterRequest(BaseModel):
    """Open registration — no invite token required (CRKY-103)."""

    email: str
    password: str
    name: str = ""
    company: str = ""
    role: str = ""
    use_case: str = ""


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
        user_id = claims.get("sub", "")
        # Read tier and name from user store (not JWT) — the user store
        # is updated immediately on approval, while the JWT may be stale
        # until the next token refresh from GoTrue.
        name = ""
        tier = app_metadata.get("tier", "pending")
        if user_id:
            user_store = get_user_store()
            user_record = user_store.get_user(user_id)
            if user_record:
                name = user_record.name
                tier = user_record.tier
        result = {
            "authenticated": True,
            "tier": tier,
            "email": claims.get("email", ""),
            "user_id": user_id,
            "name": name,
        }
        # Add queue position for pending users (CRKY-134)
        if tier == "pending" and user_id:
            try:
                pending = user_store.list_users(tier_filter="pending")
                pending.sort(key=lambda u: u.signed_up_at)
                position = next((i + 1 for i, u in enumerate(pending) if u.user_id == user_id), 0)
                result["queue_position"] = position
                result["queue_total"] = len(pending)
            except Exception:
                pass
        return result
    except Exception:
        return {"authenticated": False, "tier": None}


class UpdateProfileRequest(BaseModel):
    name: str


@router.patch("/me")
def update_profile(req: UpdateProfileRequest, request: Request):
    """Update the current user's display name."""
    from ..auth import _decode_jwt

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        claims = _decode_jwt(auth_header[7:])
        user_id = claims.get("sub", "")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token") from None

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Name too long (max 100 chars)")

    user_store = get_user_store()
    updated = user_store.update_name(user_id, name)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")

    # Update personal org name to match
    from ..orgs import get_org_store

    org_store = get_org_store()
    personal = org_store.get_personal_org(user_id)
    if personal:
        org_store.rename_org(personal.org_id, f"{name}'s workspace")

    return {"status": "updated", "name": name}


@router.post("/login")
def login_proxy(req: LoginRequest):
    """Proxy login through the server so external users don't need direct GoTrue access.

    The browser calls this instead of GoTrue directly. The server forwards
    the request to GoTrue using the internal URL and returns the session.
    """
    import urllib.request

    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()
    body = json.dumps({"email": req.email, "password": req.password}).encode()
    try:
        gotrue_req = urllib.request.Request(
            f"{gotrue_url}/token?grant_type=password",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(gotrue_req, timeout=10) as resp:
            data = json.loads(resp.read())
        # Only forward safe fields — don't leak GoTrue internals
        safe_keys = {
            "access_token",
            "refresh_token",
            "token_type",
            "expires_in",
            "expires_at",
            "user",
        }
        return {k: v for k, v in data.items() if k in safe_keys}
    except Exception as e:
        logger.error(f"GoTrue login proxy error: {e}")
        raise HTTPException(status_code=401, detail="Login failed") from e


@router.post("/refresh")
def refresh_proxy(request: Request):
    """Proxy token refresh through the server."""
    import urllib.request

    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()
    try:
        refresh_token = request.headers.get("X-Refresh-Token", "")
        if not refresh_token:
            raise HTTPException(status_code=400, detail="X-Refresh-Token header required")
        body = json.dumps({"refresh_token": refresh_token}).encode()
        gotrue_req = urllib.request.Request(
            f"{gotrue_url}/token?grant_type=refresh_token",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(gotrue_req, timeout=10) as resp:
            data = json.loads(resp.read())
        safe_keys = {
            "access_token",
            "refresh_token",
            "token_type",
            "expires_in",
            "expires_at",
            "user",
        }
        return {k: v for k, v in data.items() if k in safe_keys}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GoTrue refresh proxy error: {e}")
        raise HTTPException(status_code=401, detail="Token refresh failed") from e


class ChangePasswordRequest(BaseModel):
    password: str


@router.put("/password")
def change_password(req: ChangePasswordRequest, request: Request):
    """Proxy password change to GoTrue. Requires valid JWT.

    Only forwards the password field — GoTrue's PUT /user also accepts
    email/phone/metadata, which we don't allow changing through this endpoint.
    """
    import urllib.request

    from ..auth import _decode_jwt

    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()
    auth_header = request.headers.get("Authorization", "")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")

    # Validate the JWT before forwarding to GoTrue
    _decode_jwt(auth_header[7:])

    # Only forward the password field — prevent email/metadata injection
    safe_body = json.dumps({"password": req.password}).encode()

    try:
        gotrue_req = urllib.request.Request(
            f"{gotrue_url}/user",
            data=safe_body,
            headers={"Content-Type": "application/json", "Authorization": auth_header},
            method="PUT",
        )
        with urllib.request.urlopen(gotrue_req, timeout=10) as resp:
            resp.read()  # consume response but don't return GoTrue internals
        return {"status": "updated"}
    except Exception as e:
        logger.error(f"GoTrue password change error: {e}")
        raise HTTPException(status_code=400, detail="Password change failed") from e


class ForgotPasswordRequest(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    """Proxy password recovery to GoTrue. Sends a reset email."""
    import urllib.request

    if not req.email:
        raise HTTPException(status_code=400, detail="Email required")

    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()

    try:
        body = json.dumps({"email": req.email}).encode()
        gotrue_req = urllib.request.Request(
            f"{gotrue_url}/recover",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(gotrue_req, timeout=10) as resp:
            resp.read()
        return {"status": "sent"}
    except Exception as e:
        logger.error(f"GoTrue password recovery error: {e}")
        # Don't reveal whether the email exists — always return success
        return {"status": "sent"}


@router.get("/status")
def auth_status():
    """Check if auth is enabled and return configuration hints."""
    return {
        "auth_enabled": AUTH_ENABLED,
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
    if not invite or invite.get("used"):
        raise HTTPException(status_code=400, detail="Invalid or expired invite token")
    return {"valid": True}


@router.post("/invite/consume")
def consume_invite_token(token: str, email: str):
    """Mark an invite token as used after successful signup.

    Atomic check-and-set: reads the current state, verifies it's unused,
    and writes in a single operation. On the JSON backend this is
    single-threaded so effectively atomic. On Postgres this uses a
    single transaction.
    """
    storage = get_storage()
    # Atomic read-check-write: reload fresh state before each write
    invites = storage.get_invite_tokens()
    invite = invites.get(token)
    if not invite or invite.get("used"):
        raise HTTPException(status_code=400, detail="Invalid or expired invite token")
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

    # Validate and consume invite atomically BEFORE calling GoTrue.
    # This prevents TOCTOU: two concurrent signups with the same invite
    # would both pass validation, then both create GoTrue accounts.
    storage = get_storage()
    invites = storage.get_invite_tokens()
    invite = invites.get(req.invite_token)
    if not invite:
        raise HTTPException(status_code=400, detail="Invalid or expired invite token")
    if invite.get("used"):
        raise HTTPException(status_code=400, detail="Invalid or expired invite token")

    # Mark consumed before GoTrue call — revert on failure
    invite["used"] = True
    invite["used_by"] = req.email
    invite["used_at"] = time.time()
    storage.save_invite_token(req.invite_token, invite)

    # Create user via GoTrue admin API (use internal URL for server-to-server)
    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()
    service_key = os.environ.get("CK_SUPABASE_SERVICE_KEY", "").strip()
    anon_key = os.environ.get("ANON_KEY", "").strip()

    try:
        if service_key:
            # Use admin API (bypasses DISABLE_SIGNUP)
            import urllib.request
            admin_body = json.dumps(
                {
                    "email": req.email,
                    "password": req.password,
                    "email_confirm": False,
                    "app_metadata": {"tier": "pending"},
                    "user_metadata": {"name": req.name},
                }
            ).encode()
            admin_req = urllib.request.Request(
                f"{gotrue_url}/admin/users",
                data=admin_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {service_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(admin_req, timeout=10) as resp:
                user_data = json.loads(resp.read())
                user_id = user_data.get("id", req.email)

            # Trigger OTP/magic link email
            otp_body = json.dumps(
                {
                    "email": req.email,
                }
            ).encode()

            otp_req = urllib.request.Request(
                f"{gotrue_url}/otp",
                data=otp_body,
                headers={
                    "Content-Type": "application/json",
                    "apikey": anon_key,
                },
                method="POST",
            )
            logger.info(f"Triggering OTP email via GoTrue: {req.email}")
            
            with urllib.request.urlopen(otp_req, timeout=10) as resp:
                logger.info(f"MAIL:Email sent to {req.email} (confirmation)")
                user_data = json.loads(resp.read())
                user_id = user_data.get("id", req.email)
                # Email Sent
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
            with urllib.request.urlopen(signup_req, timeout=10) as resp:
                user_data = json.loads(resp.read())
                user_id = user_data.get("user", {}).get("id", req.email)
    except Exception as e:
        # Revert invite consumption so the token can be used again
        invite["used"] = False
        invite.pop("used_by", None)
        invite.pop("used_at", None)
        storage.save_invite_token(req.invite_token, invite)
        logger.error(f"GoTrue signup error: {e}")
        raise HTTPException(status_code=502, detail="Failed to create user account") from e

    # Record for approval workflow
    user_store = get_user_store()
    user_store.record_signup(user_id=user_id, email=req.email, name=req.name)

    return {"status": "created", "user_id": user_id}


@router.post("/register")
def open_register(req: RegisterRequest):
    """Open registration — no invite token required (CRKY-103).

    Creates a GoTrue user via admin API with tier=pending. Users must
    wait for admin approval before accessing the platform. Optional
    profile fields (company, role, use_case) are stored for admin review.
    """
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="Auth is not enabled")
    if not req.email or not req.password:
        raise HTTPException(status_code=400, detail="Email and password required")

    # Check if email is already registered
    user_store = get_user_store()
    if user_store.get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    gotrue_url = os.environ.get(
        "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
    ).strip()
    service_key = os.environ.get("CK_SUPABASE_SERVICE_KEY", "").strip()
    anon_key = os.environ.get("ANON_KEY", "").strip()


    if not service_key or not anon_key:
        raise HTTPException(status_code=500, detail="Server is not configured for open registration, check CK_SUPABASE_SERVICE_KEY and ANON_KEY")

    try:
        import urllib.request

        admin_body = json.dumps(
            {
                "email": req.email,
                "password": req.password,
                "email_confirm": False,  # require email verification before login
                "app_metadata": {"tier": "pending"},
                "user_metadata": {
                    "name": req.name,
                    "company": req.company,
                    "role": req.role,
                    "use_case": req.use_case,
                },
            }
        ).encode()
        admin_req = urllib.request.Request(
            f"{gotrue_url}/admin/users",
            data=admin_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {service_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(admin_req, timeout=10) as resp:
            user_data = json.loads(resp.read())
            user_id = user_data.get("id", req.email)

         # Trigger OTP/magic link email
        otp_body = json.dumps(
            {
                "email": req.email,
            }
        ).encode()

        otp_req = urllib.request.Request(
            f"{gotrue_url}/otp",
            data=otp_body,
            headers={
                "Content-Type": "application/json",
                "apikey": anon_key,
            },
            method="POST",
        )
        logger.info(f"Triggering OTP email via GoTrue: {req.email}")
        
        with urllib.request.urlopen(otp_req, timeout=10) as resp:
            logger.info(f"MAIL:Email sent to {req.email} (confirmation)")
            user_data = json.loads(resp.read())
            user_id = user_data.get("id", req.email)
            # Email Sent
    except Exception as e:
        error_msg = str(e)
        if "already been registered" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(status_code=409, detail="An account with this email already exists") from e
        logger.error(f"GoTrue registration error: {e}")
        raise HTTPException(status_code=502, detail="Failed to create user account") from e

    user_store.record_signup(
        user_id=user_id,
        email=req.email,
        name=req.name,
        company=req.company,
        role=req.role,
        use_case=req.use_case,
    )
    logger.info(f"Open registration: {req.email} → pending (company={req.company!r})")

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

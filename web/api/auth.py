"""JWT authentication middleware for the CorridorKey cloud platform.

Validates Supabase-issued JWTs and injects user context into requests.
Disabled by default (CK_AUTH_ENABLED=false) for backward compatibility
with single-user local deployments.

When enabled, every request (except /api/auth/* and static assets)
must carry a valid Authorization: Bearer <JWT> header.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Configuration from environment
AUTH_ENABLED = os.environ.get("CK_AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
JWT_SECRET = os.environ.get("CK_JWT_SECRET", "")
JWT_ALGORITHMS = ["HS256"]

# Paths that don't require authentication
PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/signup",
    "/api/auth/refresh",
    "/api/auth/callback",
    "/api/auth/status",
    "/api/health",
    "/metrics",
    "/docs",
    "/openapi.json",
}

# Path prefixes that don't require authentication
PUBLIC_PREFIXES = (
    "/_app/",  # SvelteKit static assets
    "/ws",  # WebSocket (has its own auth, CRKY-13)
    "/api/auth/status",  # Auth status check
    "/api/auth/login",  # Login proxy to GoTrue
    "/api/auth/refresh",  # Token refresh proxy to GoTrue
    "/api/auth/signup",  # Server-side signup
    "/api/auth/invite/validate",  # Invite validation (pre-signup)
    "/api/auth/invite/consume",  # Invite consumption (post-signup)
    "/api/auth/me",  # Current user tier check (pending page polling)
    "/api/nodes/",  # Nodes use CK_AUTH_TOKEN, not JWT
    "/api/system/weights/",  # Weight sync for nodes
)


@dataclass
class UserContext:
    """Authenticated user context injected into request state."""

    user_id: str
    email: str = ""
    tier: str = "pending"  # pending, member, contributor, org_admin, platform_admin
    org_ids: list[str] = field(default_factory=list)
    raw_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.tier == "platform_admin"

    @property
    def is_contributor(self) -> bool:
        return self.tier in ("contributor", "org_admin", "platform_admin")

    @property
    def is_member(self) -> bool:
        return self.tier in ("member", "contributor", "org_admin", "platform_admin")


def get_current_user(request: Request) -> UserContext | None:
    """Extract the authenticated user from request state.

    Returns None if auth is disabled or user is not authenticated.
    Use this in route handlers to get user context.
    """
    return getattr(request.state, "user", None)


def require_user(request: Request) -> UserContext:
    """Extract the authenticated user, raising 401 if not present.

    Use this in route handlers that require authentication.
    """
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


TIER_HIERARCHY = ["pending", "member", "contributor", "org_admin", "platform_admin"]


def require_tier(request: Request, min_tier: str) -> UserContext:
    """Require a minimum trust tier. Raises 403 if insufficient.

    Tier hierarchy: pending < member < contributor < org_admin < platform_admin
    """
    user = require_user(request)
    try:
        user_level = TIER_HIERARCHY.index(user.tier)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"Unknown tier: {user.tier}") from None
    try:
        min_level = TIER_HIERARCHY.index(min_tier)
    except ValueError:
        raise HTTPException(status_code=500, detail=f"Invalid tier requirement: {min_tier}") from None
    if user_level < min_level:
        raise HTTPException(
            status_code=403,
            detail=f"Requires {min_tier} tier or higher (you are {user.tier})",
        )
    return user


def _decode_jwt(token: str) -> dict[str, Any]:
    """Decode and validate a Supabase JWT."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=JWT_ALGORITHMS, audience="authenticated")
        return payload
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Token expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that validates JWTs and injects user context.

    When CK_AUTH_ENABLED=false (default), all requests pass through
    with request.state.user = None. Existing endpoints work unchanged.

    When CK_AUTH_ENABLED=true, requests without a valid JWT get a 401,
    except for public paths (auth endpoints, static assets, health).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.user = None

        if not AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path

        # Skip auth for public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Skip auth for SPA fallback (non-API, non-WS GET requests serve index.html)
        if not path.startswith("/api/") and request.method == "GET":
            return await call_next(request)

        # Extract Bearer token from header or query param (for img/video src)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            # Fallback: check query parameter (for <img src>, <video src>)
            token = request.query_params.get("token", "")

        if not token:
            return JSONResponse(status_code=401, content={"detail": "Missing Authorization header"})
        try:
            claims = _decode_jwt(token)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

        # Build user context from JWT claims
        # Supabase JWTs contain: sub (user_id), email, role, app_metadata, user_metadata
        app_metadata = claims.get("app_metadata", {})

        user_id = claims.get("sub", "")
        email = claims.get("email", "")

        request.state.user = UserContext(
            user_id=user_id,
            email=email,
            tier=app_metadata.get("tier", "pending"),
            org_ids=app_metadata.get("org_ids", []),
            raw_claims=claims,
        )

        # Auto-register user in local store on first auth.
        # Handles users created via create-admin.sh or GoTrue admin API
        # who don't have a local record yet.
        if user_id and email:
            try:
                from .users import get_user_store

                store = get_user_store()
                # Link email-based signup record to real UUID (CRKY-61)
                if store.get_user(email) and not store.get_user(user_id):
                    store.link_uuid(email, user_id)
                # Auto-register if not in local store at all
                if not store.get_user(user_id):
                    tier = app_metadata.get("tier", "pending")
                    store.record_signup(user_id=user_id, email=email)
                    if tier != "pending":
                        store.set_tier(user_id, tier)

                # Ensure personal org exists (may be missing if approved via tier dropdown)
                from .orgs import get_org_store

                org_store = get_org_store()
                if not org_store.get_personal_org(user_id):
                    tier = app_metadata.get("tier", "pending")
                    if tier != "pending":
                        personal_org = org_store.ensure_personal_org(user_id, email)
                        from .gpu_credits import STARTER_CREDITS, add_contributed

                        if STARTER_CREDITS > 0:
                            add_contributed(personal_org.org_id, STARTER_CREDITS)
                        logger.info(f"Auto-created personal org for {email}")
            except Exception:
                pass  # Non-critical — don't block the request

        return await call_next(request)

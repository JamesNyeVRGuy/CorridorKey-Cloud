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
    "/api/health",
    "/metrics",
    "/docs",
    "/openapi.json",
}

# Path prefixes that don't require authentication
PUBLIC_PREFIXES = (
    "/_app/",  # SvelteKit static assets
    "/ws",  # WebSocket (has its own auth, CRKY-13)
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


def require_tier(request: Request, min_tier: str) -> UserContext:
    """Require a minimum trust tier. Raises 403 if insufficient.

    Tier hierarchy: pending < member < contributor < org_admin < platform_admin
    """
    user = require_user(request)
    tiers = ["pending", "member", "contributor", "org_admin", "platform_admin"]
    if tiers.index(user.tier) < tiers.index(min_tier):
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

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "Missing Authorization header"})

        token = auth_header[7:]
        try:
            claims = _decode_jwt(token)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

        # Build user context from JWT claims
        # Supabase JWTs contain: sub (user_id), email, role, app_metadata, user_metadata
        app_metadata = claims.get("app_metadata", {})

        request.state.user = UserContext(
            user_id=claims.get("sub", ""),
            email=claims.get("email", ""),
            tier=app_metadata.get("tier", "pending"),
            org_ids=app_metadata.get("org_ids", []),
            raw_claims=claims,
        )

        return await call_next(request)

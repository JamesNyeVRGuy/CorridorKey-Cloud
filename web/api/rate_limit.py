"""Per-tier rate limiting middleware (CRKY-11).

In-memory sliding window rate limiter keyed by user_id (from JWT) or
client IP (for unauthenticated requests). Limits are configurable per
trust tier.

No external dependencies (Redis, etc.) — suitable for single-server
deployments. For multi-server deployments, replace the in-memory store
with Redis or a shared database.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Rate limits per tier: (requests_per_minute, jobs_per_hour)
# Set to (0, 0) for unlimited
# Note: a single page load triggers 5-8 API calls, so limits must
# accommodate normal browsing patterns (page nav, polling, uploads)
TIER_LIMITS: dict[str, tuple[int, int]] = {
    "pending": (30, 0),  # Limited — can check status and auth endpoints
    "member": (300, 30),  # Normal usage (~5 pages/min with full refresh)
    "contributor": (600, 60),  # Higher limits for power users
    "org_admin": (600, 60),  # Same as contributor
    "platform_admin": (0, 0),  # Unlimited
}

# Default for unknown tiers or unauthenticated
DEFAULT_LIMIT = (120, 10)

# Paths that are never rate-limited (high-frequency or critical)
EXEMPT_PREFIXES = (
    "/_app/",
    "/api/health",
    "/api/auth/status",
    "/api/auth/me",
    "/api/nodes/",  # Node agent endpoints (auth via CK_AUTH_TOKEN, not JWT)
    "/api/system/vram",  # VRAM polling (every 10s)
    "/api/farm",  # Node list refresh on WS events
    "/api/status",  # Public status page (CRKY-51)
)


_CLEANUP_INTERVAL = 600  # run cleanup every 10 minutes
_STALE_KEY_AGE = 3600  # remove keys with no activity for 1 hour


class _SlidingWindow:
    """Thread-safe sliding window counter with automatic stale key cleanup."""

    def __init__(self):
        self._lock = threading.Lock()
        # {key: [timestamp, timestamp, ...]}
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()

    def check_and_record(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        """Check if within limit, record if so.

        Returns (allowed: bool, remaining: int).
        """
        if limit <= 0:
            return True, 999  # Unlimited

        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            # Periodic cleanup of stale keys (every 10 minutes)
            if now - self._last_cleanup > _CLEANUP_INTERVAL:
                self._cleanup_locked(now)

            # Prune old entries for this key
            entries = self._windows[key]
            self._windows[key] = [t for t in entries if t > cutoff]
            entries = self._windows[key]

            if len(entries) >= limit:
                return False, 0

            entries.append(now)
            return True, limit - len(entries)

    def _cleanup_locked(self, now: float) -> None:
        """Remove keys with no recent entries. Must be called under self._lock."""
        cutoff = now - _STALE_KEY_AGE
        stale_keys = [k for k, v in self._windows.items() if not v or v[-1] < cutoff]
        if stale_keys:
            for k in stale_keys:
                del self._windows[k]
            logger.debug(f"Rate limiter cleanup: removed {len(stale_keys)} stale keys, {len(self._windows)} remaining")
        self._last_cleanup = now


# Global counters
_request_counter = _SlidingWindow()
_job_counter = _SlidingWindow()


def _get_rate_key(request: Request) -> tuple[str, str]:
    """Extract rate limit key and tier from request.

    Returns (key, tier). Key is user_id if authenticated, IP otherwise.
    """
    user = getattr(request.state, "user", None)
    if user and user.user_id:
        return f"user:{user.user_id}", user.tier
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}", ""


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces per-tier rate limits.

    Runs after AuthMiddleware (so request.state.user is populated).
    Returns 429 Too Many Requests with Retry-After header when exceeded.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip exempt paths and non-API GET requests (SPA pages)
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if not path.startswith("/api/") and request.method == "GET":
            return await call_next(request)

        key, tier = _get_rate_key(request)
        req_limit, _ = TIER_LIMITS.get(tier, DEFAULT_LIMIT)

        # Check request rate (per minute)
        allowed, remaining = _request_counter.check_and_record(f"{key}:req", req_limit, 60.0)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again shortly."},
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
            )

        # Check job submission rate (per hour) for job endpoints
        if path.startswith("/api/jobs") and request.method == "POST":
            _, job_limit = TIER_LIMITS.get(tier, DEFAULT_LIMIT)
            job_allowed, job_remaining = _job_counter.check_and_record(f"{key}:job", job_limit, 3600.0)
            if not job_allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Job submission rate limit exceeded. Try again later."},
                    headers={"Retry-After": "3600", "X-RateLimit-Remaining": "0"},
                )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

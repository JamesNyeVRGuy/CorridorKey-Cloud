"""API versioning middleware (CRKY-32).

Adds X-API-Version response header to all API responses. This enables
clients to detect version changes and adapt accordingly.

Future breaking changes will increment the major version. Clients can
use this header for compatibility checks.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .openapi_config import API_VERSION


class APIVersionMiddleware(BaseHTTPMiddleware):
    """Injects X-API-Version, security headers, and counts API requests."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        # Security headers (CRKY-87)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        is_api = request.url.path.startswith("/api/") or request.url.path in ("/docs", "/redoc", "/openapi.json")
        if is_api:
            response.headers["X-API-Version"] = API_VERSION
            # Count API requests for Prometheus metrics (CRKY-27)
            from .metrics import increment_request_count

            increment_request_count()
        return response

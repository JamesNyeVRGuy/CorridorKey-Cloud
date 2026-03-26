"""WebSocket endpoint and connection manager for real-time updates.

Supports JWT authentication (CRKY-13): when auth is enabled, clients
must pass a valid JWT as a query parameter (?token=...). Connections
without a valid token are rejected. When auth is disabled, all
connections are accepted (backward compatible).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# Global connection cap (prevents resource exhaustion via many accounts or unauthenticated mode)
MAX_TOTAL_CONNECTIONS = int(os.environ.get("CK_MAX_WS_CONNECTIONS", "500").strip())


@dataclass
class AuthenticatedConnection:
    """A WebSocket connection with optional user context."""

    ws: WebSocket
    user_id: str = ""
    org_ids: list[str] = field(default_factory=list)
    is_admin: bool = False
    expires_at: float = 0  # JWT exp claim — 0 means no expiry check


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts messages."""

    def __init__(self):
        self._connections: list[AuthenticatedConnection] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # Max concurrent WebSocket connections per user (0 = unlimited)
    MAX_CONNECTIONS_PER_USER = 3

    async def connect(
        self,
        ws: WebSocket,
        user_id: str = "",
        org_ids: list[str] | None = None,
        is_admin: bool = False,
        expires_at: float = 0,
    ) -> bool:
        """Accept a WebSocket connection. Returns False if rejected."""
        # Global connection limit
        if MAX_TOTAL_CONNECTIONS > 0 and len(self._connections) >= MAX_TOTAL_CONNECTIONS:
            await ws.close(code=4029, reason="Server connection limit reached")
            return False
        # Per-user connection limit
        if user_id and self.MAX_CONNECTIONS_PER_USER > 0:
            user_conns = sum(1 for c in self._connections if c.user_id == user_id)
            if user_conns >= self.MAX_CONNECTIONS_PER_USER:
                await ws.close(code=4029, reason="Too many connections")
                return False
        await ws.accept()
        conn = AuthenticatedConnection(
            ws=ws, user_id=user_id, org_ids=org_ids or [], is_admin=is_admin, expires_at=expires_at
        )
        self._connections.append(conn)
        logger.info(f"WebSocket connected ({len(self._connections)} total)")
        return True

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [c for c in self._connections if c.ws is not ws]
        logger.info(f"WebSocket disconnected ({len(self._connections)} total)")

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def _broadcast(self, message: dict[str, Any], org_id: str | None = None) -> None:
        """Broadcast to connections, optionally filtered by org_id."""
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for conn in list(self._connections):  # snapshot to avoid mutation during iteration
            # Org filtering: admins see all, others only see their orgs' events.
            # Empty org_ids means "sees nothing" (not "sees everything").
            if org_id and not conn.is_admin:
                if not conn.org_ids or org_id not in conn.org_ids:
                    continue
            try:
                await conn.ws.send_text(payload)
            except Exception:
                dead.append(conn.ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_sync(self, message: dict[str, Any], org_id: str | None = None) -> None:
        """Thread-safe broadcast from the worker thread."""
        if not self._connections or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message, org_id), self._loop)
        except RuntimeError:
            pass

    def send_job_progress(
        self, job_id: str, clip_name: str, current: int, total: int, org_id: str | None = None
    ) -> None:
        self.broadcast_sync(
            {
                "type": "job:progress",
                "data": {"job_id": job_id, "clip_name": clip_name, "current": current, "total": total},
            },
            org_id=org_id,
        )

    def send_job_status(self, job_id: str, status: str, error: str | None = None, org_id: str | None = None) -> None:
        self.broadcast_sync(
            {
                "type": "job:status",
                "data": {"job_id": job_id, "status": status, "error": error},
            },
            org_id=org_id,
        )

    def send_job_warning(self, job_id: str, message: str, org_id: str | None = None) -> None:
        self.broadcast_sync(
            {
                "type": "job:warning",
                "data": {"job_id": job_id, "message": message},
            },
            org_id=org_id,
        )

    def send_clip_state_changed(self, clip_name: str, new_state: str, org_id: str | None = None) -> None:
        self.broadcast_sync(
            {
                "type": "clip:state_changed",
                "data": {"clip_name": clip_name, "new_state": new_state},
            },
            org_id=org_id,
        )

    async def _broadcast_admin_only(self, message: dict[str, Any]) -> None:
        """Broadcast only to admin connections."""
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for conn in list(self._connections):
            if not conn.is_admin:
                continue
            try:
                await conn.ws.send_text(payload)
            except Exception:
                dead.append(conn.ws)
        for ws in dead:
            self.disconnect(ws)

    def send_vram_update(self, vram: dict) -> None:
        if not self._connections or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_admin_only({"type": "vram:update", "data": vram}), self._loop
            )
        except RuntimeError:
            pass

    def send_node_update(self, node_data: dict, org_id: str | None = None) -> None:
        self.broadcast_sync(
            {
                "type": "node:update",
                "data": node_data,
            },
            org_id=org_id,
        )

    def send_node_offline(self, node_id: str, org_id: str | None = None) -> None:
        self.broadcast_sync(
            {
                "type": "node:offline",
                "data": {"node_id": node_id},
            },
            org_id=org_id,
        )


manager = ConnectionManager()


def _validate_ws_token(token: str) -> dict[str, Any] | None:
    """Validate a JWT for WebSocket auth. Returns claims or None."""
    from .auth import JWT_ALGORITHMS, JWT_SECRET

    try:
        import jwt as pyjwt

        return pyjwt.decode(token, JWT_SECRET, algorithms=JWT_ALGORITHMS, audience="authenticated")
    except Exception:
        return None


async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint with optional JWT authentication.

    When CK_AUTH_ENABLED=true, requires ?token=<JWT> query parameter.
    When disabled, accepts all connections.
    """
    from .auth import AUTH_ENABLED, TIER_HIERARCHY

    user_id = ""
    org_ids: list[str] = []
    tier = ""
    expires_at: float = 0

    if AUTH_ENABLED:
        token = ws.query_params.get("token", "")
        if not token:
            await ws.close(code=4001, reason="Missing token")
            return
        claims = _validate_ws_token(token)
        if claims is None:
            await ws.close(code=4001, reason="Invalid token")
            return
        user_id = claims.get("sub", "")
        if not user_id:
            await ws.close(code=4001, reason="Invalid token")
            return
        expires_at = claims.get("exp", 0)
        app_metadata = claims.get("app_metadata", {})
        tier = app_metadata.get("tier", "pending")

        # Cross-check tier against local store — local store is authoritative
        try:
            from .users import get_user_store

            local_user = get_user_store().get_user(user_id)
            if local_user and local_user.tier in TIER_HIERARCHY and local_user.tier != tier:
                tier = local_user.tier
        except Exception:
            pass

        # Look up org_ids from org store (JWT org_ids may be stale)
        try:
            from .orgs import get_org_store

            org_ids = [o.org_id for o in get_org_store().list_user_orgs(user_id)]
        except Exception:
            org_ids = app_metadata.get("org_ids", [])

    is_admin = (tier == "platform_admin") if AUTH_ENABLED else True
    if not await manager.connect(ws, user_id=user_id, org_ids=org_ids, is_admin=is_admin, expires_at=expires_at):
        return  # Connection was rejected (limit reached)

    async def _expiry_watchdog():
        """Close connection when JWT expires."""
        if expires_at <= 0:
            return
        import time

        while True:
            remaining = expires_at - time.time()
            if remaining <= 0:
                try:
                    await ws.close(code=4001, reason="Token expired")
                except Exception:
                    pass
                return
            await asyncio.sleep(min(remaining, 30.0))

    watchdog = asyncio.create_task(_expiry_watchdog()) if expires_at > 0 else None
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket error", exc_info=True)
    finally:
        if watchdog and not watchdog.done():
            watchdog.cancel()
        manager.disconnect(ws)

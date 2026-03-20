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
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


@dataclass
class AuthenticatedConnection:
    """A WebSocket connection with optional user context."""

    ws: WebSocket
    user_id: str = ""
    org_ids: list[str] = field(default_factory=list)
    is_admin: bool = False


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts messages."""

    def __init__(self):
        self._connections: list[AuthenticatedConnection] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # Max concurrent WebSocket connections per user (0 = unlimited)
    MAX_CONNECTIONS_PER_USER = 10

    async def connect(
        self, ws: WebSocket, user_id: str = "", org_ids: list[str] | None = None, is_admin: bool = False
    ) -> None:
        # Enforce per-user connection limit
        if user_id and self.MAX_CONNECTIONS_PER_USER > 0:
            user_conns = sum(1 for c in self._connections if c.user_id == user_id)
            if user_conns >= self.MAX_CONNECTIONS_PER_USER:
                await ws.close(code=4029, reason="Too many connections")
                return
        await ws.accept()
        conn = AuthenticatedConnection(ws=ws, user_id=user_id, org_ids=org_ids or [], is_admin=is_admin)
        self._connections.append(conn)
        logger.info(f"WebSocket connected ({len(self._connections)} total)")

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
        for conn in self._connections:
            # Admins see everything. Other users only see their orgs' events.
            if org_id and not conn.is_admin and conn.org_ids and org_id not in conn.org_ids:
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

    def send_vram_update(self, vram: dict) -> None:
        # VRAM is server-level, no org filtering (only admins see it anyway)
        self.broadcast_sync(
            {
                "type": "vram:update",
                "data": vram,
            }
        )

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
    from .auth import AUTH_ENABLED

    user_id = ""
    org_ids: list[str] = []
    tier = ""

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
        app_metadata = claims.get("app_metadata", {})
        tier = app_metadata.get("tier", "pending")
        # Look up org_ids from org store (JWT org_ids may be empty)
        if user_id:
            try:
                from .orgs import get_org_store

                org_ids = [o.org_id for o in get_org_store().list_user_orgs(user_id)]
            except Exception:
                org_ids = app_metadata.get("org_ids", [])
        else:
            org_ids = app_metadata.get("org_ids", [])

    is_admin = (tier == "platform_admin") if AUTH_ENABLED else True
    await manager.connect(ws, user_id=user_id, org_ids=org_ids, is_admin=is_admin)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

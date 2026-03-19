"""Structured audit log for security-relevant actions (CRKY-18).

Records who did what, when, from where. Stored in ck.audit_log table
(created by migration 002). Falls back to Python logging if Postgres
is unavailable.

Usage:
    from web.api.audit import audit_log
    audit_log("user.approved", actor=user_id, target_type="user",
              target_id=approved_user_id, details={"tier": "member"})
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def audit_log(
    action: str,
    *,
    actor: str = "",
    target_type: str = "",
    target_id: str = "",
    details: dict[str, Any] | None = None,
    ip_address: str = "",
) -> None:
    """Record an audit event.

    Writes to ck.audit_log if Postgres is available, otherwise logs
    to Python logger at INFO level. Non-blocking — failures are logged
    but never raise.

    Args:
        action: dot-separated action name (e.g., "user.approved", "org.created")
        actor: user_id of who performed the action
        target_type: type of target (e.g., "user", "org", "clip", "node")
        target_id: ID of the target
        details: additional context as a JSON-serializable dict
        ip_address: client IP if available
    """
    try:
        from .database import get_pg_conn

        with get_pg_conn() as conn:
            if conn is not None:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO ck.audit_log
                       (timestamp, actor_user_id, action, target_type, target_id, details, ip_address)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s)""",
                    (
                        actor or None,
                        action,
                        target_type or None,
                        target_id or None,
                        json.dumps(details or {}),
                        ip_address or None,
                    ),
                )
                cur.close()
                return
    except Exception as e:
        logger.debug(f"Audit log DB write failed ({e}), falling back to logger")

    # Fallback: Python logger
    logger.info(
        f"AUDIT action={action} actor={actor} target={target_type}:{target_id} "
        f"details={json.dumps(details or {})} ip={ip_address}"
    )


def audit_from_request(
    action: str,
    request: Any,
    *,
    target_type: str = "",
    target_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """Record an audit event, extracting actor and IP from a FastAPI Request."""
    from .auth import get_current_user

    user = get_current_user(request)
    actor = user.user_id if user else ""
    ip = request.client.host if request.client else ""

    audit_log(
        action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip_address=ip,
    )

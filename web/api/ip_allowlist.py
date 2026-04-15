"""Per-org IP allowlisting (CRKY-25).

Optional network-level access control. When an org has an allowlist
configured, only requests from listed IPs/CIDRs can access that org's
resources. Platform admins always bypass.

Allowlists are stored in ck_settings via the storage backend.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import HTTPException, Request

from .auth import AUTH_ENABLED, get_current_user
from .orgs import get_org_store

logger = logging.getLogger(__name__)


def _load_allowlists() -> dict[str, list[str]]:
    """Load all allowlists as {org_id: [cidr, ...]}.

    Prefers the ck.ip_allowlist table; falls back to the legacy JSON
    blob when Postgres is unavailable.
    """
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute("SELECT org_id, cidr FROM ck.ip_allowlist")
            rows = cur.fetchall()
            cur.close()
            out: dict[str, list[str]] = {}
            for org_id, cidr in rows:
                out.setdefault(org_id, []).append(cidr)
            return out

    from .database import get_storage

    return get_storage().get_setting("ip_allowlists", {})


def save_allowlist(org_id: str, cidrs: list[str]) -> None:
    """Save the allowlist for an org (bulk replace). Empty list = no restriction.

    Postgres path uses a DELETE + INSERT inside a single CTE so the
    replacement is atomic per org and cannot interfere with a parallel
    write for a different org. The JSON fallback is still a
    blob-rewrite and remains vulnerable to cross-org interference —
    dev/test only.
    """
    from .database import get_pg_conn

    if cidrs:
        for cidr in cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid CIDR: {cidr} ({e})") from None

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            if cidrs:
                cur.execute(
                    """WITH deleted AS (
                           DELETE FROM ck.ip_allowlist WHERE org_id = %s
                       )
                       INSERT INTO ck.ip_allowlist (org_id, cidr)
                       SELECT %s, UNNEST(%s::text[])
                       ON CONFLICT DO NOTHING""",
                    (org_id, org_id, list(cidrs)),
                )
            else:
                cur.execute("DELETE FROM ck.ip_allowlist WHERE org_id = %s", (org_id,))
            cur.close()
            return

    from .database import get_storage

    storage = get_storage()
    allowlists = storage.get_setting("ip_allowlists", {})
    if cidrs:
        allowlists[org_id] = cidrs
    else:
        allowlists.pop(org_id, None)
    storage.set_setting("ip_allowlists", allowlists)


def check_ip_allowlist(request: Request) -> None:
    """Check if the client IP is allowed for the user's org.

    No-op when:
    - Auth is disabled
    - User is platform_admin
    - Org has no allowlist configured
    """
    if not AUTH_ENABLED:
        return

    user = get_current_user(request)
    if not user or user.is_admin:
        return

    client_ip = request.client.host if request.client else None
    if not client_ip:
        return

    org_store = get_org_store()
    user_orgs = org_store.list_user_orgs(user.user_id)
    if not user_orgs:
        return

    allowlists = _load_allowlists()

    for org in user_orgs:
        cidrs = allowlists.get(org.org_id)
        if not cidrs:
            continue  # No allowlist = unrestricted

        try:
            client = ipaddress.ip_address(client_ip)
            if any(client in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs):
                return  # Allowed
        except ValueError:
            pass

        raise HTTPException(
            status_code=403,
            detail=f"Access denied: your IP ({client_ip}) is not in the allowlist for {org.name}",
        )

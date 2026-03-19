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
    """Load allowlists from storage. Returns {org_id: [cidr, ...]}."""
    from .database import get_storage

    return get_storage().get_setting("ip_allowlists", {})


def save_allowlist(org_id: str, cidrs: list[str]) -> None:
    """Save allowlist for an org. Empty list = no restriction."""
    from .database import get_storage

    allowlists = _load_allowlists()
    if cidrs:
        # Validate all CIDRs
        for cidr in cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid CIDR: {cidr} ({e})") from None
        allowlists[org_id] = cidrs
    else:
        allowlists.pop(org_id, None)
    get_storage().set_setting("ip_allowlists", allowlists)


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

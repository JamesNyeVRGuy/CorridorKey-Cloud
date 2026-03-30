"""Per-org storage quotas (CRKY-15).

Tracks disk usage per org and enforces configurable quotas.
Upload endpoints check remaining quota before accepting files.
"""

from __future__ import annotations

import logging
import os
import threading

from fastapi import HTTPException, Request

from .auth import AUTH_ENABLED, get_current_user
from .org_isolation import get_base_clips_dir
from .orgs import get_org_store

logger = logging.getLogger(__name__)

# Default quotas in bytes
_GB = 1024**3
DEFAULT_PERSONAL_QUOTA = int(os.environ.get("CK_QUOTA_PERSONAL_GB", "50").strip()) * _GB
DEFAULT_TEAM_QUOTA = int(os.environ.get("CK_QUOTA_TEAM_GB", "200").strip()) * _GB

# Disk usage cache: {org_id: (timestamp, bytes)}
_usage_cache: dict[str, tuple[float, int]] = {}
_CACHE_TTL = 30  # seconds — recompute at most every 30s
_CACHE_MAX_SIZE = 500  # max org entries before eviction
_CACHE_EVICT_AGE = 300  # evict entries older than 5 minutes
# Max concurrent uploads per user
_MAX_CONCURRENT_UPLOADS = int(os.environ.get("CK_MAX_CONCURRENT_UPLOADS", "3").strip())
_active_uploads: dict[str, int] = {}  # {user_id: count}
_upload_lock = threading.Lock()


def _evict_stale_cache() -> None:
    """Remove cache entries older than _CACHE_EVICT_AGE, or oldest if over _CACHE_MAX_SIZE."""
    import time

    now = time.time()
    # Remove stale entries
    stale = [k for k, (ts, _) in _usage_cache.items() if now - ts > _CACHE_EVICT_AGE]
    for k in stale:
        del _usage_cache[k]
    # If still over max, evict oldest
    while len(_usage_cache) > _CACHE_MAX_SIZE:
        oldest_key = min(_usage_cache, key=lambda k: _usage_cache[k][0])
        del _usage_cache[oldest_key]


def get_org_disk_usage(org_id: str) -> int:
    """Calculate total disk usage for an org in bytes. Cached for 30s."""
    import time

    now = time.time()
    cached = _usage_cache.get(org_id)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    # Evict stale entries before adding new ones
    if len(_usage_cache) >= _CACHE_MAX_SIZE:
        _evict_stale_cache()

    base = get_base_clips_dir()
    if not base:
        return 0
    org_dir = os.path.join(base, org_id)
    if not os.path.isdir(org_dir):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(org_dir):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    _usage_cache[org_id] = (now, total)
    return total


def invalidate_usage_cache(org_id: str) -> None:
    """Clear cached usage after upload completes."""
    _usage_cache.pop(org_id, None)


def get_org_quota(org_id: str) -> int:
    """Get the quota for an org in bytes.

    Checks for a per-org override in settings, then falls back to
    defaults based on org type (personal vs team).
    """
    from .database import get_storage

    storage = get_storage()
    overrides = storage.get_setting("storage_quotas", {})
    if org_id in overrides:
        return int(overrides[org_id]) * _GB

    org_store = get_org_store()
    org = org_store.get_org(org_id)
    if org and org.personal:
        return DEFAULT_PERSONAL_QUOTA
    return DEFAULT_TEAM_QUOTA


def get_org_storage_info(org_id: str) -> dict:
    """Get storage usage and quota info for an org."""
    used = get_org_disk_usage(org_id)
    quota = get_org_quota(org_id)
    return {
        "org_id": org_id,
        "used_bytes": used,
        "used_gb": round(used / _GB, 2),
        "quota_bytes": quota,
        "quota_gb": round(quota / _GB, 1),
        "remaining_bytes": max(0, quota - used),
        "remaining_gb": round(max(0, quota - used) / _GB, 2),
        "percent_used": round(used / quota * 100, 1) if quota > 0 else 0,
    }


def check_storage_quota(request: Request, additional_bytes: int = 0) -> None:
    """Check if the user's org has sufficient storage quota.

    Also enforces max concurrent uploads per user.
    Raises HTTP 413 if over quota, 429 if too many concurrent uploads.
    No-op when auth is disabled.
    """
    if not AUTH_ENABLED:
        return

    user = get_current_user(request)
    if not user:
        return
    if user.is_admin:
        return

    org_store = get_org_store()
    user_orgs = org_store.list_user_orgs(user.user_id)
    if not user_orgs:
        return  # No org — no quota to check, no slot to track

    # Concurrent upload limit (after org check to avoid slot leak for org-less users)
    if _MAX_CONCURRENT_UPLOADS > 0:
        with _upload_lock:
            current = _active_uploads.get(user.user_id, 0)
            if current >= _MAX_CONCURRENT_UPLOADS:
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many concurrent uploads (max {_MAX_CONCURRENT_UPLOADS}). "
                    "Wait for current uploads to finish.",
                )
            _active_uploads[user.user_id] = current + 1

    org = user_orgs[0]
    used = get_org_disk_usage(org.org_id)
    quota = get_org_quota(org.org_id)

    if used + additional_bytes > quota:
        _release_upload_slot(user.user_id)
        used_gb = round(used / _GB, 1)
        quota_gb = round(quota / _GB, 1)
        raise HTTPException(
            status_code=413,
            detail=f"Storage quota exceeded: {used_gb} GB used of {quota_gb} GB. "
            f"Delete clips or contact an admin to increase your quota.",
        )


def _release_upload_slot(user_id: str) -> None:
    """Release a concurrent upload slot."""
    with _upload_lock:
        current = _active_uploads.get(user_id, 0)
        if current > 0:
            _active_uploads[user_id] = current - 1
        else:
            _active_uploads.pop(user_id, None)


def finish_upload(request: Request) -> None:
    """Call after an upload completes to release the slot and invalidate cache.

    Should be called in a finally block in upload endpoints.
    """
    if not AUTH_ENABLED:
        return
    user = get_current_user(request)
    if not user or user.is_admin:
        return
    _release_upload_slot(user.user_id)
    # Invalidate cached disk usage so next check is fresh
    org_store = get_org_store()
    user_orgs = org_store.list_user_orgs(user.user_id)
    if user_orgs:
        invalidate_usage_cache(user_orgs[0].org_id)

"""Per-org file isolation (CRKY-10).

Resolves the clips directory based on the authenticated user's org context.
When auth is disabled, returns the base clips dir (backward compatible).
When auth is enabled, scopes to Projects/{org_id}/ for the user's active org.

Usage in routes:
    from ..org_isolation import resolve_clips_dir
    clips_dir = resolve_clips_dir(request)
    clips = service.scan_clips(clips_dir)
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request

from .auth import AUTH_ENABLED, get_current_user
from .orgs import get_org_store

logger = logging.getLogger(__name__)

# Base clips dir — set at startup from app.py
_base_clips_dir: str = ""


def set_base_clips_dir(path: str) -> None:
    global _base_clips_dir
    _base_clips_dir = path


def get_base_clips_dir() -> str:
    return _base_clips_dir


def resolve_clips_dir(request: Request, org_id: str | None = None) -> str:
    """Resolve the clips directory for the current request.

    When auth is disabled: returns the base clips dir.
    When auth is enabled: returns base/{org_id}/ scoped to the user's org.

    If org_id is provided, uses that org (after validating membership).
    Otherwise, uses the user's first org (personal org fallback).
    """
    if not AUTH_ENABLED or not _base_clips_dir:
        return _base_clips_dir

    user = get_current_user(request)
    if user is None:
        return _base_clips_dir

    store = get_org_store()

    # Accept org_id from: explicit param > X-Org-Id header > default (personal org)
    if not org_id:
        org_id = request.headers.get("X-Org-Id", "").strip() or None

    if org_id:
        # Validate user is a member of the requested org
        if not user.is_admin and not store.is_member(org_id, user.user_id):
            raise HTTPException(status_code=403, detail="Not a member of this org")
    else:
        # Default to first org (personal org)
        user_orgs = store.list_user_orgs(user.user_id)
        if not user_orgs:
            # User has no orgs — create personal org
            personal = store.ensure_personal_org(user.user_id, user.email)
            org_id = personal.org_id
        else:
            # Prefer personal org, fall back to first
            personal = store.get_personal_org(user.user_id)
            org_id = personal.org_id if personal else user_orgs[0].org_id

    org_dir = os.path.join(_base_clips_dir, org_id)
    os.makedirs(org_dir, exist_ok=True)
    return org_dir


def resolve_node_clips_dir(node_org_id: str | None) -> str:
    """Resolve the clips directory for a node based on its org_id.

    When auth is disabled or node has no org: returns the base clips dir.
    When auth is enabled and node has an org: returns base/{org_id}/.
    """
    if not AUTH_ENABLED or not _base_clips_dir or not node_org_id:
        return _base_clips_dir
    org_dir = os.path.join(_base_clips_dir, node_org_id)
    os.makedirs(org_dir, exist_ok=True)
    return org_dir


def validate_clip_access(request: Request, clip_root_path: str) -> bool:
    """Check that a clip's path is within the user's accessible org directories.

    Returns True if access is allowed, raises 403 if not.
    When auth is disabled, always returns True.
    """
    if not AUTH_ENABLED:
        return True

    user = get_current_user(request)
    if user is None:
        return True

    # Platform admins can access everything
    if user.is_admin:
        return True

    store = get_org_store()
    user_orgs = store.list_user_orgs(user.user_id)
    abs_clip = os.path.abspath(clip_root_path)
    abs_base = os.path.abspath(_base_clips_dir)

    for org in user_orgs:
        org_dir = os.path.join(abs_base, org.org_id)
        # Use os.sep suffix to prevent prefix collision (e.g., org_id "abc" matching "abcdef/")
        if abs_clip == org_dir or abs_clip.startswith(org_dir + os.sep):
            return True

    raise HTTPException(status_code=403, detail="You don't have access to this clip's organization")

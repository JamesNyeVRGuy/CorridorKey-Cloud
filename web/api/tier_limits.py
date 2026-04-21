"""Per-tier resource limits (CRKY-114).

Enforces frame count and concurrent job limits based on user tier.
Checked at job submission time before _stamp_job.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from .auth import AUTH_ENABLED, get_current_user
from .deps import get_queue

logger = logging.getLogger(__name__)

# Limits per tier: max_frames per clip, max concurrent jobs (running + queued)
# Base limits per tier. Actual frame limit = base + frames contributed by user's nodes.
# A member who contributes 3000 frames via their node gets 500 + 3000 = 3500 frame limit.
TIER_LIMITS = {
    "pending": {
        "max_frames": 0,
        "max_concurrent": 0,
        "max_resolution": 0,
        "max_fps": 0,
        "max_duration": 0,
    },
    "member": {
        "max_frames": 500,
        "max_concurrent": 3,
        "max_resolution": 1280,
        "max_fps": 60,
        "max_duration": 30,
    },
    "contributor": {
        "max_frames": 2000,
        "max_concurrent": 5,
        "max_resolution": 2160,
        "max_fps": 60,
        "max_duration": 120,
    },
    "org_admin": {
        "max_frames": 5000,
        "max_concurrent": 10,
        "max_resolution": 4096,
        "max_fps": 120,
        "max_duration": 300,
    },
    "platform_admin": {
        "max_frames": 0,
        "max_concurrent": 0,
        "max_resolution": 0,
        "max_fps": 0,
        "max_duration": 0,
    },
}


def _contributed_frames(user_id: str) -> int:
    """Total frames contributed by all nodes owned by this user's orgs."""
    try:
        from .deps import get_node_state
        from .node_reputation import get_all_reputations
        from .orgs import get_org_store

        store = get_org_store()
        user_orgs = {o.org_id for o in store.list_user_orgs(user_id)}
        total = 0
        for rep in get_all_reputations():
            node = get_node_state().get_node(rep.node_id)
            if node and node.org_id in user_orgs:
                total += rep.total_frames
        return total
    except Exception:
        return 0


def check_tier_limits(request: Request, frame_count: int = 0) -> None:
    """Check if the user's tier allows this job submission.

    Frame limit = tier base + frames contributed by the user's nodes.
    Contributing GPU time directly increases your frame allowance.

    Raises HTTP 403 if frame count exceeds limit or too many concurrent jobs.
    No-op when auth is disabled or user is platform_admin.
    """
    if not AUTH_ENABLED:
        return

    user = get_current_user(request)
    if not user:
        return
    if user.is_admin:
        return

    limits = TIER_LIMITS.get(user.tier, TIER_LIMITS["pending"])
    base_max = limits["max_frames"]
    max_concurrent = limits["max_concurrent"]

    # Dynamic frame limit: base + contributed frames
    contributed = _contributed_frames(user.user_id)
    max_frames = base_max + contributed if base_max > 0 else 0

    # Frame count check
    if max_frames > 0 and frame_count > max_frames:
        raise HTTPException(
            status_code=403,
            detail=f"Frame limit: {max_frames} (base {base_max} + {contributed} contributed). "
            f"This clip has {frame_count} frames. Contribute more GPU time to increase your limit.",
        )

    # Concurrent job check
    if max_concurrent > 0:
        queue = get_queue()
        user_running = sum(1 for j in queue.running_jobs if j.submitted_by == user.user_id)
        user_queued = sum(1 for j in queue.queue_snapshot if j.submitted_by == user.user_id)
        total = user_running + user_queued
        if total >= max_concurrent:
            raise HTTPException(
                status_code=429,
                detail=f"You have {total} jobs running/queued (limit: {max_concurrent} for {user.tier} tier). "
                f"Wait for current jobs to finish or contribute a GPU to increase your limit.",
            )


def get_user_concurrent_limit(user_id: str | None = None, request: Request | None = None) -> int:
    """Return the user's tier cap on concurrent jobs, or 0 for unlimited.

    Used by the auto-sharder to avoid creating more shards than the user can
    actually submit — otherwise shards past the tier limit get rejected at
    submit time and the user sees a silently-truncated result (CRKY-191).

    Looks up the user from `request` if provided, otherwise resolves from
    `user_id` via the user store. Returns 0 (meaning "no cap") when auth is
    disabled, the user is a platform admin, or the lookup fails — callers
    should treat 0 as "do not apply a tier-based cap".
    """
    if not AUTH_ENABLED:
        return 0
    user = None
    if request is not None:
        user = get_current_user(request)
    elif user_id:
        try:
            from .users import get_user_store

            u = get_user_store().get_user(user_id)
            if u:
                # UserContext-like shim: tier_limits only needs .tier and .is_admin
                class _U:
                    pass

                user = _U()
                user.tier = u.tier
                user.is_admin = u.tier == "platform_admin"
        except Exception:
            return 0
    if user is None or getattr(user, "is_admin", False):
        return 0
    limits = TIER_LIMITS.get(user.tier, TIER_LIMITS["pending"])
    return int(limits.get("max_concurrent", 0))


def check_video_limits(request: Request, video_info: dict) -> None:
    """Validate video resolution, framerate, and duration against tier limits.

    Called after upload, before frame extraction. Raises HTTP 413 on violation.
    video_info should contain: width, height, fps, duration (from probe_video).
    """
    if not AUTH_ENABLED:
        return

    user = get_current_user(request)
    if not user:
        return

    tier = user.tier
    limits = TIER_LIMITS.get(tier, TIER_LIMITS.get("member", {}))

    # Resolution check (max of width/height)
    max_res = limits.get("max_resolution", 0)
    if max_res > 0:
        width = video_info.get("width", 0)
        height = video_info.get("height", 0)
        actual = max(width, height)
        if actual > max_res:
            raise HTTPException(
                status_code=413,
                detail=f"Video resolution {width}x{height} exceeds your {tier} tier limit of {max_res}p. "
                f"Please downscale your footage before uploading.",
            )

    # Framerate check
    max_fps = limits.get("max_fps", 0)
    if max_fps > 0:
        fps = video_info.get("fps", 0)
        if fps > max_fps:
            raise HTTPException(
                status_code=413,
                detail=f"Video framerate {fps:.1f}fps exceeds your {tier} tier limit of {max_fps}fps. "
                f"Please re-encode at a lower framerate.",
            )

    # Duration check
    max_duration = limits.get("max_duration", 0)
    if max_duration > 0:
        duration = video_info.get("duration", 0)
        if duration > max_duration:
            raise HTTPException(
                status_code=413,
                detail=f"Video duration {duration:.0f}s exceeds your {tier} tier limit of {max_duration}s. "
                f"Please trim your footage before uploading.",
            )

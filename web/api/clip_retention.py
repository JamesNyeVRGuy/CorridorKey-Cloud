"""Automatic clip cleanup based on tier-based retention policy (CRKY-115).

Runs as a background daemon thread. Scans org directories periodically,
deletes expired clips based on the org owner's tier. Uses a distributed
Redis lock for multi-instance safety.

Configurable via admin API:
    GET /api/admin/retention   — read current policy
    PUT /api/admin/retention   — update policy

Delete modes:
    outputs_only — remove Output/ (FG, Matte, Processed, Comp). Keep Source/, Frames/.
    full         — remove entire clip + empty project folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from backend.project import read_project_json

logger = logging.getLogger(__name__)

_LOCK_KEY = "ck:lock:cleanup"
_SETTINGS_KEY = "clip_retention_policy"

_DEFAULT_RETENTION = {
    "pending": 7,
    "member": 30,
    "contributor": 90,
    "org_admin": 90,
    "platform_admin": -1,
}


@dataclass
class RetentionPolicy:
    enabled: bool = True
    retention_days: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_RETENTION))
    delete_mode: str = "outputs_only"  # "outputs_only" or "full"
    check_interval: int = 3600  # seconds

    def days_for_tier(self, tier: str) -> int:
        return self.retention_days.get(tier, self.retention_days.get("member", 30))


def get_retention_policy() -> RetentionPolicy:
    """Load the retention policy from settings, or return defaults."""
    from .database import get_storage

    raw = get_storage().get_setting(_SETTINGS_KEY)
    if raw and isinstance(raw, dict):
        return RetentionPolicy(
            enabled=raw.get("enabled", True),
            retention_days=raw.get("retention_days", dict(_DEFAULT_RETENTION)),
            delete_mode=raw.get("delete_mode", "outputs_only"),
            check_interval=raw.get("check_interval", 3600),
        )
    return RetentionPolicy()


def set_retention_policy(policy: RetentionPolicy) -> None:
    """Save the retention policy to settings."""
    from .database import get_storage

    get_storage().set_setting(_SETTINGS_KEY, asdict(policy))


def _get_clip_age_days(clip_root: str, project_dir: str | None) -> float:
    """Get clip age in days. Uses project.json 'created' field, falls back to dir mtime."""
    if project_dir:
        data = read_project_json(project_dir)
        if data and data.get("created"):
            try:
                created = datetime.fromisoformat(data["created"])
                return (datetime.now() - created).total_seconds() / 86400
            except (ValueError, TypeError):
                pass
    # Fallback to filesystem modification time
    try:
        mtime = os.stat(clip_root).st_mtime
        return (time.time() - mtime) / 86400
    except OSError:
        return 0


def _get_org_tier(org_id: str) -> str:
    """Get the retention tier for an org (based on owner's tier)."""
    try:
        from .orgs import get_org_store
        from .users import get_user_store

        org = get_org_store().get_org(org_id)
        if org is None:
            return "member"
        user = get_user_store().get_user(org.owner_id)
        return user.tier if user else "member"
    except Exception:
        return "member"


def _find_project_dir(clip_root: str) -> str | None:
    """Find the project directory for a clip (parent of clips/ dir)."""
    parent = os.path.dirname(clip_root)
    if os.path.basename(parent) == "clips":
        return os.path.dirname(parent)
    return None


def _delete_clip_outputs(clip_root: str) -> int:
    """Delete only the Output/ directory. Returns bytes freed."""
    output_dir = os.path.join(clip_root, "Output")
    if not os.path.isdir(output_dir):
        return 0
    freed = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(output_dir) for f in fns)
    shutil.rmtree(output_dir)
    return freed


def _delete_clip_full(clip_root: str) -> int:
    """Delete entire clip directory. Cleans up empty project. Returns bytes freed."""
    freed = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(clip_root) for f in fns)
    shutil.rmtree(clip_root)

    # Clean up empty project folder
    parent = os.path.dirname(clip_root)
    if os.path.basename(parent) == "clips" and os.path.isdir(parent):
        remaining = [d for d in os.listdir(parent) if not d.startswith(".")]
        if not remaining:
            project_dir = os.path.dirname(parent)
            shutil.rmtree(project_dir)
            logger.info(f"Removed empty project: {project_dir}")

    return freed


def _sweep_cas_dir(cas_dir: str, org_id: str) -> tuple[int, int]:
    """Sweep a per-org CAS directory for orphaned entries.

    Uses a tombstone-based TTL: first sweep marks orphans (nlink==1) with a
    ``.{name}.orphan`` file; subsequent sweeps reap entries whose tombstone
    is older than 1 hour.

    Note: reap latency is at least ~2 sweep cycles one to create the
    tombstone, one to check it after TTL.  With the default
    ``check_interval=3600`` and a 1-hour TTL, expect ~2 hours minimum
    between "last hardlink removed" and "bytes reclaimed"

    Returns:
        (bytes_freed, files_removed)
    """
    freed_total = 0
    removed = 0

    for fname in os.listdir(cas_dir):
        if fname.startswith(".") or fname.endswith(".probe"):
            continue
        path = os.path.join(cas_dir, fname)
        tombstone = os.path.join(cas_dir, f".{fname}.orphan")
        try:
            st = os.stat(path)
            if st.st_nlink == 1:
                if not os.path.exists(tombstone):
                    with open(tombstone, "w") as f:
                        f.write("")
                    continue

                tomb_mtime = os.stat(tombstone).st_mtime
                if time.time() - tomb_mtime > 3600:
                    # Atomic check-and-remove: hardlink to a probe path,
                    # then re-check nlink. If a concurrent upload linked
                    # onto the CAS entry between our stat and now, nlink
                    # will be >2 and we abort instead of deleting live data.
                    probe = path + f".{uuid.uuid4().hex}.probe"
                    try:
                        os.link(path, probe)
                    except OSError:
                        continue
                    try:
                        real_nlink = os.stat(probe).st_nlink
                        if real_nlink == 2:
                            freed = os.stat(probe).st_size
                            os.remove(path)
                            os.remove(tombstone)
                            freed_total += freed
                            removed += 1
                            logger.info(f"Cleaned up CAS file: {fname} in org {org_id} ({freed} bytes)")
                        else:
                            logger.debug(f"CAS file {fname} re-linked (nlink={real_nlink}), skipping reap")
                            if os.path.exists(tombstone):
                                os.remove(tombstone)
                    finally:
                        try:
                            os.remove(probe)
                        except OSError:
                            pass
            else:
                if os.path.exists(tombstone):
                    os.remove(tombstone)
        except FileNotFoundError:
            if os.path.exists(tombstone):
                try:
                    os.remove(tombstone)
                except OSError:
                    pass
        except Exception:
            logger.warning(f"Failed to stat/remove CAS file {path}", exc_info=True)

    # Clean up orphaned tombstones whose CAS files no longer exist
    for fname in os.listdir(cas_dir):
        if fname.startswith(".") and fname.endswith(".orphan"):
            cas_name = fname[1 : -len(".orphan")]
            if not os.path.exists(os.path.join(cas_dir, cas_name)):
                try:
                    os.remove(os.path.join(cas_dir, fname))
                except OSError:
                    pass

    return freed_total, removed


def cleanup_once(base_clips_dir: str) -> dict[str, list[str]]:
    """Scan all orgs and delete expired clips. Returns {org_id: [deleted_clip_names]}."""
    from backend.clip_state import scan_clips_dir

    from .storage_quota import invalidate_usage_cache
    from .ws import manager

    policy = get_retention_policy()
    if not policy.enabled:
        return {}

    if not os.path.isdir(base_clips_dir):
        return {}

    result: dict[str, list[str]] = {}
    total_freed = 0
    cas_removed = 0

    for org_id in os.listdir(base_clips_dir):
        org_dir = os.path.join(base_clips_dir, org_id)
        if not os.path.isdir(org_dir) or org_id.startswith("."):
            continue

        cas_dir = os.path.join(org_dir, ".cas")
        if os.path.isdir(cas_dir):
            freed, removed = _sweep_cas_dir(cas_dir, org_id)
            total_freed += freed
            cas_removed += removed

        tier = _get_org_tier(org_id)
        max_days = policy.days_for_tier(tier)
        if max_days < 0:
            continue  # unlimited retention

        try:
            clips = scan_clips_dir(org_dir, allow_standalone_videos=False)
        except Exception:
            logger.warning(f"Failed to scan org {org_id} for cleanup", exc_info=True)
            continue

        deleted_names: list[str] = []
        for clip in clips:
            project_dir = _find_project_dir(clip.root_path)
            age_days = _get_clip_age_days(clip.root_path, project_dir)

            if age_days <= max_days:
                continue

            try:
                if policy.delete_mode == "full":
                    freed = _delete_clip_full(clip.root_path)
                else:
                    freed = _delete_clip_outputs(clip.root_path)

                if freed > 0:
                    total_freed += freed
                    deleted_names.append(clip.name)
                    logger.info(
                        f"Cleanup: deleted {'clip' if policy.delete_mode == 'full' else 'outputs'} "
                        f"'{clip.name}' in org {org_id} (age={age_days:.0f}d, freed={freed / 1e6:.1f}MB)"
                    )
                    manager.send_clip_deleted(clip.name, org_id=org_id)
            except Exception:
                logger.warning(f"Failed to delete clip '{clip.name}' in org {org_id}", exc_info=True)

        if deleted_names:
            result[org_id] = deleted_names
            invalidate_usage_cache(org_id)

    if total_freed > 0:
        clip_count = sum(len(v) for v in result.values())
        parts = []
        if clip_count:
            parts.append(f"{clip_count} clips")
        if cas_removed:
            parts.append(f"{cas_removed} CAS files")
        summary = ", ".join(parts) if parts else "orphaned data"
        logger.info(f"Cleanup cycle complete: freed {total_freed / 1e9:.2f}GB across {summary}")

    return result


def _acquire_lock(ttl_ms: int = 55000) -> str | None:
    """Acquire distributed cleanup lock. Returns token or None."""
    from .redis_client import get_redis, is_redis_configured

    if not is_redis_configured():
        return "local"
    r = get_redis()
    if r is None:
        return "local"
    token = uuid.uuid4().hex
    if r.set(_LOCK_KEY, token, nx=True, px=ttl_ms):
        return token
    return None


def _release_lock(token: str) -> None:
    """Release the distributed cleanup lock."""
    from .redis_client import get_redis, is_redis_configured

    if not is_redis_configured() or token == "local":
        return
    r = get_redis()
    if r is None:
        return
    # Safe release: only delete if we still own it
    lua = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
    try:
        r.eval(lua, 1, _LOCK_KEY, token)
    except Exception:
        logger.debug("Failed to release cleanup lock", exc_info=True)


def cleanup_loop(base_clips_dir: str, stop_event: threading.Event) -> None:
    """Background thread: run cleanup on interval."""
    logger.info("Clip cleanup daemon started")
    while not stop_event.is_set():
        policy = get_retention_policy()
        stop_event.wait(policy.check_interval)
        if stop_event.is_set():
            break

        token = _acquire_lock()
        if token is None:
            logger.debug("Cleanup lock held by another instance, skipping")
            continue
        try:
            cleanup_once(base_clips_dir)
        except Exception:
            logger.exception("Clip cleanup error")
        finally:
            _release_lock(token)


def start_cleanup(base_clips_dir: str, stop_event: threading.Event) -> threading.Thread:
    """Start the cleanup daemon thread."""
    thread = threading.Thread(
        target=cleanup_loop,
        args=(base_clips_dir, stop_event),
        daemon=True,
        name="clip-cleanup",
    )
    thread.start()
    return thread

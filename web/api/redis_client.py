"""Redis connection management for multi-instance state (CRKY-105).

Provides a singleton redis.Redis client selected by CK_REDIS_URL.
Thread-safe via redis-py's internal connection pooling.

When CK_REDIS_URL is not set, get_redis() returns None and
is_redis_configured() returns False — the server falls back
to in-memory state (single-instance mode).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("CK_REDIS_URL", "").strip()
_client = None  # type: ignore[assignment]  # lazy redis.Redis


def is_redis_configured() -> bool:
    """Check if CK_REDIS_URL is set."""
    return bool(_REDIS_URL)


def get_redis():
    """Get the shared Redis client. Returns None if CK_REDIS_URL is not set.

    Raises redis.ConnectionError on first call if Redis is unreachable.
    This is intentional — a misconfigured CK_REDIS_URL should crash at
    startup, not silently fall back to in-memory (split-brain risk).
    """
    global _client
    if not _REDIS_URL:
        return None
    if _client is None:
        import redis

        _client = redis.Redis.from_url(
            _REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        _client.ping()
        # Log host only, never credentials
        safe_url = _REDIS_URL.split("@")[-1] if "@" in _REDIS_URL else _REDIS_URL
        logger.info(f"Redis connected: {safe_url}")
    return _client


# ---------------------------------------------------------------------------
# Lua script registry (cached SHAs)
# ---------------------------------------------------------------------------
_script_shas: dict[str, str] = {}


def load_script(name: str, lua_source: str) -> str:
    """Register a Lua script on the Redis server, return its SHA.

    Cached — subsequent calls for the same name return the stored SHA.
    """
    if name not in _script_shas:
        client = get_redis()
        _script_shas[name] = client.script_load(lua_source)
    return _script_shas[name]


def run_script(name: str, lua_source: str, keys: list[str], args: list[str]):
    """Load (if needed) and execute a Lua script. Returns the script result."""
    sha = load_script(name, lua_source)
    client = get_redis()
    try:
        return client.evalsha(sha, len(keys), *keys, *args)
    except Exception:
        # Script may have been flushed — reload and retry once
        _script_shas.pop(name, None)
        sha = load_script(name, lua_source)
        return client.evalsha(sha, len(keys), *keys, *args)

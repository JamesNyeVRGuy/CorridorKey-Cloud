"""Node agent configuration — loaded from env vars or .env file."""

from __future__ import annotations

import os
import platform
import uuid

# Try loading .env from the working directory
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


MAIN_URL: str = _get("CK_MAIN_URL", "http://localhost:3000")
NODE_NAME: str = _get("CK_NODE_NAME", platform.node() or "unnamed-node")
NODE_ID: str = _get("CK_NODE_ID", uuid.uuid4().hex[:12])
NODE_GPUS: str = _get("CK_NODE_GPUS", "auto")  # "auto" | "0" | "0,1"
SHARED_STORAGE: str = _get("CK_SHARED_STORAGE", "")  # empty = HTTP transfer
POLL_INTERVAL: float = float(_get("CK_POLL_INTERVAL", "2"))
HEARTBEAT_INTERVAL: float = float(_get("CK_HEARTBEAT_INTERVAL", "10"))
AUTH_TOKEN: str = _get("CK_AUTH_TOKEN", "")  # shared secret for node auth
# Comma-separated job types this node accepts. Empty = all.
# Valid types: inference, gvm_alpha, videomama_alpha, video_extract, video_stitch
ACCEPTED_TYPES: str = _get("CK_NODE_ACCEPTED_TYPES", "")

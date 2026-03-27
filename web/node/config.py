"""Node agent configuration — loaded from env vars or .env file."""

from __future__ import annotations

import hashlib
import os
import platform
import sys

# Load config from .env files. Checked in order (first found wins per var):
# 1. .env in the working directory (Docker / dev)
# 2. node.env next to the executable (standalone binary)
# 3. ~/.corridorkey/node.env (user config)
try:
    from dotenv import load_dotenv

    load_dotenv()  # .env in cwd

    # Standalone binary: check next to executable
    _exe_dir = os.path.dirname(os.path.abspath(getattr(sys, "executable", __file__)))
    _exe_env = os.path.join(_exe_dir, "node.env")
    if os.path.isfile(_exe_env):
        load_dotenv(_exe_env, override=False)

    # User home config
    _home_env = os.path.join(os.path.expanduser("~"), ".corridorkey", "node.env")
    if os.path.isfile(_home_env):
        load_dotenv(_home_env, override=False)
except ImportError:
    pass


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


MAIN_URL: str = _get("CK_MAIN_URL", "http://localhost:3000")
NODE_NAME: str = _get("CK_NODE_NAME", platform.node() or "unnamed-node")
# Stable ID derived from node name — same name always gets the same ID
NODE_ID: str = _get("CK_NODE_ID", hashlib.sha256(NODE_NAME.encode()).hexdigest()[:12])
NODE_GPUS: str = _get("CK_NODE_GPUS", "auto")  # "auto" | "0" | "0,1"
SHARED_STORAGE: str = _get("CK_SHARED_STORAGE", "")  # empty = HTTP transfer
POLL_INTERVAL: float = float(_get("CK_POLL_INTERVAL", "2"))
HEARTBEAT_INTERVAL: float = float(_get("CK_HEARTBEAT_INTERVAL", "10"))
AUTH_TOKEN: str = _get("CK_AUTH_TOKEN", "")  # shared secret for node auth
# Comma-separated job types this node accepts. Empty = all.
# Valid types: inference, gvm_alpha, videomama_alpha, video_extract, video_stitch
ACCEPTED_TYPES: str = _get("CK_NODE_ACCEPTED_TYPES", "")
# Pre-load model into VRAM on startup (avoids cold-start delay on first job)
PREWARM: bool = _get("CK_NODE_PREWARM", "true").lower() in ("true", "1", "yes")
# Hardened mode (CRKY-43): all frame I/O through tmpfs, extra security
HARDENED: bool = _get("CK_NODE_HARDENED", "false").lower() in ("true", "1", "yes")
# Temp directory for frame processing. In hardened mode, this should be a tmpfs mount.
TEMP_DIR: str = _get("CK_TEMP_DIR", "")

"""Entry point for the CorridorKey node agent.

Usage:
    CK_MAIN_URL=http://192.168.1.100:3000 python -m web.node
    CK_MAIN_URL=http://192.168.1.100:3000 uv run python -m web.node
"""

from __future__ import annotations

import logging
import signal
import sys

from . import config
from .agent import NodeAgent
from .log_buffer import install as install_log_buffer


def _security_checks() -> None:
    """Validate the runtime environment before starting."""
    import os

    log = logging.getLogger(__name__)

    # Refuse to run as root (uid 0) unless explicitly overridden
    if hasattr(os, "getuid") and os.getuid() == 0 and not os.environ.get("CK_ALLOW_ROOT", "").strip():
        log.error(
            "Node agent is running as root (uid 0). This is a security risk. "
            "Run as a non-root user, or set CK_ALLOW_ROOT=true to override."
        )
        sys.exit(1)

    # Warn if temp directory is writable to others
    temp_dir = os.environ.get("CK_TEMP_DIR", "/tmp/ck-work").strip()
    if os.path.isdir(temp_dir):
        import stat

        mode = os.stat(temp_dir).st_mode
        if mode & stat.S_IWOTH:
            log.warning(f"Temp directory {temp_dir} is world-writable — consider restricting permissions")

    # Warn about insecure HTTP (non-TLS) connections to the main server
    main_url = config.MAIN_URL
    if main_url.startswith("http://") and "localhost" not in main_url and "127.0.0.1" not in main_url:
        log.warning(
            f"Connecting to {main_url} over plain HTTP. Auth tokens are sent in cleartext. Use HTTPS in production."
        )


def main() -> None:
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    install_log_buffer()

    _security_checks()

    if config.MAIN_URL == "http://localhost:3000":
        logging.getLogger(__name__).warning(
            "CK_MAIN_URL not set — defaulting to http://localhost:3000. Set CK_MAIN_URL to the main machine's address."
        )

    # Start tray icon if not explicitly disabled (e.g., Docker headless)
    tray = None
    if os.environ.get("CK_NO_TRAY", "").strip().lower() not in ("true", "1"):
        try:
            from .tray import TrayApp

            tray = TrayApp()
            tray.start()
        except Exception:
            logging.getLogger(__name__).debug("Tray icon unavailable", exc_info=True)

    agent = NodeAgent(tray=tray)

    def shutdown(signum, frame):
        if tray:
            tray.stop()
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    agent.run()


if __name__ == "__main__":
    # Required for PyInstaller on Windows — must be first call.
    # Prevents infinite subprocess spawn loop in frozen executables.
    import multiprocessing

    multiprocessing.freeze_support()
    main()

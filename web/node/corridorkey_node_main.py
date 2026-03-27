"""PyInstaller entry point for the CorridorKey node agent.

Uses absolute imports to avoid relative import failures in frozen builds.
"""

import logging
import multiprocessing
import os
import signal
import sys

# Force PyInstaller to bundle these — they're loaded dynamically by httpx/anyio
# and PyInstaller's static analysis doesn't find them. These MUST be at module
# level (not inside if __name__ == "__main__") or PyInstaller won't see them.
import anyio  # noqa: F401
import certifi  # noqa: F401
import h11  # noqa: F401
import httpcore  # noqa: F401
import httpx  # noqa: F401
import sniffio  # noqa: F401

if __name__ == "__main__":
    multiprocessing.freeze_support()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # First-run config dialog (before anything else)
    from web.node.first_run import needs_setup, run_setup_dialog

    if needs_setup():
        if not run_setup_dialog():
            logging.getLogger(__name__).warning(
                "CK_MAIN_URL not set — defaulting to http://localhost:3000. "
                "Set CK_MAIN_URL to the main machine's address."
            )

    # GPU addon: detect GPU and install CUDA/ROCm torch before anything imports torch
    from web.node.gpu_addon import ensure_gpu_addon

    gpu_vendor = ensure_gpu_addon()

    from web.node import config
    from web.node.agent import NodeAgent
    from web.node.log_buffer import install as install_log_buffer

    install_log_buffer()

    if config.MAIN_URL == "http://localhost:3000":
        logging.getLogger(__name__).warning(
            "CK_MAIN_URL not set — defaulting to http://localhost:3000. Set CK_MAIN_URL to the main machine's address."
        )

    # Start tray icon if not explicitly disabled
    tray = None
    if os.environ.get("CK_NO_TRAY", "").strip().lower() not in ("true", "1"):
        try:
            from web.node.tray import TrayApp

            tray = TrayApp()
            tray.start()
            if gpu_vendor:
                tray.set_status("connecting")
            else:
                tray._notify("No GPU detected — running in CPU mode (slow)")
        except Exception:
            logging.getLogger(__name__).debug("Tray icon unavailable", exc_info=True)

    # Start auto-updater (only active in frozen builds)
    updater = None
    try:
        from web.node.updater import UpdateChecker

        updater = UpdateChecker(tray=tray)
        updater.start()
    except Exception:
        logging.getLogger(__name__).debug("Auto-updater unavailable", exc_info=True)

    agent = NodeAgent(tray=tray)

    def shutdown(signum, frame):
        if updater:
            updater.stop()
        if tray:
            tray.stop()
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    agent.run()

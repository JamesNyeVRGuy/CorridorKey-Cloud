"""PyInstaller entry point for the CorridorKey node agent.

Uses absolute imports to avoid relative import failures in frozen builds.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

    # Absolute imports — PyInstaller can't resolve relative imports in __main__
    import logging
    import os
    import signal
    import sys

    # Force PyInstaller to bundle these (not detected via lazy/dynamic imports)
    import httpx  # noqa: F401
    import certifi  # noqa: F401
    import h11  # noqa: F401
    import anyio  # noqa: F401
    import sniffio  # noqa: F401
    import httpcore  # noqa: F401

    from web.node import config
    from web.node.agent import NodeAgent
    from web.node.log_buffer import install as install_log_buffer

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
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

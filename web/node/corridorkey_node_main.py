"""PyInstaller entry point for the CorridorKey node agent.

Uses absolute imports to avoid relative import failures in frozen builds.
"""

import logging
import multiprocessing
import os
import signal
import sys
import threading

# Force PyInstaller to bundle these — they're loaded dynamically and
# PyInstaller's static analysis doesn't find them. MUST be at module level.
import anyio  # noqa: F401 — httpx → anyio
import certifi  # noqa: F401 — httpx SSL certs
import dotenv  # noqa: F401 — config.py loads .env files
import h11  # noqa: F401 — httpx HTTP/1.1
import httpcore  # noqa: F401 — httpx transport
import httpx  # noqa: F401 — node agent HTTP client
import sniffio  # noqa: F401 — anyio backend detection

try:
    import desktop_notifier  # noqa: F401 — tray notifications (optional)
    import pystray  # noqa: F401 — system tray icon (optional)
except ImportError:
    pass  # optional — tray degrades gracefully

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

    # Create settings window (headless mode skips this)
    gui = None
    headless = os.environ.get("CK_NO_TRAY", "").strip().lower() in ("true", "1")
    if not headless:
        try:
            from web.node.gui import NodeSettingsWindow

            gui = NodeSettingsWindow(tray=tray)
            gui.create()
        except Exception:
            logging.getLogger(__name__).debug("Settings window unavailable", exc_info=True)

    # Wire tray ↔ GUI
    if tray and gui:
        tray._on_settings_window = gui.toggle
        tray.gui = gui
        gui.tray = tray

    agent = NodeAgent(tray=tray)

    # Connect GUI to agent log output
    if gui:
        _orig_handler = logging.getLogger().handlers[0] if logging.getLogger().handlers else None

        class _GUILogHandler(logging.Handler):
            def emit(self, record):
                try:
                    gui.append_log(self.format(record))
                except Exception:
                    pass

        gui_handler = _GUILogHandler()
        gui_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(gui_handler)

    def shutdown(signum, frame):
        if updater:
            updater.stop()
        if tray:
            tray.stop()
        if gui:
            gui.destroy()
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    # Run agent in background thread, GUI mainloop on main thread
    if gui:
        agent_thread = threading.Thread(target=agent.run, daemon=True, name="agent")
        agent_thread.start()
        gui.run()  # blocks until window is destroyed
    else:
        agent.run()  # headless — agent runs on main thread

"""System tray UI for the CorridorKey node agent.

Runs pystray in a daemon thread while the node agent runs on the main thread.
Provides status indicator, credits display, pause/resume, and quit controls.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Any

logger = logging.getLogger(__name__)

# Optional dependencies — tray degrades gracefully if not installed
try:
    import pystray
    from PIL import Image, ImageDraw

    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    from desktop_notifier import DesktopNotifier

    HAS_NOTIFIER = True
except ImportError:
    HAS_NOTIFIER = False


# Status colors (RGBA)
_COLORS = {
    "idle": (136, 136, 136, 255),
    "working": (0, 255, 136, 255),
    "paused": (255, 170, 0, 255),
    "error": (255, 68, 68, 255),
    "connecting": (100, 100, 200, 255),
}


def _create_icon(status: str = "idle") -> Image.Image:
    """Generate a 64x64 tray icon with status indicator dot."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark rounded base
    draw.rounded_rectangle([4, 4, 60, 60], radius=8, fill=(26, 26, 46, 255))
    # "CK" text (simplified — just two lines forming a K shape)
    draw.text((14, 16), "CK", fill=(255, 242, 3, 255))
    # Status dot
    color = _COLORS.get(status, _COLORS["idle"])
    draw.ellipse([44, 44, 60, 60], fill=color)
    return img


class TrayApp:
    """System tray interface for the CorridorKey node agent."""

    def __init__(self) -> None:
        self._status = "connecting"
        self._credits = 0.0
        self._gpu_name = ""
        self._gpu_vram_free = 0.0
        self._current_job = ""
        self._progress_current = 0
        self._progress_total = 0
        self._paused = False
        self._server_url = ""
        self._icon: Any = None
        self._notifier: DesktopNotifier | None = None
        self._lock = threading.Lock()

        if HAS_NOTIFIER:
            self._notifier = DesktopNotifier(app_name="CorridorKey Node")

    # -- Public API (called from agent thread) --

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
        self._update()

    def set_progress(self, job_id: str, current: int, total: int) -> None:
        with self._lock:
            self._current_job = job_id
            self._progress_current = current
            self._progress_total = total
            if current > 0:
                self._status = "working"
        self._update()

    def set_credits(self, credits: float) -> None:
        with self._lock:
            self._credits = credits
        self._update()

    def set_gpu_info(self, name: str, vram_free_gb: float) -> None:
        with self._lock:
            self._gpu_name = name
            self._gpu_vram_free = vram_free_gb

    def set_server_url(self, url: str) -> None:
        with self._lock:
            self._server_url = url

    def job_completed(self, job_id: str, credits_earned: float) -> None:
        with self._lock:
            self._current_job = ""
            self._progress_current = 0
            self._progress_total = 0
            self._status = "idle"
            self._credits += credits_earned
        self._update()
        self._notify(f"Job completed! +{credits_earned:.1f} credits earned.")

    def job_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            self._current_job = ""
            self._status = "error"
        self._update()
        self._notify(f"Job failed: {error}")

    @property
    def paused(self) -> bool:
        return self._paused

    # -- Menu builders --

    def _status_text(self, _: Any = None) -> str:
        with self._lock:
            if self._progress_total > 0:
                pct = int(self._progress_current / self._progress_total * 100)
                return f"Processing {self._progress_current}/{self._progress_total} ({pct}%)"
            return f"Status: {self._status.capitalize()}"

    def _credits_text(self, _: Any = None) -> str:
        with self._lock:
            return f"Credits: {self._credits:.1f}"

    def _gpu_text(self, _: Any = None) -> str:
        with self._lock:
            if self._gpu_name:
                return f"GPU: {self._gpu_name} — {self._gpu_vram_free:.1f}GB free"
            return "GPU: detecting..."

    def _on_pause(self, icon: Any, item: Any) -> None:
        self._paused = not self._paused
        self._update()

    def _on_dashboard(self, icon: Any, item: Any) -> None:
        with self._lock:
            url = self._server_url
        if url:
            webbrowser.open(url)

    def _on_quit(self, icon: Any, item: Any) -> None:
        if self._icon:
            self._icon.stop()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem(self._credits_text, None, enabled=False),
            pystray.MenuItem(self._gpu_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Pause" if not self._paused else "Resume",
                self._on_pause,
                checked=lambda item: self._paused,
            ),
            pystray.MenuItem("Open Dashboard", self._on_dashboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    # -- Internal --

    def _update(self) -> None:
        """Refresh icon and menu from current state."""
        if self._icon is None:
            return
        with self._lock:
            status = self._status
        try:
            self._icon.icon = _create_icon(status)
            self._icon.title = f"CorridorKey Node — {self._status_text()}"
            self._icon.update_menu()
        except Exception:
            pass  # icon not ready yet

    def _notify(self, message: str) -> None:
        """Send a desktop notification."""
        if self._notifier is None:
            return
        try:
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._notifier.send(title="CorridorKey Node", message=message))
            loop.close()
        except Exception:
            logger.debug("Notification failed", exc_info=True)

    # -- Lifecycle --

    def start(self) -> None:
        """Start the tray icon in a daemon thread. Non-blocking."""
        if not HAS_TRAY:
            logger.info("pystray not installed — running without tray icon")
            return

        self._icon = pystray.Icon(
            name="corridorkey-node",
            icon=_create_icon(self._status),
            title="CorridorKey Node — Starting...",
            menu=self._build_menu(),
        )

        thread = threading.Thread(target=self._icon.run, daemon=True)
        thread.start()
        logger.info("Tray icon started")

    def stop(self) -> None:
        """Stop the tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

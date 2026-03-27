"""Settings window for the CorridorKey node agent.

A tkinter GUI that shows node status, credits, GPU info, editable config,
and a live log viewer. Opens from the tray icon or on launch. Minimizing
hides to the system tray.

Dark theme matching the CorridorKey brand (warm black + yellow accent).
"""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from tkinter import scrolledtext

logger = logging.getLogger(__name__)

# Brand colors
_BG = "#111118"
_BG_FIELD = "#1a1a2e"
_BG_CARD = "#16161f"
_BORDER = "#2a2a3a"
_ACCENT = "#FFF203"
_TEXT = "#e0e0e0"
_TEXT_DIM = "#888899"
_GREEN = "#00ff88"
_RED = "#ff4444"
_YELLOW = "#ffaa00"

# Config file path
_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".corridorkey")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "node.env")


class NodeSettingsWindow:
    """Main settings window for the node agent."""

    def __init__(self, tray=None, agent=None):
        self.tray = tray
        self.agent = agent
        self.root: tk.Tk | None = None
        self._log_lines: list[str] = []
        self._lock = threading.Lock()
        self._visible = False

        # State mirrors
        self._status = "Starting..."
        self._credits = 0.0
        self._gpu_name = ""
        self._gpu_vram = ""
        self._job_text = ""
        self._node_name = ""
        self._server_url = ""

    def create(self) -> None:
        """Create the window (must be called from the main thread or a dedicated thread)."""
        self.root = tk.Tk()
        self.root.title("CorridorKey Node")
        self.root.geometry("520x640")
        self.root.configure(bg=_BG)
        self.root.resizable(False, False)

        # Minimize to tray instead of taskbar
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        # Try to set the window/taskbar icon
        try:
            import sys

            from PIL import Image, ImageTk

            icon_paths = [
                os.path.join(os.path.dirname(__file__), "icon.png"),
            ]
            if getattr(sys, "frozen", False):
                icon_paths.insert(0, os.path.join(os.path.dirname(sys.executable), "icon.png"))
                icon_paths.insert(0, os.path.join(sys._MEIPASS, "web", "node", "icon.png"))
            for p in icon_paths:
                if os.path.isfile(p):
                    pil_img = Image.open(p).convert("RGBA")
                    # Multiple sizes for taskbar (large) and title bar (small)
                    self._icon_imgs = [
                        ImageTk.PhotoImage(pil_img.resize((s, s), Image.LANCZOS)) for s in (256, 64, 48, 32, 16)
                    ]
                    self.root.iconphoto(True, *self._icon_imgs)
                    break
            # On Windows, also try .ico for proper taskbar grouping
            if sys.platform == "win32":
                ico_paths = [p.replace(".png", ".ico") for p in icon_paths]
                for p in ico_paths:
                    if os.path.isfile(p):
                        self.root.iconbitmap(p)
                        break
        except Exception:
            pass

        self._build_ui()
        self._visible = True

    def _build_ui(self) -> None:
        root = self.root

        # Header
        header = tk.Frame(root, bg=_BG, pady=12)
        header.pack(fill="x", padx=16)

        tk.Label(
            header,
            text="CorridorKey Node",
            font=("Segoe UI", 16, "bold"),
            bg=_BG,
            fg=_ACCENT,
        ).pack(side="left")

        self._status_label = tk.Label(
            header,
            text="Starting...",
            font=("Consolas", 10),
            bg=_BG,
            fg=_TEXT_DIM,
        )
        self._status_label.pack(side="right")

        # Separator
        tk.Frame(root, bg=_BORDER, height=1).pack(fill="x", padx=16)

        # Stats row
        stats = tk.Frame(root, bg=_BG, pady=10)
        stats.pack(fill="x", padx=16)

        self._credits_label = self._stat_card(stats, "Credits", "0.0")
        self._gpu_label = self._stat_card(stats, "GPU", "detecting...")
        self._vram_label = self._stat_card(stats, "VRAM", "—")
        self._job_label = self._stat_card(stats, "Job", "idle")

        # Separator
        tk.Frame(root, bg=_BORDER, height=1).pack(fill="x", padx=16, pady=(4, 0))

        # Config section
        config_frame = tk.Frame(root, bg=_BG, pady=8)
        config_frame.pack(fill="x", padx=16)

        tk.Label(
            config_frame,
            text="CONFIGURATION",
            font=("Consolas", 9),
            bg=_BG,
            fg=_TEXT_DIM,
            anchor="w",
        ).pack(fill="x")

        self._url_var = tk.StringVar(value=os.environ.get("CK_MAIN_URL", "https://corridorkey.cloud"))
        self._token_var = tk.StringVar(value=os.environ.get("CK_AUTH_TOKEN", ""))
        self._name_var = tk.StringVar(value=os.environ.get("CK_NODE_NAME", ""))

        self._config_field(config_frame, "Server URL", self._url_var)
        self._config_field(config_frame, "Auth Token", self._token_var, show="*")
        self._config_field(config_frame, "Node Name", self._name_var)

        # Save button
        btn_frame = tk.Frame(config_frame, bg=_BG, pady=4)
        btn_frame.pack(fill="x")

        self._save_btn = tk.Button(
            btn_frame,
            text="Save Config",
            font=("Segoe UI", 10, "bold"),
            bg=_ACCENT,
            fg="#000",
            activebackground="#fff",
            relief="flat",
            padx=16,
            pady=4,
            cursor="hand2",
            command=self._save_config,
        )
        self._save_btn.pack(side="left")

        self._save_status = tk.Label(
            btn_frame,
            text="",
            font=("Consolas", 9),
            bg=_BG,
            fg=_GREEN,
        )
        self._save_status.pack(side="left", padx=8)

        # Controls
        ctrl_frame = tk.Frame(config_frame, bg=_BG, pady=4)
        ctrl_frame.pack(fill="x")

        self._pause_btn = tk.Button(
            ctrl_frame,
            text="Pause",
            font=("Consolas", 9),
            bg=_BG_FIELD,
            fg=_TEXT,
            activebackground=_BORDER,
            relief="flat",
            padx=12,
            pady=3,
            cursor="hand2",
            command=self._toggle_pause,
        )
        self._pause_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            ctrl_frame,
            text="Open Dashboard",
            font=("Consolas", 9),
            bg=_BG_FIELD,
            fg=_TEXT,
            activebackground=_BORDER,
            relief="flat",
            padx=12,
            pady=3,
            cursor="hand2",
            command=self._open_dashboard,
        ).pack(side="left")

        # Separator
        tk.Frame(root, bg=_BORDER, height=1).pack(fill="x", padx=16, pady=(8, 0))

        # Log viewer
        log_header = tk.Frame(root, bg=_BG, pady=4)
        log_header.pack(fill="x", padx=16)

        tk.Label(
            log_header,
            text="LOGS",
            font=("Consolas", 9),
            bg=_BG,
            fg=_TEXT_DIM,
            anchor="w",
        ).pack(side="left")

        self._log_text = scrolledtext.ScrolledText(
            root,
            font=("Consolas", 8),
            bg=_BG_CARD,
            fg=_TEXT_DIM,
            insertbackground=_TEXT,
            selectbackground=_ACCENT,
            selectforeground="#000",
            relief="flat",
            borderwidth=0,
            height=12,
            wrap="word",
            state="disabled",
        )
        self._log_text.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def _stat_card(self, parent: tk.Frame, label: str, value: str) -> tk.Label:
        card = tk.Frame(parent, bg=_BG_CARD, padx=8, pady=6, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(side="left", fill="x", expand=True, padx=(0, 6))

        tk.Label(card, text=label.upper(), font=("Consolas", 7), bg=_BG_CARD, fg=_TEXT_DIM).pack(anchor="w")
        val_label = tk.Label(card, text=value, font=("Consolas", 11, "bold"), bg=_BG_CARD, fg=_ACCENT)
        val_label.pack(anchor="w")
        return val_label

    def _config_field(self, parent: tk.Frame, label: str, var: tk.StringVar, show: str = "") -> None:
        frame = tk.Frame(parent, bg=_BG, pady=2)
        frame.pack(fill="x")

        tk.Label(frame, text=label, font=("Consolas", 9), bg=_BG, fg=_TEXT_DIM, width=12, anchor="w").pack(side="left")
        entry = tk.Entry(
            frame,
            textvariable=var,
            font=("Consolas", 10),
            bg=_BG_FIELD,
            fg=_TEXT,
            insertbackground=_TEXT,
            relief="flat",
            borderwidth=0,
        )
        if show:
            entry.configure(show=show)
        entry.pack(side="left", fill="x", expand=True, ipady=3)

    def _save_config(self) -> None:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_CONFIG_FILE, "w") as f:
            f.write(f"CK_MAIN_URL={self._url_var.get()}\n")
            f.write(f"CK_AUTH_TOKEN={self._token_var.get()}\n")
            f.write(f"CK_NODE_NAME={self._name_var.get()}\n")
            f.write("CK_NODE_GPUS=auto\n")
            f.write("CK_NODE_PREWARM=true\n")

        self._save_status.config(text="Saved! Restart to apply.", fg=_GREEN)
        self.root.after(3000, lambda: self._save_status.config(text=""))

    def _toggle_pause(self) -> None:
        if self.tray:
            self.tray._paused = not self.tray._paused
            paused = self.tray._paused
        else:
            paused = False
        self._pause_btn.config(text="Resume" if paused else "Pause")

    def _open_dashboard(self) -> None:
        import webbrowser

        url = self._url_var.get().strip()
        if url:
            webbrowser.open(url)

    # -- Public API (called from agent/tray thread) --

    def update_status(self, status: str) -> None:
        with self._lock:
            self._status = status
        if self.root and self._visible:
            self.root.after(
                0,
                lambda: self._status_label.config(
                    text=status.capitalize(),
                    fg=_GREEN
                    if status == "idle"
                    else _YELLOW
                    if status == "working"
                    else _RED
                    if status == "error"
                    else _TEXT_DIM,
                ),
            )

    def update_credits(self, credits: float) -> None:
        with self._lock:
            self._credits = credits
        if self.root and self._visible:
            self.root.after(0, lambda: self._credits_label.config(text=f"{credits:.1f}"))

    def update_gpu(self, name: str, vram_free: float) -> None:
        if self.root and self._visible:
            self.root.after(0, lambda: self._gpu_label.config(text=name[:20] if name else "—"))
            self.root.after(0, lambda: self._vram_label.config(text=f"{vram_free:.1f}GB"))

    def update_job(self, text: str) -> None:
        if self.root and self._visible:
            self.root.after(0, lambda: self._job_label.config(text=text or "idle"))

    def append_log(self, line: str) -> None:
        if self.root and self._visible:

            def _append():
                self._log_text.config(state="normal")
                self._log_text.insert("end", line + "\n")
                self._log_text.see("end")
                # Keep last 500 lines
                line_count = int(self._log_text.index("end-1c").split(".")[0])
                if line_count > 500:
                    self._log_text.delete("1.0", f"{line_count - 500}.0")
                self._log_text.config(state="disabled")

            self.root.after(0, _append)

    # -- Window visibility --

    def show(self) -> None:
        if self.root:
            self.root.deiconify()
            self.root.lift()
            self._visible = True

    def hide(self) -> None:
        if self.root:
            self.root.withdraw()
            self._visible = False

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def run(self) -> None:
        """Run the tkinter main loop. Blocks the calling thread."""
        if self.root:
            self.root.mainloop()

    def destroy(self) -> None:
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass

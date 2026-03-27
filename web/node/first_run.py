"""First-run configuration dialog for the standalone node binary.

Shows a simple tkinter dialog prompting for server URL and auth token
when node.env is missing or CK_MAIN_URL isn't configured. Saves the
config to ~/.corridorkey/node.env for subsequent launches.

Falls back silently if tkinter is unavailable (headless/Docker).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".corridorkey")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "node.env")


def needs_setup() -> bool:
    """Check if first-run setup is needed."""
    # Already configured via environment
    url = os.environ.get("CK_MAIN_URL", "").strip()
    token = os.environ.get("CK_AUTH_TOKEN", "").strip()
    if url and url != "http://localhost:3000" and token:
        return False

    # Config file exists with a real URL
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE) as f:
                content = f.read()
            if "CK_MAIN_URL=" in content and "localhost:3000" not in content:
                return False
        except Exception:
            pass

    return True


def run_setup_dialog() -> bool:
    """Show a configuration dialog. Returns True if config was saved.

    Falls back silently if tkinter is unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        logger.debug("tkinter not available — skipping setup dialog")
        return False

    root = tk.Tk()
    root.title("CorridorKey Node — Setup")
    root.geometry("450x280")
    root.resizable(False, False)

    # Try dark theme
    try:
        root.configure(bg="#1a1a2e")
    except Exception:
        pass

    bg = "#1a1a2e"
    fg = "#e0e0e0"
    accent = "#FFF203"
    entry_bg = "#2a2a3e"

    frame = tk.Frame(root, bg=bg, padx=20, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="CorridorKey Node Setup", font=("Arial", 14, "bold"), bg=bg, fg=accent).pack(pady=(0, 15))

    # Server URL
    tk.Label(frame, text="Server URL:", font=("Arial", 10), bg=bg, fg=fg, anchor="w").pack(fill="x")
    url_var = tk.StringVar(value="https://corridorkey.cloud")
    url_entry = tk.Entry(frame, textvariable=url_var, font=("Consolas", 10), bg=entry_bg, fg=fg, insertbackground=fg)
    url_entry.pack(fill="x", pady=(2, 10))

    # Auth Token
    tk.Label(frame, text="Auth Token (from the Nodes page):", font=("Arial", 10), bg=bg, fg=fg, anchor="w").pack(
        fill="x"
    )
    token_var = tk.StringVar()
    token_entry = tk.Entry(
        frame, textvariable=token_var, font=("Consolas", 10), bg=entry_bg, fg=fg, insertbackground=fg
    )
    token_entry.pack(fill="x", pady=(2, 10))

    # Node Name
    tk.Label(frame, text="Node Name:", font=("Arial", 10), bg=bg, fg=fg, anchor="w").pack(fill="x")
    import platform

    name_var = tk.StringVar(value=platform.node() or "my-node")
    name_entry = tk.Entry(frame, textvariable=name_var, font=("Consolas", 10), bg=entry_bg, fg=fg, insertbackground=fg)
    name_entry.pack(fill="x", pady=(2, 15))

    saved = [False]

    def on_save():
        url = url_var.get().strip()
        token = token_var.get().strip()
        name = name_var.get().strip()

        if not url or not token:
            messagebox.showwarning("Missing fields", "Server URL and Auth Token are required.")
            return

        # Save to config file
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_CONFIG_FILE, "w") as f:
            f.write(f"CK_MAIN_URL={url}\n")
            f.write(f"CK_AUTH_TOKEN={token}\n")
            f.write(f"CK_NODE_NAME={name}\n")
            f.write("CK_NODE_GPUS=auto\n")
            f.write("CK_NODE_PREWARM=true\n")

        # Also set in current environment
        os.environ["CK_MAIN_URL"] = url
        os.environ["CK_AUTH_TOKEN"] = token
        os.environ["CK_NODE_NAME"] = name

        logger.info("Config saved to %s", _CONFIG_FILE)
        saved[0] = True
        root.destroy()

    save_btn = tk.Button(
        frame,
        text="Save & Start",
        font=("Arial", 11, "bold"),
        bg=accent,
        fg="#000",
        activebackground="#fff",
        command=on_save,
        cursor="hand2",
    )
    save_btn.pack(fill="x")

    # Focus the token entry (URL has a default)
    token_entry.focus_set()

    root.mainloop()
    return saved[0]

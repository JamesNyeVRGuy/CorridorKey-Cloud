"""GPU addon manager — detects GPU vendor and installs the correct torch wheels.

On first launch of a standalone binary (CPU-only torch), this module:
1. Detects NVIDIA or AMD GPU via nvidia-smi / amd-smi / rocminfo
2. Downloads the correct torch+torchvision wheels from PyTorch's index
3. Installs them into the binary's lib directory
4. Marks the addon as installed so subsequent launches skip this step

The addon is ~2.5GB and downloads once. Model weights are handled separately
by weight_sync.py.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys

logger = logging.getLogger(__name__)

# Where to cache the GPU addon (next to the binary or in user home)
_ADDON_DIR = os.path.join(os.path.expanduser("~"), ".corridorkey", "gpu_addon")
_MARKER_FILE = os.path.join(_ADDON_DIR, ".installed")


def detect_gpu_vendor() -> str | None:
    """Detect GPU vendor. Returns 'nvidia', 'amd', or None."""
    # Check NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info("Detected NVIDIA GPU: %s", result.stdout.strip().split("\n")[0])
            return "nvidia"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check AMD (amd-smi)
    try:
        result = subprocess.run(
            ["amd-smi", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info("Detected AMD GPU via amd-smi")
            return "amd"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check AMD (rocminfo)
    try:
        result = subprocess.run(
            ["rocminfo"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and "gfx" in result.stdout:
            logger.info("Detected AMD GPU via rocminfo")
            return "amd"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check HIP_PATH (Windows AMD)
    if os.environ.get("HIP_PATH"):
        logger.info("Detected AMD GPU via HIP_PATH")
        return "amd"

    return None


def is_addon_installed() -> bool:
    """Check if the GPU addon is already installed."""
    return os.path.isfile(_MARKER_FILE)


def _get_pip_executable() -> str:
    """Get the pip executable path."""
    # In frozen builds, use the system pip
    if getattr(sys, "frozen", False):
        if platform.system() == "Windows":
            return "pip"
        return "pip3"
    return sys.executable + " -m pip"


def install_addon(vendor: str, on_progress=None) -> bool:
    """Download and install the GPU torch addon.

    Args:
        vendor: 'nvidia' or 'amd'
        on_progress: Optional callback(message: str) for status updates

    Returns True if successful.
    """
    os.makedirs(_ADDON_DIR, exist_ok=True)

    if vendor == "nvidia":
        index_url = "https://download.pytorch.org/whl/cu128"
        packages = ["torch==2.8.0", "torchvision==0.23.0"]
        label = "CUDA"
    elif vendor == "amd":
        index_url = "https://download.pytorch.org/whl/rocm6.3"
        packages = ["torch==2.8.0", "torchvision==0.23.0"]
        label = "ROCm"
    else:
        logger.warning("Unknown GPU vendor: %s", vendor)
        return False

    msg = f"Downloading {label} GPU acceleration (~2.5GB, one-time download)..."
    logger.info(msg)
    if on_progress:
        on_progress(msg)

    try:
        # Install GPU torch over the CPU version
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            "--target",
            _ADDON_DIR,
            "--index-url",
            index_url,
            *packages,
        ]

        logger.debug("Running: %s", " ".join(cmd))
        # Stream pip output, only log meaningful lines (download progress, errors)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # Show download progress and key status lines
            if "Downloading" in line or "━" in line or "ERROR" in line or "Successfully" in line:
                logger.info("[gpu-addon] %s", line)
            else:
                logger.debug("[gpu-addon] %s", line)
        proc.wait(timeout=600)

        if proc.returncode != 0:
            logger.error("GPU addon install failed (exit code %d)", proc.returncode)
            if on_progress:
                on_progress(f"{label} install failed. Running in CPU mode.")
            return False

        # Write marker file
        with open(_MARKER_FILE, "w") as f:
            f.write(f"{vendor}\n")

        msg = f"{label} GPU acceleration installed successfully!"
        logger.info(msg)
        if on_progress:
            on_progress(msg)
        return True

    except subprocess.TimeoutExpired:
        logger.error("GPU addon download timed out")
        if on_progress:
            on_progress("Download timed out. Running in CPU mode.")
        return False
    except Exception:
        logger.error("GPU addon install error", exc_info=True)
        return False


def ensure_gpu_addon() -> str | None:
    """Detect GPU and install addon if needed. Returns vendor or None.

    Call this early in startup, before any torch imports that need GPU.
    """
    # Already installed?
    if is_addon_installed():
        try:
            with open(_MARKER_FILE) as f:
                vendor = f.read().strip()
            # Prepend addon dir to sys.path so GPU torch is found first
            if _ADDON_DIR not in sys.path:
                sys.path.insert(0, _ADDON_DIR)
            logger.info("GPU addon already installed (%s)", vendor)
            return vendor
        except Exception:
            pass

    # Detect GPU
    vendor = detect_gpu_vendor()
    if vendor is None:
        logger.info("No GPU detected — running in CPU mode")
        return None

    # Install addon
    success = install_addon(vendor)
    if success:
        # Prepend addon dir so GPU torch takes priority over bundled CPU torch
        if _ADDON_DIR not in sys.path:
            sys.path.insert(0, _ADDON_DIR)
        return vendor

    return None

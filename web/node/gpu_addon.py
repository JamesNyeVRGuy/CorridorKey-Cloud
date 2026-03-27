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
        # Find pip — can't use sys.executable in frozen builds (it's the .exe itself)
        import shutil

        pip_exe = shutil.which("pip3") or shutil.which("pip")
        if pip_exe:
            cmd = [pip_exe, "install"]
        else:
            # Fallback: download wheels directly via httpx (no pip needed)
            logger.info("pip not found — downloading wheels directly")
            return _download_wheels_direct(vendor, index_url, packages, on_progress)

        cmd += [
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


def _download_wheels_direct(vendor: str, index_url: str, packages: list[str], on_progress=None) -> bool:
    """Download torch wheels directly via httpx when pip isn't available.

    Downloads the .whl files and extracts them into the addon directory.
    """
    import zipfile

    import httpx

    os.makedirs(_ADDON_DIR, exist_ok=True)
    label = "CUDA" if vendor == "nvidia" else "ROCm"

    # Determine platform tag for wheel filename
    if platform.system() == "Windows":
        plat = "win_amd64"
    else:
        plat = "manylinux1_x86_64"

    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"

    for pkg in packages:
        name, version = pkg.split("==")
        # Construct wheel URL (PyTorch index uses specific naming)
        # e.g., https://download.pytorch.org/whl/cu128/torch-2.8.0+cu128-cp311-cp311-win_amd64.whl
        suffix = index_url.rstrip("/").split("/")[-1]  # e.g., "cu128" or "rocm6.3"
        wheel_name = f"{name}-{version}+{suffix}-{py_tag}-{py_tag}-{plat}.whl"
        wheel_url = f"{index_url}/{name}/{wheel_name}"

        dest = os.path.join(_ADDON_DIR, wheel_name)
        logger.info("Downloading %s...", wheel_name)
        if on_progress:
            on_progress(f"Downloading {name} ({label})...")

        try:
            with httpx.stream("GET", wheel_url, timeout=300, follow_redirects=True) as r:
                if r.status_code != 200:
                    logger.error("Failed to download %s: HTTP %d", wheel_url, r.status_code)
                    return False
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and on_progress:
                            pct = int(downloaded / total * 100)
                            on_progress(f"Downloading {name}: {pct}% ({downloaded // (1024 * 1024)}MB)")

            # Extract wheel (it's a zip file)
            logger.info("Extracting %s...", wheel_name)
            with zipfile.ZipFile(dest, "r") as z:
                z.extractall(_ADDON_DIR)
            os.remove(dest)

        except Exception:
            logger.error("Failed to download %s", wheel_name, exc_info=True)
            return False

    # Write marker
    with open(_MARKER_FILE, "w") as f:
        f.write(f"{vendor}\n")

    msg = f"{label} GPU acceleration installed successfully!"
    logger.info(msg)
    if on_progress:
        on_progress(msg)
    return True


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

"""Auto-updater for the CorridorKey node agent standalone binary.

Checks the HuggingFace Hub repo that the release workflow publishes to
(see .github/workflows/release-node.yml), downloads a newer node-v* build
in the background, and applies it via a platform-specific helper script
that replaces the running binary after exit.

Only active in frozen (PyInstaller) builds: no-op when running from source.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import zipfile

logger = logging.getLogger(__name__)

# HuggingFace repo hosting the node binaries. The release workflow uploads
# every build here under {tag}/... and mirrors the latest build to latest/.
_HF_REPO = "JamesNyeVRGuy/corridorkey-node"
_CHECK_INTERVAL = 3600  # 1 hour between checks


def is_frozen() -> bool:
    """Return True if running as a PyInstaller frozen binary."""
    return getattr(sys, "frozen", False)


def _embedded_version() -> dict[str, str]:
    """Return the embedded _version.env dict; empty on non-frozen/dev builds."""
    try:
        from .agent import _EMBEDDED_VERSION
    except Exception:
        return {}
    return _EMBEDDED_VERSION or {}


def _current_tag() -> str | None:
    """Embedded release tag like 'node-v0.0.43', or None on dev/legacy builds."""
    tag = os.environ.get("CK_BUILD_TAG", "").strip() or _embedded_version().get("CK_BUILD_TAG", "").strip()
    return tag or None


def _parse_node_version(tag: str) -> tuple[int, ...] | None:
    """Parse 'node-v0.0.44' -> (0, 0, 44). Returns None on malformed input."""
    stripped = tag.removeprefix("node-v")
    try:
        parts = tuple(int(p) for p in stripped.split("."))
    except ValueError:
        return None
    return parts or None


def get_current_version() -> str:
    """Human-readable current version: semver ('0.0.43') when tagged, else commit."""
    tag = _current_tag()
    if tag:
        return tag.removeprefix("node-v")
    return os.environ.get("CK_BUILD_COMMIT", "dev")


def _gpu_variant() -> str | None:
    """Return 'nvidia' or 'amd' based on the torch build shipped in this binary."""
    try:
        import torch
    except Exception:
        return None
    if getattr(torch.version, "hip", None):
        return "amd"
    if getattr(torch.version, "cuda", None):
        return "nvidia"
    return None


def get_install_dir() -> str:
    """Get the directory containing the running binary."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _staging_dir() -> str:
    """Get or create the staging directory for downloaded updates."""
    d = os.path.join(os.path.expanduser("~"), ".corridorkey", "update")
    os.makedirs(d, exist_ok=True)
    return d


def check_for_update() -> dict | None:
    """Poll HuggingFace for a newer node-v* build matching this binary's variant.

    Returns an update_info dict {tag, version, download_url, size, name} if
    a newer release exists, else None. Binaries built before CK_BUILD_TAG was
    embedded in _version.env cannot be compared and are skipped: they must be
    reinstalled manually once to pick up auto-update.
    """
    import httpx

    current_tag = _current_tag()
    if not current_tag:
        logger.debug("No CK_BUILD_TAG on this build, skipping update check")
        return None
    current_ver = _parse_node_version(current_tag)
    if current_ver is None:
        logger.debug("Unparseable current tag %r, skipping update check", current_tag)
        return None

    if sys.platform != "win32":
        logger.debug("Auto-update only implemented for Windows binaries")
        return None

    variant = _gpu_variant()
    if not variant:
        logger.debug("Could not determine GPU variant, skipping update check")
        return None
    asset_name = f"corridorkey-node-{variant}-win-x64.zip"

    try:
        url = f"https://huggingface.co/api/models/{_HF_REPO}/tree/main?recursive=true"
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        tree = r.json()
    except Exception:
        logger.debug("HF tree listing failed", exc_info=True)
        return None

    # Pick the highest node-v* whose directory contains our variant's zip.
    best: tuple[tuple[int, ...], str, int] | None = None  # (ver, tag, size)
    for entry in tree:
        if entry.get("type") != "file":
            continue
        path = entry.get("path", "")
        tag, _, fname = path.partition("/")
        if fname != asset_name or not tag.startswith("node-v"):
            continue
        ver = _parse_node_version(tag)
        if ver is None or ver <= current_ver:
            continue
        if best is None or ver > best[0]:
            best = (ver, tag, int(entry.get("size") or 0))

    if best is None:
        return None

    _, tag, size = best
    return {
        "tag": tag,
        "version": tag.removeprefix("node-v"),
        "download_url": f"https://huggingface.co/{_HF_REPO}/resolve/main/{tag}/{asset_name}",
        "size": size,
        "name": asset_name,
    }


def download_update(update_info: dict, on_progress=None) -> str | None:
    """Download the update to the staging directory.

    Returns the path to the downloaded file, or None on failure.
    """
    import httpx

    staging = _staging_dir()
    dest = os.path.join(staging, update_info["name"])

    # Skip if already downloaded
    if os.path.isfile(dest) and os.path.getsize(dest) == update_info["size"]:
        logger.info("Update already downloaded: %s", dest)
        return dest

    try:
        logger.info("Downloading update: %s (%dMB)", update_info["name"], update_info["size"] // (1024 * 1024))
        with httpx.stream("GET", update_info["download_url"], timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            total = update_info["size"]
            downloaded = 0
            with open(dest + ".part", "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total > 0:
                        on_progress(downloaded, total)

        os.replace(dest + ".part", dest)
        logger.info("Update downloaded: %s", dest)
        return dest

    except Exception:
        logger.warning("Update download failed", exc_info=True)
        if os.path.isfile(dest + ".part"):
            os.remove(dest + ".part")
        return None


def apply_update(archive_path: str) -> None:
    """Apply the downloaded update by extracting and spawning a replacement script.

    On Windows: writes a batch script that waits, copies files, relaunches.
    On Linux: writes a shell script that replaces the binary and relaunches.

    This function does NOT return: it exits the current process.
    """
    install_dir = get_install_dir()
    staging = _staging_dir()
    extract_dir = os.path.join(staging, "extracted")

    # Clean and extract
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(extract_dir)
    elif archive_path.endswith(".tar.gz"):
        import tarfile

        with tarfile.open(archive_path, "r:gz") as t:
            t.extractall(extract_dir)
    else:
        logger.error("Unknown archive format: %s", archive_path)
        return

    # Find the extracted directory (should be corridorkey-node/)
    extracted_contents = os.listdir(extract_dir)
    if len(extracted_contents) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_contents[0])):
        source_dir = os.path.join(extract_dir, extracted_contents[0])
    else:
        source_dir = extract_dir

    if sys.platform == "win32":
        _apply_windows(source_dir, install_dir)
    else:
        _apply_linux(source_dir, install_dir)


def _apply_windows(source_dir: str, install_dir: str) -> None:
    """Windows: spawn a batch script that replaces files after we exit."""
    script = os.path.join(_staging_dir(), "update.bat")
    exe_name = os.path.basename(sys.executable)

    with open(script, "w") as f:
        f.write(f"""@echo off
timeout /t 3 /nobreak >nul
xcopy /s /y /q "{source_dir}\\*" "{install_dir}\\"
start "" "{os.path.join(install_dir, exe_name)}"
del "%~f0"
""")

    import subprocess

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    subprocess.Popen(
        ["cmd", "/c", script],
        creationflags=subprocess.DETACHED_PROCESS,
        startupinfo=si,
        close_fds=True,
    )
    logger.info("Update script launched, exiting for update")
    os._exit(0)


def _apply_linux(source_dir: str, install_dir: str) -> None:
    """Linux: spawn a shell script that replaces files after we exit."""
    script = os.path.join(_staging_dir(), "update.sh")
    exe_name = os.path.basename(sys.executable)

    with open(script, "w") as f:
        f.write(f"""#!/bin/bash
sleep 2
rm -rf "{install_dir}"/*
cp -a "{source_dir}"/* "{install_dir}"/
chmod +x "{os.path.join(install_dir, exe_name)}"
exec "{os.path.join(install_dir, exe_name)}"
""")

    os.chmod(script, 0o755)

    import subprocess

    subprocess.Popen(["/bin/bash", script], start_new_session=True)
    logger.info("Update script launched, exiting for update")
    os._exit(0)


class UpdateChecker:
    """Background thread that periodically checks for updates."""

    def __init__(self, tray=None):
        self.tray = tray
        self._pending_update: dict | None = None
        self._downloaded_path: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Start the background update checker."""
        if not is_frozen():
            logger.debug("Not a frozen build, auto-updater disabled")
            return

        self._thread = threading.Thread(target=self._loop, daemon=True, name="updater")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def apply_pending(self) -> None:
        """Apply a downloaded update. Call this when the user is ready to restart."""
        if self._downloaded_path:
            apply_update(self._downloaded_path)

    @property
    def has_update(self) -> bool:
        return self._downloaded_path is not None

    def _loop(self) -> None:
        # Wait a bit before first check (let the agent start up)
        self._stop.wait(30)

        while not self._stop.is_set():
            try:
                update = check_for_update()
                if update:
                    logger.info("Update available: %s", update["version"])
                    path = download_update(update)
                    if path:
                        self._pending_update = update
                        self._downloaded_path = path
                        logger.info("Update ready to apply: %s", update["version"])
                        if self.tray:
                            self.tray.set_update_available(self.apply_pending)
                            self.tray._notify(f"Update available: {update['version']}. Restart to apply.")
                        # Stop checking after finding an update
                        return
            except Exception:
                logger.debug("Update check error", exc_info=True)

            self._stop.wait(_CHECK_INTERVAL)

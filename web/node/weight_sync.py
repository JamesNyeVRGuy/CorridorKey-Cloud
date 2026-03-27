"""Weight sync — downloads model weights on node startup.

Priority order:
1. HuggingFace (canonical source, CDN-backed, fast)
2. Main server (LAN fallback for air-gapped environments)

Checks which weights are installed locally and pulls missing ones.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


def _weights_root() -> str:
    """Base directory for model weights.

    Frozen builds (PyInstaller): next to the executable (persistent, user-visible)
    CK_WEIGHTS_DIR env var: explicit override for custom location
    Source/Docker: relative to working directory (same layout as repo)
    """
    import sys

    # Explicit override takes priority
    custom = os.environ.get("CK_WEIGHTS_DIR", "").strip()
    if custom:
        return custom

    # Frozen build: store next to the .exe so weights survive restarts
    # and the user can see/manage them (they're 10+ GB)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    return "."


# Relative to weights root
_WEIGHT_SUBDIRS: dict[str, str] = {
    "corridorkey": os.path.join("CorridorKeyModule", "checkpoints"),
    "gvm": os.path.join("gvm_core", "weights"),
    "videomama": os.path.join("VideoMaMaInferenceModule", "checkpoints", "VideoMaMa"),
}

WEIGHT_SETS: dict[str, str] = {k: os.path.join(_weights_root(), v) for k, v in _WEIGHT_SUBDIRS.items()}

# HuggingFace repos for direct download fallback
HF_REPOS: dict[str, dict] = {
    "corridorkey": {
        "repo": "nikopueringer/CorridorKey_v1.0",
        "files": ["CorridorKey_v1.0.pth"],
    },
    "gvm": {
        "repo": "geyongtao/gvm",
    },
    "videomama": {
        "repo": "SammyLim/VideoMaMa",
    },
}

# Required files per weight set — _check_weights_exist verifies ALL of these exist.
# Prevents "already present" false positives from partial downloads.
_REQUIRED_FILES: dict[str, list[str]] = {
    "corridorkey": ["CorridorKey_v1.0.pth"],
    "gvm": [
        "vae/config.json",
        "vae/diffusion_pytorch_model.safetensors",
        "unet/config.json",
        "unet/diffusion_pytorch_model.safetensors",
        "scheduler/scheduler_config.json",
    ],
    "videomama": ["unet/config.json", "unet/diffusion_pytorch_model.safetensors"],
}


def _repo_root() -> str:
    """Find the repo root by walking up from this file."""
    d = os.path.dirname(os.path.abspath(__file__))
    # web/node/ -> web/ -> repo root
    return os.path.dirname(os.path.dirname(d))


def sync_weights(main_url: str, weight_names: list[str] | None = None) -> None:
    """Download missing weights. Tries HuggingFace first, falls back to main server.

    Args:
        main_url: Base URL of the main CorridorKey server (LAN fallback).
        weight_names: Which weight sets to sync. None = all available.
    """
    root = _repo_root()

    if weight_names is None:
        weight_names = list(WEIGHT_SETS.keys())

    for name in weight_names:
        local_dir = WEIGHT_SETS.get(name)
        if local_dir is None:
            logger.warning(f"Unknown weight set: {name}")
            continue

        abs_local = os.path.join(root, local_dir)

        # Already have weights?
        if _check_weights_exist(name, abs_local):
            logger.info(f"Weights '{name}' already present")
            continue

        # Try 1: HuggingFace (canonical source, CDN-backed)
        logger.info(f"Attempting HuggingFace download for '{name}'...")
        try:
            _download_from_hf(name, abs_local)
            if _check_weights_exist(name, abs_local):
                continue  # Success
        except Exception as e:
            logger.warning(f"HuggingFace download failed for '{name}': {e}, falling back to server")

        # Try 2: Main server (LAN fallback for air-gapped environments)
        _download_from_server(main_url, name, abs_local)


def _download_from_server(main_url: str, name: str, abs_local: str) -> None:
    """Download weights from the main CorridorKey server."""
    base = main_url.rstrip("/")
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(f"{base}/api/system/weights/{name}/manifest")
            if r.status_code != 200:
                logger.debug(f"Server weight manifest for '{name}': HTTP {r.status_code}")
                return
            manifest = r.json()
    except Exception as e:
        logger.warning(f"Could not fetch manifest for '{name}' from server: {e}")
        return

    remote_files = manifest.get("files", [])
    if not remote_files:
        logger.debug(f"No files for '{name}' on server")
        return

    # Determine what's missing locally
    to_download = []
    for entry in remote_files:
        rel_path = entry["path"]
        local_path = os.path.join(abs_local, rel_path)
        if os.path.isfile(local_path):
            local_size = os.path.getsize(local_path)
            if local_size == entry["size"]:
                continue
        to_download.append(entry)

    if not to_download:
        logger.info(f"Weights '{name}' up to date from server ({len(remote_files)} files)")
        return

    total_mb = sum(f["size"] for f in to_download) / (1024 * 1024)
    logger.info(f"Downloading {len(to_download)} files for '{name}' from server ({total_mb:.0f} MB)...")

    import time as _time

    downloaded = 0
    with httpx.Client(timeout=600) as client:
        for entry in to_download:
            rel_path = entry["path"]
            local_path = os.path.join(abs_local, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            url = f"{base}/api/system/weights/{name}/file/{rel_path}"
            try:
                dl_start = _time.time()
                total_size = entry["size"]
                total_mb = total_size / (1024 * 1024)
                written = 0
                last_pct = -1
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                            if total_size > 10 * 1024 * 1024:
                                pct = int(written / total_size * 100)
                                if pct >= last_pct + 10:
                                    elapsed = _time.time() - dl_start
                                    speed = written / max(1, elapsed)
                                    logger.info(
                                        f"  {rel_path}: {written / (1024 * 1024):.0f}/{total_mb:.0f} MB "
                                        f"({pct}%, {speed / (1024 * 1024):.1f} MB/s)"
                                    )
                                    last_pct = pct
                downloaded += 1
                elapsed = _time.time() - dl_start
                avg_speed = total_mb / max(1, elapsed)
                logger.info(f"  [{downloaded}/{len(to_download)}] {rel_path} ({total_mb:.1f} MB, {avg_speed:.1f} MB/s)")
            except Exception as e:
                logger.error(f"  Failed to download {rel_path}: {e}")

    logger.info(f"Server weight sync for '{name}': {downloaded}/{len(to_download)} files")


def _check_weights_exist(name: str, local_dir: str) -> bool:
    """Check if a weight set is complete by verifying all required files exist.

    Uses _REQUIRED_FILES for the specific model. Falls back to "any model file exists"
    for unknown weight sets.
    """
    if not os.path.isdir(local_dir):
        return False

    required = _REQUIRED_FILES.get(name)
    if required:
        for rel_path in required:
            full_path = os.path.join(local_dir, rel_path)
            if not os.path.isfile(full_path):
                logger.debug(f"Weight check '{name}': missing {rel_path}")
                return False
        return True

    # Fallback for unknown weight sets: any model file exists
    for _root, _dirs, files in os.walk(local_dir):
        for f in files:
            if f.endswith((".pth", ".safetensors", ".bin")) and not f.startswith("."):
                return True
    return False


def _download_from_hf(name: str, local_dir: str) -> None:
    """Download weights from HuggingFace using the raw HTTP API.

    No pip packages needed — just httpx (already installed).
    Uses the HF API to list repo files, then downloads each via CDN.
    """
    hf_info = HF_REPOS.get(name)
    if not hf_info:
        logger.warning(f"No HuggingFace repo configured for '{name}'")
        return

    if _check_weights_exist(name, local_dir):
        logger.info(f"Weights '{name}' already present locally, skipping HuggingFace download")
        return

    repo = hf_info["repo"]
    specific_files = hf_info.get("files")
    os.makedirs(local_dir, exist_ok=True)

    logger.info(f"Downloading '{name}' from HuggingFace ({repo})...")

    if specific_files:
        file_list = specific_files
    else:
        # List repo contents via HF API
        api_url = f"https://huggingface.co/api/models/{repo}/tree/main"
        with httpx.Client(timeout=30) as client:
            r = client.get(api_url)
            r.raise_for_status()
            tree = r.json()
        # Recursively collect all files
        file_list = _hf_collect_files(repo, tree)

    downloaded = 0
    for rel_path in file_list:
        local_path = os.path.join(local_dir, rel_path)
        if os.path.isfile(local_path):
            continue  # already have it

        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        url = f"https://huggingface.co/{repo}/resolve/main/{rel_path}"

        try:
            import time as _time

            with httpx.Client(timeout=600, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total_size = int(resp.headers.get("content-length", 0))
                    total_mb = total_size / (1024 * 1024) if total_size else 0
                    written = 0
                    last_pct = -1
                    dl_start = _time.time()
                    with open(local_path + ".part", "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                            if total_size > 10 * 1024 * 1024:  # progress for files >10MB
                                pct = int(written / total_size * 100)
                                if pct >= last_pct + 10:  # log every 10%
                                    elapsed = _time.time() - dl_start
                                    speed = written / max(1, elapsed)
                                    logger.info(
                                        f"  {rel_path}: {written / (1024 * 1024):.0f}/{total_mb:.0f} MB "
                                        f"({pct}%, {speed / (1024 * 1024):.1f} MB/s)"
                                    )
                                    last_pct = pct
            os.replace(local_path + ".part", local_path)
            downloaded += 1
            size_mb = os.path.getsize(local_path) / (1024 * 1024)
            elapsed = _time.time() - dl_start
            avg_speed = size_mb / max(1, elapsed)
            logger.info(f"  [{downloaded}] {rel_path} ({size_mb:.1f} MB, {avg_speed:.1f} MB/s)")
        except Exception as e:
            # Clean up partial file
            if os.path.isfile(local_path + ".part"):
                os.remove(local_path + ".part")
            logger.error(f"  Failed to download {rel_path}: {e}")
            raise

    logger.info(f"HuggingFace download complete for '{name}' ({downloaded} files)")


def _hf_collect_files(repo: str, tree: list, prefix: str = "") -> list[str]:
    """Recursively collect file paths from HuggingFace tree API response."""
    files = []
    for item in tree:
        path = item.get("path", "")
        if item.get("type") == "file":
            # Skip .gitattributes and other metadata
            if not path.startswith(".") and not path.endswith(".md"):
                files.append(path)
        elif item.get("type") == "directory":
            # Fetch subtree
            try:
                api_url = f"https://huggingface.co/api/models/{repo}/tree/main/{path}"
                with httpx.Client(timeout=30) as client:
                    r = client.get(api_url)
                    r.raise_for_status()
                    subtree = r.json()
                files.extend(_hf_collect_files(repo, subtree, path))
            except Exception:
                pass
    return files

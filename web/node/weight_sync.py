"""Weight sync — downloads model weights from the main machine on node startup.

Checks which weights are installed locally and pulls missing files from
the main server's /api/system/weights/{name}/file/ endpoints. Falls back
to downloading directly from HuggingFace if the main server doesn't have
weights installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import httpx

logger = logging.getLogger(__name__)

# Relative to repo root (same layout as main machine)
WEIGHT_SETS: dict[str, str] = {
    "corridorkey": os.path.join("CorridorKeyModule", "checkpoints"),
    "gvm": os.path.join("gvm_core", "weights"),
    "videomama": os.path.join("VideoMaMaInferenceModule", "checkpoints", "VideoMaMa"),
}

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


def _repo_root() -> str:
    """Find the repo root by walking up from this file."""
    d = os.path.dirname(os.path.abspath(__file__))
    # web/node/ -> web/ -> repo root
    return os.path.dirname(os.path.dirname(d))


def sync_weights(main_url: str, weight_names: list[str] | None = None) -> None:
    """Download missing weights from the main server.

    Args:
        main_url: Base URL of the main CorridorKey server.
        weight_names: Which weight sets to sync. None = all available.
    """
    base = main_url.rstrip("/")
    root = _repo_root()

    if weight_names is None:
        weight_names = list(WEIGHT_SETS.keys())

    for name in weight_names:
        local_dir = WEIGHT_SETS.get(name)
        if local_dir is None:
            logger.warning(f"Unknown weight set: {name}")
            continue

        abs_local = os.path.join(root, local_dir)

        # Fetch manifest from main
        try:
            with httpx.Client(timeout=30) as client:
                r = client.get(f"{base}/api/system/weights/{name}/manifest")
                if r.status_code != 200:
                    logger.debug(f"Skipping {name}: manifest returned {r.status_code}")
                    continue
                manifest = r.json()
        except Exception as e:
            logger.warning(f"Could not fetch manifest for {name}: {e}")
            logger.info(f"Trying HuggingFace fallback for '{name}'...")
            _download_from_hf(name, abs_local)
            continue

        remote_files = manifest.get("files", [])
        if not remote_files:
            logger.info(f"Weight set '{name}' not on main server, trying HuggingFace...")
            _download_from_hf(name, abs_local)
            continue

        # Determine what's missing locally
        to_download = []
        for entry in remote_files:
            rel_path = entry["path"]
            local_path = os.path.join(abs_local, rel_path)
            if os.path.isfile(local_path):
                local_size = os.path.getsize(local_path)
                if local_size == entry["size"]:
                    continue  # already have it
            to_download.append(entry)

        if not to_download:
            logger.info(f"Weights '{name}' already up to date ({len(remote_files)} files)")
            continue

        total_bytes = sum(f["size"] for f in to_download)
        total_mb = total_bytes / (1024 * 1024)
        logger.info(f"Downloading {len(to_download)} files for '{name}' ({total_mb:.0f} MB)...")

        downloaded = 0
        with httpx.Client(timeout=600) as client:
            for entry in to_download:
                rel_path = entry["path"]
                local_path = os.path.join(abs_local, rel_path)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)

                url = f"{base}/api/system/weights/{name}/file/{rel_path}"
                try:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(local_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                                f.write(chunk)
                    downloaded += 1
                    size_mb = entry["size"] / (1024 * 1024)
                    logger.info(f"  [{downloaded}/{len(to_download)}] {rel_path} ({size_mb:.1f} MB)")
                except Exception as e:
                    logger.error(f"  Failed to download {rel_path}: {e}")

        logger.info(f"Weight sync complete for '{name}': {downloaded}/{len(to_download)} files")


def _check_weights_exist(name: str, local_dir: str) -> bool:
    """Quick check if a weight set has any real files."""
    if not os.path.isdir(local_dir):
        return False
    for _root, _dirs, files in os.walk(local_dir):
        for f in files:
            if f.endswith((".pth", ".safetensors", ".bin", ".json")) and not f.startswith("."):
                return True
    return False


def _download_from_hf(name: str, local_dir: str) -> None:
    """Download weights directly from HuggingFace as a fallback."""
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

    # Try huggingface-cli first, fall back to python -m
    cmd = _build_hf_cmd(repo, local_dir, specific_files)

    logger.info(f"Downloading '{name}' from HuggingFace ({repo})...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            logger.info(f"HuggingFace download complete for '{name}'")
        else:
            error = result.stderr.strip()[-300:] if result.stderr else "Unknown error"
            logger.error(f"HuggingFace download failed for '{name}': {error}")
    except subprocess.TimeoutExpired:
        logger.error(f"HuggingFace download timed out for '{name}'")
    except FileNotFoundError:
        logger.error("huggingface-cli not found. Install with: pip install huggingface-hub")


def _build_hf_cmd(repo: str, local_dir: str, specific_files: list[str] | None = None) -> list[str]:
    """Build the huggingface download command."""
    import shutil

    for candidate in ["huggingface-cli", "hf"]:
        found = shutil.which(candidate)
        if found:
            cmd = [found, "download", repo, "--local-dir", local_dir]
            if specific_files:
                cmd.extend(specific_files)
            return cmd

    # Fallback: python -m huggingface_hub
    cmd = [sys.executable, "-m", "huggingface_hub", "download", repo, "--local-dir", local_dir]
    if specific_files:
        cmd.extend(specific_files)
    return cmd

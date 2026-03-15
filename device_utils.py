"""Centralized cross-platform device selection for CorridorKey."""

import logging
import os
import subprocess
from dataclasses import dataclass

import torch

logger = logging.getLogger(__name__)

DEVICE_ENV_VAR = "CORRIDORKEY_DEVICE"
VALID_DEVICES = ("auto", "cuda", "mps", "cpu")


def detect_best_device() -> str:
    """Auto-detect best available device: CUDA > MPS > CPU."""
    if torch.cuda.is_available():
        device = "cuda"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    logger.info("Auto-selected device: %s", device)
    return device


def resolve_device(requested: str | None = None) -> str:
    """Resolve device from explicit request > env var > auto-detect.

    Args:
        requested: Device string from CLI arg. None or "auto" triggers
                   env var lookup then auto-detection.

    Returns:
        Validated device string ("cuda", "mps", or "cpu").

    Raises:
        RuntimeError: If the requested backend is unavailable.
    """
    # CLI arg takes priority, then env var, then auto
    device = requested
    if device is None or device == "auto":
        device = os.environ.get(DEVICE_ENV_VAR, "auto")

    if device == "auto":
        return detect_best_device()

    device = device.lower()
    if device not in VALID_DEVICES:
        raise RuntimeError(f"Unknown device '{device}'. Valid options: {', '.join(VALID_DEVICES)}")

    # Validate the explicit request
    if device == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA requested but torch.cuda.is_available() is False. Install a CUDA-enabled PyTorch build."
            )
    elif device == "mps":
        if not hasattr(torch.backends, "mps"):
            raise RuntimeError(
                "MPS requested but this PyTorch build has no MPS support. Install PyTorch >= 1.12 with MPS backend."
            )
        if not torch.backends.mps.is_available():
            raise RuntimeError(
                "MPS requested but not available on this machine. Requires Apple Silicon (M1+) with macOS 12.3+."
            )

    return device


@dataclass
class GPUInfo:
    """Information about a single GPU."""

    index: int
    name: str
    vram_total_gb: float
    vram_free_gb: float


def enumerate_gpus() -> list[GPUInfo]:
    """List all available CUDA GPUs with VRAM info via nvidia-smi.

    Falls back to torch.cuda if nvidia-smi is unavailable.
    Returns an empty list on non-CUDA systems.
    """
    gpus: list[GPUInfo] = []

    # Try nvidia-smi first (sees all GPUs regardless of CUDA_VISIBLE_DEVICES)
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append(
                        GPUInfo(
                            index=int(parts[0]),
                            name=parts[1],
                            vram_total_gb=float(parts[2]) / 1024,
                            vram_free_gb=float(parts[3]) / 1024,
                        )
                    )
            return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback to torch
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            total = props.total_memory / (1024**3)
            gpus.append(
                GPUInfo(
                    index=i,
                    name=props.name,
                    vram_total_gb=total,
                    vram_free_gb=total,  # can't query free without setting device
                )
            )

    return gpus


def check_gpu_available(gpu_index: int = 0, min_free_gb: float = 0.0) -> tuple[bool, str]:
    """Check if a GPU is available for CorridorKey work.

    Checks GPU utilization via nvidia-smi. If another process is using
    significant GPU compute (>50% utilization), the GPU is considered busy.

    Returns:
        (available, reason) — True if GPU can accept work, else False with reason.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--id={gpu_index}",
                "--query-gpu=utilization.gpu,memory.free",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return True, "nvidia-smi unavailable"

        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 2:
            return True, "parse error"

        util_pct = int(parts[0])
        free_mb = float(parts[1])
        free_gb = free_mb / 1024

        if util_pct > 50:
            return False, f"GPU {gpu_index} busy ({util_pct}% utilization)"
        if min_free_gb > 0 and free_gb < min_free_gb:
            return False, f"GPU {gpu_index} low VRAM ({free_gb:.1f}GB free, need {min_free_gb:.1f}GB)"
        return True, "ok"

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True, "nvidia-smi unavailable"


def clear_device_cache(device: torch.device | str) -> None:
    """Clear GPU memory cache if applicable (no-op for CPU)."""
    device_type = device.type if isinstance(device, torch.device) else device
    if device_type == "cuda":
        torch.cuda.empty_cache()
    elif device_type == "mps":
        torch.mps.empty_cache()

"""Centralized cross-platform device selection for CorridorKey."""

import json
import logging
import os
import sys
from dataclasses import dataclass
from subprocess import TimeoutExpired

from web.shared.subprocess_utils import run_silent as subprocess_run

logger = logging.getLogger(__name__)

DEVICE_ENV_VAR = "CORRIDORKEY_DEVICE"
VALID_DEVICES = ("auto", "cuda", "mps", "cpu")


def is_rocm_system() -> bool:
    """Detect if the system has AMD ROCm available.

    Checks: /opt/rocm (Linux), HIP_PATH (Windows, default C:\\hip),
    HIP_VISIBLE_DEVICES (any platform), CORRIDORKEY_ROCM=1 (explicit opt-in).
    """
    return (
        os.path.exists("/opt/rocm")
        or os.environ.get("HIP_PATH") is not None
        or os.environ.get("HIP_VISIBLE_DEVICES") is not None
        or os.environ.get("CORRIDORKEY_ROCM") == "1"
    )


def setup_rocm_env() -> None:
    """Set ROCm environment variables and apply optional patches.

    These env vars are read by PyTorch/MIOpen at operation time (not import
    time), so calling this after ``import torch`` is fine. Safe to call on
    non-ROCm systems (no-op).
    """
    if not is_rocm_system():
        return
    os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
    os.environ.setdefault("MIOPEN_FIND_MODE", "2")
    # Level 4 = suppress info/debug but keep warnings and errors visible
    os.environ.setdefault("MIOPEN_LOG_LEVEL", "4")
    # Enable GTT (system RAM as GPU overflow) on Linux for 16GB cards.
    # pytorch-rocm-gtt must be installed separately: pip install pytorch-rocm-gtt
    try:
        import pytorch_rocm_gtt

        pytorch_rocm_gtt.patch()
    except ImportError:
        pass  # not installed — expected on most systems
    except Exception:
        logger.warning("pytorch-rocm-gtt is installed but patch() failed", exc_info=True)


import torch  # noqa: E402


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


def _enumerate_nvidia() -> list[GPUInfo] | None:
    """Enumerate NVIDIA GPUs via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess_run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        gpus: list[GPUInfo] = []
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
    except (FileNotFoundError, TimeoutExpired):
        return None


def _enumerate_amd_windows() -> list[GPUInfo] | None:
    """Enumerate AMD GPUs on Windows via PowerShell/WMI. Returns None if unavailable."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess_run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'AMD|Radeon' } | "
                "Select-Object Name, AdapterRAM | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        gpus: list[GPUInfo] = []
        for i, gpu in enumerate(data):
            name = gpu.get("Name", f"AMD GPU {i}")
            adapter_ram = gpu.get("AdapterRAM", 0)
            total_gb = float(adapter_ram) / (1024**3) if adapter_ram else 0
            gpus.append(GPUInfo(index=i, name=name, vram_total_gb=total_gb, vram_free_gb=total_gb))
        return gpus if gpus else None
    except (FileNotFoundError, TimeoutExpired, json.JSONDecodeError):
        return None


def _enumerate_amd() -> list[GPUInfo] | None:
    """Enumerate AMD GPUs via amd-smi (ROCm). Returns None if unavailable.

    Tries amd-smi first (modern), then rocm-smi (legacy), then Windows WMI.
    """
    # Try amd-smi (ROCm 6.0+)
    try:
        result = subprocess_run(
            ["amd-smi", "static", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            gpus: list[GPUInfo] = []
            for i, gpu in enumerate(data):
                try:
                    name = gpu.get("asic", {}).get("market_name", f"AMD GPU {i}")
                    vram_info = gpu.get("vram", {})
                    total_mb = vram_info.get("size", {}).get("value", 0)
                    total_gb = float(total_mb) / 1024 if total_mb else 0
                    gpus.append(GPUInfo(index=i, name=name, vram_total_gb=total_gb, vram_free_gb=total_gb))
                except (KeyError, TypeError, ValueError):
                    logger.debug("Failed to parse amd-smi entry %d, skipping", i)
            if gpus:
                # Try to get live VRAM usage from monitor
                try:
                    mon = subprocess_run(
                        ["amd-smi", "monitor", "--vram", "--json"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if mon.returncode == 0:
                        mon_data = json.loads(mon.stdout)
                        for entry in mon_data:
                            idx = entry.get("gpu", 0)
                            used_pct = entry.get("vram_use", 0)
                            if idx < len(gpus) and gpus[idx].vram_total_gb > 0:
                                used_gb = gpus[idx].vram_total_gb * float(used_pct) / 100
                                gpus[idx].vram_free_gb = gpus[idx].vram_total_gb - used_gb
                except Exception:
                    pass
                return gpus
    except (FileNotFoundError, TimeoutExpired, json.JSONDecodeError):
        pass

    # Fallback: rocm-smi (legacy, deprecated but still ships)
    try:
        result = subprocess_run(
            ["rocm-smi", "--showid", "--showmeminfo", "vram", "--csv"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            gpus = []
            for line in result.stdout.strip().split("\n")[1:]:  # skip header
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    idx = int(parts[0]) if parts[0].isdigit() else len(gpus)
                    total_b = int(parts[1]) if parts[1].isdigit() else 0
                    used_b = int(parts[2]) if parts[2].isdigit() else 0
                    gpus.append(
                        GPUInfo(
                            index=idx,
                            name=f"AMD GPU {idx}",
                            vram_total_gb=total_b / (1024**3),
                            vram_free_gb=(total_b - used_b) / (1024**3),
                        )
                    )
            if gpus:
                return gpus
    except (FileNotFoundError, TimeoutExpired):
        pass

    # Windows: no amd-smi/rocm-smi, fall back to WMI
    return _enumerate_amd_windows()


def enumerate_gpus() -> list[GPUInfo]:
    """List all available GPUs with VRAM info.

    Tries nvidia-smi (NVIDIA), then amd-smi/rocm-smi (AMD ROCm),
    then falls back to torch.cuda API.
    Returns an empty list on non-GPU systems.
    """
    # NVIDIA
    gpus = _enumerate_nvidia()
    if gpus is not None:
        return gpus

    # AMD ROCm
    gpus = _enumerate_amd()
    if gpus is not None:
        return gpus

    # Fallback to torch (works for both NVIDIA and ROCm via HIP)
    try:
        if torch.cuda.is_available():
            fallback: list[GPUInfo] = []
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                total = props.total_memory / (1024**3)
                fallback.append(
                    GPUInfo(
                        index=i,
                        name=props.name,
                        vram_total_gb=total,
                        vram_free_gb=total,  # can't query free without setting device
                    )
                )
            return fallback
    except RuntimeError:
        # AMD torch ships caffe2_nvrtc.dll (NVIDIA) which crashes on AMD-only machines.
        # _lazy_init() fails with LoadLibrary error — fall through to empty list.
        logger.debug("torch.cuda init failed (caffe2_nvrtc?), falling through", exc_info=True)

    return []


@dataclass
class CPUStats:
    """System CPU and memory statistics."""

    cpu_percent: float  # overall CPU usage 0-100
    cpu_count: int
    ram_total_gb: float
    ram_used_gb: float
    ram_free_gb: float

    def to_dict(self) -> dict:
        return {
            "cpu_percent": round(self.cpu_percent, 1),
            "cpu_count": self.cpu_count,
            "ram_total_gb": round(self.ram_total_gb, 1),
            "ram_used_gb": round(self.ram_used_gb, 1),
            "ram_free_gb": round(self.ram_free_gb, 1),
        }


def get_cpu_stats() -> CPUStats:
    """Get current CPU usage and memory stats."""
    import psutil

    cpu_pct = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory()
    return CPUStats(
        cpu_percent=cpu_pct,
        cpu_count=psutil.cpu_count() or 1,
        ram_total_gb=mem.total / (1024**3),
        ram_used_gb=mem.used / (1024**3),
        ram_free_gb=mem.available / (1024**3),
    )


def check_gpu_available(gpu_index: int = 0, min_free_gb: float = 0.0) -> tuple[bool, str]:
    """Check if a GPU is available for CorridorKey work.

    Checks GPU utilization via nvidia-smi or amd-smi. If another process
    is using significant GPU compute (>50% utilization), the GPU is busy.

    Returns:
        (available, reason) — True if GPU can accept work, else False with reason.
    """
    # Try NVIDIA
    try:
        result = subprocess_run(
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
        if result.returncode == 0:
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            if len(parts) >= 2:
                util_pct = int(parts[0])
                free_gb = float(parts[1]) / 1024
                if util_pct > 50:
                    return False, f"GPU {gpu_index} busy ({util_pct}% utilization)"
                if min_free_gb > 0 and free_gb < min_free_gb:
                    return False, f"GPU {gpu_index} low VRAM ({free_gb:.1f}GB free, need {min_free_gb:.1f}GB)"
                return True, "ok"
    except (FileNotFoundError, TimeoutExpired):
        pass

    # Try AMD
    try:
        result = subprocess_run(
            ["amd-smi", "monitor", "--gpu-use", "--vram", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if gpu_index < len(data):
                entry = data[gpu_index]
                util_pct = int(entry.get("gpu_use", 0))
                if util_pct > 50:
                    return False, f"GPU {gpu_index} busy ({util_pct}% utilization)"
                return True, "ok"
    except (FileNotFoundError, TimeoutExpired, Exception):
        pass

    return True, "gpu monitoring unavailable"


def clear_device_cache(device: torch.device | str) -> None:
    """Clear GPU memory cache if applicable (no-op for CPU)."""
    device_type = device.type if isinstance(device, torch.device) else device
    if device_type == "cuda":
        torch.cuda.empty_cache()
    elif device_type == "mps":
        torch.mps.empty_cache()

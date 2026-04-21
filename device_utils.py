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
    # CUDA compute capability as "major.minor" (e.g. "6.1", "7.5", "8.6").
    # Empty string for AMD/unknown. Used to gate out GPUs that the shipped
    # torch build can't run on (see check_gpu_torch_compat).
    compute_capability: str = ""


# Minimum CUDA compute capability supported by the torch build we ship.
# torch 2.8.0 cu128 wheels drop sm_60/sm_61 (Pascal) and below.
# Supported archs: sm_70 sm_75 sm_80 sm_86 sm_90 sm_100 sm_120.
MIN_CUDA_COMPUTE_CAPABILITY = (7, 0)


def _parse_cc(cc: str) -> tuple[int, int] | None:
    """Parse a compute capability string like '6.1' into (6, 1)."""
    try:
        major, minor = cc.split(".", 1)
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


def check_gpu_torch_compat(gpu: GPUInfo) -> tuple[bool, str]:
    """Check whether a GPU meets the minimum compute capability for our torch build.

    AMD GPUs (empty compute_capability) are assumed compatible and not gated
    here; they have their own compatibility handling in the ROCm path.

    Returns (compatible, reason). reason is an empty string when compatible.
    """
    if not gpu.compute_capability:
        return True, ""
    parsed = _parse_cc(gpu.compute_capability)
    if parsed is None:
        return True, ""  # can't parse, don't block
    if parsed < MIN_CUDA_COMPUTE_CAPABILITY:
        min_str = f"{MIN_CUDA_COMPUTE_CAPABILITY[0]}.{MIN_CUDA_COMPUTE_CAPABILITY[1]}"
        return False, (
            f"GPU {gpu.index} '{gpu.name}' has CUDA compute capability "
            f"{gpu.compute_capability} (sm_{parsed[0]}{parsed[1]}), "
            f"below the minimum {min_str} (sm_{MIN_CUDA_COMPUTE_CAPABILITY[0]}"
            f"{MIN_CUDA_COMPUTE_CAPABILITY[1]}) required by this node build. "
            f"Jobs would fail with 'no kernel image is available for execution on the device'."
        )
    return True, ""


def _enumerate_nvidia() -> list[GPUInfo] | None:
    """Enumerate NVIDIA GPUs via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess_run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free,compute_cap",
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
                        compute_capability=parts[4] if len(parts) >= 5 else "",
                    )
                )
        return gpus
    # Expected failures, continue to next fallback
    except (FileNotFoundError, TimeoutExpired):
        pass
    # Catch known bad nvidia-smi output
    except ValueError as e:
        if "[N/A]" in str(e):
            logger.debug("bad nvidia-smi output, continuing to fallbacks")
        else:
            logger.debug("Unexpected ValueError trying to enumerate GPUs", exc_info=True)
    # Catch all exceptions and log but continue to fallbacks
    except Exception:
        logger.debug("Unexpected failure trying to enumerate GPUs", exc_info=True)
    return None


def _enumerate_amd_windows() -> list[GPUInfo] | None:
    """Enumerate AMD GPUs on Windows via registry. Returns None if unavailable.

    Win32_VideoController.AdapterRAM is uint32 and overflows for >4GB GPUs.
    The registry stores the real VRAM size as qwMemorySize (64-bit).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        gpus: list[GPUInfo] = []
        base_key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_key)
        for i in range(20):
            try:
                subkey = winreg.OpenKey(key, f"{i:04d}")
                provider, _ = winreg.QueryValueEx(subkey, "ProviderName")
                if "AMD" not in provider.upper() and "ATI" not in provider.upper():
                    continue
                desc, _ = winreg.QueryValueEx(subkey, "DriverDesc")
                total_gb = 0
                # Try 64-bit value first, then 32-bit fallback
                for reg_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                    try:
                        mem_bytes, _ = winreg.QueryValueEx(subkey, reg_name)
                        total_gb = float(mem_bytes) / (1024**3)
                        if total_gb > 0:
                            break
                    except OSError:
                        continue
                gpus.append(GPUInfo(index=len(gpus), name=desc, vram_total_gb=total_gb, vram_free_gb=total_gb))
            except OSError:
                continue
        return gpus if gpus else None
    except Exception:
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
    # Expected failures, continue to next fallback
    except (FileNotFoundError, TimeoutExpired, json.JSONDecodeError):
        pass
    # Catch all exceptions and log but continue to fallbacks
    except Exception:
        logger.debug("Unexpected failure trying to enumerate GPUs", exc_info=True)

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
    # Expected failures, continue to next fallback
    except (FileNotFoundError, TimeoutExpired):
        pass
    # Catch all exceptions and log but continue to fallbacks
    except Exception:
        logger.debug("Unexpected failure trying to enumerate GPUs", exc_info=True)

    # Fallback: pyrsmi Python package (pip install pyrsmi)
    try:
        from pyrsmi import rocml

        rocml.smi_initialize()
        num_devices = rocml.smi_get_device_count()
        gpus = []
        for i in range(num_devices):
            name = rocml.smi_get_device_name(i)
            total_bytes = rocml.smi_get_device_memory_total(i)
            used_bytes = rocml.smi_get_device_memory_used(i)
            gpus.append(
                GPUInfo(
                    index=i,
                    name=name,
                    vram_total_gb=total_bytes / (1024**3),
                    vram_free_gb=(total_bytes - used_bytes) / (1024**3),
                )
            )
        rocml.smi_shutdown()
        if gpus:
            return gpus
    except (ImportError, Exception):
        pass

    # Windows: no amd-smi/rocm-smi, fall back to WMI
    return _enumerate_amd_windows()


def _enumerate_torch() -> list[GPUInfo] | None:
    """Enumerate GPUs via torch. Returns None if unavailable."""
    try:
        if torch.cuda.is_available():
            gpus = []
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                total = props.total_memory / (1024**3)
                cc = f"{props.major}.{props.minor}" if hasattr(props, "major") and hasattr(props, "minor") else ""
                gpus.append(
                    GPUInfo(
                        index=i,
                        name=props.name,
                        vram_total_gb=total,
                        vram_free_gb=total,  # can't query free without setting device
                        compute_capability=cc,
                    )
                )
            if gpus:
                return gpus
    except RuntimeError:
        # AMD torch ships caffe2_nvrtc.dll (NVIDIA) which crashes on AMD-only machines.
        # _lazy_init() fails with LoadLibrary error — fall through to empty list.
        logger.debug("torch.cuda init failed (caffe2_nvrtc?), falling through", exc_info=True)
    # Catch all exceptions and log but continue to try all enumeration fallbacks (continue to default return)
    except Exception:
        logger.debug("Unexpected failure trying to enumerate GPUs", exc_info=True)
    return None


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
    gpus = _enumerate_torch()
    if gpus is not None:
        return gpus

    # return empty list as a default
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
    # Expected failures, continue to next fallback
    except (FileNotFoundError, TimeoutExpired):
        pass
    # Catch known bad nvidia-smi output
    except ValueError as e:
        if "[N/A]" in str(e):
            logger.debug("bad nvidia-smi output, continuing to fallbacks")
        else:
            logger.debug("Unexpected ValueError trying to check GPU usage", exc_info=True)
    # Catch all exceptions and log but continue to fallbacks
    except Exception:
        logger.debug("Unexpected failure trying to check GPU usage", exc_info=True)

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

    # Try PyTorch
    if torch.cuda.is_available():
        try:
            # Set the device to query
            device = torch.device(f"cuda:{gpu_index}")

            # mem_get_info returns (free_bytes, total_bytes)
            free_bytes, _ = torch.cuda.mem_get_info(device)
            free_gb = free_bytes / (1024**3)

            # No utility check so fallback relies on just vram usage
            if min_free_gb > 0 and free_gb < min_free_gb:
                return False, f"GPU {gpu_index} low VRAM (PyTorch: {free_gb:.1f}GB free)"

            return True, "ok"
        # Catch all so we return default
        except Exception:
            pass

    return True, "gpu monitoring unavailable"


def clear_device_cache(device: torch.device | str) -> None:
    """Clear GPU memory cache if applicable (no-op for CPU)."""
    device_type = device.type if isinstance(device, torch.device) else device
    if device_type == "cuda":
        torch.cuda.empty_cache()
    elif device_type == "mps":
        torch.mps.empty_cache()

"""Unit tests for device_utils — cross-platform device selection.

Tests cover all code paths in detect_best_device(), resolve_device(),
and clear_device_cache() using monkeypatch to mock hardware availability.
No GPU required.
"""

from subprocess import TimeoutExpired
from unittest.mock import MagicMock

import pytest
import torch

from device_utils import (
    DEVICE_ENV_VAR,
    MIN_CUDA_COMPUTE_CAPABILITY,
    GPUInfo,
    _parse_nvidia_availability,
    _query_nvidia_smi,
    check_gpu_torch_compat,
    clear_device_cache,
    detect_best_device,
    resolve_device,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_gpu(monkeypatch, *, cuda=False, mps=False):
    """Mock CUDA and MPS availability flags."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
    # MPS lives behind torch.backends.mps; ensure the attr path exists
    mps_backend = MagicMock()
    mps_backend.is_available = MagicMock(return_value=mps)
    monkeypatch.setattr(torch.backends, "mps", mps_backend)


# ---------------------------------------------------------------------------
# detect_best_device
# ---------------------------------------------------------------------------


class TestDetectBestDevice:
    """Priority chain: CUDA > MPS > CPU."""

    def test_returns_cuda_when_available(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=True, mps=True)
        assert detect_best_device() == "cuda"

    def test_returns_mps_when_no_cuda(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False, mps=True)
        assert detect_best_device() == "mps"

    def test_returns_cpu_when_nothing(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False, mps=False)
        assert detect_best_device() == "cpu"


# ---------------------------------------------------------------------------
# resolve_device
# ---------------------------------------------------------------------------


class TestResolveDevice:
    """Priority chain: CLI arg > env var > auto-detect."""

    # --- auto-detect path ---

    def test_none_triggers_auto_detect(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False, mps=False)
        monkeypatch.delenv(DEVICE_ENV_VAR, raising=False)
        assert resolve_device(None) == "cpu"

    def test_auto_string_triggers_auto_detect(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=True)
        monkeypatch.delenv(DEVICE_ENV_VAR, raising=False)
        assert resolve_device("auto") == "cuda"

    # --- env var fallback ---

    def test_env_var_used_when_no_cli_arg(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=True, mps=True)
        monkeypatch.setenv(DEVICE_ENV_VAR, "cpu")
        assert resolve_device(None) == "cpu"

    def test_env_var_auto_triggers_detect(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False, mps=True)
        monkeypatch.setenv(DEVICE_ENV_VAR, "auto")
        assert resolve_device(None) == "mps"

    # --- CLI arg overrides env var ---

    def test_cli_arg_overrides_env_var(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=True, mps=True)
        monkeypatch.setenv(DEVICE_ENV_VAR, "mps")
        assert resolve_device("cuda") == "cuda"

    # --- explicit valid devices ---

    def test_explicit_cuda(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=True)
        assert resolve_device("cuda") == "cuda"

    def test_explicit_mps(self, monkeypatch):
        _patch_gpu(monkeypatch, mps=True)
        assert resolve_device("mps") == "mps"

    def test_explicit_cpu(self, monkeypatch):
        assert resolve_device("cpu") == "cpu"

    def test_case_insensitive(self, monkeypatch):
        assert resolve_device("CPU") == "cpu"

    # --- unavailable backend errors ---

    def test_cuda_unavailable_raises(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False)
        with pytest.raises(RuntimeError, match="CUDA requested"):
            resolve_device("cuda")

    def test_mps_no_backend_raises(self, monkeypatch):
        # Simulate PyTorch build without MPS module in torch.backends
        _patch_gpu(monkeypatch, cuda=False, mps=False)
        # Replace torch.backends with an object that lacks "mps" entirely
        fake_backends = type("Backends", (), {})()
        monkeypatch.setattr(torch, "backends", fake_backends)
        with pytest.raises(RuntimeError, match="no MPS support"):
            resolve_device("mps")

    def test_mps_unavailable_raises(self, monkeypatch):
        _patch_gpu(monkeypatch, cuda=False, mps=False)
        with pytest.raises(RuntimeError, match="not available on this machine"):
            resolve_device("mps")

    # --- invalid device string ---

    def test_invalid_device_raises(self, monkeypatch):
        with pytest.raises(RuntimeError, match="Unknown device"):
            resolve_device("tpu")


# ---------------------------------------------------------------------------
# clear_device_cache
# ---------------------------------------------------------------------------


class TestClearDeviceCache:
    """Dispatches to correct backend cache clear."""

    def test_cuda_clears_cache(self, monkeypatch):
        mock_empty = MagicMock()
        monkeypatch.setattr(torch.cuda, "empty_cache", mock_empty)
        clear_device_cache("cuda")
        mock_empty.assert_called_once()

    def test_mps_clears_cache(self, monkeypatch):
        mock_empty = MagicMock()
        monkeypatch.setattr(torch.mps, "empty_cache", mock_empty)
        clear_device_cache("mps")
        mock_empty.assert_called_once()

    def test_cpu_is_noop(self):
        # Should not raise
        clear_device_cache("cpu")

    def test_accepts_torch_device_object(self, monkeypatch):
        mock_empty = MagicMock()
        monkeypatch.setattr(torch.cuda, "empty_cache", mock_empty)
        clear_device_cache(torch.device("cuda"))
        mock_empty.assert_called_once()

    def test_accepts_mps_device_object(self, monkeypatch):
        mock_empty = MagicMock()
        monkeypatch.setattr(torch.mps, "empty_cache", mock_empty)
        clear_device_cache(torch.device("mps"))
        mock_empty.assert_called_once()


# ---------------------------------------------------------------------------
# check_gpu_torch_compat
# ---------------------------------------------------------------------------


class TestCheckGpuTorchCompat:
    """Gate GPUs by the torch build's actual compute capability support (CRKY-188).

    Tests the static-fallback path by patching _torch_arch_list to return
    None (unavailable), then the dynamic path with a stubbed arch list.
    """

    def _make(self, cc: str) -> GPUInfo:
        return GPUInfo(index=0, name="Test GPU", vram_total_gb=24.0, vram_free_gb=24.0, compute_capability=cc)

    # --- static-fallback path (torch arch list unavailable) ---

    def test_pascal_rejected_via_static_floor(self, monkeypatch):
        monkeypatch.setattr("device_utils._torch_arch_list", lambda: None)
        ok, reason = check_gpu_torch_compat(self._make("6.1"))
        assert ok is False
        assert "6.1" in reason
        assert "below the minimum" in reason

    def test_maxwell_rejected_via_static_floor(self, monkeypatch):
        monkeypatch.setattr("device_utils._torch_arch_list", lambda: None)
        ok, reason = check_gpu_torch_compat(self._make("5.2"))
        assert ok is False
        assert "5.2" in reason

    def test_volta_accepted_via_static_floor(self, monkeypatch):
        monkeypatch.setattr("device_utils._torch_arch_list", lambda: None)
        ok, _ = check_gpu_torch_compat(self._make("7.0"))
        assert ok is True

    # --- dynamic path (torch reports its actual arch list) ---

    def test_pascal_accepted_when_torch_has_sm_61(self, monkeypatch):
        """If torch was built with Pascal kernels, Pascal should pass the gate."""
        monkeypatch.setattr(
            "device_utils._torch_arch_list",
            lambda: [(6, 1), (7, 0), (7, 5), (8, 0), (8, 6)],
        )
        ok, reason = check_gpu_torch_compat(self._make("6.1"))
        assert ok is True, f"expected Pascal accepted, got: {reason}"

    def test_pascal_rejected_when_torch_lacks_sm_61(self, monkeypatch):
        """Modern torch wheel without Pascal: 1080 Ti rejected with the actual supported list."""
        monkeypatch.setattr(
            "device_utils._torch_arch_list",
            lambda: [(7, 0), (7, 5), (8, 0), (8, 6), (9, 0), (12, 0)],
        )
        ok, reason = check_gpu_torch_compat(self._make("6.1"))
        assert ok is False
        assert "sm_70" in reason  # supported list should be listed

    def test_blackwell_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "device_utils._torch_arch_list",
            lambda: [(7, 0), (7, 5), (8, 0), (12, 0)],
        )
        ok, _ = check_gpu_torch_compat(self._make("12.0"))
        assert ok is True

    # --- non-NVIDIA / unparseable cases ---

    def test_empty_cc_not_gated(self):
        ok, reason = check_gpu_torch_compat(self._make(""))
        assert ok is True
        assert reason == ""

    def test_unparseable_cc_not_gated(self):
        ok, _ = check_gpu_torch_compat(self._make("gfx1030"))
        assert ok is True

    def test_min_constant_is_sensible(self):
        # The fallback constant should match the most common torch wheel floor.
        # Update deliberately if torch drops further.
        assert MIN_CUDA_COMPUTE_CAPABILITY == (7, 0)


# ---------------------------------------------------------------------------
# _parse_nvidia_availability
# ---------------------------------------------------------------------------


class TestNvidiaSmiParsing:
    """Check that NVIDIA smi output is parsed correctly"""

    def test_parse_nvidia_availability_success(self):
        # Input: util, free, draw, limit
        # Expected: eff_util = 80 * (100/200) = 40%
        stdout = "80, 8192, 100, 200"
        available, reason = _parse_nvidia_availability(stdout, 0, 0)
        assert available is True
        assert reason == "ok"

    def test_parse_nvidia_availability_gpu_util_busy(self):
        # Input: util, free, draw, limit
        # Expected: eff_util = 60 * (200/200) = 60% (should be busy)
        stdout = "60, 8192, 200, 200"
        available, reason = _parse_nvidia_availability(stdout, 0, 0)
        assert available is False
        assert "busy" in reason

    def test_parse_nvidia_availability_low_vram_busy(self):
        stdout = "10, 512, 100, 200"  # 512 MB free
        available, reason = _parse_nvidia_availability(stdout, 0, 1.0)  # Need 1GB
        assert available is False
        assert "low VRAM" in reason

    def test_parse_nvidia_availability_invalid_power_fallback(self):
        # power_draw/limit are "invalid" strings
        # eff_util = raw_util = 60 (should be busy)
        stdout = "60, 8192, [N/A], [N/A]"
        available, reason = _parse_nvidia_availability(stdout, 0, 0)
        assert available is False
        assert "busy" in reason

    def test_parse_nvidia_availability_malformed_input_short(self):
        stdout = "10, 8192"
        assert _parse_nvidia_availability(stdout, 0, 0) is None

    def test_parse_nvidia_availability_malformed_input_invalid_fields(self):
        stdout = "[N/A], [N/A], [N/A], [N/A]"
        assert _parse_nvidia_availability(stdout, 0, 0) is None

    def test_parse_nvidia_availability_calculation(self):
        # Input: util=100, free=8192, draw=3, limit=4
        # Expected: eff_util = 100 * (3/4) = 75%
        stdout = "100, 8192, 3, 4"
        available, reason = _parse_nvidia_availability(stdout, 0, 0)
        assert available is False
        assert "(75" in reason


# ---------------------------------------------------------------------------
# _query_nvidia_smi
# ---------------------------------------------------------------------------


class TestNvidiaSmiQuery:
    """Try to check that NVIDIA smi query is correct"""

    @pytest.mark.gpu
    def test_query_nvidia_smi(self):
        if not torch.cuda.is_available():
            pytest.skip("CUDA not available")

        try:
            query = _query_nvidia_smi(0)
        except (FileNotFoundError, TimeoutExpired) as e:
            pytest.skip(f"Could not succesfully query nvidia-smi\n{e}")

        try:
            parsed_query = _parse_nvidia_availability(query, 0, 0)

            assert parsed_query is None or isinstance(parsed_query, tuple), "Parsed result should be a tuple or None"

            if parsed_query is not None:
                assert len(parsed_query) == 2, "Tuple must contain exactly 2 elements"
                assert isinstance(parsed_query[0], bool), "First element must be a boolean"
                assert isinstance(parsed_query[1], str), "Second element must be a string"

        except Exception as e:
            pytest.fail(f"Encounted unexpected exception querying nvidia-smi\n{e}")

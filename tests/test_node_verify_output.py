"""Tests for NodeAgent._verify_output_produced (CRKY-189).

Guards against the silent-failure case where a job completes its inference
phase but produced no frames (torch op skipped on unsupported hardware, a
library crashed post-success, or the shard had zero input frames). Without
this guard those jobs ride the normal completion path and users see
"completed 0 frames" in the UI.
"""

from __future__ import annotations

import os

import pytest

from web.node.agent import NodeAgent


def _make_agent() -> NodeAgent:
    # Agent.__init__ touches config/env; we just need the method on the class.
    # Create a bare instance without running init.
    return NodeAgent.__new__(NodeAgent)


class TestVerifyOutputProduced:
    def test_gvm_empty_output_raises(self, tmp_path):
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        (clip_dir / "AlphaHint").mkdir()
        with pytest.raises(RuntimeError, match="empty"):
            _make_agent()._verify_output_produced("gvm_alpha", str(clip_dir))

    def test_gvm_missing_output_dir_raises(self, tmp_path):
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        # Note: AlphaHint directory was never created
        with pytest.raises(RuntimeError, match="never created"):
            _make_agent()._verify_output_produced("gvm_alpha", str(clip_dir))

    def test_gvm_with_output_passes(self, tmp_path):
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        alpha = clip_dir / "AlphaHint"
        alpha.mkdir()
        (alpha / "frame_0001.png").write_bytes(b"\x89PNG")
        # Should not raise
        _make_agent()._verify_output_produced("gvm_alpha", str(clip_dir))

    def test_videomama_shares_alpha_path(self, tmp_path):
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        (clip_dir / "AlphaHint").mkdir()
        with pytest.raises(RuntimeError):
            _make_agent()._verify_output_produced("videomama_alpha", str(clip_dir))

    def test_inference_any_output_pass_is_ok(self, tmp_path):
        clip_dir = tmp_path / "clip"
        out = clip_dir / "Output"
        out.mkdir(parents=True)
        # Only Matte has content — that's enough to be considered a real run
        matte = out / "Matte"
        matte.mkdir()
        (matte / "frame_0001.exr").write_bytes(b"OpenEXR")
        _make_agent()._verify_output_produced("inference", str(clip_dir))

    def test_inference_all_empty_raises(self, tmp_path):
        clip_dir = tmp_path / "clip"
        out = clip_dir / "Output"
        out.mkdir(parents=True)
        for sub in ("FG", "Matte", "Comp", "Processed"):
            (out / sub).mkdir()
        with pytest.raises(RuntimeError, match="no output"):
            _make_agent()._verify_output_produced("inference", str(clip_dir))

    def test_unknown_job_type_passes(self, tmp_path):
        """Unknown job types don't have a well-defined output dir; skip the check."""
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        # Should not raise even though nothing exists
        _make_agent()._verify_output_produced("some_future_type", str(clip_dir))

    def test_inference_file_not_dir(self, tmp_path):
        """Handles the case where the output path is present but unexpected."""
        clip_dir = tmp_path / "clip"
        (clip_dir / "Output").mkdir(parents=True)
        # Output/FG is a file, not a directory (shouldn't happen but be robust)
        (clip_dir / "Output" / "FG").write_bytes(b"")
        with pytest.raises(RuntimeError):
            _make_agent()._verify_output_produced("inference", str(clip_dir))


# Ensure os is imported (ruff F401 appeasement for readers):
assert os is os

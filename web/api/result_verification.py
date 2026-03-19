"""Result verification — duplicate processing for untrusted nodes (CRKY-26).

Randomly selects a percentage of frames to process on two different nodes.
Compares outputs to detect tampering or faulty hardware.

Only applies to HTTP-transfer nodes (shared storage nodes write to the
same directory, so duplicate processing would overwrite).

Configuration:
- CK_VERIFY_PERCENT: percentage of jobs to verify (default 5)
- CK_VERIFY_PSNR_THRESHOLD: minimum PSNR for frames to be considered
  matching (default 40 dB — allows minor floating-point differences)
"""

from __future__ import annotations

import logging
import os
import random

logger = logging.getLogger(__name__)

VERIFY_PERCENT = int(os.environ.get("CK_VERIFY_PERCENT", "5"))
VERIFY_PSNR_THRESHOLD = float(os.environ.get("CK_VERIFY_PSNR_THRESHOLD", "40"))


def should_verify_job() -> bool:
    """Randomly decide if a job should be verified (duplicate processed).

    Returns True VERIFY_PERCENT% of the time.
    """
    if VERIFY_PERCENT <= 0:
        return False
    return random.randint(1, 100) <= VERIFY_PERCENT


def create_verification_job(original_job_id: str, org_id: str | None = None) -> dict | None:
    """Create a verification copy of a job.

    The verification job processes the same frames but on a different node.
    Returns the verification job data, or None if verification is disabled.
    """
    from .database import get_storage

    storage = get_storage()
    verifications = storage.get_setting("verification_jobs", {})
    verifications[original_job_id] = {
        "original_job_id": original_job_id,
        "verification_job_id": None,  # Set when the verification job is created
        "status": "pending",
        "org_id": org_id,
    }
    storage.set_setting("verification_jobs", verifications)
    return verifications[original_job_id]


def record_verification_result(
    original_job_id: str,
    verification_job_id: str,
    passed: bool,
    details: dict | None = None,
) -> None:
    """Record the result of a verification comparison."""
    from .database import get_storage

    storage = get_storage()
    verifications = storage.get_setting("verification_jobs", {})
    if original_job_id in verifications:
        verifications[original_job_id]["verification_job_id"] = verification_job_id
        verifications[original_job_id]["status"] = "passed" if passed else "failed"
        verifications[original_job_id]["details"] = details or {}
        storage.set_setting("verification_jobs", verifications)

    if not passed:
        logger.warning(
            f"Verification FAILED for job {original_job_id} "
            f"(verified by {verification_job_id}): {details}"
        )


def compare_frames(frame_a_path: str, frame_b_path: str) -> tuple[bool, float]:
    """Compare two frames using PSNR.

    Returns (passed, psnr_value). Passed if PSNR >= threshold.
    """
    try:
        import cv2
        import numpy as np

        a = cv2.imread(frame_a_path, cv2.IMREAD_UNCHANGED)
        b = cv2.imread(frame_b_path, cv2.IMREAD_UNCHANGED)

        if a is None or b is None:
            return False, 0.0
        if a.shape != b.shape:
            return False, 0.0

        mse = np.mean((a.astype(float) - b.astype(float)) ** 2)
        if mse == 0:
            return True, float("inf")

        max_val = 255.0 if a.dtype == np.uint8 else 65535.0
        psnr = 10 * np.log10(max_val**2 / mse)

        return psnr >= VERIFY_PSNR_THRESHOLD, float(psnr)
    except Exception as e:
        logger.warning(f"Frame comparison failed: {e}")
        return False, 0.0

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

VERIFY_PERCENT = int(os.environ.get("CK_VERIFY_PERCENT", "5").strip())
VERIFY_PSNR_THRESHOLD = float(os.environ.get("CK_VERIFY_PSNR_THRESHOLD", "40").strip())


def should_verify_job() -> bool:
    """Randomly decide if a job should be verified (duplicate processed).

    Returns True VERIFY_PERCENT% of the time.
    """
    if VERIFY_PERCENT <= 0:
        return False
    return random.randint(1, 100) <= VERIFY_PERCENT


def create_verification_job(original_job_id: str, org_id: str | None = None) -> dict | None:
    """Create a verification record for a job.

    Idempotent: a second call for the same original_job_id is a no-op
    and returns the existing record. Uses INSERT ... ON CONFLICT DO
    NOTHING at the DB level so two parallel calls cannot both insert.
    """
    from .database import get_pg_conn

    record = {
        "original_job_id": original_job_id,
        "verification_job_id": None,
        "status": "pending",
        "org_id": org_id,
        "details": {},
    }

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.verification_jobs (original_job_id, org_id, status, details)
                   VALUES (%s, %s, 'pending', '{}'::jsonb)
                   ON CONFLICT (original_job_id) DO NOTHING
                   RETURNING original_job_id, verification_job_id, status, org_id, details""",
                (original_job_id, org_id),
            )
            row = cur.fetchone()
            if row is None:
                # Row already existed — return the existing state.
                cur.execute(
                    """SELECT original_job_id, verification_job_id, status, org_id, details
                       FROM ck.verification_jobs WHERE original_job_id = %s""",
                    (original_job_id,),
                )
                row = cur.fetchone()
            cur.close()
            if row:
                return {
                    "original_job_id": row[0],
                    "verification_job_id": row[1],
                    "status": row[2],
                    "org_id": row[3],
                    "details": row[4] or {},
                }
            return record

    from .database import get_storage

    storage = get_storage()
    verifications = storage.get_setting("verification_jobs", {})
    verifications.setdefault(original_job_id, record)
    storage.set_setting("verification_jobs", verifications)
    return verifications[original_job_id]


def record_verification_result(
    original_job_id: str,
    verification_job_id: str,
    passed: bool,
    details: dict | None = None,
) -> None:
    """Record the result of a verification comparison.

    First-write-wins: the update is gated on ``status = 'pending'`` so a
    concurrent second write (retry, duplicate callback) cannot overwrite
    an already-recorded verdict. In particular this prevents a late
    ``passed`` from silently clobbering a just-recorded ``failed``,
    which would let tampered frames into the final render.
    """
    import json

    from .database import get_pg_conn

    status = "passed" if passed else "failed"
    details_json = json.dumps(details or {})

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """UPDATE ck.verification_jobs
                   SET verification_job_id = %s,
                       status = %s,
                       details = %s::jsonb,
                       completed_at = NOW()
                   WHERE original_job_id = %s AND status = 'pending'""",
                (verification_job_id, status, details_json, original_job_id),
            )
            cur.close()
            if not passed:
                logger.warning(
                    "Verification FAILED for job %s (verified by %s): %s",
                    original_job_id,
                    verification_job_id,
                    details,
                )
            return

    from .database import get_storage

    storage = get_storage()
    verifications = storage.get_setting("verification_jobs", {})
    existing = verifications.get(original_job_id)
    if existing and existing.get("status") == "pending":
        existing["verification_job_id"] = verification_job_id
        existing["status"] = status
        existing["details"] = details or {}
        storage.set_setting("verification_jobs", verifications)

    if not passed:
        logger.warning(
            "Verification FAILED for job %s (verified by %s): %s",
            original_job_id,
            verification_job_id,
            details,
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

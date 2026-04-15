"""Background monthly credit grant daemon (CRKY-185).

Wakes every ``_GRANT_CHECK_INTERVAL`` seconds and runs
``run_monthly_grant_cycle``. The cycle itself is idempotent within a
calendar month (the ``ck.credit_grants`` ledger short-circuits
re-runs), so the interval is essentially "maximum latency between a
calendar month rolling over and the new grants appearing in org
balances". 6 hours is the default; the first run happens shortly
after startup so restarts at the start of a new month don't have to
wait an interval before the grants apply.

The daemon is a no-op when ``MONTHLY_CREDITS == 0`` — operators flip
``CK_MONTHLY_CREDITS=0`` to opt out of the recurring grant entirely.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Re-run cadence. The ledger makes this cheap — re-runs inside the
# same period are pure ON CONFLICT DO NOTHING and skip add_contributed.
_GRANT_CHECK_INTERVAL = 6 * 3600

# Small initial delay so the daemon doesn't fire inside the critical
# startup path. Gives the DB connection pool and the org store time
# to finish warming up.
_GRANT_STARTUP_DELAY = 60


def grant_loop(stop_event: threading.Event) -> None:
    """Background thread: run monthly grants on interval."""
    from .audit import audit_log
    from .gpu_credits import MONTHLY_CREDITS, run_monthly_grant_cycle

    if MONTHLY_CREDITS <= 0:
        logger.info("Monthly credit grant daemon disabled (CK_MONTHLY_CREDITS=0)")
        return

    logger.info(
        "Monthly credit grant daemon started "
        f"(amount={MONTHLY_CREDITS:.0f}s/org/month, interval={_GRANT_CHECK_INTERVAL}s)"
    )

    stop_event.wait(_GRANT_STARTUP_DELAY)
    if stop_event.is_set():
        return

    while not stop_event.is_set():
        try:
            result = run_monthly_grant_cycle()
            if result.get("disabled"):
                return
            if result.get("granted", 0) > 0:
                logger.info(
                    "Monthly grant cycle %s: granted=%d skipped=%d total=%.1fh",
                    result["period"],
                    result["granted"],
                    result["skipped"],
                    result["total_seconds"] / 3600.0,
                )
                try:
                    audit_log(
                        "credits.monthly_grant",
                        actor="system",
                        target_type="cycle",
                        target_id=result["period"],
                        details={
                            "granted": result["granted"],
                            "skipped": result["skipped"],
                            "total_seconds": result["total_seconds"],
                            "per_org_seconds": MONTHLY_CREDITS,
                        },
                    )
                except Exception:
                    logger.debug("Failed to record audit entry for monthly grant", exc_info=True)
            else:
                logger.debug(
                    "Monthly grant cycle %s: no-op (granted=0, skipped=%d)",
                    result["period"],
                    result["skipped"],
                )
        except Exception:
            logger.exception("Monthly grant cycle failed")

        stop_event.wait(_GRANT_CHECK_INTERVAL)


def start_grant_scheduler(stop_event: threading.Event) -> threading.Thread:
    """Start the monthly grant daemon thread."""
    thread = threading.Thread(
        target=grant_loop,
        args=(stop_event,),
        daemon=True,
        name="credit-grant-scheduler",
    )
    thread.start()
    return thread

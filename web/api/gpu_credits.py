"""GPU credit tracking — contributed vs consumed per org (CRKY-6).

Tracks GPU-seconds contributed by nodes and consumed by jobs per org.
Used by the credit enforcement system (CRKY-37) to ensure fair
resource sharing.

Credits are stored in ck.gpu_credits (org_id keyed). When Postgres is
not available, falls back to the JSON storage backend.

Monthly recurring grants (CRKY-185) are gated by a separate ledger
(ck.credit_grants) so the sweep is idempotent within a calendar month
and multi-worker safe via ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Starter credits granted to new orgs on user approval (GPU-seconds).
# Default 3600 = 1 hour. Set to 0 to disable.
STARTER_CREDITS = float(os.environ.get("CK_STARTER_CREDITS", "3600").strip())

# Monthly recurring credits granted to every non-pending org once per
# calendar month. Default 3600 = 1 hour per month. Set to 0 to disable
# the recurring grant daemon entirely. Enabled by default (CRKY-185).
MONTHLY_CREDITS = float(os.environ.get("CK_MONTHLY_CREDITS", "3600").strip())


@dataclass
class OrgCredits:
    """Credit balance for an org."""

    org_id: str
    contributed_seconds: float = 0.0
    consumed_seconds: float = 0.0

    @property
    def balance(self) -> float:
        """Net balance: contributed - consumed. Positive = surplus."""
        return self.contributed_seconds - self.consumed_seconds

    @property
    def ratio(self) -> float:
        """Consumption ratio: consumed / contributed. <1.0 = surplus."""
        if self.contributed_seconds <= 0:
            return float("inf") if self.consumed_seconds > 0 else 0.0
        return self.consumed_seconds / self.contributed_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "contributed_seconds": round(self.contributed_seconds, 1),
            "consumed_seconds": round(self.consumed_seconds, 1),
            "balance_seconds": round(self.balance, 1),
            "contributed_hours": round(self.contributed_seconds / 3600, 2),
            "consumed_hours": round(self.consumed_seconds / 3600, 2),
            "ratio": round(self.ratio, 3) if self.ratio != float("inf") else None,
        }


def get_org_credits(org_id: str) -> OrgCredits:
    """Get the credit balance for an org."""
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                "SELECT contributed_seconds, consumed_seconds FROM ck.gpu_credits WHERE org_id = %s",
                (org_id,),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                return OrgCredits(org_id=org_id, contributed_seconds=row[0], consumed_seconds=row[1])

    # Fallback: JSON storage
    from .database import get_storage

    storage = get_storage()
    credits = storage.get_setting("gpu_credits", {})
    data = credits.get(org_id, {})
    return OrgCredits(
        org_id=org_id,
        contributed_seconds=data.get("contributed_seconds", 0.0),
        consumed_seconds=data.get("consumed_seconds", 0.0),
    )


def add_contributed(org_id: str, seconds: float) -> None:
    """Add contributed GPU-seconds for an org (from node processing)."""
    if seconds <= 0 or not org_id:
        return

    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.gpu_credits (org_id, contributed_seconds, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (org_id) DO UPDATE
                   SET contributed_seconds = ck.gpu_credits.contributed_seconds + %s,
                       updated_at = NOW()""",
                (org_id, seconds, seconds),
            )
            cur.close()
            return

    # Fallback: JSON storage
    from .database import get_storage

    storage = get_storage()
    credits = storage.get_setting("gpu_credits", {})
    if org_id not in credits:
        credits[org_id] = {"contributed_seconds": 0.0, "consumed_seconds": 0.0}
    credits[org_id]["contributed_seconds"] += seconds
    storage.set_setting("gpu_credits", credits)


def add_consumed(org_id: str, seconds: float) -> None:
    """Add consumed GPU-seconds for an org (from job completion)."""
    if seconds <= 0 or not org_id:
        return

    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.gpu_credits (org_id, consumed_seconds, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (org_id) DO UPDATE
                   SET consumed_seconds = ck.gpu_credits.consumed_seconds + %s,
                       updated_at = NOW()""",
                (org_id, seconds, seconds),
            )
            cur.close()
            return

    # Fallback: JSON storage
    from .database import get_storage

    storage = get_storage()
    credits = storage.get_setting("gpu_credits", {})
    if org_id not in credits:
        credits[org_id] = {"contributed_seconds": 0.0, "consumed_seconds": 0.0}
    credits[org_id]["consumed_seconds"] += seconds
    storage.set_setting("gpu_credits", credits)


def get_all_credits() -> list[OrgCredits]:
    """Get credits for all orgs (admin view)."""
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute("SELECT org_id, contributed_seconds, consumed_seconds FROM ck.gpu_credits")
            result = [
                OrgCredits(org_id=row[0], contributed_seconds=row[1], consumed_seconds=row[2]) for row in cur.fetchall()
            ]
            cur.close()
            return result

    # Fallback: JSON storage
    from .database import get_storage

    storage = get_storage()
    credits = storage.get_setting("gpu_credits", {})
    return [
        OrgCredits(
            org_id=oid,
            contributed_seconds=data.get("contributed_seconds", 0.0),
            consumed_seconds=data.get("consumed_seconds", 0.0),
        )
        for oid, data in credits.items()
    ]


# --- Monthly recurring grants (CRKY-185) ---


def _current_period() -> str:
    """Current grant period identifier. One period = one UTC calendar month."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m")


def grant_monthly_credits(org_id: str, period: str, seconds: float) -> bool:
    """Grant monthly credits for an org if not already granted this period.

    The ledger row in ``ck.credit_grants`` is the source of truth. Insert
    wins atomically: if the row already exists, ON CONFLICT DO NOTHING
    short-circuits and we do NOT bump the balance. Multiple workers
    racing the same (org_id, period) is safe — exactly one INSERT wins
    and only that worker calls add_contributed.

    Returns:
        True if a grant was applied (insert won and balance bumped),
        False if skipped because this org already has a ledger row for
        the period, or the inputs are zero/empty.
    """
    if seconds <= 0 or not org_id or not period:
        return False

    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.credit_grants (org_id, grant_type, period, seconds)
                   VALUES (%s, 'monthly', %s, %s)
                   ON CONFLICT (org_id, grant_type, period) DO NOTHING
                   RETURNING org_id""",
                (org_id, period, seconds),
            )
            won = cur.fetchone() is not None
            cur.close()
            if won:
                add_contributed(org_id, seconds)
            return won

    # Dev/test fallback: the JSON path is single-process anyway, so a
    # naive in-memory dedupe key is sufficient.
    from .database import get_storage

    storage = get_storage()
    ledger = storage.get_setting("credit_grants", {})
    key = f"{org_id}:monthly:{period}"
    if key in ledger:
        return False
    ledger[key] = {
        "org_id": org_id,
        "grant_type": "monthly",
        "period": period,
        "seconds": seconds,
        "granted_at": time.time(),
    }
    storage.set_setting("credit_grants", ledger)
    add_contributed(org_id, seconds)
    return True


def run_monthly_grant_cycle(seconds: float | None = None, period: str | None = None) -> dict:
    """Run one monthly grant sweep over every org.

    Idempotent within a calendar month via the credit_grants ledger.
    Returns a summary dict suitable for logging and audit.

    Pending users' personal orgs are skipped, matching the existing
    approve_user gate that already withholds starter credits from
    unapproved accounts. Non-personal orgs are always granted.

    Args:
        seconds: per-org grant amount. Defaults to ``MONTHLY_CREDITS``.
                 Passing 0 disables the cycle.
        period: override the period string. Defaults to the current UTC
                month (``YYYY-MM``).
    """
    amount = MONTHLY_CREDITS if seconds is None else float(seconds)
    if amount <= 0:
        return {
            "granted": 0,
            "skipped": 0,
            "total_seconds": 0.0,
            "period": period or _current_period(),
            "disabled": True,
        }

    from .orgs import get_org_store
    from .users import get_user_store

    period = period or _current_period()
    org_store = get_org_store()
    user_store = get_user_store()

    granted = 0
    skipped = 0

    for org in org_store.list_orgs():
        # Personal orgs for pending users don't get starter credits on
        # signup, so they shouldn't get recurring ones either. Match
        # the approve_user flow at routes/admin.py.
        if org.personal:
            owner = user_store.get_user(org.owner_id)
            if owner is None or owner.tier == "pending" or owner.tier == "rejected":
                skipped += 1
                continue

        if grant_monthly_credits(org.org_id, period, amount):
            granted += 1
        else:
            skipped += 1

    return {
        "granted": granted,
        "skipped": skipped,
        "total_seconds": granted * amount,
        "period": period,
        "disabled": False,
    }

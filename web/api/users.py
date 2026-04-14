"""User management and approval workflow (CRKY-2).

Tracks users and provides tier management. When a user signs up via the
invite flow they're recorded with tier=pending. Admins approve (promoting
to member) or reject users through the API.

Tier updates are written to both:
1. ck.users (per-row table) — for listing/querying from the admin UI
2. Supabase auth.users.raw_app_meta_data — so the JWT reflects the new tier

Historical note: records used to live as a single JSON blob in
ck.settings['users']. Multiple web containers doing read-modify-write on
that one row raced and clobbered each other, silently losing signups
(CRKY fix 2026-04). The per-row table makes every mutation a single
atomic statement, eliminating the race. On first use, `_ensure_migrated`
copies any legacy blob into ck.users and backfills missing users from
auth.users so nothing is lost.

When Postgres is not available (JSONBackend, no-auth local mode) the
store falls back to the legacy blob layout — single-process, no race.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserRecord:
    """Local user record for tracking and approval."""

    user_id: str
    email: str
    tier: str = "pending"
    name: str = ""
    company: str = ""
    role: str = ""
    use_case: str = ""
    signed_up_at: float = 0.0
    approved_at: float = 0.0
    approved_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "tier": self.tier,
            "name": self.name,
            "company": self.company,
            "role": self.role,
            "use_case": self.use_case,
            "signed_up_at": self.signed_up_at,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
        }


_COLS = "user_id, email, tier, name, company, role, use_case, signed_up_at, approved_at, approved_by"


def _row_to_record(row: tuple) -> UserRecord:
    return UserRecord(
        user_id=row[0],
        email=row[1],
        tier=row[2],
        name=row[3] or "",
        company=row[4] or "",
        role=row[5] or "",
        use_case=row[6] or "",
        signed_up_at=float(row[7] or 0),
        approved_at=float(row[8] or 0),
        approved_by=row[9] or "",
    )


class UserStore:
    """Manages user records. Prefers ck.users when Postgres is available."""

    def __init__(self):
        from .database import PostgresBackend, get_storage

        self._storage = get_storage()
        self._use_pg = isinstance(self._storage, PostgresBackend)
        self._migrated = False

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _ensure_migrated(self) -> None:
        """Lazily run the one-time blob → table migration and auth.users backfill.

        Idempotent: safe to run on every container start. ON CONFLICT DO
        NOTHING guards against duplicate inserts if multiple containers
        race the first call.
        """
        if self._migrated or not self._use_pg:
            return
        from .database import get_pg_conn

        try:
            with get_pg_conn() as conn:
                if conn is None:
                    return
                cur = conn.cursor()

                # Ensure the table exists. init-db.sql only runs on first
                # Postgres boot, so databases initialized before this fix
                # need the table created at runtime.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ck.users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        tier TEXT NOT NULL DEFAULT 'pending',
                        name TEXT NOT NULL DEFAULT '',
                        company TEXT NOT NULL DEFAULT '',
                        role TEXT NOT NULL DEFAULT '',
                        use_case TEXT NOT NULL DEFAULT '',
                        signed_up_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        approved_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        approved_by TEXT NOT NULL DEFAULT ''
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ck_users_email ON ck.users (email)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ck_users_tier ON ck.users (tier)")

                # Copy legacy blob → ck.users. Preserve whatever data the blob has.
                cur.execute("SELECT value FROM ck.settings WHERE key = 'users'")
                row = cur.fetchone()
                if row and row[0]:
                    legacy: dict = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    for rec in legacy.values():
                        if not rec.get("user_id") or not rec.get("email"):
                            continue
                        cur.execute(
                            f"""INSERT INTO ck.users ({_COLS})
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (user_id) DO NOTHING""",
                            (
                                rec.get("user_id"),
                                rec.get("email"),
                                rec.get("tier") or "pending",
                                rec.get("name") or "",
                                rec.get("company") or "",
                                rec.get("role") or "",
                                rec.get("use_case") or "",
                                float(rec.get("signed_up_at") or 0),
                                float(rec.get("approved_at") or 0),
                                rec.get("approved_by") or "",
                            ),
                        )

                # Backfill from auth.users for anyone still missing.
                # This is what recovers users lost to the old blob race.
                cur.execute("""
                    INSERT INTO ck.users
                        (user_id, email, tier, name, signed_up_at)
                    SELECT
                        id::text,
                        email,
                        COALESCE(raw_app_meta_data->>'tier', 'pending'),
                        COALESCE(raw_user_meta_data->>'name', ''),
                        EXTRACT(EPOCH FROM created_at)
                    FROM auth.users
                    WHERE email IS NOT NULL
                    ON CONFLICT (user_id) DO NOTHING
                """)

                cur.close()
            self._migrated = True
            logger.info("ck.users migration/backfill complete")
        except Exception as e:
            logger.warning(f"ck.users migration failed: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Blob fallback (JSONBackend / no-Postgres mode)
    # ------------------------------------------------------------------

    def _blob_load(self) -> dict[str, dict]:
        return self._storage.get_setting("users", {})

    def _blob_save(self, users: dict[str, dict]) -> None:
        self._storage.set_setting("users", users)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_signup(
        self,
        user_id: str,
        email: str,
        name: str = "",
        company: str = "",
        role: str = "",
        use_case: str = "",
    ) -> UserRecord:
        """Record a new user signup. Idempotent: returns existing record if present."""
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    cur.execute(
                        f"""INSERT INTO ck.users ({_COLS})
                            VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, 0, '')
                            ON CONFLICT (user_id) DO NOTHING
                            RETURNING {_COLS}""",
                        (user_id, email, name, company, role, use_case, time.time()),
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(f"SELECT {_COLS} FROM ck.users WHERE user_id = %s", (user_id,))
                        row = cur.fetchone()
                    cur.close()
                    if row is not None:
                        return _row_to_record(row)

        # Blob fallback
        users = self._blob_load()
        if user_id in users:
            return UserRecord(**users[user_id])
        record = UserRecord(
            user_id=user_id,
            email=email,
            tier="pending",
            name=name,
            company=company,
            role=role,
            use_case=use_case,
            signed_up_at=time.time(),
        )
        users[user_id] = record.to_dict()
        self._blob_save(users)
        return record

    def get_user(self, user_id: str) -> UserRecord | None:
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    cur.execute(f"SELECT {_COLS} FROM ck.users WHERE user_id = %s", (user_id,))
                    row = cur.fetchone()
                    cur.close()
                    return _row_to_record(row) if row else None
        data = self._blob_load().get(user_id)
        return UserRecord(**data) if data else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    cur.execute(f"SELECT {_COLS} FROM ck.users WHERE email = %s LIMIT 1", (email,))
                    row = cur.fetchone()
                    cur.close()
                    return _row_to_record(row) if row else None
        for data in self._blob_load().values():
            if data.get("email") == email:
                return UserRecord(**data)
        return None

    def link_uuid(self, email: str, real_uuid: str) -> UserRecord | None:
        """Re-key an email-based signup record to the real Supabase UUID (CRKY-61).

        Called on first authenticated request to fix the signup-time
        mismatch where user_id was set to the email address.
        """
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    # Real UUID already present — nothing to do.
                    cur.execute(f"SELECT {_COLS} FROM ck.users WHERE user_id = %s", (real_uuid,))
                    row = cur.fetchone()
                    if row:
                        cur.close()
                        return _row_to_record(row)
                    # Re-key the email row if it exists.
                    cur.execute(
                        f"""UPDATE ck.users SET user_id = %s
                            WHERE user_id = %s
                            RETURNING {_COLS}""",
                        (real_uuid, email),
                    )
                    row = cur.fetchone()
                    cur.close()
                    return _row_to_record(row) if row else None

        users = self._blob_load()
        if real_uuid in users:
            return UserRecord(**users[real_uuid])
        email_record = users.get(email)
        if not email_record:
            return None
        del users[email]
        email_record["user_id"] = real_uuid
        users[real_uuid] = email_record
        self._blob_save(users)
        return UserRecord(**email_record)

    def list_users(self, tier_filter: str | None = None) -> list[UserRecord]:
        """List all users, optionally filtered by tier."""
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    if tier_filter:
                        cur.execute(
                            f"""SELECT {_COLS} FROM ck.users
                                WHERE tier = %s
                                ORDER BY signed_up_at DESC""",
                            (tier_filter,),
                        )
                    else:
                        cur.execute(f"SELECT {_COLS} FROM ck.users ORDER BY signed_up_at DESC")
                    rows = cur.fetchall()
                    cur.close()
                    return [_row_to_record(r) for r in rows]

        records = [UserRecord(**v) for v in self._blob_load().values()]
        if tier_filter:
            records = [r for r in records if r.tier == tier_filter]
        return records

    def set_tier(self, user_id: str, tier: str, approved_by: str = "") -> UserRecord | None:
        """Update a user's tier. Also mirrors to Supabase auth.users."""
        self._ensure_migrated()
        result: UserRecord | None = None
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    if tier == "pending":
                        cur.execute(
                            f"""UPDATE ck.users SET tier = %s
                                WHERE user_id = %s
                                RETURNING {_COLS}""",
                            (tier, user_id),
                        )
                    else:
                        cur.execute(
                            f"""UPDATE ck.users
                                SET tier = %s, approved_at = %s, approved_by = %s
                                WHERE user_id = %s
                                RETURNING {_COLS}""",
                            (tier, time.time(), approved_by, user_id),
                        )
                    row = cur.fetchone()
                    cur.close()
                    result = _row_to_record(row) if row else None
        else:
            users = self._blob_load()
            if user_id in users:
                users[user_id]["tier"] = tier
                if tier != "pending":
                    users[user_id]["approved_at"] = time.time()
                    users[user_id]["approved_by"] = approved_by
                self._blob_save(users)
                result = UserRecord(**users[user_id])

        _update_supabase_tier(user_id, tier)
        return result

    def update_name(self, user_id: str, name: str) -> UserRecord | None:
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    cur.execute(
                        f"""UPDATE ck.users SET name = %s
                            WHERE user_id = %s
                            RETURNING {_COLS}""",
                        (name, user_id),
                    )
                    row = cur.fetchone()
                    cur.close()
                    return _row_to_record(row) if row else None

        users = self._blob_load()
        if user_id not in users:
            return None
        users[user_id]["name"] = name
        self._blob_save(users)
        return UserRecord(**users[user_id])

    def delete_user(self, user_id: str) -> bool:
        self._ensure_migrated()
        if self._use_pg:
            from .database import get_pg_conn

            with get_pg_conn() as conn:
                if conn is not None:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM ck.users WHERE user_id = %s", (user_id,))
                    deleted = cur.rowcount > 0
                    cur.close()
                    return deleted

        users = self._blob_load()
        if user_id not in users:
            return False
        del users[user_id]
        self._blob_save(users)
        return True


def _update_supabase_tier(user_id: str, tier: str) -> None:
    """Mirror a tier change into Supabase auth.users.raw_app_meta_data.

    Uses the shared connection pool (CRKY-64). Fails silently if Postgres
    is unavailable.
    """
    from .database import get_pg_conn

    try:
        with get_pg_conn() as conn:
            if conn is None:
                logger.debug("No Postgres connection — skipping Supabase tier update")
                return
            cur = conn.cursor()
            meta_patch = json.dumps({"tier": tier})
            cur.execute(
                "UPDATE auth.users SET raw_app_meta_data = raw_app_meta_data || %s::jsonb WHERE id = %s::uuid",
                (meta_patch, user_id),
            )
            updated = cur.rowcount
            cur.close()
            if updated:
                logger.info(f"Updated Supabase tier for {user_id} to {tier}")
            else:
                logger.warning(f"No Supabase auth.users row found for {user_id}")
    except Exception as e:
        logger.warning(f"Failed to update Supabase tier for {user_id}: {e}")


# Singleton
_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store

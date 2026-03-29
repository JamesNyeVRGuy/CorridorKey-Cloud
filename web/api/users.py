"""User management and approval workflow (CRKY-2).

Tracks users locally and provides tier management. When a user signs up
via the invite flow, they're recorded here with tier=pending. Admins
approve (promoting to member) or reject users through the API.

Tier updates are written to both:
1. Local storage (ck_settings key "users") — for listing/querying
2. Supabase auth.users.raw_app_meta_data — so the JWT reflects the new tier

The Supabase update requires CK_DATABASE_URL to be set (pointing at the
same Postgres instance as GoTrue). Without it, only local storage is updated
and the user must re-login to pick up the new tier from a refreshed JWT.
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


class UserStore:
    """Manages local user records backed by the database abstraction."""

    def __init__(self):
        from .database import get_storage

        self._storage = get_storage()

    def _load_users(self) -> dict[str, dict]:
        return self._storage.get_setting("users", {})

    def _save_users(self, users: dict[str, dict]) -> None:
        self._storage.set_setting("users", users)

    def record_signup(
        self,
        user_id: str,
        email: str,
        name: str = "",
        company: str = "",
        role: str = "",
        use_case: str = "",
    ) -> UserRecord:
        """Record a new user signup."""
        users = self._load_users()
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
        self._save_users(users)
        return record

    def get_user(self, user_id: str) -> UserRecord | None:
        users = self._load_users()
        data = users.get(user_id)
        return UserRecord(**data) if data else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        """Look up a user by email (searches all records)."""
        users = self._load_users()
        for data in users.values():
            if data.get("email") == email:
                return UserRecord(**data)
        return None

    def link_uuid(self, email: str, real_uuid: str) -> UserRecord | None:
        """Replace the email-based key with the real Supabase UUID.

        Called on first authenticated request to fix the signup-time
        mismatch where user_id was set to email (CRKY-61).
        """
        users = self._load_users()
        # Already linked?
        if real_uuid in users:
            return UserRecord(**users[real_uuid])
        # Find by email key
        email_record = users.get(email)
        if not email_record:
            return None
        # Re-key: remove email entry, add UUID entry
        del users[email]
        email_record["user_id"] = real_uuid
        users[real_uuid] = email_record
        self._save_users(users)
        return UserRecord(**email_record)

    def list_users(self, tier_filter: str | None = None) -> list[UserRecord]:
        """List all users, optionally filtered by tier."""
        users = self._load_users()
        records = [UserRecord(**v) for v in users.values()]
        if tier_filter:
            records = [r for r in records if r.tier == tier_filter]
        return records

    def set_tier(self, user_id: str, tier: str, approved_by: str = "") -> UserRecord | None:
        """Update a user's tier in local storage and Supabase."""
        users = self._load_users()
        if user_id not in users:
            return None
        users[user_id]["tier"] = tier
        if tier != "pending":
            users[user_id]["approved_at"] = time.time()
            users[user_id]["approved_by"] = approved_by
        self._save_users(users)

        # Also update Supabase auth.users if we have a DB connection
        _update_supabase_tier(user_id, tier)

        return UserRecord(**users[user_id])

    def update_name(self, user_id: str, name: str) -> UserRecord | None:
        """Update a user's display name."""
        users = self._load_users()
        if user_id not in users:
            return None
        users[user_id]["name"] = name
        self._save_users(users)
        return UserRecord(**users[user_id])

    def delete_user(self, user_id: str) -> bool:
        """Remove a user record."""
        users = self._load_users()
        if user_id not in users:
            return False
        del users[user_id]
        self._save_users(users)
        return True


def _update_supabase_tier(user_id: str, tier: str) -> None:
    """Update the user's tier in Supabase auth.users.raw_app_meta_data.

    Uses the shared connection pool (CRKY-64) instead of creating a
    separate unmanaged connection. Fails silently if Postgres is not
    available.
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

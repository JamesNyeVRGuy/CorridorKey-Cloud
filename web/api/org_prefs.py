"""Per-org processing preferences (CRKY-176).

Thin store around ck.org_preferences with a JSON-blob fallback. Each
row is one org's preferences dict, so updates for different orgs are
independent writes and cannot clobber each other the way the legacy
``ck.settings["org_preferences"]`` blob did.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULTS: dict[str, Any] = {
    "allow_shared_nodes": True,
}


def _merge_defaults(prefs: dict[str, Any]) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    out.update(prefs or {})
    return out


def get_preferences(org_id: str) -> dict[str, Any]:
    """Get preferences for a single org, with defaults applied."""
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                "SELECT preferences FROM ck.org_preferences WHERE org_id = %s",
                (org_id,),
            )
            row = cur.fetchone()
            cur.close()
            return _merge_defaults(row[0] if row else {})

    from .database import get_storage

    all_prefs = get_storage().get_setting("org_preferences", {})
    return _merge_defaults(all_prefs.get(org_id, {}))


def update_preferences(org_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the org's preferences and return the new state.

    Postgres path uses INSERT ... ON CONFLICT DO UPDATE with a
    jsonb-level merge (``preferences || EXCLUDED.preferences``) so a
    parallel write for a different org is a different row and a
    parallel write for the same org merges at the jsonb level instead
    of overwriting the whole preference dict.
    """
    from .database import get_pg_conn

    updates = {k: v for k, v in (updates or {}).items() if v is not None}

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.org_preferences (org_id, preferences, updated_at)
                   VALUES (%s, %s::jsonb, NOW())
                   ON CONFLICT (org_id) DO UPDATE SET
                       preferences = ck.org_preferences.preferences || EXCLUDED.preferences,
                       updated_at = NOW()
                   RETURNING preferences""",
                (org_id, json.dumps(updates)),
            )
            row = cur.fetchone()
            cur.close()
            return _merge_defaults(row[0] if row else updates)

    from .database import get_storage

    storage = get_storage()
    all_prefs = storage.get_setting("org_preferences", {})
    current = all_prefs.get(org_id, {})
    current.update(updates)
    all_prefs[org_id] = current
    storage.set_setting("org_preferences", all_prefs)
    return _merge_defaults(current)


def get_orgs_disallowing_shared_nodes() -> set[str]:
    """Return org_ids that have opted out of shared-node dispatch.

    Used by the job dispatch path to build an exclusion list when a
    shared node asks for a job.
    """
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """SELECT org_id FROM ck.org_preferences
                   WHERE (preferences->>'allow_shared_nodes')::boolean = FALSE"""
            )
            rows = cur.fetchall()
            cur.close()
            return {r[0] for r in rows}

    from .database import get_storage

    all_prefs = get_storage().get_setting("org_preferences", {})
    return {oid for oid, prefs in all_prefs.items() if not prefs.get("allow_shared_nodes", True)}

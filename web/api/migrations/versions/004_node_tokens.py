"""Add node_tokens table (CRKY-170).

Revision ID: 004
Revises: 003
Create Date: 2026-04-14

Moves per-node auth tokens out of the ck.settings JSON blob into a
real table so that generate / validate / revoke can be atomic at the
database level. Previously all three operations did a non-atomic
load -> mutate -> save on a single "node_tokens" blob, which meant a
revocation could be silently overwritten by a concurrent validate().

The migration backfills existing tokens from ck.settings["node_tokens"]
into the new table. The blob key is intentionally left in place so a
rollback has somewhere to read from; a follow-up release will delete it
once the new path has been verified in prod.
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.node_tokens (
            token TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_used_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            node_id TEXT,
            revoked BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ck_node_tokens_org ON ck.node_tokens (org_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ck_node_tokens_active "
        "ON ck.node_tokens (token) WHERE revoked = FALSE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.node_tokens TO postgres';
            END IF;
        END $$
    """)

    # Backfill from the existing ck.settings["node_tokens"] blob, if any.
    # jsonb_each expands the token dict into one row per stored token; we
    # coalesce the fields so malformed legacy rows don't break the migration.
    op.execute("""
        INSERT INTO ck.node_tokens (
            token, org_id, label, created_by, created_at, last_used_at, node_id, revoked
        )
        SELECT
            entry.key AS token,
            COALESCE(entry.value->>'org_id', ''),
            COALESCE(entry.value->>'label', ''),
            COALESCE(entry.value->>'created_by', ''),
            COALESCE((entry.value->>'created_at')::DOUBLE PRECISION, 0),
            COALESCE((entry.value->>'last_used_at')::DOUBLE PRECISION, 0),
            NULLIF(entry.value->>'node_id', ''),
            COALESCE((entry.value->>'revoked')::BOOLEAN, FALSE)
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'node_tokens'
          AND jsonb_typeof(s.value) = 'object'
        ON CONFLICT (token) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.node_tokens")

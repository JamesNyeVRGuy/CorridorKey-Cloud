"""Add org_preferences table (CRKY-176).

Revision ID: 010
Revises: 009
Create Date: 2026-04-14

Moves per-org processing preferences out of the ck.settings JSON
blob into a dedicated per-org row. Previously update_org_preferences
loaded one dict keyed by org_id, mutated a single org's entry, and
saved the whole blob, so parallel updates for different orgs could
clobber each other.
"""

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.org_preferences (
            org_id TEXT PRIMARY KEY,
            preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.org_preferences TO postgres';
            END IF;
        END $$
    """)

    op.execute("""
        INSERT INTO ck.org_preferences (org_id, preferences)
        SELECT entry.key, entry.value
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'org_preferences'
          AND jsonb_typeof(s.value) = 'object'
          AND jsonb_typeof(entry.value) = 'object'
        ON CONFLICT (org_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.org_preferences")

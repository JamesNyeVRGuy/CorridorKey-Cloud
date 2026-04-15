"""Add ip_allowlist table (CRKY-175).

Revision ID: 009
Revises: 008
Create Date: 2026-04-14

Moves per-org IP allowlists out of the ck.settings JSON blob into a
real table. Previously save_allowlist did a load -> mutate -> save on
a single dict keyed by org_id, so two admins changing allowlists for
two different orgs in parallel could lose one of the changes. Same
blob, cross-org interference.

New schema is one row per (org_id, cidr) so each org's entries are
independent and bulk-replace-per-org can be done as a single CTE
(DELETE ... WHERE org_id = ? + INSERT ... SELECT unnest(...)).
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.ip_allowlist (
            org_id TEXT NOT NULL,
            cidr TEXT NOT NULL,
            PRIMARY KEY (org_id, cidr)
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.ip_allowlist TO postgres';
            END IF;
        END $$
    """)

    # Backfill: flatten {org_id: [cidr, ...]} into one row per (org_id, cidr).
    op.execute("""
        INSERT INTO ck.ip_allowlist (org_id, cidr)
        SELECT entry.key, jsonb_array_elements_text(entry.value)
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'ip_allowlists'
          AND jsonb_typeof(s.value) = 'object'
          AND jsonb_typeof(entry.value) = 'array'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.ip_allowlist")

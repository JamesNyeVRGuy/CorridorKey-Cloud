"""Add credit_grants ledger table (CRKY-185).

Revision ID: 013
Revises: 012
Create Date: 2026-04-15

Ledger backing the monthly credit grant daemon. One row per
(org_id, grant_type, period) so re-running the grant sweep within
the same period is a no-op via ON CONFLICT DO NOTHING. That makes
the sweep safe to call from any worker as often as we like: the
unique constraint ensures exactly one worker's INSERT wins and the
losers short-circuit without double-crediting.

``grant_type`` is 'monthly' today. The column exists so later
grant cadences (weekly promos, one-off bonuses) can share the same
ledger without schema churn.
"""

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.credit_grants (
            org_id TEXT NOT NULL,
            grant_type TEXT NOT NULL,
            period TEXT NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY (org_id, grant_type, period)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ck_credit_grants_period "
        "ON ck.credit_grants (grant_type, period)"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.credit_grants TO postgres';
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.credit_grants")

"""Re-key gpu_credits by org_id instead of user_id.

Revision ID: 003
Revises: 002
Create Date: 2026-03-19

Credits belong to orgs, not individual users. Nodes earn for their
org, members spend from their org's balance (CRKY-6).
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old user-keyed table and recreate as org-keyed
    op.execute("DROP TABLE IF EXISTS ck.gpu_credits")
    op.execute("""
        CREATE TABLE ck.gpu_credits (
            org_id TEXT PRIMARY KEY,
            contributed_seconds FLOAT DEFAULT 0,
            consumed_seconds FLOAT DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.gpu_credits TO postgres';
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.gpu_credits")
    op.execute("""
        CREATE TABLE ck.gpu_credits (
            user_id TEXT PRIMARY KEY,
            contributed_seconds FLOAT DEFAULT 0,
            consumed_seconds FLOAT DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.gpu_credits TO postgres';
            END IF;
        END $$
    """)

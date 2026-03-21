"""Initial schema — captures tables from deploy/init-db.sql.

Revision ID: 001
Revises: None
Create Date: 2026-03-18

This migration creates the ck schema and base tables if they don't
exist. Safe to run on databases already initialized by init-db.sql
(uses IF NOT EXISTS throughout).
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ck")
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.settings (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.invite_tokens (
            token TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.job_history (
            id SERIAL PRIMARY KEY,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.gpu_credits (
            user_id TEXT PRIMARY KEY,
            contributed_seconds FLOAT DEFAULT 0,
            consumed_seconds FLOAT DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Ensure permissions — grant to postgres if it exists (Supabase uses supabase_admin)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA ck TO postgres';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ck TO postgres';
                EXECUTE 'GRANT ALL ON TABLE ck.alembic_version TO postgres';
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.gpu_credits")
    op.execute("DROP TABLE IF EXISTS ck.job_history")
    op.execute("DROP TABLE IF EXISTS ck.invite_tokens")
    op.execute("DROP TABLE IF EXISTS ck.settings")

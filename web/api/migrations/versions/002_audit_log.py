"""Add audit_log table.

Revision ID: 002
Revises: 001
Create Date: 2026-03-18

Structured audit log for security-relevant actions (CRKY-18).
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.audit_log (
            id BIGSERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            actor_user_id TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            details JSONB DEFAULT '{}',
            ip_address TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON ck.audit_log (timestamp DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON ck.audit_log (actor_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON ck.audit_log (action)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.audit_log TO postgres';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ck.audit_log_id_seq TO postgres';
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.audit_log")

"""Add node_reputations table (CRKY-173).

Revision ID: 007
Revises: 006
Create Date: 2026-04-14

Moves per-node reputation counters out of the ck.settings JSON blob
into a dedicated table. Previously every record_job_completed /
record_job_failed / record_heartbeat / record_security_warning call
did a non-atomic load -> mutate -> save on a single "node_reputations"
blob, so a busy node finishing two jobs in parallel would record one
and lose the other. Reputation drives the auto-pause decision, so
lost counters mean good nodes get falsely booted and bad nodes stay
active.

The migration backfills existing records from the blob. The blob key
is left in place so rollback has somewhere to read from.
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.node_reputations (
            node_id TEXT PRIMARY KEY,
            completed_jobs BIGINT NOT NULL DEFAULT 0,
            failed_jobs BIGINT NOT NULL DEFAULT 0,
            total_frames BIGINT NOT NULL DEFAULT 0,
            total_processing_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
            missed_heartbeats BIGINT NOT NULL DEFAULT 0,
            total_heartbeats BIGINT NOT NULL DEFAULT 0,
            security_warnings INTEGER NOT NULL DEFAULT 0,
            last_updated DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.node_reputations TO postgres';
            END IF;
        END $$
    """)

    op.execute("""
        INSERT INTO ck.node_reputations (
            node_id, completed_jobs, failed_jobs, total_frames,
            total_processing_seconds, missed_heartbeats, total_heartbeats,
            security_warnings, last_updated
        )
        SELECT
            entry.key,
            COALESCE((entry.value->>'completed_jobs')::BIGINT, 0),
            COALESCE((entry.value->>'failed_jobs')::BIGINT, 0),
            COALESCE((entry.value->>'total_frames')::BIGINT, 0),
            COALESCE((entry.value->>'total_processing_seconds')::DOUBLE PRECISION, 0),
            COALESCE((entry.value->>'missed_heartbeats')::BIGINT, 0),
            COALESCE((entry.value->>'total_heartbeats')::BIGINT, 0),
            COALESCE((entry.value->>'security_warnings')::INTEGER, 0),
            COALESCE((entry.value->>'last_updated')::DOUBLE PRECISION, 0)
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'node_reputations'
          AND jsonb_typeof(s.value) = 'object'
        ON CONFLICT (node_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.node_reputations")

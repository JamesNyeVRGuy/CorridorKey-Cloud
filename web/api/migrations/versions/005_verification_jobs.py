"""Add verification_jobs table (CRKY-171).

Revision ID: 005
Revises: 004
Create Date: 2026-04-14

Moves duplicate-processing verification records out of the
ck.settings JSON blob into a real table. The previous implementation
did a non-atomic load -> mutate -> save on every create/record call,
which meant a concurrent write could overwrite a just-recorded
"failed" verdict with a stale "pending" or "passed" one — a security
hole, since this is the gate that decides whether untrusted nodes'
output enters the final render.

The migration backfills existing records from ck.settings
["verification_jobs"] into the new table. The blob key is left in
place so rollback has somewhere to read from.
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.verification_jobs (
            original_job_id TEXT PRIMARY KEY,
            verification_job_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            org_id TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ck_verification_jobs_status "
        "ON ck.verification_jobs (status)"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.verification_jobs TO postgres';
            END IF;
        END $$
    """)

    # Backfill from the existing ck.settings["verification_jobs"] blob.
    op.execute("""
        INSERT INTO ck.verification_jobs (
            original_job_id, verification_job_id, status, org_id, details
        )
        SELECT
            entry.key,
            NULLIF(entry.value->>'verification_job_id', ''),
            COALESCE(entry.value->>'status', 'pending'),
            NULLIF(entry.value->>'org_id', ''),
            COALESCE(entry.value->'details', '{}'::jsonb)
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'verification_jobs'
          AND jsonb_typeof(s.value) = 'object'
        ON CONFLICT (original_job_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.verification_jobs")

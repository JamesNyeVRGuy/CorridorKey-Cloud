"""Add orgs and org_members tables (CRKY-172).

Revision ID: 006
Revises: 005
Create Date: 2026-04-14

Moves orgs and org memberships out of the ck.settings JSON blob into
real tables. Previously both lived inside a pair of settings keys and
every mutation was a non-atomic load -> modify -> save, which:

- allowed duplicate personal orgs when two requests raced a first
  login (CRKY-61), and
- left delete_org writing "orgs" and "org_members" as two separate
  settings calls, so a crash between the two produced orphaned
  memberships.

The new schema pins one personal org per owner with a partial unique
index so duplicate creation becomes a DB-level conflict (handled by
INSERT ... ON CONFLICT DO NOTHING), and FK ON DELETE CASCADE makes
delete_org a single atomic statement.

Backfill strategy: pull every org from the blob into the new table
ordered by created_at ASC so the oldest personal org per owner wins
the partial unique index slot; later duplicates are dropped by
ON CONFLICT. Memberships are then backfilled only for orgs that
survived. Memberships of dropped duplicate personal orgs are left
behind — the application's ensure_personal_org dedupe path already
rehomes them on next access. The legacy blob keys are left in place
for rollback.
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.orgs (
            org_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            owner_id TEXT NOT NULL,
            personal BOOLEAN NOT NULL DEFAULT FALSE,
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ck_orgs_owner ON ck.orgs (owner_id)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS orgs_one_personal_per_owner "
        "ON ck.orgs (owner_id) WHERE personal = TRUE"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS ck.org_members (
            org_id TEXT NOT NULL REFERENCES ck.orgs(org_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY (org_id, user_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ck_org_members_user ON ck.org_members (user_id)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE 'GRANT ALL ON TABLE ck.orgs TO postgres';
                EXECUTE 'GRANT ALL ON TABLE ck.org_members TO postgres';
            END IF;
        END $$
    """)

    # Backfill orgs from ck.settings["orgs"], oldest first so the partial
    # unique index on personal orgs picks the oldest per owner.
    op.execute("""
        INSERT INTO ck.orgs (org_id, name, owner_id, personal, created_at)
        SELECT
            entry.key,
            COALESCE(entry.value->>'name', ''),
            COALESCE(entry.value->>'owner_id', ''),
            COALESCE((entry.value->>'personal')::boolean, FALSE),
            COALESCE((entry.value->>'created_at')::DOUBLE PRECISION, 0)
        FROM ck.settings s,
             LATERAL jsonb_each(s.value) AS entry
        WHERE s.key = 'orgs'
          AND jsonb_typeof(s.value) = 'object'
        ORDER BY COALESCE((entry.value->>'created_at')::DOUBLE PRECISION, 0) ASC
        ON CONFLICT DO NOTHING
    """)

    # Backfill memberships for orgs that survived. Memberships belonging
    # to a dropped duplicate personal org are silently skipped — the
    # app's dedupe routine re-homes any such user on next access.
    op.execute("""
        INSERT INTO ck.org_members (org_id, user_id, role, joined_at)
        SELECT
            m->>'org_id',
            m->>'user_id',
            COALESCE(m->>'role', 'member'),
            COALESCE((m->>'joined_at')::DOUBLE PRECISION, 0)
        FROM ck.settings s,
             LATERAL jsonb_array_elements(s.value) AS m
        WHERE s.key = 'org_members'
          AND jsonb_typeof(s.value) = 'array'
          AND EXISTS (SELECT 1 FROM ck.orgs o WHERE o.org_id = m->>'org_id')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ck.org_members")
    op.execute("DROP TABLE IF EXISTS ck.orgs")

-- CorridorKey database initialization.
-- Runs automatically on first Postgres container start via
-- /docker-entrypoint-initdb.d/ mount.
--
-- Creates a dedicated 'ck' schema for CorridorKey application tables,
-- separate from Supabase's auth/storage/public schemas.
--
-- Note: The Supabase Postgres image uses supabase_admin as the superuser,
-- not the standard 'postgres' role. We also grant access to 'postgres'
-- if it exists (for non-Supabase Postgres setups).

-- Create schema owned by supabase_admin (the init script runs as this role)
CREATE SCHEMA IF NOT EXISTS ck;

-- Grant access to supabase_admin (the role our CK_MIGRATION_URL connects as)
GRANT USAGE ON SCHEMA ck TO supabase_admin;
GRANT ALL PRIVILEGES ON SCHEMA ck TO supabase_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT ALL ON TABLES TO supabase_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT USAGE, SELECT ON SEQUENCES TO supabase_admin;

-- Also grant to 'postgres' if it exists (non-Supabase Postgres, CK_DATABASE_URL)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        GRANT USAGE ON SCHEMA ck TO postgres;
        GRANT ALL PRIVILEGES ON SCHEMA ck TO postgres;
        ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT ALL ON TABLES TO postgres;
        ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT USAGE, SELECT ON SEQUENCES TO postgres;
    END IF;
END $$;

-- Application tables
CREATE TABLE IF NOT EXISTS ck.settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ck.invite_tokens (
    token TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ck.job_history (
    id SERIAL PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ck.gpu_credits (
    user_id TEXT PRIMARY KEY,
    contributed_seconds FLOAT DEFAULT 0,
    consumed_seconds FLOAT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Per-user records. Replaces the legacy ck.settings['users'] JSON blob,
-- which was a read-modify-write hot spot across containers and lost writes
-- under concurrent signups/approvals.
CREATE TABLE IF NOT EXISTS ck.users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'pending',
    name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    use_case TEXT NOT NULL DEFAULT '',
    signed_up_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    approved_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    approved_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ck_users_email ON ck.users (email);
CREATE INDEX IF NOT EXISTS idx_ck_users_tier ON ck.users (tier);

-- Audit log (CRKY-20)
CREATE TABLE IF NOT EXISTS ck.audit_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    actor_user_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details JSONB,
    ip_address TEXT
);

-- Grant table and sequence access explicitly
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA ck TO supabase_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ck TO supabase_admin;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA ck TO postgres;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ck TO postgres;
    END IF;
END $$;


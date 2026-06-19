-- Ensure the 'postgres' superuser role exists.
-- Runs as 00-init-roles.sql BEFORE the Supabase migration scripts.
--
-- The Supabase Postgres image (supabase/postgres) uses supabase_admin as
-- the superuser, but the image's own init scripts (00-schema.sql) expect
-- a 'postgres' role to exist. In some environments (Docker Swarm, certain
-- NFS setups), this role is missing, causing the entire init chain to fail.
--
-- This script creates it if missing, ensuring Supabase's init scripts
-- can run successfully.

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        CREATE USER postgres SUPERUSER;
    ELSE
        ALTER USER postgres WITH SUPERUSER;
    END IF;
END $$;

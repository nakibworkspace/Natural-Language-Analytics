-- =============================================================================
-- Read-only analytics role for the skills
-- =============================================================================
-- Created automatically by docker-entrypoint-initdb.d so it exists from the
-- first boot. The skills MUST connect with this role, never the superuser.
-- The role can SELECT from all tables/views in the public schema only.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_reader') THEN
        CREATE ROLE analytics_reader LOGIN PASSWORD 'analytics_reader_pw';
    END IF;
END$$;

GRANT CONNECT ON DATABASE ride_analytics TO analytics_reader;
GRANT USAGE   ON SCHEMA public TO analytics_reader;

-- Tables
GRANT SELECT ON ALL TABLES    IN SCHEMA public TO analytics_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO analytics_reader;

-- Future tables (defensive default)
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analytics_reader;
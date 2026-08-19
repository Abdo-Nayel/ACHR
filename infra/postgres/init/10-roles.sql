-- =============================================================================
-- Two database roles, two jobs. Run once by the postgres image's initdb hook.
--
-- docker-compose.yml mounts this directory at /docker-entrypoint-initdb.d and
-- names this file in the comment above POSTGRES_APP_USER. Until it existed,
-- the compose stack started a database with neither role in it, `migrate`
-- failed to authenticate as erp_migrator, and anyone who worked around that by
-- pointing POSTGRES_APP_USER at `postgres` got a running system with Row-Level
-- Security silently disabled -- see apps/tenancy/migrations/0002_row_level_security.py.
--
--   erp_migrator  owns the schema. Has DDL rights. Runs `manage.py migrate`.
--                 Never serves a request.
--   erp_app       runs the application. NOSUPERUSER, NOBYPASSRLS, no DDL,
--                 no TRUNCATE, owns nothing. Serves every request.
--
-- Why the split matters more than it looks
-- ----------------------------------------
-- RLS is skipped outright for a role with rolsuper or rolbypassrls. It is also
-- skipped for the table *owner* unless the table carries FORCE ROW LEVEL
-- SECURITY -- which migration 0002 does set, so ownership alone is no longer a
-- bypass here. The split is still worth keeping: it means a SQL-injection
-- foothold in the application cannot DROP or ALTER a table, and it keeps the
-- blast radius of the runtime credential to rows the policy already admits.
--
-- Passwords below match docker-compose.yml's defaults and are for local
-- development only. Production provisions these roles through its own secret
-- management; nothing here is intended to reach it.
--
-- For a native (non-Docker) PostgreSQL, do not paste this file -- run
--     python manage.py provision_db_roles
-- which performs the equivalent work idempotently against an existing
-- database and then verifies the result.
-- =============================================================================

\set ON_ERROR_STOP on

-- Idempotent: initdb runs this only on an empty data directory, but a mounted
-- volume that survived a partial first boot would otherwise fail here and
-- leave the container in a restart loop with a misleading error.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_migrator') THEN
        CREATE ROLE erp_migrator LOGIN PASSWORD 'erp_migrator'
            NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
        -- NOBYPASSRLS is the load-bearing word in this file.
        CREATE ROLE erp_app LOGIN PASSWORD 'erp_app'
            NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB INHERIT;
    END IF;
END
$$;

-- The migrator owns the database and the schema, so every table `migrate`
-- creates is owned by it rather than by postgres.
ALTER DATABASE erp OWNER TO erp_migrator;

\connect erp

ALTER SCHEMA public OWNER TO erp_migrator;

-- Revoke the implicit PUBLIC grant first. Without this, every role in the
-- cluster can create objects in `public` and the ownership model above is
-- decorative.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO erp_app;
GRANT ALL ON SCHEMA public TO erp_migrator;

GRANT CONNECT ON DATABASE erp TO erp_app;

-- No tables exist yet -- initdb runs before `migrate`. These two statements
-- are what actually matter: they say that anything erp_migrator creates from
-- now on is readable and writable by erp_app. Omit them and the app role can
-- authenticate, see an empty schema, and fail every query with a permission
-- error that looks nothing like the missing grant that caused it.
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO erp_app;

-- Deliberately NOT granted to erp_app:
--   TRUNCATE  -- bypasses row-level DELETE policies entirely
--   REFERENCES, TRIGGER, CREATE  -- DDL surface the runtime never needs
--   EXECUTE on functions by default (added per-function if ever required)

-- Consumed by the conditional GRANT block in
-- apps/iam/migrations/0003_invitation.py, which grants the app role access to
-- tables it creates outside the default-privileges path.
ALTER DATABASE erp SET "app.app_role" = 'erp_app';

-- Autonomous Accounting — privileges for the `authenticated` role.
--
-- Apply LAST, after db/local_auth_schema.sql, db/schema_pg.sql and
-- `python -m db.migrate`: it grants on tables that must already exist.
--
-- Why this file is necessary
-- --------------------------
-- db/database_pg.py does not merely filter by user_id in SQL. For every
-- request it stores the JWT claims as transaction-local settings and then runs
-- `SET LOCAL ROLE authenticated`, dropping the connection out of the
-- privileged pool role so that the row-level-security policies are the thing
-- actually enforcing tenant isolation. It verifies the switch took effect and
-- refuses to run queries if it did not.
--
-- That is what makes `authenticated` need real table privileges, and this file
-- is where they are stated.
--
-- Least privilege: `authenticated` gets DML on the application tables and
-- nothing else — no DDL, no ownership, no BYPASSRLS. Which rows it can reach
-- is still decided by the policies. There is no unauthenticated database role,
-- because no endpoint in this repository is reachable without a token.

-- Schema access. `auth` is needed only so the policies can call auth.uid().
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA auth   TO authenticated;

-- Application tables: DML only. RLS decides which rows.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO authenticated;

-- SERIAL primary keys need the sequence, or every INSERT fails.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

-- Tables created by a later migration inherit the same grants, so a new
-- migration cannot silently ship a table the app is not allowed to read.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO authenticated;

-- The identity tables hold every account's address, password hash,
-- confirmation token and recovery token. Every statement that touches them
-- (server/local_auth.py, server/google_auth.py, server/access.py) runs on a
-- pool connection as the cluster owner, never with the role switched, so
-- `authenticated` needs nothing here at all. Foreign keys into auth.users
-- still work: referential-integrity checks bypass both privileges and RLS.
REVOKE ALL ON ALL TABLES IN SCHEMA auth FROM authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth
    REVOKE ALL ON TABLES FROM authenticated;

-- schema_migrations is bookkeeping for db/migrate.py, which connects as the
-- owner. Nothing running as `authenticated` has any business reading it, and
-- it is the one table in `public` with no row-level security.
REVOKE ALL ON public.schema_migrations FROM authenticated;

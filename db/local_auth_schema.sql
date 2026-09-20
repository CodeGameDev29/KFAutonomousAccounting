-- Autonomous Accounting — the identity schema.
--
-- The application keeps identity in its own PostgreSQL cluster: there is no
-- external auth provider. This file creates everything the rest of the schema
-- depends on, and it must be applied BEFORE db/schema_pg.sql, because the
-- row-level-security policies there call `auth.uid()` and several tables have
-- a foreign key into `auth.users`.
--
--   * `auth.users`           the account store, written by server/local_auth.py
--   * `auth.refresh_tokens`  one row per issued refresh token
--   * `auth.uid()`           the current request's user id, read by every policy
--   * role `authenticated`   the grant target the request path drops down to
--
-- Idempotent: safe to re-apply.

CREATE SCHEMA IF NOT EXISTS auth;

-- ── auth.users ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth.users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT UNIQUE NOT NULL,
    -- PBKDF2-HMAC-SHA256, salt and work factor encoded in the string. NULL
    -- means the account has no password: it was created through Google
    -- sign-in. `verify_password` treats NULL as a failed verification and
    -- returns the same generic error as a wrong password, so nothing is
    -- disclosed; the account sets a password through the reset flow.
    encrypted_password  TEXT,
    email_confirmed_at  TIMESTAMPTZ,
    confirmation_token  TEXT,
    confirmation_sent_at TIMESTAMPTZ,
    recovery_token      TEXT,
    -- Bounds the reset window: a recovery token older than an hour is refused.
    recovery_sent_at    TIMESTAMPTZ,
    last_sign_in_at     TIMESTAMPTZ,
    raw_app_meta_data   JSONB NOT NULL DEFAULT '{"provider":"email","providers":["email"]}'::jsonb,
    raw_user_meta_data  JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sign-in looks the address up case-insensitively, so the index has to be too.
CREATE INDEX IF NOT EXISTS idx_auth_users_email ON auth.users (LOWER(email));
CREATE INDEX IF NOT EXISTS idx_auth_users_confirmation ON auth.users (confirmation_token)
    WHERE confirmation_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auth_users_recovery ON auth.users (recovery_token)
    WHERE recovery_token IS NOT NULL;

-- ── auth.refresh_tokens ──────────────────────────────────────────────
-- Tokens are arranged as *families*: one sign-in starts a family (generation
-- 0) and every refresh appends the next generation to it. Rotation marks the
-- presented row `rotated_at` / `replaced_by` rather than revoking it outright,
-- because for a short grace window (`_REFRESH_GRACE_SECONDS` in
-- server/local_auth.py) presenting it again has to return the *same*
-- successor — otherwise several tabs refreshing at once sign the browser out.
--
-- Reuse outside that window is treated as theft and costs the whole family:
-- replaying a rotated token after the grace, or replaying one whose successor
-- has itself been rotated, sets revoked=TRUE on every row sharing the
-- family_id. `revoked` is checked before the grace, so signing out and
-- changing a password (both revoke by user_id) end a session mid-window.
CREATE TABLE IF NOT EXISTS auth.refresh_tokens (
    token          TEXT PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Every token descended from one sign-in shares this. Revocation is by
    -- family, which is what makes theft cost the thief the session.
    family_id      UUID NOT NULL DEFAULT gen_random_uuid(),
    -- 0 for the token minted at sign-in, +1 per rotation. Diagnostic only:
    -- replay detection follows `replaced_by`, not this number.
    generation     INTEGER NOT NULL DEFAULT 0,
    revoked        BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at     TIMESTAMPTZ,
    -- 'reuse_detected' | 'logout' | 'password_change'
    revoked_reason TEXT,
    -- Set when this token was exchanged; the grace window is measured from it.
    rotated_at     TIMESTAMPTZ,
    -- The token this one was rotated into: what a replay inside the grace
    -- window is answered with.
    replaced_by    TEXT,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_user ON auth.refresh_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_expiry ON auth.refresh_tokens (expires_at);
-- The family revocation UPDATE, and any audit of a suspected theft.
CREATE INDEX IF NOT EXISTS idx_auth_refresh_family ON auth.refresh_tokens (family_id);

-- ── auth.uid() ───────────────────────────────────────────────────────
-- The requesting user's id, read out of a transaction-local setting that
-- db/database_pg.py writes before it switches role. Every row-level-security
-- policy in db/schema_pg.sql calls this function; it returns NULL when nothing
-- has been set, and a NULL comparison matches no rows, so a connection that
-- forgot to identify itself sees nothing rather than everything.
CREATE OR REPLACE FUNCTION auth.uid()
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('request.jwt.claim.sub', TRUE), '')::UUID;
$$;

-- ── Roles ────────────────────────────────────────────────────────────
-- `authenticated` is the only role the application uses, and it is a grant
-- target: NOLOGIN, no BYPASSRLS, nothing connects as it. The request path
-- connects as the cluster owner and runs `SET LOCAL ROLE authenticated` for
-- the duration of every transaction, which is what puts the policies in
-- charge. db/local_grants.sql gives it its privileges, after the tables exist.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO authenticated;
-- Needed so the policies can call auth.uid(). It carries no privilege on the
-- tables in this schema — see db/local_grants.sql, which revokes those.
GRANT USAGE ON SCHEMA auth TO authenticated;

-- ── Row-level security on the identity tables ────────────────────────
-- server/local_auth.py, server/google_auth.py and server/access.py are the
-- only readers, and all three go through the pool as the cluster owner, which
-- owns these tables and is therefore not subject to their policies. Nothing
-- reaches them as `authenticated`.
--
-- The policies below are the second lock: should a future query reach these
-- tables with the role switched, a session can see only its own account row
-- and its own tokens — never another account's address, password hash,
-- confirmation token or recovery token. Both halves are present, so such a
-- session could not write a row under somebody else's id either.
ALTER TABLE auth.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.refresh_tokens ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'auth' AND tablename = 'users'
          AND policyname = 'users_self_policy'
    ) THEN
        CREATE POLICY users_self_policy ON auth.users
            FOR ALL USING (id = auth.uid()) WITH CHECK (id = auth.uid());
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'auth' AND tablename = 'refresh_tokens'
          AND policyname = 'refresh_tokens_self_policy'
    ) THEN
        CREATE POLICY refresh_tokens_self_policy ON auth.refresh_tokens
            FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    END IF;
END $$;

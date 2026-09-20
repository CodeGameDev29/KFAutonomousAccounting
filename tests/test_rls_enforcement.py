"""Regression tests for the things that keep one tenant out of another's rows.

1. Role switching in ``db.database_pg.DatabasePg._conn``. Guards against a
   refactor that silently removes the ``SET LOCAL ROLE authenticated`` call or
   the defensive ``current_user`` verification. Without those, every query would
   run as the pool's privileged role and RLS would be bypassed.
2. The tables themselves. A session running as ``authenticated`` whose
   ``auth.uid()`` owns nothing must read **no** row out of **any** table, in
   ``public`` or in ``auth``, and must not be able to write one under somebody
   else's id. Both schemas are enumerated from the catalogue rather than listed
   here, so a table added later is covered without anyone remembering to.
3. Views. A view runs with the privileges and the RLS context of its OWNER
   unless it was created ``WITH (security_invoker = true)``, so a view over the
   application tables that omits the option returns every tenant's rows to any
   caller with SELECT on it — whatever the policies on the base tables say.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from db.database_pg import DatabasePg


def _fake_pool_with_current_user(returned_role: str) -> MagicMock:
    """Build a pool whose SELECT current_user returns *returned_role*."""
    cur = MagicMock()
    cur.fetchone.return_value = (returned_role,)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


def test_conn_fails_closed_when_role_switch_does_not_take_effect() -> None:
    """If SELECT current_user returns something other than 'authenticated',
    refuse to yield the connection — otherwise RLS is silently bypassed."""
    pool = _fake_pool_with_current_user("postgres")  # superuser-ish
    db = DatabasePg(pool, user_id="u-1")

    with pytest.raises(RuntimeError) as info:
        with db._conn():
            pytest.fail("Should not reach the body of _conn on role mismatch")
    assert "authenticated" in str(info.value).lower()


def test_conn_succeeds_when_role_actually_authenticated() -> None:
    """Happy path: cursor reports the role switched correctly."""
    pool = _fake_pool_with_current_user("authenticated")
    db = DatabasePg(pool, user_id="u-2")

    with db._conn() as conn:
        assert conn is pool.getconn.return_value

    # Cursor was asked for current_user exactly once
    cur = pool.getconn.return_value.cursor.return_value.__enter__.return_value
    executed_sql = [c.args[0] for c in cur.execute.call_args_list]
    assert any("SET LOCAL ROLE authenticated" in s for s in executed_sql)
    assert any("current_user" in s.lower() for s in executed_sql)
    assert any("request.jwt.claims" in s for s in executed_sql)


def test_conn_sets_request_jwt_claim_sub_to_user_id() -> None:
    pool = _fake_pool_with_current_user("authenticated")
    db = DatabasePg(pool, user_id="user-xyz-123")
    with db._conn():
        pass
    cur = pool.getconn.return_value.cursor.return_value.__enter__.return_value
    # Find the call that sets request.jwt.claim.sub
    for c in cur.execute.call_args_list:
        sql = c.args[0]
        if "request.jwt.claim.sub" in sql:
            params = c.args[1] if len(c.args) > 1 else None
            assert params == ("user-xyz-123",)
            return
    pytest.fail("Expected request.jwt.claim.sub to be set with user_id")


# ── What a caller who owns nothing can read ─────────────────────────


@pytest.fixture()
def seeded_owner():
    """A real PostgreSQL holding one account's rows, removed afterwards.

    Skips — loudly, never silently passes — when there is no database. A mock
    cannot answer this question: the whole point is what the server does with a
    view's `security_invoker` option and the policies underneath it.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    # db.database_pg, imported at the top of this module, has already imported
    # psycopg2.pool, so the submodule is reachable as an attribute.
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not set — no PostgreSQL to exercise")
    try:
        owner = psycopg2.connect(dsn, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unreachable: {exc}")

    user_id = str(uuid.uuid4())
    owner.autocommit = True
    with owner.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email, encrypted_password, "
            "email_confirmed_at, created_at, updated_at) "
            "VALUES (%s, %s, 'x', NOW(), NOW(), NOW())",
            (user_id, f"rls-view-{user_id[:8]}@example.invalid"),
        )

    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=dsn)
    db = DatabasePg(pool, user_id=user_id)
    try:
        _seed_one_account(db)
        _seed_wider(owner, user_id)
        yield owner, dsn, user_id
    finally:
        pool.closeall()
        with owner.cursor() as cur:
            # Every user-scoped table, not just the ones seeded: the account
            # has to leave nothing behind for the next test to trip over.
            cur.execute(_USER_SCOPED_TABLES_SQL)
            for schema, table in cur.fetchall():
                try:
                    cur.execute(
                        f'DELETE FROM {schema}."{table}" WHERE user_id = %s',  # noqa: S608
                        (user_id,),
                    )
                except Exception:
                    pass
            cur.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))
        owner.close()


def _seed_one_account(db: DatabasePg) -> None:
    """Rows in the tables any proof-shaped view draws on, owned by one user."""
    from models.document import Document
    from models.transaction import Transaction, TransactionStatus

    def _txn(row: int, amount: str, desc: str) -> int:
        return db.insert_transaction(Transaction(
            account="BMO CAD", transaction_type="DEBIT",
            date_posted=date(2026, 3, 4), amount=Decimal(amount), currency="CAD",
            description=desc, source_file="rls-view-test.csv", source_row=row,
            status=TransactionStatus.UNMATCHED,
        ))

    src = _txn(1, "-500.00", "OUTGOING WIRE")
    tgt = _txn(2, "500.00", "WISE TRANSFER RECEIVED")

    db.create_transaction_link(
        source_txn_id=src, target_txn_id=tgt, link_type="INTERNAL_TRANSFER",
        confidence_score=0.9, match_source="AUTO", explanation="seeded by a test",
    )
    db.insert_document(Document(
        original_filename="rls-view-test.pdf",
        stored_path="/tmp/rls-view-test.pdf",
        file_hash=f"rls-view-{uuid.uuid4().hex}",
        vendor="Northwind Trading",
        document_date=date(2026, 3, 4),
        currency="CAD",
        total=Decimal("500.00"),
    ))


#: Every base table in `public` or `auth` that carries a `user_id` column.
_USER_SCOPED_TABLES_SQL = """
    SELECT c.relnamespace::regnamespace::text, c.relname
      FROM pg_class c
      JOIN pg_attribute a ON a.attrelid = c.oid
     WHERE c.relkind = 'r'
       AND c.relnamespace::regnamespace::text IN ('public', 'auth')
       AND a.attname = 'user_id'
       AND a.attnum > 0 AND NOT a.attisdropped
     ORDER BY 1, 2
"""

#: Rows for the tables the short seed above does not reach, so the cross-tenant
#: assertions are answered by tables that actually hold something.
#: (table, column list, values after user_id).
_EXTRA_SEED: tuple[tuple[str, str, tuple], ...] = (
    ("business_profile", "user_id, company_name", ("Example Corp Inc.",)),
    ("month_status", "user_id, year, month", (2026, 3)),
    ("audit_log", "user_id, entity_type, entity_id, action",
     ("transaction", 1, "LINK")),
    ("statements", "user_id, source_file", ("rls-statement.pdf",)),
    ("processing_jobs", "user_id, kind, filename", ("receipt", "rls-job.pdf")),
    ("gmail_oauth_consents", "user_id", ()),
    ("vendor_rules", "user_id, vendor_pattern, category",
     ("NORTHWIND", "Office supplies")),
    ("user_categories", "user_id, name", ("Office supplies",)),
    ("api_calls", "user_id, endpoint", ("/rls/probe",)),
    ("gather_sources", "user_id, source_type", ("manual",)),
    ("gather_log", "user_id, source, filename", ("manual", "rls-gathered.pdf")),
    ("scan_runs", "user_id, source", ("gmail",)),
    ("connection_health", "user_id, source", ("gmail",)),
    ("onboarding_steps", "user_id, step", ("profile",)),
    ("categorization_run", "user_id, started_at, scope", ("2026-03-04", "all")),
    ("reasonableness_dismissed_flags", "user_id, flag_key", ("rls-flag",)),
    ("ledger_entries",
     "user_id, account, month, transaction_type, date_posted, amount, "
     "currency, description",
     ("BMO CAD", "Mar2026", "DEBIT", "2026-03-04", "500.00", "CAD",
      "OUTGOING WIRE")),
)


def _seed_wider(owner, user_id: str) -> None:
    """Fill the remaining user-scoped tables, plus one refresh token.

    Written as the owner, which is not subject to the policies: the point is to
    put rows in place that a foreign session must then fail to see. A table
    this checkout does not have is skipped rather than failing the fixture.
    """
    for table, columns, values in _EXTRA_SEED:
        placeholders = ", ".join(["%s"] * (len(values) + 1))
        with owner.cursor() as cur:
            try:
                cur.execute(
                    f"INSERT INTO public.{table} ({columns}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    (user_id, *values),
                )
            except Exception:  # pragma: no cover - environment dependent
                pass
    with owner.cursor() as cur:
        cur.execute(
            "INSERT INTO public.gmail_oauth_states (user_id, state) VALUES (%s, %s)",
            (user_id, f"rls-{uuid.uuid4().hex}"),
        )
        cur.execute(
            "INSERT INTO public.processed_files "
            "(user_id, file_hash, original_filename) VALUES (%s, %s, %s)",
            (user_id, uuid.uuid4().hex, "rls-processed.csv"),
        )
        cur.execute(
            "INSERT INTO public.shared_reports "
            "(user_id, token, year, month, expires_at) "
            "VALUES (%s, %s, 2026, 3, NOW() + INTERVAL '7 days')",
            (user_id, uuid.uuid4().hex),
        )
        cur.execute(
            "INSERT INTO auth.refresh_tokens (token, user_id, expires_at) "
            "VALUES (%s, %s, NOW() + INTERVAL '1 day')",
            (f"rls-{uuid.uuid4().hex}", user_id),
        )


def _public_views(cur) -> list[tuple[str, list[str] | None]]:
    cur.execute(
        "SELECT c.relname, c.reloptions "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('v', 'm') AND n.nspname = 'public' "
        "ORDER BY c.relname"
    )
    return list(cur.fetchall())


def test_every_view_runs_with_the_callers_privileges(seeded_owner) -> None:
    """A view without security_invoker ignores the policies on its base tables."""
    owner, _dsn, _user_id = seeded_owner
    with owner.cursor() as cur:
        views = _public_views(cur)

    unsafe = [
        name for name, options in views
        if "security_invoker=true" not in (options or [])
    ]
    assert not unsafe, (
        "views in schema public without WITH (security_invoker = true): "
        f"{unsafe}. Such a view returns every tenant's rows to any caller that "
        "can select from it. Add the option, or drop the view."
    )


def _rows_visible_to(psycopg2, dsn: str, uid: str, view: str) -> int:
    """COUNT(*) from *view* in a session running as `authenticated` with auth.uid() = uid."""
    reader = psycopg2.connect(dsn, connect_timeout=3)
    try:
        with reader.cursor() as cur:
            cur.execute("SET LOCAL ROLE authenticated")
            cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (uid,))
            try:
                cur.execute(f'SELECT COUNT(*) FROM public."{view}"')  # noqa: S608
                return cur.fetchone()[0]
            except psycopg2.errors.InsufficientPrivilege:
                return 0  # unreadable is also zero rows
    finally:
        reader.rollback()
        reader.close()


def test_the_view_check_is_not_vacuous(seeded_owner) -> None:
    """The two tests above only mean something if an unsafe view would fail them.

    Two throwaway views over the seeded account, identical but for the option:
    the one without `security_invoker` hands its rows to a stranger, the one
    with it hands over nothing. Both are dropped again.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    owner, dsn, _user_id = seeded_owner
    stranger = str(uuid.uuid4())
    body = "SELECT id, user_id, description FROM transactions"

    with owner.cursor() as cur:
        cur.execute(f"CREATE VIEW rls_probe_owner_rights AS {body}")  # noqa: S608
        cur.execute(
            "CREATE VIEW rls_probe_caller_rights WITH (security_invoker = true) AS "
            f"{body}"  # noqa: S608
        )
        cur.execute("GRANT SELECT ON rls_probe_owner_rights, rls_probe_caller_rights "
                    "TO authenticated")
    try:
        with owner.cursor() as cur:
            found = dict(_public_views(cur))
        assert "rls_probe_owner_rights" in found, "the catalogue query missed a view"
        assert "security_invoker=true" not in (found["rls_probe_owner_rights"] or [])
        assert "security_invoker=true" in (found["rls_probe_caller_rights"] or [])

        leaked = _rows_visible_to(psycopg2, dsn, stranger, "rls_probe_owner_rights")
        assert leaked > 0, (
            "a view created without security_invoker did NOT leak, so these tests "
            "prove nothing on this PostgreSQL — check the server version"
        )
        safe = _rows_visible_to(psycopg2, dsn, stranger, "rls_probe_caller_rights")
        assert safe == 0, "security_invoker did not apply the base-table policies"
    finally:
        with owner.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS rls_probe_owner_rights")
            cur.execute("DROP VIEW IF EXISTS rls_probe_caller_rights")


def test_a_foreign_auth_uid_reads_no_rows_from_any_view(seeded_owner) -> None:
    """The account seeded above is invisible to a session that is not its owner.

    Enumerated from the catalogue, so a view added later is covered here the
    moment it exists.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    owner, dsn, user_id = seeded_owner

    with owner.cursor() as cur:
        views = _public_views(cur)
        cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone()[0] == 2, "fixture did not seed"

    stranger = str(uuid.uuid4())
    assert stranger != user_id

    reader = psycopg2.connect(dsn, connect_timeout=3)
    try:
        for name, _options in views:
            with reader.cursor() as cur:
                cur.execute("SET LOCAL ROLE authenticated")
                cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)",
                            (stranger,))
                try:
                    cur.execute(f'SELECT COUNT(*) FROM public."{name}"')  # noqa: S608
                    visible = cur.fetchone()[0]
                except psycopg2.errors.InsufficientPrivilege:
                    visible = 0  # unreadable is also zero rows
                assert visible == 0, (
                    f"view {name!r} showed {visible} row(s) to a session whose "
                    "auth.uid() owns nothing — the view is reading across tenants"
                )
            reader.rollback()
    finally:
        reader.close()


# ── Tables: the same question, asked of every table in both schemas ───────


def _base_tables(cur) -> list[tuple[str, str]]:
    """Every ordinary table in `public` and `auth`, from the catalogue."""
    cur.execute(
        "SELECT c.relnamespace::regnamespace::text, c.relname "
        "FROM pg_class c "
        "WHERE c.relkind = 'r' "
        "  AND c.relnamespace::regnamespace::text IN ('public', 'auth') "
        "ORDER BY 1, 2"
    )
    return list(cur.fetchall())


def _count_as_stranger(psycopg2, dsn: str, uid: str, schema: str, table: str) -> int:
    """COUNT(*) as `authenticated` with auth.uid() = uid. Unreadable is zero."""
    reader = psycopg2.connect(dsn, connect_timeout=3)
    try:
        with reader.cursor() as cur:
            cur.execute("SET LOCAL ROLE authenticated")
            cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (uid,))
            try:
                cur.execute(f'SELECT COUNT(*) FROM {schema}."{table}"')  # noqa: S608
                return cur.fetchone()[0]
            except psycopg2.errors.InsufficientPrivilege:
                return 0
    finally:
        reader.rollback()
        reader.close()


def test_every_table_has_row_level_security(seeded_owner) -> None:
    """A table without RLS is readable by every signed-in session.

    `schema_migrations` is the one exception, and only because
    db/local_grants.sql revokes it from `authenticated` outright: it holds no
    user data, and the migration runner connects as the owner.
    """
    owner, dsn, _user_id = seeded_owner
    with owner.cursor() as cur:
        cur.execute(
            "SELECT c.relnamespace::regnamespace::text || '.' || c.relname "
            "FROM pg_class c "
            "WHERE c.relkind = 'r' "
            "  AND c.relnamespace::regnamespace::text IN ('public', 'auth') "
            "  AND NOT c.relrowsecurity "
            "ORDER BY 1"
        )
        unprotected = [row[0] for row in cur.fetchall()]

    assert unprotected == ["public.schema_migrations"], (
        f"tables without row-level security: {unprotected}. Every table holding "
        "user data needs ENABLE ROW LEVEL SECURITY and a policy, or one signed-in "
        "account reads another's rows."
    )

    # …and that exception is only safe because the grant is not there either.
    psycopg2 = pytest.importorskip("psycopg2")
    reader = psycopg2.connect(dsn, connect_timeout=3)
    try:
        with reader.cursor() as cur:
            cur.execute("SET LOCAL ROLE authenticated")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.schema_migrations")
    finally:
        reader.rollback()
        reader.close()


def test_every_policy_has_a_with_check(seeded_owner) -> None:
    """USING alone decides what is *visible*, never what may be written.

    A policy with `USING (user_id = auth.uid())` and no `WITH CHECK` lets a
    signed-in session INSERT — or UPDATE a row into — another account.
    """
    owner, _dsn, _user_id = seeded_owner
    with owner.cursor() as cur:
        cur.execute(
            "SELECT schemaname || '.' || tablename || ' / ' || policyname "
            "FROM pg_policies "
            "WHERE schemaname IN ('public', 'auth') "
            "  AND cmd IN ('ALL', 'INSERT', 'UPDATE') "
            "  AND with_check IS NULL "
            "ORDER BY 1"
        )
        missing = [row[0] for row in cur.fetchall()]
    assert not missing, (
        "write-capable policies with no WITH CHECK clause: "
        f"{missing}. Such a policy permits writing a row owned by someone else."
    )


def test_a_foreign_auth_uid_reads_no_rows_from_any_table(seeded_owner) -> None:
    """The seeded account is invisible to every table, to a session that is not it.

    Enumerated from the catalogue so a table added later is covered the moment
    it exists, and checked against the owner's own counts so the assertion
    cannot pass merely because everything is empty.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    owner, dsn, user_id = seeded_owner

    with owner.cursor() as cur:
        tables = _base_tables(cur)
        cur.execute(_USER_SCOPED_TABLES_SQL)
        user_scoped = cur.fetchall()
        non_empty = []
        for schema, table in user_scoped:
            cur.execute(
                f'SELECT COUNT(*) FROM {schema}."{table}" WHERE user_id = %s',  # noqa: S608
                (user_id,),
            )
            if cur.fetchone()[0]:
                non_empty.append(f"{schema}.{table}")
        cur.execute("SELECT COUNT(*) FROM auth.users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] == 1, "fixture did not seed auth.users"

    assert len(non_empty) >= 20, (
        "the fixture seeded too few tables for this test to mean anything: "
        f"{non_empty}"
    )

    stranger = str(uuid.uuid4())
    assert stranger != user_id

    for schema, table in tables:
        visible = _count_as_stranger(psycopg2, dsn, stranger, schema, table)
        assert visible == 0, (
            f"{schema}.{table} showed {visible} row(s) to a session whose "
            "auth.uid() owns nothing — that table is readable across tenants"
        )


def test_a_foreign_auth_uid_cannot_write_under_another_user_id(seeded_owner) -> None:
    """WITH CHECK refuses the write; nothing lands under the other account."""
    psycopg2 = pytest.importorskip("psycopg2")
    owner, dsn, user_id = seeded_owner
    stranger = str(uuid.uuid4())

    attempts = (
        (
            "INSERT INTO public.transactions (user_id, account, transaction_type, "
            "date_posted, amount, currency, description, source_file, source_row) "
            "VALUES (%s, 'BMO CAD', 'DEBIT', '2026-03-05', '1.00', 'CAD', "
            "'CROSS TENANT WRITE', 'rls-write-test.csv', 99)",
            (user_id,),
        ),
        (
            "INSERT INTO public.documents (user_id, original_filename, file_hash) "
            "VALUES (%s, 'cross-tenant.pdf', 'cross-tenant-hash')",
            (user_id,),
        ),
        (
            "INSERT INTO auth.users (id, email, created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, NOW(), NOW())",
            (f"cross-tenant-{stranger[:8]}@example.invalid",),
        ),
    )

    for sql, params in attempts:
        reader = psycopg2.connect(dsn, connect_timeout=3)
        try:
            with reader.cursor() as cur:
                cur.execute("SET LOCAL ROLE authenticated")
                cur.execute(
                    "SELECT set_config('request.jwt.claim.sub', %s, true)", (stranger,)
                )
                with pytest.raises(psycopg2.Error):
                    cur.execute(sql, params)
        finally:
            reader.rollback()
            reader.close()

    # And nothing arrived.
    with owner.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM public.transactions "
            "WHERE user_id = %s AND description = 'CROSS TENANT WRITE'",
            (user_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM public.documents WHERE file_hash = 'cross-tenant-hash'"
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM auth.users WHERE email LIKE 'cross-tenant-%'"
        )
        assert cur.fetchone()[0] == 0

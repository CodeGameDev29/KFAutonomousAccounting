"""Guards for `DatabasePg.reset_all_tables` — a reset must never report success
while deleting nothing.

`POST /api/actions/reset` can answer 200 with a row count of zero for every
table while the account is left exactly as it was. Two independent faults do
that, and both are covered here:

* a delete list that omits ``transaction_links`` or ``note_attachments``, so
  ``DELETE FROM transactions`` hits a foreign key that blocks (NO ACTION); and
* a per-table delete inside its own savepoint under a bare ``except``, which
  rolls the savepoint back and records a count of **0** — indistinguishable in
  the response from "there was nothing to delete".

The integration test at the bottom is the one with teeth: it seeds a link and a
note attachment against a live PostgreSQL and asserts the account is empty
afterwards. It skips — loudly, never silently passes — when no database is
reachable.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from db.database_pg import DatabasePg

# ── The delete list itself ────────────────────────────────────────────────


def test_the_blocking_child_tables_are_in_the_list():
    assert "transaction_links" in DatabasePg.RESET_TABLES
    assert "note_attachments" in DatabasePg.RESET_TABLES


def test_every_child_is_deleted_before_its_parent():
    """The foreign keys that actually block, in the order they constrain.

    Each pair is (child, parent) as the schema declares them. A refactor that
    reorders the list has to keep every one of them true, or the delete fails on
    a live account.
    """
    order = {table: i for i, table in enumerate(DatabasePg.RESET_TABLES)}
    dependencies = [
        ("ledger_entries", "reconciliation_matches"),
        ("reconciliation_matches", "transactions"),
        ("reconciliation_matches", "documents"),
        ("transaction_links", "transactions"),
        ("note_attachments", "transactions"),
        ("reconciliation_adjudications", "transactions"),
        ("reconciliation_adjudications", "documents"),
        ("extraction_log", "documents"),
        ("transactions", "statements"),
    ]
    for child, parent in dependencies:
        assert child in order, f"{child} missing from RESET_TABLES"
        assert parent in order, f"{parent} missing from RESET_TABLES"
        assert order[child] < order[parent], (
            f"{child} must be deleted before {parent} — its foreign key blocks"
        )


def test_no_table_is_listed_twice():
    assert len(DatabasePg.RESET_TABLES) == len(set(DatabasePg.RESET_TABLES))


# ── A failing delete must not be reported as a count of zero ──────────────


def _pool_where(delete_fails_on: str | None = None, remaining: int = 0) -> MagicMock:
    """A pool whose cursor raises on `DELETE FROM <table>`, if asked to."""
    cur = MagicMock()
    cur.rowcount = 1

    def _execute(sql, params=None):
        if "SELECT current_user" in sql:
            cur.fetchone.return_value = ("authenticated",)
            return
        if "information_schema.tables" in sql:
            cur.fetchall.return_value = [(t,) for t in DatabasePg.RESET_TABLES]
            return
        if sql.startswith("SELECT COUNT(*)"):
            cur.fetchone.return_value = (remaining,)
            return
        if delete_fails_on and sql.startswith(f"DELETE FROM {delete_fails_on} "):
            raise RuntimeError(
                'update or delete on table "transactions" violates foreign key '
                'constraint "transaction_links_source_transaction_id_fkey"'
            )

    cur.execute.side_effect = _execute
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


def test_a_blocked_delete_raises_instead_of_reporting_zero():
    db = DatabasePg(_pool_where(delete_fails_on="transactions"), user_id="u-reset-1")
    with pytest.raises(RuntimeError) as info:
        db.reset_all_tables()
    message = str(info.value)
    assert "transactions" in message
    assert "foreign key" in message.lower()
    assert "atomic" in message.lower()


def test_a_failed_reset_rolls_the_whole_thing_back():
    pool = _pool_where(delete_fails_on="transactions")
    db = DatabasePg(pool, user_id="u-reset-2")
    with pytest.raises(RuntimeError):
        db.reset_all_tables()
    conn = pool.getconn.return_value
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


def test_rows_left_behind_fail_the_reset_even_when_no_delete_errored():
    """A delete that quietly matched nothing is still a broken reset."""
    db = DatabasePg(_pool_where(remaining=512), user_id="u-reset-3")
    with pytest.raises(RuntimeError) as info:
        db.reset_all_tables()
    assert "512" in str(info.value)


def test_a_clean_reset_commits_and_reports_per_table_counts():
    pool = _pool_where()
    db = DatabasePg(pool, user_id="u-reset-4")
    counts = db.reset_all_tables()
    assert set(counts) == set(DatabasePg.RESET_TABLES)
    assert all(n == 1 for n in counts.values())
    pool.getconn.return_value.commit.assert_called()


def test_a_table_this_database_does_not_have_is_skipped_not_caught():
    """A schema without one of the tables still resets; it reports fewer of them."""
    cur = MagicMock()
    cur.rowcount = 2
    known = [t for t in DatabasePg.RESET_TABLES if t != "reconciliation_adjudications"]

    def _execute(sql, params=None):
        if "SELECT current_user" in sql:
            cur.fetchone.return_value = ("authenticated",)
        elif "information_schema.tables" in sql:
            cur.fetchall.return_value = [(t,) for t in known]
        elif sql.startswith("SELECT COUNT(*)"):
            cur.fetchone.return_value = (0,)
        elif sql.startswith("DELETE FROM reconciliation_adjudications"):
            pytest.fail("deleted from a table the catalog says does not exist")

    cur.execute.side_effect = _execute
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn

    counts = DatabasePg(pool, user_id="u-reset-5").reset_all_tables()
    assert "reconciliation_adjudications" not in counts
    assert "transactions" in counts


# ── The same reset against a live PostgreSQL ──────────────────────────────


@pytest.fixture()
def live_db():
    """A throwaway account on a live PostgreSQL, deleted afterwards.

    Skips when there is no database to talk to. It does not fake one: the whole
    point of this test is the foreign keys, and a mock has none.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from dotenv import load_dotenv

    load_dotenv()
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
            (user_id, f"reset-test-{user_id[:8]}@example.invalid"),
        )

    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=dsn)
    db = DatabasePg(pool, user_id=user_id)
    try:
        yield db, user_id, owner
    finally:
        pool.closeall()
        with owner.cursor() as cur:
            for table in DatabasePg.RESET_TABLES:
                try:
                    cur.execute(
                        f"DELETE FROM {table} WHERE user_id = %s",  # noqa: S608
                        (user_id,),
                    )
                except Exception:
                    pass
            cur.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))
        owner.close()


def test_a_reset_empties_an_account_that_has_links_and_note_attachments(live_db):
    """End to end: seed exactly the rows whose foreign keys block the delete."""
    from models.transaction import Transaction, TransactionStatus

    db, user_id, owner = live_db

    def _txn(row: int, amount: str, desc: str) -> int:
        return db.insert_transaction(Transaction(
            account="CAD", transaction_type="DEBIT",
            date_posted=date(2026, 3, 4), amount=Decimal(amount), currency="CAD",
            description=desc, source_file="reset-test.csv", source_row=row,
            status=TransactionStatus.UNMATCHED,
        ))

    src = _txn(1, "-500.00", "OUTGOING WIRE")
    tgt = _txn(2, "500.00", "WISE TRANSFER RECEIVED")

    # The two tables whose foreign keys block a delete of transactions.
    db.create_transaction_link(
        source_txn_id=src, target_txn_id=tgt, link_type="INTERNAL_TRANSFER",
        confidence_score=0.9, match_source="AUTO", explanation="seeded by a test",
    )
    with owner.cursor() as cur:
        cur.execute(
            "INSERT INTO note_attachments (user_id, transaction_id, filename, "
            "stored_path, mime_type, file_size) "
            "VALUES (%s, %s, 'note.pdf', '/tmp/note.pdf', 'application/pdf', 10)",
            (user_id, src),
        )

    with owner.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone()[0] == 2, "fixture did not seed"
        cur.execute("SELECT COUNT(*) FROM transaction_links WHERE user_id = %s", (user_id,))
        assert cur.fetchone()[0] == 1, "fixture did not seed the blocking link"

    counts = db.reset_all_tables()

    assert counts["transactions"] == 2, counts
    assert counts["transaction_links"] == 1, counts
    assert counts["note_attachments"] == 1, counts

    with owner.cursor() as cur:
        for table in ("transactions", "transaction_links", "note_attachments"):
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = %s",  # noqa: S608
                (user_id,),
            )
            assert cur.fetchone()[0] == 0, f"{table} still has rows after the reset"

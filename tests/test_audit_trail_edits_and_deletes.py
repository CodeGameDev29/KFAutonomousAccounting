"""Category changes, notes and deletions must reach the audit trail.

/activity promises an "audit trail of all actions — approvals, rejections,
matches, links, and corrections", and a delete is the hardest half of that
promise to keep: a receipt, or a statement together with every transaction it
carried, can disappear in one call. A silent cascading delete is the dangerous
case — in a set of books the record of what was removed matters as much as the
record of what was changed.

Each test drives the real endpoint and asserts the entry it must leave behind:
action, entity, before → after.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

FAKE_USER = AuthUser(
    id="audit-user",
    email="audit-user@example.com",
    role="authenticated",
    email_verified=True,
)


class FakeCursor:
    """A cursor that answers from a plan of (sql-substring -> response)."""

    def __init__(self, plan: list[tuple[str, dict]]) -> None:
        self.plan = plan
        self.executed: list[str] = []
        self.rowcount = 0
        self._current: dict = {}

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append(flat)
        self._current = {}
        for needle, resp in self.plan:
            if needle in flat:
                self._current = resp
                break
        self.rowcount = self._current.get("rowcount", 0)

    def fetchone(self):
        return self._current.get("one")

    def fetchall(self):
        return self._current.get("all", [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDb:
    """Records audit writes; answers raw SQL from a plan."""

    def __init__(self, plan: list[tuple[str, dict]] | None = None) -> None:
        self.user_id = FAKE_USER.id
        self.cursor = FakeCursor(plan or [])
        self.audit: list[dict] = []

    # -- audit ---------------------------------------------------------------
    def insert_audit_log(self, **kwargs):
        self.audit.append(kwargs)
        return len(self.audit)

    def entries(self, action: str) -> list[dict]:
        return [e for e in self.audit if e.get("action") == action]

    # -- raw SQL -------------------------------------------------------------
    def _conn(self):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = self.cursor
        return conn

    # -- typed helpers the endpoints call ------------------------------------
    def update_transaction_note(self, txn_id, note):
        self.saved_note = note
        return True

    def confirm_transaction_category(self, txn_id, category):
        self.saved_category = category
        return True

    def update_transaction_category(self, txn_id, category):
        self.saved_category = category
        return True

    def resync_statuses_in_cursor(self, cur, transaction_ids=(), document_ids=()):
        """Stand-in for the real status re-derivation.

        The endpoint counts the rows the re-derivation changed — a transaction
        that still has another proof does NOT go back to unmatched — so the
        fake reports every id it was handed as changed.
        """
        self.resynced = (list(transaction_ids), list(document_ids))
        return {
            "transactions": [
                {"transaction_id": tid, "from": "MATCHED", "to": "UNMATCHED"}
                for tid in transaction_ids
            ],
            "documents": [],
        }


@pytest.fixture()
def client_for():
    def _factory(fake_db: FakeDb) -> TestClient:
        async def _user():
            return FAKE_USER

        app.dependency_overrides[get_current_user] = _user
        app.dependency_overrides[get_db] = lambda: fake_db
        return TestClient(app, raise_server_exceptions=False)

    yield _factory
    for key in (get_current_user, get_db):
        app.dependency_overrides.pop(key, None)


# ── Category ───────────────────────────────────────────────────────────────

def test_category_change_is_audited_with_before_and_after(client_for):
    db = FakeDb(plan=[("SELECT category FROM transactions", {"one": ("Meals and entertainment",)})])
    resp = client_for(db).patch(
        "/api/transactions/101/category", json={"category": "Office supplies"}
    )
    assert resp.status_code == 200, resp.text

    entries = db.entries("RECATEGORIZE")
    assert len(entries) == 1, db.audit
    entry = entries[0]
    assert entry["entity_type"] == "transaction"
    assert entry["entity_id"] == 101
    assert entry["old_value"] == "Meals and entertainment"
    assert entry["new_value"] == "Office supplies"
    assert entry["performed_by"] == FAKE_USER.id


def test_category_cleared_is_audited_as_uncategorized(client_for):
    db = FakeDb(plan=[("SELECT category FROM transactions", {"one": ("Office supplies",)})])
    resp = client_for(db).patch("/api/transactions/101/category", json={"category": None})
    assert resp.status_code == 200, resp.text

    entry = db.entries("RECATEGORIZE")[0]
    assert entry["old_value"] == "Office supplies"
    assert entry["new_value"] == "Uncategorized"


def test_first_categorization_records_uncategorized_as_the_before(client_for):
    db = FakeDb(plan=[("SELECT category FROM transactions", {"one": (None,)})])
    resp = client_for(db).patch(
        "/api/transactions/901/category", json={"category": "Travel"}
    )
    assert resp.status_code == 200, resp.text
    entry = db.entries("RECATEGORIZE")[0]
    assert entry["old_value"] == "Uncategorized"
    assert entry["new_value"] == "Travel"


def test_setting_the_same_category_writes_no_entry(client_for):
    """The trail records corrections, not no-ops."""
    db = FakeDb(plan=[("SELECT category FROM transactions", {"one": ("Office supplies",)})])
    resp = client_for(db).patch(
        "/api/transactions/101/category", json={"category": "Office supplies"}
    )
    assert resp.status_code == 200, resp.text
    assert db.entries("RECATEGORIZE") == []


def test_rejected_category_writes_no_entry(client_for):
    """An off-taxonomy name is a 422 — nothing changed, so nothing is logged."""
    db = FakeDb(plan=[("SELECT category FROM transactions", {"one": ("Office supplies",)})])
    resp = client_for(db).patch(
        "/api/transactions/101/category", json={"category": "Bitcoin"}
    )
    assert resp.status_code == 422, resp.text
    assert db.audit == []


# ── Notes ──────────────────────────────────────────────────────────────────

def test_note_added_is_audited(client_for):
    db = FakeDb(plan=[("SELECT note FROM transactions", {"one": (None,)})])
    resp = client_for(db).patch(
        "/api/transactions/818/note", json={"note": "Client dinner, 2 attendees"}
    )
    assert resp.status_code == 200, resp.text

    entries = db.entries("EDIT_NOTE")
    assert len(entries) == 1, db.audit
    assert entries[0]["entity_type"] == "transaction"
    assert entries[0]["entity_id"] == 818
    assert entries[0].get("old_value") is None
    assert entries[0]["new_value"] == "Client dinner, 2 attendees"


def test_note_edited_records_both_sides(client_for):
    db = FakeDb(plan=[("SELECT note FROM transactions", {"one": ("Old note",)})])
    resp = client_for(db).patch("/api/transactions/818/note", json={"note": "New note"})
    assert resp.status_code == 200, resp.text
    entry = db.entries("EDIT_NOTE")[0]
    assert entry["old_value"] == "Old note"
    assert entry["new_value"] == "New note"


def test_note_cleared_says_so(client_for):
    db = FakeDb(plan=[("SELECT note FROM transactions", {"one": ("Old note",)})])
    resp = client_for(db).patch("/api/transactions/818/note", json={"note": ""})
    assert resp.status_code == 200, resp.text
    entry = db.entries("EDIT_NOTE")[0]
    assert entry["old_value"] == "Old note"
    assert entry["new_value"] == "(note cleared)"


def test_long_note_is_truncated_in_the_trail(client_for):
    db = FakeDb(plan=[("SELECT note FROM transactions", {"one": (None,)})])
    resp = client_for(db).patch(
        "/api/transactions/818/note", json={"note": "x" * 400}
    )
    assert resp.status_code == 200, resp.text
    entry = db.entries("EDIT_NOTE")[0]
    assert len(entry["new_value"]) <= 120
    assert entry["new_value"].endswith("…")


def test_unchanged_note_writes_no_entry(client_for):
    db = FakeDb(plan=[("SELECT note FROM transactions", {"one": ("Same",)})])
    resp = client_for(db).patch("/api/transactions/818/note", json={"note": "Same"})
    assert resp.status_code == 200, resp.text
    assert db.entries("EDIT_NOTE") == []


# ── Receipt delete ─────────────────────────────────────────────────────────

def _receipt_delete_plan(*, txn_ids: list[tuple[int]]):
    return [
        ("SELECT id, file_hash, original_filename, vendor FROM documents",
         {"one": (101, "hash-101", "20260115-receipt.pdf", "Example Retailer")}),
        ("SELECT DISTINCT transaction_id FROM reconciliation_matches",
         {"all": txn_ids}),
    ]


def test_receipt_delete_is_audited(client_for):
    db = FakeDb(plan=_receipt_delete_plan(txn_ids=[]))
    resp = client_for(db).delete("/api/receipts/294")
    assert resp.status_code == 200, resp.text

    entries = db.entries("DELETE_RECEIPT")
    assert len(entries) == 1, db.audit
    entry = entries[0]
    assert entry["entity_type"] == "document"
    assert entry["entity_id"] == 294
    assert "20260115-receipt.pdf" in entry["old_value"]
    assert "Example Retailer" in entry["old_value"]
    assert entry.get("new_value") is None
    assert entry["performed_by"] == FAKE_USER.id


def test_receipt_delete_records_the_transactions_it_freed(client_for):
    db = FakeDb(plan=_receipt_delete_plan(txn_ids=[(801,), (802,)]))
    resp = client_for(db).delete("/api/receipts/294")
    assert resp.status_code == 200, resp.text
    entry = db.entries("DELETE_RECEIPT")[0]
    assert "2 transactions back to unmatched" in entry["old_value"]


def test_missing_receipt_delete_writes_no_entry(client_for):
    db = FakeDb(plan=[("SELECT id, file_hash, original_filename, vendor FROM documents",
                       {"one": None})])
    resp = client_for(db).delete("/api/receipts/999999")
    assert resp.status_code == 404
    assert db.audit == []


# ── Statement delete ───────────────────────────────────────────────────────

def _statement_delete_plan(*, txn_count: int):
    return [
        ("SELECT id FROM transactions WHERE user_id = %s AND source_file",
         {"all": [(i,) for i in range(1, txn_count + 1)]}),
        ("SELECT DISTINCT document_id FROM reconciliation_matches", {"all": []}),
        ("SELECT source_transaction_id, target_transaction_id FROM transaction_links",
         {"all": []}),
        ("DELETE FROM transactions WHERE user_id = %s AND source_file",
         {"rowcount": txn_count}),
        ("DELETE FROM statements", {"rowcount": 1}),
        ("DELETE FROM processed_files", {"rowcount": 1}),
    ]


def test_statement_delete_is_audited_with_the_transaction_count(client_for):
    db = FakeDb(plan=_statement_delete_plan(txn_count=4))
    resp = client_for(db).delete("/api/transactions/source-files/test_bank_statement.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 4

    entries = db.entries("DELETE_STATEMENT")
    assert len(entries) == 1, db.audit
    entry = entries[0]
    assert entry["entity_type"] == "transaction"
    assert entry["old_value"] == "test_bank_statement.pdf (4 transactions)"
    assert entry.get("new_value") is None
    assert entry["performed_by"] == FAKE_USER.id


def test_statement_delete_singular_count_reads_correctly(client_for):
    db = FakeDb(plan=_statement_delete_plan(txn_count=1))
    resp = client_for(db).delete("/api/transactions/source-files/one_row.csv")
    assert resp.status_code == 200, resp.text
    assert db.entries("DELETE_STATEMENT")[0]["old_value"] == "one_row.csv (1 transaction)"


def test_missing_statement_delete_writes_no_entry(client_for):
    db = FakeDb(plan=[
        ("SELECT id FROM transactions WHERE user_id = %s AND source_file", {"all": []}),
        ("SELECT DISTINCT document_id FROM reconciliation_matches", {"all": []}),
        ("SELECT source_transaction_id, target_transaction_id FROM transaction_links",
         {"all": []}),
    ])
    resp = client_for(db).delete("/api/transactions/source-files/never_uploaded.csv")
    assert resp.status_code == 404
    assert db.audit == []


def test_clear_all_statements_is_audited(client_for):
    db = FakeDb(plan=[
        ("SELECT COUNT(*) FROM transactions", {"one": (12,)}),
        ("DELETE FROM transactions", {"rowcount": 12}),
    ])
    resp = client_for(db).post("/api/transactions/source-files/clear-all")
    assert resp.status_code == 200, resp.text
    entry = db.entries("DELETE_STATEMENT")[0]
    assert entry["old_value"] == "all statements (12 transactions)"


def test_clear_all_receipts_is_audited(client_for):
    db = FakeDb(plan=[
        ("SELECT id, file_hash FROM documents", {"all": [(1, "h1"), (2, "h2")]}),
        ("DELETE FROM documents", {"rowcount": 2}),
    ])
    resp = client_for(db).post("/api/receipts/clear-all")
    assert resp.status_code == 200, resp.text
    entry = db.entries("DELETE_RECEIPT")[0]
    assert entry["old_value"] == "all receipts (2 documents)"


# ── Manual link / unlink (audited at the db layer, not the endpoint) ──────────

def test_manual_link_and_unlink_write_audit_rows():
    """LINK/UNLINK are written inside the same transaction as the link itself.

    These two verbs are audited at the database layer rather than in the
    endpoint, which is what makes the audit row atomic with the link. This
    test reads the writers' SQL so that behaviour cannot drift out of them
    without something failing.
    """
    import inspect

    from db.database_pg import DatabasePg

    link_sql = inspect.getsource(DatabasePg._insert_link)
    assert "INSERT INTO audit_log" in link_sql
    assert "'LINK'" in link_sql

    # An unlink stamps the row USER_REJECTED instead of deleting it, because
    # the row is what remembers the decision. The audit action is still named
    # UNLINK, for what the user did rather than for the SQL verb.
    unlink_sql = inspect.getsource(DatabasePg.reject_transaction_link)
    assert "INSERT INTO audit_log" in unlink_sql
    assert "'UNLINK'" in unlink_sql

    group_sql = inspect.getsource(DatabasePg.reject_transaction_link_group)
    assert "INSERT INTO audit_log" in group_sql
    assert "'UNLINK'" in group_sql


# ── The value shape the UI renders ─────────────────────────────────────────

def test_audit_value_keeps_plain_strings_readable():
    """/activity renders old_value/new_value as-is: a string must stay a string."""
    from db.database_pg import _audit_value

    assert _audit_value("Office supplies") == "Office supplies"
    assert _audit_value(None) is None
    assert _audit_value("") is None
    assert _audit_value({"category": "Office supplies"}) == '{"category": "Office supplies"}'

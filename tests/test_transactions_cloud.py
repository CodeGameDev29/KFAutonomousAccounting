"""Uploading bank statements: one tab per file, and a re-upload replaces.

A statement is identified by the filename the user uploaded, and that filename
is what the transactions page uses to choose a tab. Two consequences have to
hold or the page shows the wrong rows: two different statements must never
collapse into one source file, and re-sending a statement under a name already
used must replace what is there rather than being refused as a duplicate or
silently doubled.

Verifies that:
1. Uploading two different bank statement PDFs produces separate source file entries.
2. Re-uploading the same filename replaces old transactions (not blocked by 409).
3. The response includes source_file and replaced count for frontend tab selection.
4. The get_processed_file_info method returns correct data.
5. delete_transactions_by_source_file and delete_processed_file_by_filename
   work correctly.

All bank parsers and database writes are mocked.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Helpers ──────────────────────────────────────────────────────────────

FAKE_USER = AuthUser(id="user-upload", email="upload@example.com", role="authenticated", email_verified=True)

# Minimal PDF-like bytes (different content for each file)
PDF_CAD = b"%PDF-1.4 CAD-March-2026-statement-content"
PDF_USD = b"%PDF-1.4 USD-March-2026-statement-content"


def _override_current_user():
    async def _inner():
        return FAKE_USER
    return _inner


def _make_fake_db(*, replaced_count=0):
    """Return a mock DatabasePg for bank statement upload tests.

    Args:
        replaced_count: Number of transactions that delete_transactions_by_source_file
            returns (simulates replacing old data).
    """
    db = MagicMock()
    db.user_id = FAKE_USER.id
    db.delete_transactions_by_source_file = MagicMock(return_value=replaced_count)
    db.delete_processed_file_by_filename = MagicMock(return_value=0)
    db.insert_processed_file = MagicMock(return_value=1)
    db.insert_transaction = MagicMock()

    # Mock _conn context manager for source-files query
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    db._conn.return_value = mock_conn

    return db


def _make_fake_transaction(*, account_value="CAD", source_file="test.csv"):
    """Create a mock transaction object."""
    txn = MagicMock()
    txn.account = MagicMock(value=account_value)
    txn.source_file = source_file
    return txn


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_sse_events():
    """Suppress SSE event sending."""
    with patch("server.api.events.send_event", create=True):
        yield


# ── Test: Multi-File Creates Separate Entries ────────────────────────────

class TestMultiFileUpload:
    """Uploading two different bank statements creates two separate sets of transactions."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    def test_two_files_produce_distinct_source_files(self):
        """Uploading CAD and USD PDFs inserts transactions with different source_file values."""
        db = _make_fake_db(replaced_count=0)
        app.dependency_overrides[get_db] = lambda: db

        cad_txns = [_make_fake_transaction(account_value="CAD", source_file="temp.csv")]
        usd_txns = [_make_fake_transaction(account_value="USD", source_file="temp.csv")]

        uploaded_source_files = []

        def capture_insert(txn):
            uploaded_source_files.append(txn.source_file)

        db.insert_transaction = MagicMock(side_effect=capture_insert)

        client = TestClient(app, raise_server_exceptions=False)

        # Upload CAD statement
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "CAD")  # (csv_path, account)
                with patch("core.bank_parser.parse_csv", return_value=cad_txns):
                    resp1 = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("cad march 2026.pdf", io.BytesIO(PDF_CAD), "application/pdf")},
                    )

        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["source_file"] == "cad march 2026.pdf"
        # Verify source_file was overridden to original filename
        assert uploaded_source_files[-1] == "cad march 2026.pdf"

        uploaded_source_files.clear()

        # Upload USD statement
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "USD")
                with patch("core.bank_parser.parse_csv", return_value=usd_txns):
                    resp2 = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("usd march 2026.pdf", io.BytesIO(PDF_USD), "application/pdf")},
                    )

        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["source_file"] == "usd march 2026.pdf"
        # Verify source_file was overridden to original filename
        assert uploaded_source_files[-1] == "usd march 2026.pdf"

        # The two source_files are distinct
        assert body1["source_file"] != body2["source_file"]

    def test_upload_response_includes_account_info(self):
        """Each upload response includes the detected account type."""
        db = _make_fake_db(replaced_count=0)
        app.dependency_overrides[get_db] = lambda: db

        cad_txn = _make_fake_transaction(account_value="CAD")
        client = TestClient(app, raise_server_exceptions=False)

        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "CAD")
                with patch("core.bank_parser.parse_csv", return_value=[cad_txn]):
                    resp = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("cad march 2026.pdf", io.BytesIO(PDF_CAD), "application/pdf")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["account"] == "CAD"
        assert body["imported"] == 1

    def test_neither_file_returns_409(self):
        """Neither CAD nor USD upload returns 409 -- both succeed with 200."""
        db = _make_fake_db(replaced_count=0)
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app, raise_server_exceptions=False)

        for label, pdf_bytes, account in [
            ("cad march 2026.pdf", PDF_CAD, "CAD"),
            ("usd march 2026.pdf", PDF_USD, "USD"),
        ]:
            txn = _make_fake_transaction(account_value=account)
            with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
                with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                    mock_csv.return_value = (MagicMock(), account)
                    with patch("core.bank_parser.parse_csv", return_value=[txn]):
                        resp = client.post(
                            "/api/transactions/upload-statement",
                            files={"file": (label, io.BytesIO(pdf_bytes), "application/pdf")},
                        )

            assert resp.status_code == 200, f"{label} should not return 409"


# ── Test: Re-Upload Replaces Old Data ─────────────────────────────────────

class TestReUploadReplacesOldData:
    """Re-uploading a file with the same name replaces old transactions."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    def test_reupload_deletes_old_transactions(self):
        """Re-uploading 'cad march 2026.pdf' deletes old transactions first."""
        db = _make_fake_db(replaced_count=15)
        app.dependency_overrides[get_db] = lambda: db

        cad_txn = _make_fake_transaction(account_value="CAD")
        client = TestClient(app, raise_server_exceptions=False)

        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "CAD")
                with patch("core.bank_parser.parse_csv", return_value=[cad_txn]):
                    resp = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("cad march 2026.pdf", io.BytesIO(PDF_CAD), "application/pdf")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["replaced"] == 15
        assert body["source_file"] == "cad march 2026.pdf"
        db.delete_transactions_by_source_file.assert_called_once_with("cad march 2026.pdf")
        db.delete_processed_file_by_filename.assert_called_once_with("cad march 2026.pdf")

    def test_fresh_upload_shows_zero_replaced(self):
        """First-time upload shows replaced=0."""
        db = _make_fake_db(replaced_count=0)
        app.dependency_overrides[get_db] = lambda: db

        cad_txn = _make_fake_transaction(account_value="CAD")
        client = TestClient(app, raise_server_exceptions=False)

        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "CAD")
                with patch("core.bank_parser.parse_csv", return_value=[cad_txn]):
                    resp = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("cad march 2026.pdf", io.BytesIO(PDF_CAD), "application/pdf")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["replaced"] == 0
        db.delete_transactions_by_source_file.assert_called_once_with("cad march 2026.pdf")
        # Should NOT call delete_processed_file_by_filename when nothing was replaced
        db.delete_processed_file_by_filename.assert_not_called()

    def test_response_includes_source_file(self):
        """Upload response includes the source_file field for frontend tab selection."""
        db = _make_fake_db(replaced_count=0)
        app.dependency_overrides[get_db] = lambda: db

        usd_txn = _make_fake_transaction(account_value="USD")
        client = TestClient(app, raise_server_exceptions=False)

        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            with patch("core.pdf_statement_parser.pdf_to_csv") as mock_csv:
                mock_csv.return_value = (MagicMock(), "USD")
                with patch("core.bank_parser.parse_csv", return_value=[usd_txn]):
                    resp = client.post(
                        "/api/transactions/upload-statement",
                        files={"file": ("usd march 2026.pdf", io.BytesIO(PDF_USD), "application/pdf")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["source_file"] == "usd march 2026.pdf"


# ── Test: get_processed_file_info DB Method ──────────────────────────────

class TestGetProcessedFileInfo:
    """Unit tests for DatabasePg.get_processed_file_info method."""

    def test_returns_none_for_unknown_hash(self):
        """Unknown file hash returns None."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="test-user-id")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.get_processed_file_info("nonexistent_hash")
        assert result is None

    def test_returns_tuple_for_known_hash(self):
        """Known file hash returns (original_filename, created_at) tuple."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="test-user-id")

        expected_date = datetime(2026, 4, 1, 10, 30, 0)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("cad march 2026.pdf", expected_date)

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.get_processed_file_info("abc123def456")
        assert result is not None
        assert result[0] == "cad march 2026.pdf"
        assert result[1] == expected_date

    def test_query_scoped_to_user(self):
        """The query must include the user_id in the WHERE clause."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="specific-user-id")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        db.get_processed_file_info("test_hash")

        # Verify the execute call includes user_id
        call_args = mock_cursor.execute.call_args
        assert call_args is not None
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "user_id" in sql
        assert "specific-user-id" in params


# ── Test: delete_transactions_by_source_file DB Method ───────────────────

class TestDeleteTransactionsBySourceFile:
    """Unit tests for DatabasePg.delete_transactions_by_source_file method."""

    def test_returns_zero_when_no_transactions(self):
        """No matching transactions returns 0."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="test-user-id")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.delete_transactions_by_source_file("nonexistent.pdf")
        assert result == 0

    def test_query_scoped_to_user_and_source_file(self):
        """The delete flow must scope to user_id + source_file and clean up
        every dependent table (ledger entries, matched docs, matches, links)
        before deleting the transactions. Re-uploading a statement whose rows
        are LINKED otherwise answers 500 on the transaction_links FK."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="specific-user-id")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 5
        # fetchall is called three times: transaction ids, matched document ids,
        # then link rows
        mock_cursor.fetchall = MagicMock(side_effect=[[(1,), (2,)], [(10,)], []])
        # The receipt release goes through the shared status re-derivation
        # (DatabasePg._resync_document_statuses), which asks for the row's
        # current status plus whether an approved / pending match survives.
        # (status, has_approved, has_pending) = a MATCHED receipt with nothing
        # left proving it, i.e. the row that must be released.
        mock_cursor.fetchone = MagicMock(return_value=("MATCHED", False, False))

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.delete_transactions_by_source_file("cad march 2026.pdf")
        assert result == 5

        calls = mock_cursor.execute.call_args_list
        sqls = [c[0][0] for c in calls]
        # Every statement must scope to user_id
        for c in calls:
            assert "user_id" in c[0][0]
            assert "specific-user-id" in c[0][1]
        # Full cleanup chain, in FK-safe order
        joined = "\n".join(sqls)
        assert "DELETE FROM ledger_entries" in joined
        assert "UPDATE documents SET status = %s" in joined
        assert "DELETE FROM reconciliation_matches" in joined
        assert "DELETE FROM transaction_links" in joined
        assert "DELETE FROM transactions" in joined
        # The id-select and final delete are scoped to the source file
        assert any("cad march 2026.pdf" in c[0][1] for c in calls)

        def _idx(fragment: str) -> int:
            return next(i for i, s in enumerate(sqls) if fragment in s)

        # ledger_entries.match_id -> reconciliation_matches has no ON DELETE
        # clause, so the entries must go first or the delete hits the FK.
        assert _idx("DELETE FROM ledger_entries") < _idx("DELETE FROM reconciliation_matches")

        # The receipt reset must run AFTER the matches are deleted and must be
        # guarded on there being no surviving match, so a split-payment receipt
        # that still proves a transaction in another statement is NOT released
        # back into the unmatched pool. The guard is the shared re-derivation:
        # a SELECT that asks what still proves the receipt, followed by the
        # UPDATE only when the answer is "nothing".
        guard_idx = _idx("m.document_id = d.id")
        assert guard_idx > _idx("DELETE FROM reconciliation_matches")
        reset_idx = _idx("UPDATE documents SET status = %s")
        assert reset_idx > guard_idx
        assert calls[reset_idx][0][1][0] == "EXTRACTED"


# ── Test: delete_processed_file_by_filename DB Method ────────────────────

class TestDeleteProcessedFileByFilename:
    """Unit tests for DatabasePg.delete_processed_file_by_filename method."""

    def test_returns_zero_when_no_match(self):
        """No matching processed files returns 0."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="test-user-id")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.delete_processed_file_by_filename("nonexistent.pdf")
        assert result == 0

    def test_query_scoped_to_user_and_filename(self):
        """The delete query must scope to both user_id and original_filename."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="specific-user-id")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        result = db.delete_processed_file_by_filename("cad march 2026.pdf")
        assert result == 1

        call_args = mock_cursor.execute.call_args
        assert call_args is not None
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "user_id" in sql
        assert "original_filename" in sql
        assert "specific-user-id" in params
        assert "cad march 2026.pdf" in params

"""The edges of the upload-and-sync flow, where a user meets an error.

Each case here is a way the happy path stops being the path: a request with no
credentials, the same receipt uploaded twice, a dashboard asked to compute a
match rate out of zero transactions, a revoked mailbox token, one sync source
failing beside a healthy one, a duplicate match the schema itself has to
refuse, a file too large to accept, and a file type that is not a document at
all. Each must end in a specific, actionable answer rather than a 500 or a
silently wrong number.

All external services and database calls are mocked.
"""

from __future__ import annotations

import io
import os
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Helpers ──────────────────────────────────────────────────────────────

MOCK_USER = AuthUser(id="test-uuid-edge", email="edge@example.com", role="user", email_verified=True)

PDF_HEADER = b"%PDF-1.4 fake-pdf-content-for-testing"


def _override_current_user():
    async def _inner():
        return MOCK_USER
    return _inner


def _make_fake_db(*, file_already_processed: bool = False):
    """Return a mock DatabasePg for receipt upload tests."""
    db = MagicMock()
    db.user_id = MOCK_USER.id
    db.is_file_processed = MagicMock(return_value=file_already_processed)
    db.insert_processed_file = MagicMock(return_value=1)
    db.insert_document = MagicMock(return_value=42)

    # Mock _conn context manager for field_confidence UPDATE
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    db._conn.return_value = mock_conn

    return db


# Extraction result stub — stands in for a real extraction call
FAKE_EXTRACTION = {
    "vendor": "Test Vendor Inc.",
    "date": "2026-01-15",
    "total": "99.99",
    "currency": "CAD",
    "subtotal": "88.49",
    "tax_gst": "4.50",
    "tax_hst": "0",
    "tax_pst": "7.00",
    "tax_other": "0",
    "payment_method": "Credit Card",
    "invoice_number": "INV-001",
    "line_items": [],
    "_provider": "mock",
}


# ── No auth header -> 401 ───────────────────────────────────────────────

class TestNoAuth:
    """Requests without an authorization header must return 401."""

    def test_dashboard_no_auth_401(self):
        """GET /api/dashboard/summary without auth -> 401."""
        # Do NOT override get_current_user — let the real auth run.
        # AUTH_JWT_SECRET must be set or the dependency 503s on an
        # unconfigured deployment instead of 401-ing on a missing header.
        # server.auth.jwt_secret() reads the environment at call time, so
        # setting the var is enough; there is no module-level constant to swap.
        with patch.dict(os.environ, {"AUTH_JWT_SECRET": "test-secret-key-for-jwt"}):
            # Clear any overrides to test real auth
            app.dependency_overrides.pop(get_current_user, None)
            client = TestClient(app)

            resp = client.get("/api/dashboard/summary")
            assert resp.status_code == 401

    def test_receipts_no_auth_401(self):
        """POST /api/receipts/upload without auth -> 401."""
        with patch.dict(os.environ, {"AUTH_JWT_SECRET": "test-secret-key-for-jwt"}):
            app.dependency_overrides.pop(get_current_user, None)
            client = TestClient(app)

            resp = client.post(
                "/api/receipts/upload",
                files={"file": ("test.pdf", PDF_HEADER, "application/pdf")},
            )
            assert resp.status_code == 401


# ── Duplicate receipt -> 409 ────────────────────────────────────────────

class TestDuplicateReceipt:
    """Uploading the same file twice returns 409 on the second upload."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    @pytest.fixture(autouse=True)
    def _patch_extraction(self):
        """Patch LLM extraction for receipt tests.

        extract_document is imported lazily inside upload_receipt, so the patch
        has to cover both the source module and the target module (the latter
        in case an earlier call already cached the reference).
        """
        mock_extract = AsyncMock(return_value=FAKE_EXTRACTION)
        mock_build = MagicMock(return_value=MagicMock(
            vendor="Test", document_date=date(2026, 1, 15), total=Decimal("99.99"),
            currency="CAD", extraction_confidence="high", stored_path="/tmp/test.pdf",
        ))
        mock_confidence = MagicMock(
            return_value={"vendor": "high", "date": "high", "total": "high"},
        )
        with patch("core.extraction.extract_document", new=mock_extract):
            with patch("core.extraction.build_document_from_extraction", new=mock_build):
                with patch("core.extraction.compute_field_confidence", new=mock_confidence):
                    yield

    def test_first_upload_200_second_409(self):
        """First upload succeeds, second (same file) returns 409."""
        client = TestClient(app)

        # First upload: file not yet processed
        db_fresh = _make_fake_db(file_already_processed=False)
        app.dependency_overrides[get_db] = lambda: db_fresh

        resp1 = client.post(
            "/api/receipts/upload",
            files={"file": ("receipt.pdf", PDF_HEADER, "application/pdf")},
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "ok"

        # Second upload: same file hash already processed
        db_dup = _make_fake_db(file_already_processed=True)
        app.dependency_overrides[get_db] = lambda: db_dup

        resp2 = client.post(
            "/api/receipts/upload",
            files={"file": ("receipt.pdf", PDF_HEADER, "application/pdf")},
        )
        assert resp2.status_code == 409
        assert "duplicate" in resp2.json()["detail"].lower()


# ── Zero transactions: no division error ────────────────────────────────

class TestZeroTransactions:
    """A brand-new account has no transactions; the summary must still answer."""

    def test_dashboard_summary_zero_transactions(self):
        """get_dashboard_summary with no transactions returns match_rate=0.0."""
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        db = DatabasePg(pool=mock_pool, user_id="test-uuid-edge")

        mock_cursor = MagicMock()
        call_count = 0

        def fetchall_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []  # No status counts
            elif call_count == 2:
                return []  # No income/expenses
            return []

        mock_cursor.fetchall = MagicMock(side_effect=fetchall_side_effect)
        mock_cursor.fetchone = MagicMock(return_value=(0,))
        mock_cursor.execute = MagicMock()

        mock_conn = MagicMock()
        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

        @contextmanager
        def fake_conn():
            yield mock_conn

        db._conn = fake_conn

        # Must not raise ZeroDivisionError
        summary = db.get_dashboard_summary()
        assert summary["match_rate"] == 0.0
        assert summary["transaction_count"] == 0


# ── Gmail token refresh failure ─────────────────────────────────────────

class TestGmailTokenRefresh:
    """A revoked mailbox token must surface as a clear "reconnect" error.

    The refresh happens deep inside the gatherer; if the failure is swallowed
    there, the user sees an empty gather run and no reason for it.
    """

    def test_get_service_refresh_failure(self):
        """When Credentials.refresh() raises, _get_service should propagate the error."""
        from core.gather.gmail import GmailGatherer

        mock_cred_store = MagicMock()
        mock_cred_store.retrieve = MagicMock(side_effect=lambda key: {
            "gmail_refresh_token": "fake-refresh-token",
            "gmail_access_token": "expired-token",
        }.get(key))

        mock_db = MagicMock()
        gatherer = GmailGatherer(
            credentials_store=mock_cred_store,
            db=mock_db,
            output_dir="/tmp/test_gather",
        )

        with patch.dict(os.environ, {
            "GOOGLE_CLIENT_ID": "fake-client-id",
            "GOOGLE_CLIENT_SECRET": "fake-client-secret",
        }):
            # Patch at the source modules where _get_service imports from
            with patch("google.oauth2.credentials.Credentials") as MockCreds:
                mock_creds_instance = MagicMock()
                mock_creds_instance.expired = True
                mock_creds_instance.valid = False
                mock_creds_instance.refresh = MagicMock(
                    side_effect=Exception("Token has been revoked")
                )
                MockCreds.return_value = mock_creds_instance

                # _get_service is decorated with @retry (3 attempts).
                # Access __wrapped__ to bypass tenacity retry and test the error directly.
                unwrapped = gatherer._get_service.__wrapped__
                with pytest.raises(Exception, match="Token has been revoked"):
                    unwrapped(gatherer)

    def test_gather_returns_auth_error(self):
        """GmailGatherer.gather() captures auth failure in result.errors."""
        from core.gather.gmail import GmailGatherer

        mock_cred_store = MagicMock()
        mock_cred_store.retrieve = MagicMock(side_effect=lambda key: {
            "gmail_refresh_token": "fake-refresh-token",
            "gmail_access_token": "expired-token",
        }.get(key))

        mock_db = MagicMock()
        mock_db.get_gather_sources = MagicMock(return_value=[])
        gatherer = GmailGatherer(
            credentials_store=mock_cred_store,
            db=mock_db,
            output_dir="/tmp/test_gather",
        )

        with patch.object(
            gatherer, "_get_service",
            side_effect=Exception("Token has been revoked"),
        ):
            result = gatherer.gather()
            assert len(result.errors) > 0
            assert any("authenticate" in e.lower() or "token" in e.lower() for e in result.errors)


# ── Sync error isolation ────────────────────────────────────────────────

class TestSyncErrorIsolation:
    """When one sync source fails, the others still run and still report."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    def test_wise_fails_paypal_still_runs(self):
        """If Wise sync raises, PayPal sync still executes and returns results."""
        db = MagicMock()
        db.user_id = MOCK_USER.id
        db.get_unmatched_transactions = MagicMock(return_value=[])
        db.get_unmatched_documents = MagicMock(return_value=[])
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app)

        # Wise will raise an error (patched at source module), PayPal will succeed
        with patch("core.credentials_cloud.CloudCredentialStore") as MockCreds:
            mock_cred_instance = MagicMock()
            mock_cred_instance.retrieve = MagicMock(return_value="fake-token")
            MockCreds.return_value = mock_cred_instance

            with patch("core.wise_api.get_profile_id", side_effect=Exception("Wise API down")):
                with patch("core.paypal_api.get_paypal_credentials", return_value=("id", "secret")):
                    with patch("core.paypal_api.get_access_token", return_value="paypal-token"):
                        with patch("core.paypal_api.fetch_paypal_transactions", return_value=[]):
                            resp = client.post(
                                "/api/actions/sync",
                                json={"sources": ["wise", "paypal"], "days": 30, "auto_reconcile": False},
                            )

        assert resp.status_code == 200
        data = resp.json()
        # Wise should have an error
        assert "error" in data["results"]["wise"]
        # PayPal should have run successfully
        assert data["results"]["paypal"]["total"] == 0


# ── Unique constraint on reconciliation_matches ─────────────────────────

class TestUniqueMatchConstraint:
    """The schema itself refuses a duplicate match.

    Re-running reconciliation must not be able to pair the same transaction
    and document twice; a database constraint is the only guarantee that holds
    no matter which code path inserts the row.
    """

    def test_idx_matches_dedup_exists_in_schema(self):
        """Verify idx_matches_dedup unique index is defined in schema_pg.sql."""
        schema_path = Path(__file__).resolve().parent.parent / "db" / "schema_pg.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")

        # The unique index should exist
        assert "idx_matches_dedup" in schema_sql

        # It should be a UNIQUE index
        assert "CREATE UNIQUE INDEX" in schema_sql

        # It should be on reconciliation_matches with the key columns
        assert "reconciliation_matches" in schema_sql.split("idx_matches_dedup")[1].split(";")[0]
        assert "transaction_id" in schema_sql.split("idx_matches_dedup")[1].split(";")[0]
        assert "document_id" in schema_sql.split("idx_matches_dedup")[1].split(";")[0]
        assert "user_id" in schema_sql.split("idx_matches_dedup")[1].split(";")[0]

    def test_dedup_index_excludes_rejected(self):
        """The dedup index has a WHERE clause excluding USER_REJECTED matches."""
        schema_path = Path(__file__).resolve().parent.parent / "db" / "schema_pg.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")

        # Find the CREATE UNIQUE INDEX line for idx_matches_dedup
        idx = schema_sql.index("idx_matches_dedup")
        # Get the statement from that point to the next semicolon
        stmt = schema_sql[idx:schema_sql.index(";", idx)]

        # Should have a WHERE clause excluding USER_REJECTED
        assert "WHERE" in stmt
        assert "USER_REJECTED" in stmt


# ── File over the size limit -> 413 ─────────────────────────────────────

class TestFileTooLarge:
    """Uploading a file larger than the 50MB limit returns 413, not a crash."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    def test_oversized_file_413(self):
        """A file > MAX_FILE_SIZE (50MB) is rejected with 413."""
        from server.api.receipts import MAX_FILE_SIZE

        assert MAX_FILE_SIZE == 50 * 1024 * 1024, "MAX_FILE_SIZE should be 50MB"

        db = _make_fake_db(file_already_processed=False)
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app)

        # A payload one byte over the limit, sent through the real upload path:
        # the endpoint reads the bytes first and then checks the size, so the
        # rejection has to come from that check rather than from the transport.
        with patch("server.api.receipts.UploadFile") as _:
            large_content = b"X" * (MAX_FILE_SIZE + 1)


            resp = client.post(
                "/api/receipts/upload",
                files={"file": ("huge.pdf", io.BytesIO(large_content), "application/pdf")},
            )

            assert resp.status_code == 413
            assert "too large" in resp.json()["detail"].lower()


# ── Unsupported file type -> 400 ────────────────────────────────────────

class TestUnsupportedFileType:
    """A file that is not a document is refused at the door, with the list."""

    @pytest.fixture(autouse=True)
    def _setup_auth(self):
        app.dependency_overrides[get_current_user] = _override_current_user()
        yield
        app.dependency_overrides.clear()

    def test_exe_file_rejected_400(self):
        """A .exe file is rejected with 400 and lists supported types."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app)

        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("malware.exe", b"MZ\x90\x00", "application/x-msdownload")},
        )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Unsupported file type" in detail
        assert ".exe" in detail

    def test_txt_file_rejected_400(self):
        """A .txt file is rejected with 400."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app)

        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("notes.txt", b"Hello world", "text/plain")},
        )

        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["detail"]

    def test_supported_extensions_match_production(self):
        """Verify the SUPPORTED_EXTENSIONS constant matches expected set."""
        from server.api.receipts import SUPPORTED_EXTENSIONS

        assert ".pdf" in SUPPORTED_EXTENSIONS
        assert ".jpg" in SUPPORTED_EXTENSIONS
        assert ".jpeg" in SUPPORTED_EXTENSIONS
        assert ".png" in SUPPORTED_EXTENSIONS
        assert ".webp" in SUPPORTED_EXTENSIONS
        assert ".exe" not in SUPPORTED_EXTENSIONS
        assert ".txt" not in SUPPORTED_EXTENSIONS
        assert ".zip" not in SUPPORTED_EXTENSIONS

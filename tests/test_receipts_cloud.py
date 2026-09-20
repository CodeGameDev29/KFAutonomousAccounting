"""Tests for cloud receipt handling.

Verifies that duplicate receipt uploads are rejected with HTTP 409 while
first-time uploads succeed.  All LLM extraction, S3 storage, and database
calls are mocked so that these tests run in isolation without external
services.

Also includes concurrent upload tests validating that the server handles
multiple simultaneous uploads without cascading failures.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Helpers ──────────────────────────────────────────────────────────────

FAKE_USER = AuthUser(id="user-receipts", email="receipts@example.com", role="authenticated", email_verified=True)

PDF_HEADER = b"%PDF-1.4 fake-pdf-content-for-testing"


def _override_current_user():
    """FastAPI dependency override that skips JWT verification."""
    async def _inner():
        return FAKE_USER
    return _inner


def _make_fake_db(*, file_already_processed: bool = False):
    """Return a mock DatabasePg whose dedup method honours the flag.

    Note: The receipt upload handler uses db.is_document_hash_exists() for
    dedup (not db.is_file_processed). Both are mocked for completeness.
    """
    db = MagicMock()
    db.user_id = FAKE_USER.id
    db.is_file_processed = MagicMock(return_value=file_already_processed)
    db.is_document_hash_exists = MagicMock(return_value=file_already_processed)
    db.insert_processed_file = MagicMock(return_value=1)
    db.insert_document = MagicMock(return_value=42)
    # _conn used by the UPDATE for field_confidence — return a no-op context manager
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    db._conn.return_value = mock_conn
    return db


# Extraction result stub — no model is called
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


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_extraction_and_s3():
    """Patch LLM extraction and S3 upload for every test in this module."""
    # Patch the lazy imports inside the upload handler function body
    fake_doc = MagicMock(
        original_filename="test.pdf", stored_path="/tmp/test.pdf", file_hash="abc123",
        vendor="TestVendor", document_date=None, currency="CAD", subtotal=None,
        tax_gst=None, tax_hst=None, tax_pst=None, tax_other=None, total=None,
        payment_method=None, invoice_number=None, line_items=None,
        extraction_confidence="high", extraction_raw_json=None,
        status=MagicMock(value="EXTRACTED"),
    )
    mock_extract = AsyncMock(return_value=FAKE_EXTRACTION)
    mock_build = MagicMock(return_value=fake_doc)
    mock_conf = MagicMock(return_value={})

    import core.extraction
    orig_extract = getattr(core.extraction, "extract_document", None)
    orig_build = getattr(core.extraction, "build_document_from_extraction", None)
    orig_conf = getattr(core.extraction, "compute_field_confidence", None)

    core.extraction.extract_document = mock_extract
    core.extraction.build_document_from_extraction = mock_build
    core.extraction.compute_field_confidence = mock_conf
    try:
        yield
    finally:
        if orig_extract is not None:
            core.extraction.extract_document = orig_extract
        if orig_build is not None:
            core.extraction.build_document_from_extraction = orig_build
        if orig_conf is not None:
            core.extraction.compute_field_confidence = orig_conf


@pytest.fixture(autouse=True)
def _patch_sse_events():
    """Suppress SSE event sending."""
    with patch("server.api.receipts.send_event", create=True):
        yield


# ── Duplicate receipt upload rejected with 409 ────────────────────────

class TestDuplicateReceiptUpload:
    """First upload succeeds (200), second upload of the same file is
    rejected with 409 and a ``"duplicate"`` detail message."""

    def _upload(self, client: TestClient, content: bytes = PDF_HEADER, filename: str = "receipt.pdf"):
        return client.post(
            "/api/receipts/upload",
            files={"file": (filename, io.BytesIO(content), "application/pdf")},
        )

    def test_first_upload_succeeds(self):
        """First upload: db.is_document_hash_exists returns False -> 200 OK."""
        fake_db = _make_fake_db(file_already_processed=False)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = self._upload(client)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
            body = resp.json()
            assert body["status"] == "ok"
            assert body.get("doc_id") is not None

            # Verify dedup check was called
            fake_db.is_document_hash_exists.assert_called_once()
            # Verify the file was recorded in processed_files
            fake_db.insert_processed_file.assert_called_once()
        finally:
            app.dependency_overrides.clear()

    def test_duplicate_upload_rejected_409(self):
        """Second upload (same file): db.is_document_hash_exists returns True -> 409."""
        fake_db = _make_fake_db(file_already_processed=True)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = self._upload(client)
            assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
            body = resp.json()
            assert "duplicate" in body["detail"].lower()

            # Verify dedup check was called but no document was inserted
            fake_db.is_document_hash_exists.assert_called_once()
            fake_db.insert_processed_file.assert_not_called()
            fake_db.insert_document.assert_not_called()
        finally:
            app.dependency_overrides.clear()

    def test_first_then_duplicate_sequence(self):
        """Simulates the real two-upload sequence: first succeeds, second is 409."""
        fake_db = _make_fake_db(file_already_processed=False)
        call_count = 0

        def _is_document_hash_exists_side_effect(file_hash: str) -> bool:
            nonlocal call_count
            call_count += 1
            # First call: not yet processed.  Second call: already processed.
            return call_count > 1

        fake_db.is_document_hash_exists = MagicMock(side_effect=_is_document_hash_exists_side_effect)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)

            # First upload — should succeed
            resp1 = self._upload(client)
            assert resp1.status_code == 200, f"First upload: expected 200, got {resp1.status_code}"

            # Second upload (same content) — should be rejected
            resp2 = self._upload(client)
            assert resp2.status_code == 409, f"Second upload: expected 409, got {resp2.status_code}"
            assert "duplicate" in resp2.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    def test_unsupported_file_type_rejected(self):
        """Non-PDF/image files should be rejected with 400, not 409."""
        fake_db = _make_fake_db(file_already_processed=False)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/receipts/upload",
                files={"file": ("notes.txt", io.BytesIO(b"not a pdf"), "text/plain")},
            )
            assert resp.status_code == 400
            assert "unsupported" in resp.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


# ── Concurrent upload handling ─────────────────────────────────────────

class TestConcurrentReceiptUploads:
    """Verify the server can handle multiple simultaneous receipt upload
    requests without cascading failures."""

    def test_concurrent_receipt_uploads(self):
        """10 concurrent uploads should all succeed independently.

        Each upload uses unique file content (different hash) to avoid
        deduplication rejects.  The server-side extraction semaphore
        queues excess requests rather than rejecting them.
        """
        fake_db = _make_fake_db(file_already_processed=False)
        fake_db.is_document_hash_exists = MagicMock(return_value=False)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)

            def upload_one(i: int):
                # Each file has unique content so hashes differ
                content = f"%PDF-1.4 fake-pdf-content-{i}-unique".encode()
                return client.post(
                    "/api/receipts/upload",
                    files={"file": (f"receipt_{i}.pdf", io.BytesIO(content), "application/pdf")},
                )

            # Fire 10 concurrent uploads using threads
            responses = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(upload_one, i) for i in range(10)]
                for future in as_completed(futures):
                    responses.append(future.result())

            # All 10 should succeed (200)
            successes = [r for r in responses if r.status_code == 200]
            assert len(successes) == 10, (
                f"Expected 10 successes, got {len(successes)}. "
                f"Status codes: {[r.status_code for r in responses]}"
            )

            # Each response should have doc_id
            for resp in successes:
                body = resp.json()
                assert body["status"] == "ok"
                assert body.get("doc_id") is not None
        finally:
            app.dependency_overrides.clear()

    def test_concurrent_uploads_individual_failures_do_not_cascade(self):
        """When some uploads fail (e.g., duplicates), others should still succeed."""
        call_count = 0

        def _is_document_hash_exists_alternating(file_hash: str) -> bool:
            """Odd-indexed files are treated as duplicates."""
            nonlocal call_count
            call_count += 1
            return call_count % 2 == 0  # Every other file is a "duplicate"

        fake_db = _make_fake_db(file_already_processed=False)
        fake_db.is_document_hash_exists = MagicMock(side_effect=_is_document_hash_exists_alternating)

        app.dependency_overrides[get_current_user] = _override_current_user()
        app.dependency_overrides[get_db] = lambda: fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)

            def upload_one(i: int):
                content = f"%PDF-1.4 fake-pdf-content-{i}-mixed".encode()
                return client.post(
                    "/api/receipts/upload",
                    files={"file": (f"receipt_{i}.pdf", io.BytesIO(content), "application/pdf")},
                )

            responses = []
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(upload_one, i) for i in range(6)]
                for future in as_completed(futures):
                    responses.append(future.result())

            # Mix of 200 and 409 responses — no 500 errors
            status_codes = [r.status_code for r in responses]
            assert all(sc in (200, 409) for sc in status_codes), (
                f"Unexpected status codes: {status_codes}"
            )
            # At least some should succeed
            assert any(sc == 200 for sc in status_codes), "No uploads succeeded"
            # At least some should be duplicates
            assert any(sc == 409 for sc in status_codes), "No duplicates detected"
        finally:
            app.dependency_overrides.clear()

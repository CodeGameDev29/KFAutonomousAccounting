"""A statement upload the API refuses must not delete anything.

``POST /api/transactions/upload-statement`` replaces the prior import of a
statement before it parses the new one. If it only discovered on the
``statements_unique_hash`` INSERT that it was holding bytes it already had, a
second drop of a file still in the queue would delete the in-flight
``statements`` row out from under the running job.

So both duplicate checks run first, before anything is deleted. What is
asserted here:

  * same bytes under a different name → 409, nothing deleted;
  * same bytes still in the queue → 202 carrying the first upload's job,
    ``duplicate: true``, nothing deleted;
  * same bytes under the *same* name is still the documented
    replace-on-re-upload path, not a duplicate — it goes through;
  * the ``statements_unique_hash`` catch is still there as a backstop for the
    race between the check and the INSERT;
  * the auto zone (``POST /api/receipts/upload?auto_detect=1``) gets the same
    treatment on the statement path it shares, and the plain receipt path's
    duplicate checks stay ahead of its own INSERT too.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.access import require_verified_email
from server.api import transactions as txn_api
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

PDF_BYTES = b"%PDF-1.4 one-month-of-bank-statement"
CSV_BYTES = b"Date,Description,Amount\n2026-01-02,Coffee,-4.50\n"

FAKE_USER = AuthUser(
    id="user-dupe", email="dupe@example.com", role="authenticated", email_verified=True
)

QUEUED_JOB = {
    "id": "11111111-2222-3333-4444-555555555555",
    "user_id": FAKE_USER.id,
    "kind": "statement",
    "filename": "bmo march 2026.pdf",
    "stored_path": "stored/bmo.pdf",
    "file_hash": "hash-of-the-pdf",
    "status": "queued",
    "position": 7,
    "attempts": 0,
    "max_attempts": 3,
    "transient_waits": 0,
    "next_attempt_at": None,
    "pages_total": 4,
    "pages_done": 0,
    "error_code": None,
    "error_message": None,
    "result": {"statement_id": "stmt-already-queued"},
    "created_at": None,
    "started_at": None,
    "finished_at": None,
    "updated_at": None,
}



def _fake_db() -> MagicMock:
    db = MagicMock()
    db.user_id = FAKE_USER.id
    # The two duplicate checks: "no" unless a test says otherwise.
    db.find_active_processing_job_by_hash = MagicMock(return_value=None)
    db.find_statement_by_hash = MagicMock(return_value=None)
    db.source_file_holding_period = MagicMock(return_value=None)
    db.delete_transactions_by_source_file = MagicMock(return_value=0)
    db.delete_statements_by_source_file = MagicMock(return_value=0)
    db.delete_processed_file_by_filename = MagicMock(return_value=0)
    db.insert_processed_file = MagicMock(return_value=1)
    db.insert_statement_processing = MagicMock(return_value="stmt-new")
    db.update_statement_status = MagicMock(return_value=1)
    db.insert_transaction = MagicMock()
    db.insert_audit_log = MagicMock()
    db.get_document_by_hash = MagicMock(return_value=None)

    def _insert_job(**kwargs):
        job = dict(QUEUED_JOB)
        job.update({
            "kind": kwargs["kind"],
            "filename": kwargs["filename"],
            "stored_path": kwargs.get("stored_path"),
            "file_hash": kwargs.get("file_hash"),
            "pages_total": kwargs.get("pages_total"),
            "status": kwargs.get("status", "queued"),
            "result": kwargs.get("result"),
        })
        return job

    db.insert_processing_job = MagicMock(side_effect=_insert_job)
    return db


@pytest.fixture
def db() -> MagicMock:
    return _fake_db()


@pytest.fixture
def client(db):
    async def _user():
        return FAKE_USER


    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_verified_email] = _user
    app.dependency_overrides[get_db] = lambda: db
    with patch("core.file_storage.upload_file", return_value="stored/path.pdf"), \
         patch("server.jobs.document_queue.wake_worker"), \
         patch("server.jobs.document_queue.emit_job_event"):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _post_statement(client: TestClient, name: str):
    return client.post(
        "/api/transactions/upload-statement",
        files={"file": (name, io.BytesIO(PDF_BYTES), "application/pdf")},
    )


def _post_auto_receipt(client: TestClient, name: str):
    return client.post(
        "/api/receipts/upload?auto_detect=1",
        files={"file": (name, io.BytesIO(PDF_BYTES), "application/pdf")},
    )


def _assert_nothing_was_deleted(db: MagicMock) -> None:
    db.insert_processing_job.assert_not_called()
    db.insert_statement_processing.assert_not_called()
    db.delete_transactions_by_source_file.assert_not_called()
    db.delete_statements_by_source_file.assert_not_called()


class TestAlreadyImported:
    def test_the_same_statement_under_another_name_is_409_before_any_delete(
        self, client, db
    ):
        db.find_statement_by_hash.return_value = {
            "id": "stmt-already-there",
            "source_file": "bmo march 2026.pdf",
            "status": "imported",
            "row_count": 31,
            "created_at": None,
        }
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, "bmo-march-2026 (1).pdf")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "Statement already imported."
        _assert_nothing_was_deleted(db)

    def test_re_uploading_the_same_filename_still_replaces_rather_than_409(
        self, client, db
    ):
        """Replace-on-re-upload is the documented escape from a bad parse."""
        db.find_statement_by_hash.return_value = {
            "id": "stmt-already-there",
            "source_file": "bmo march 2026.pdf",
            "status": "imported",
            "row_count": 31,
            "created_at": None,
        }
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, "bmo march 2026.pdf")

        assert resp.status_code == 202, resp.text
        db.delete_statements_by_source_file.assert_called_once_with("bmo march 2026.pdf")

    def test_the_unique_constraint_is_still_the_backstop(self, client, db):
        """The check and the INSERT are not one statement; the race still exists."""
        db.insert_statement_processing.side_effect = Exception(
            'duplicate key value violates unique constraint "statements_unique_hash"'
        )
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, "bmo march 2026.pdf")

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Statement already imported."
        db.insert_processing_job.assert_not_called()


class TestAlreadyInTheQueue:
    def test_the_same_bytes_still_queued_answer_with_that_job_without_reprocessing(
        self, client, db
    ):
        db.find_active_processing_job_by_hash.return_value = dict(QUEUED_JOB)
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, "bmo march 2026 (again).pdf")

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["duplicate"] is True
        assert body["job_id"] == QUEUED_JOB["id"]
        assert body["statement_id"] == "stmt-already-queued"
        _assert_nothing_was_deleted(db)

    def test_the_in_flight_statement_row_is_not_deleted_by_the_second_drop(
        self, client, db
    ):
        """The data-loss half: running ``replace_prior_statement_data`` on the
        *same filename* before looking would close the statements row that the
        job already running is about to fill in."""
        db.find_active_processing_job_by_hash.return_value = dict(QUEUED_JOB)
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, QUEUED_JOB["filename"])

        assert resp.status_code == 202
        assert resp.json()["duplicate"] is True
        db.delete_statements_by_source_file.assert_not_called()
        db.delete_transactions_by_source_file.assert_not_called()

    def test_an_unanswerable_dedupe_check_never_rejects_a_real_upload(
        self, client, db
    ):
        """A database hiccup in a *check* must not read as "this is a duplicate"."""
        db.find_active_processing_job_by_hash.side_effect = RuntimeError("pool exhausted")
        db.find_statement_by_hash.side_effect = RuntimeError("pool exhausted")
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_statement(client, "bmo march 2026.pdf")

        assert resp.status_code == 202, resp.text
        db.insert_processing_job.assert_called_once()


class TestTheAutoZoneSharesTheGuard:
    def test_an_auto_detected_statement_already_imported_is_409_before_any_delete(
        self, client, db
    ):
        db.find_statement_by_hash.return_value = {
            "id": "stmt-already-there",
            "source_file": "bmo march 2026.pdf",
            "status": "imported",
            "row_count": 31,
            "created_at": None,
        }
        detection = MagicMock(
            is_statement=True, summary="looks like a statement",
        )
        detection.as_dict.return_value = {"score": 9}
        with patch("core.statement_detector.detect_bank_statement", return_value=detection):
            resp = _post_auto_receipt(client, "bmo-march-2026 (1).pdf")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "Statement already imported."
        _assert_nothing_was_deleted(db)

    def test_an_auto_detected_statement_already_queued_keeps_its_reroute_link(
        self, client, db
    ):
        """The 202 the auto zone gets back has to stay usable: the browser offers
        "no, it's a receipt" off ``reroute_url`` whether this was the first drop
        of the file or the second."""
        db.find_active_processing_job_by_hash.return_value = dict(QUEUED_JOB)
        detection = MagicMock(is_statement=True, summary="looks like a statement")
        detection.as_dict.return_value = {"score": 9}
        with patch("core.statement_detector.detect_bank_statement", return_value=detection):
            resp = _post_auto_receipt(client, "bmo march 2026.pdf")

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["duplicate"] is True
        assert body["detected_as"] == txn_api.DETECTED_AS_STATEMENT
        assert body["reroute_url"] == f"/api/jobs/{QUEUED_JOB['id']}/reroute"


class TestTheReceiptPathAlreadyChecksFirst:
    """The same ordering on the receipt path, so it cannot drift out of step."""

    def test_an_already_extracted_receipt_returns_the_existing_doc_unextracted(
        self, client, db
    ):
        db.get_document_by_hash.return_value = {
            "id": 42,
            "vendor_name": "Sample Street Cafe",
            "document_date": "2026-01-02",
            "total": "14.20",
            "currency": "CAD",
            "extraction_confidence": "HIGH",
        }
        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("receipt.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["duplicate"] is True
        db.insert_processing_job.assert_not_called()

    def test_a_receipt_already_in_the_queue_returns_that_job_unextracted(
        self, client, db
    ):
        queued = dict(QUEUED_JOB)
        queued["kind"] = "receipt"
        db.find_active_processing_job_by_hash.return_value = queued
        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("receipt.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["duplicate"] is True
        db.insert_processing_job.assert_not_called()


class TestCsvSharesTheGuard:
    def test_a_csv_whose_bytes_are_already_imported_is_409_before_any_delete(
        self, client, db
    ):
        """The hash guard sits ahead of the format branch, so it covers CSV too."""
        db.find_statement_by_hash.return_value = {
            "id": "stmt-already-there",
            "source_file": "jan.csv",
            "status": "imported",
            "row_count": 1,
            "created_at": None,
        }
        resp = client.post(
            "/api/transactions/upload-statement",
            files={"file": ("jan (1).csv", io.BytesIO(CSV_BYTES), "text/csv")},
        )
        assert resp.status_code == 409, resp.text
        _assert_nothing_was_deleted(db)

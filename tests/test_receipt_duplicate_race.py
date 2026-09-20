"""A duplicate receipt upload must never surface as a 500 or a dead job.

`POST /api/receipts/upload` can collide on the `(user_id, file_hash)` dedup
index inside `insert_processed_file`. An unhandled
`psycopg2.errors.UniqueViolation` there answers 500, and it raises *after* the
extraction has finished, so the work is done and then thrown away.

The NOT_A_FINANCIAL_DOCUMENT branch writes `processed_files` and never
`documents`, so a re-upload of the same non-financial file gets past the
`get_document_by_hash` guard (there is no document row to find) and lands on the
same (user_id, file_hash) again. The same collision is reachable by two uploads
of one file in flight together.

The persist step runs in `process_receipt_document`, called by the queue
worker rather than by the request, so that is where these tests hold the
line: a UniqueViolation anywhere in the
persist step resolves to the existing document with a single WARNING, never an
exception that would fail the job and never a traceback. The endpoint's own two
duplicate guards — an already-extracted document, and the same bytes already in
the queue — are asserted over HTTP at the bottom.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from psycopg2.errors import UniqueViolation

from db.database_pg import DatabasePg
from server.api.receipts import process_receipt_document
from server.app import app
from server.auth import AuthUser
from server.deps import get_db

PDF_BYTES = b"%PDF-1.4 duplicate-race-fixture"

FAKE_USER = AuthUser(
    id="user-duplicate", email="duplicate@example.com", role="authenticated", email_verified=True
)

EXTRACTION = {
    "vendor": "Duplicate Vendor Ltd.",
    "date": "2026-02-03",
    "total": "41.25",
    "currency": "CAD",
    "subtotal": "39.29",
    "tax_gst": "1.96",
    "tax_hst": "0",
    "tax_pst": "0",
    "tax_other": "0",
    "payment_method": "Visa",
    "invoice_number": "DUP-1",
    "line_items": [],
    "_provider": "test",
    "_model": "test-model",
}

EXISTING_DOC = {
    "id": 4,
    "original_filename": "receipt.pdf",
    "vendor": "Duplicate Vendor Ltd.",
    "document_date": "2026-02-03",
    "total": "41.25",
    "currency": "CAD",
    "extraction_confidence": "high",
    "status": "EXTRACTED",
}

QUEUED_JOB = {
    "id": "11111111-2222-3333-4444-555555555555",
    "user_id": FAKE_USER.id,
    "kind": "receipt",
    "filename": "receipt.pdf",
    "stored_path": "user-duplicate/202602/abc_receipt.pdf",
    "file_hash": "deadbeef",
    "status": "queued",
    "position": 7,
    "attempts": 0,
    "max_attempts": 3,
    "transient_waits": 0,
    "next_attempt_at": None,
    "pages_total": 1,
    "pages_done": 0,
    "error_code": None,
    "error_message": None,
    "result": None,
    "created_at": None,
    "started_at": None,
    "finished_at": None,
    "updated_at": None,
}


class FakeDb:
    """Only the surface the receipt path touches, with scriptable failures."""

    def __init__(
        self,
        *,
        hash_lookups: list[dict | None] | None = None,
        processed_file_error: Exception | None = None,
        document_error: Exception | None = None,
        active_job: dict | None = None,
    ) -> None:
        self.user_id = FAKE_USER.id
        self._hash_lookups = list(hash_lookups or [])
        self._processed_file_error = processed_file_error
        self._document_error = document_error
        self._active_job = active_job
        self.hash_lookup_count = 0
        self.audit_entries: list[dict] = []
        self.inserted_jobs: list[dict] = []

    # -- dedup ---------------------------------------------------------------
    def get_document_by_hash(self, file_hash: str):
        self.hash_lookup_count += 1
        if self._hash_lookups:
            return self._hash_lookups.pop(0)
        return None

    def find_active_processing_job_by_hash(self, kind: str, file_hash: str):
        return self._active_job

    # -- the queue -----------------------------------------------------------
    def insert_processing_job(self, **kwargs):
        self.inserted_jobs.append(kwargs)
        job = dict(QUEUED_JOB)
        job.update({
            "kind": kwargs["kind"],
            "filename": kwargs["filename"],
            "stored_path": kwargs.get("stored_path"),
            "file_hash": kwargs.get("file_hash"),
            "pages_total": kwargs.get("pages_total"),
            "position": 1,
        })
        return job

    def insert_processed_file(self, file_hash, original_filename, stored_path=None):
        if self._processed_file_error is not None:
            raise self._processed_file_error
        return 1

    def insert_document(self, doc):
        if self._document_error is not None:
            raise self._document_error
        return EXISTING_DOC["id"]

    def insert_audit_log(self, **kwargs):
        self.audit_entries.append(kwargs)
        return len(self.audit_entries)

    # -- the field_confidence UPDATE the persist step runs inline -------------
    def _conn(self):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn


def _dedup_violation() -> UniqueViolation:
    return UniqueViolation(
        'duplicate key value violates unique constraint "idx_processed_files_dedup"'
    )


@pytest.fixture()
def patched_extraction(monkeypatch):
    """Extraction stands in for the extraction call."""
    import core.extraction

    async def _extract(path, db=None, **kwargs):
        return dict(EXTRACTION)

    monkeypatch.setattr(core.extraction, "extract_document", _extract)
    yield


@pytest.fixture()
def receipt_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt.pdf"
    path.write_bytes(PDF_BYTES)
    return path


async def _process(db: FakeDb, receipt_file: Path) -> dict:
    return await process_receipt_document(
        db=db,
        user_id=FAKE_USER.id,
        filename="receipt.pdf",
        file_hash="deadbeef",
        file_path=receipt_file,
        stored_path="user-duplicate/202602/deadbeef_receipt.pdf",
    )


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── The 500 itself ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unique_violation_on_processed_file_resolves_to_the_existing_document(
    patched_extraction, receipt_file, caplog
):
    """A UniqueViolation out of insert_processed_file resolves, it does not raise.

    The other job's document is the answer — a finished job carrying
    duplicate: true, never an exception on work that is already processed and
    complete.
    """
    db = FakeDb(
        hash_lookups=[EXISTING_DOC],  # the post-conflict re-check finds it
        processed_file_error=_dedup_violation(),
    )
    with caplog.at_level(logging.DEBUG):
        result = await _process(db, receipt_file)

    assert result["duplicate"] is True
    assert result["document_id"] == EXISTING_DOC["id"]
    assert result["summary"]["vendor"] == EXISTING_DOC["vendor"]
    assert result["summary"]["total"] == 41.25
    assert db.hash_lookup_count == 1, "the conflict must re-check for the raced document"


@pytest.mark.asyncio
async def test_duplicate_race_logs_one_warning_and_no_traceback(
    patched_extraction, receipt_file, caplog
):
    """A handled race is one WARNING line — a traceback here buries real crashes."""
    db = FakeDb(hash_lookups=[EXISTING_DOC], processed_file_error=_dedup_violation())
    with caplog.at_level(logging.DEBUG):
        await _process(db, receipt_file)

    warnings = _warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    only = warnings[0]
    assert only.levelno == logging.WARNING, only.levelname
    assert only.exc_info is None, "must not log the traceback for a handled duplicate"
    assert "duplicate" in only.getMessage().lower()
    assert str(EXISTING_DOC["id"]) in only.getMessage()


@pytest.mark.asyncio
async def test_unique_violation_from_document_insert_resolves_the_same_way(
    patched_extraction, receipt_file
):
    """Same answer when the collision comes from documents, not processed_files."""
    db = FakeDb(
        hash_lookups=[EXISTING_DOC],
        document_error=UniqueViolation(
            'duplicate key value violates unique constraint "idx_documents_hash"'
        ),
    )
    result = await _process(db, receipt_file)

    assert result["duplicate"] is True
    assert result["document_id"] == EXISTING_DOC["id"]


@pytest.mark.asyncio
async def test_unique_violation_with_no_document_row_does_not_fail_the_job(
    patched_extraction, receipt_file, caplog
):
    """Defensive tail: a collision with nothing to return still finishes cleanly."""
    db = FakeDb(hash_lookups=[None], processed_file_error=_dedup_violation())
    with caplog.at_level(logging.DEBUG):
        result = await _process(db, receipt_file)

    assert result["duplicate"] is True
    assert result["document_id"] is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].exc_info is None


@pytest.mark.asyncio
async def test_non_financial_reupload_does_not_raise(
    patched_extraction, receipt_file, monkeypatch
):
    """A non-financial file uploaded twice.

    No documents row is ever written for it, so the hash guard cannot catch the
    second upload — it must still resolve without raising.
    """
    import core.extraction

    async def _not_financial(path, db=None, **kwargs):
        return {"vendor": "NOT_A_FINANCIAL_DOCUMENT", "_provider": "test"}

    monkeypatch.setattr(core.extraction, "extract_document", _not_financial)

    first = FakeDb()
    result1 = await _process(first, receipt_file)
    assert result1["skipped"] is True
    assert result1["reason"] == "Not a financial document"

    # Second upload, against a database whose insert still collides: the job
    # has to finish, and it must not be an exception.
    second = FakeDb(hash_lookups=[None], processed_file_error=_dedup_violation())
    result2 = await _process(second, receipt_file)
    assert result2["duplicate"] is True


# ── The endpoint's two duplicate guards, over HTTP ─────────────────────────

@pytest.fixture()
def client_for(monkeypatch):
    """Return a factory: fake db -> TestClient with auth bypassed."""
    from server.auth import get_current_user

    monkeypatch.setattr(
        "core.file_storage.upload_file",
        lambda *a, **k: "user-duplicate/202602/deadbeef_receipt.pdf",
    )

    def _factory(fake_db: FakeDb) -> TestClient:
        async def _user():
            return FAKE_USER

        app.dependency_overrides[get_current_user] = _user
        app.dependency_overrides[get_db] = lambda: fake_db
        return TestClient(app, raise_server_exceptions=False)

    yield _factory
    for key in (get_current_user, get_db):
        app.dependency_overrides.pop(key, None)


def _upload(client: TestClient):
    return client.post(
        "/api/receipts/upload",
        files={"file": ("receipt.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
    )


def test_an_already_extracted_file_is_answered_200_not_queued_again(client_for):
    db = FakeDb(hash_lookups=[EXISTING_DOC])
    resp = _upload(client_for(db))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is True
    assert body["doc_id"] == EXISTING_DOC["id"]
    assert db.inserted_jobs == [], "a duplicate must not be processed twice"


def test_the_same_bytes_already_in_the_queue_return_that_job(client_for):
    db = FakeDb(active_job=dict(QUEUED_JOB))
    resp = _upload(client_for(db))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is True
    assert body["job_id"] == QUEUED_JOB["id"]
    assert body["position"] == QUEUED_JOB["position"]
    assert db.inserted_jobs == []


# ── The db layer that raised it ────────────────────────────────────────────

def _fake_pool(returning_id: int) -> MagicMock:
    cur = MagicMock()
    # _conn() checks SELECT current_user first, then the INSERT returns an id.
    cur.fetchone.side_effect = [("authenticated",), (returning_id,)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


def test_insert_processed_file_is_idempotent_sql():
    """The INSERT must not be able to raise UniqueViolation on the dedup index."""
    pool = _fake_pool(77)
    db = DatabasePg(pool, user_id="user-duplicate")

    assert db.insert_processed_file("deadbeef", "receipt.pdf") == 77

    cur = pool.getconn.return_value.cursor.return_value.__enter__.return_value
    inserts = [c.args[0] for c in cur.execute.call_args_list if "processed_files" in c.args[0]]
    assert len(inserts) == 1
    sql = " ".join(inserts[0].split())
    assert "ON CONFLICT (user_id, file_hash)" in sql
    assert "RETURNING id" in sql, "callers depend on getting an id back either way"

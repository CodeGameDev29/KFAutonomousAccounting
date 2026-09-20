"""The statement upload answers 202 and the queue does the parsing.

Why this exists: an LLM parse of a multi-page statement can outlast a request
timeout. Parsing inside the request therefore means a longer statement finishes
server-side after the browser has already shown the user a dead upload.

So the endpoint stores the file, opens a ``statements`` row in ``processing``,
inserts a job and answers 202 — and ``server/jobs/document_queue.py`` runs the
parse when its turn comes, which is what makes a batch of statements dropped in
at once work at all. What these tests hold to:

  * the PDF path answers 202 with a job **and** the statement id, and queues
    exactly one job carrying that statement id;
  * CSV stays synchronous (a deterministic, in-process parse) but still
    produces a finished job, so the UI has one shape;
  * ``run_statement_parse_job`` imports the rows and returns the job result on
    success, and **raises** on failure — an unreachable LLM endpoint as a
    transient failure the queue waits out, an unreadable file as a hard one —
    while leaving the statements row in ``processing`` for the queue to decide
    about;
  * the user's stored document is never deleted by a parse;
  * ``fail_statement_row`` is what finally fails the row, with both event names.

The queue mechanics themselves (FIFO, concurrency, the backoff ladder, restart
recovery) live in ``tests/test_document_queue.py``.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.llm_errors import LOCAL_LLM_UNREACHABLE_CODE, LOCAL_LLM_UNREACHABLE_MESSAGE
from core.llm_statement_parser import (
    StatementLocalEndpointDown,
    StatementMetadata,
    StatementParseResult,
    TransactionRow,
)
from server.access import require_verified_email
from server.api import transactions as txn_api
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db
from server.jobs.document_queue import JobFailure

PDF_BYTES = b"%PDF-1.4 a-bank-statement-for-the-async-upload-tests"
CSV_BYTES = b"Date,Description,Amount\n2026-01-02,Coffee,-4.50\n"

FAKE_USER = AuthUser(
    id="user-async", email="async@example.com", role="authenticated", email_verified=True
)

JOB_ROW = {
    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "user_id": FAKE_USER.id,
    "kind": "statement",
    "filename": "bmo march 2026.pdf",
    "stored_path": "stored/path.pdf",
    "file_hash": "deadbeef",
    "status": "queued",
    "position": 3,
    "attempts": 0,
    "max_attempts": 3,
    "transient_waits": 0,
    "next_attempt_at": None,
    "pages_total": None,
    "pages_done": 0,
    "error_code": None,
    "error_message": None,
    "result": None,
    "created_at": None,
    "started_at": None,
    "finished_at": None,
    "updated_at": None,
}



def _fake_db() -> MagicMock:
    db = MagicMock()
    db.user_id = FAKE_USER.id
    db.increment_usage = MagicMock(return_value=(True, 1))
    db.delete_transactions_by_source_file = MagicMock(return_value=0)
    db.delete_statements_by_source_file = MagicMock(return_value=0)
    db.delete_processed_file_by_filename = MagicMock(return_value=0)
    db.insert_processed_file = MagicMock(return_value=1)
    db.insert_statement_processing = MagicMock(return_value="stmt-uuid-1")
    db.update_statement_parsed = MagicMock(return_value=1)
    db.update_statement_status = MagicMock(return_value=1)
    db.insert_transaction = MagicMock()
    db.insert_audit_log = MagicMock()

    def _insert_job(**kwargs):
        job = dict(JOB_ROW)
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


@pytest.fixture(autouse=True)
def no_sse_counts():
    """emit_job_event would reach for a database pool; the row is the truth."""
    with patch("server.jobs.document_queue.emit_job_event"):
        yield


@pytest.fixture
def client(db):
    """A TestClient with auth, the database, the file store and the worker wake
    all stubbed out, so only the endpoint's own behaviour is under test."""
    async def _user():
        return FAKE_USER


    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_verified_email] = _user
    app.dependency_overrides[get_db] = lambda: db
    with patch("core.file_storage.upload_file", return_value="stored/path.pdf"), \
         patch("server.jobs.document_queue.wake_worker"):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _post_pdf(client: TestClient, name: str = "bmo march 2026.pdf"):
    return client.post(
        "/api/transactions/upload-statement",
        files={"file": (name, io.BytesIO(PDF_BYTES), "application/pdf")},
    )


def _parse_result(rows: int = 2) -> StatementParseResult:
    return StatementParseResult(
        transactions=[
            TransactionRow(
                date=f"2026-01-0{i}",
                amount="12.34",
                description=f"Row {i}",
                type="debit",
            )
            for i in range(1, rows + 1)
        ],
        metadata=StatementMetadata(
            institution="BMO",
            account_type="CHECKING",
            account_number="1234",
            statement_period_start="2026-01-01",
            statement_period_end="2026-01-31",
            opening_balance="1000.00",
            closing_balance="975.32",
            currency="CAD",
            page_count=1,
        ),
        confidence="HIGH",
        flags=[],
        parse_method="local_text",
        balance_matches=True,
        balance_difference="0.00",
        llm_tokens_in=111,
        llm_tokens_out=222,
        llm_cost_usd=0.0,
    )


# ── The endpoint ──────────────────────────────────────────────────────────


class TestUploadAccepts202:
    def test_pdf_returns_202_with_a_queued_job_and_the_statement_id(self, client, db):
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_pdf(client)

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["kind"] == "statement"
        assert body["filename"] == "bmo march 2026.pdf"
        assert body["job_id"] == JOB_ROW["id"]
        assert body["position"] == JOB_ROW["position"]
        # Legacy keys the transactions page still reads off a 202.
        assert body["statement_id"] == "stmt-uuid-1"
        assert body["poll_url"] == "/api/statements/stmt-uuid-1"

        # The statements row exists before the model has said anything, and the
        # job carries its id so the worker knows what to fill in.
        db.insert_statement_processing.assert_called_once()
        assert db.insert_statement_processing.call_args.kwargs["source_file"] == (
            "bmo march 2026.pdf"
        )
        db.insert_processing_job.assert_called_once()
        kwargs = db.insert_processing_job.call_args.kwargs
        assert kwargs["kind"] == "statement"
        assert kwargs["stored_path"] == "stored/path.pdf"
        assert kwargs["result"]["statement_id"] == "stmt-uuid-1"

    def test_the_file_is_stored_before_the_job_is_queued(self, client, db):
        """The worker may not run until after a restart — a temp file will not do."""
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            assert _post_pdf(client).status_code == 202
        db.insert_processed_file.assert_called_once()
        assert db.insert_processing_job.call_args.kwargs["stored_path"]

    def test_a_pdf_that_is_not_a_statement_is_still_rejected_inline(self, client, db):
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=False):
            resp = _post_pdf(client, "a receipt.pdf")

        assert resp.status_code == 400
        assert "does not appear to be a bank statement" in resp.json()["detail"]
        db.insert_statement_processing.assert_not_called()
        db.insert_processing_job.assert_not_called()

    def test_the_content_hash_dedupe_answers_409(self, client, db):
        db.insert_statement_processing.side_effect = Exception(
            'duplicate key value violates unique constraint "statements_unique_hash"'
        )
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True):
            resp = _post_pdf(client)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Statement already imported."
        db.insert_processing_job.assert_not_called()

    def test_storage_failure_is_503_and_the_statement_is_failed_not_queued(
        self, client, db
    ):
        """Without a durable copy there is nothing for the worker to read."""
        with patch("core.pdf_statement_parser.is_bank_statement_pdf", return_value=True), \
             patch("core.file_storage.upload_file", side_effect=RuntimeError("disk gone")):
            resp = _post_pdf(client)

        assert resp.status_code == 503
        assert "storage" in resp.json()["detail"].lower()
        db.insert_processing_job.assert_not_called()
        assert db.update_statement_status.call_args.kwargs["status"] == "failed"

    def test_csv_stays_synchronous_but_still_records_a_finished_job(self, client, db):
        """A CSV parse is deterministic and in-process — no need to defer it."""
        txn = MagicMock()
        txn.account = MagicMock(value="CAD")
        txn.institution = "BMO"
        txn.status = "UNMATCHED"
        with patch("core.bank_parser.parse_csv", return_value=[txn]):
            resp = client.post(
                "/api/transactions/upload-statement",
                files={"file": ("jan.csv", io.BytesIO(CSV_BYTES), "text/csv")},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Legacy shape intact …
        assert body["imported"] == 1
        assert body["statement_id"] is None
        # … and a job object so the queue list shows it like everything else.
        assert body["status"] == "done"
        assert body["kind"] == "statement"
        kwargs = db.insert_processing_job.call_args.kwargs
        assert kwargs["status"] == "done"
        assert kwargs["finished"] is True
        assert kwargs["result"]["imported"] == 1


# ── The queued parse ──────────────────────────────────────────────────────


async def _run(db: MagicMock, tmp_path: Path, name: str = "bmo march 2026.pdf") -> dict:
    pdf = tmp_path / "bmo.pdf"
    pdf.write_bytes(PDF_BYTES)
    return await txn_api.run_statement_parse_job(
        db=db,
        user_id=FAKE_USER.id,
        statement_id="stmt-uuid-1",
        filename=name,
        file_hash="deadbeef",
        source_path=pdf,
        replaced_count=0,
    )


class TestQueuedParse:
    @pytest.mark.asyncio
    async def test_success_marks_imported_and_pushes_statement_parsed(self, db, tmp_path):
        events: list[tuple[str, dict]] = []

        async def _fake_parse(*args, **kwargs):
            return _parse_result(rows=2)

        with patch("core.llm_statement_parser.parse_statement_with_llm", _fake_parse), \
             patch("server.api.events.send_event",
                   side_effect=lambda uid, t, d=None: events.append((t, d or {}))):
            result = await _run(db, tmp_path)

        # Telemetry still lands on the row, unchanged.
        kwargs = db.update_statement_parsed.call_args.kwargs
        assert kwargs["parse_method"] == "local_text"
        assert kwargs["institution"] == "BMO"
        assert kwargs["row_count"] == 2
        assert kwargs["llm_tokens_in"] == 111
        assert kwargs["llm_tokens_out"] == 222
        assert kwargs["import_confidence"] == "HIGH"
        assert db.insert_transaction.call_count == 2
        db.update_statement_status.assert_not_called()

        # The job result the queue writes onto the row.
        assert result["statement_id"] == "stmt-uuid-1"
        assert result["imported"] == 2
        assert result["row_count"] == 2
        assert result["summary"]["rows"] == 2
        assert result["summary"]["institution"] == "BMO"
        assert "2 row(s) imported" in result["summary"]["text"]

        by_type = dict(events)
        assert "statement_parsed" in by_type
        assert by_type["statement_parsed"]["statement_id"] == "stmt-uuid-1"
        assert by_type["statement_parsed"]["imported"] == 2
        assert by_type["statement_parsed"]["status"] == "imported"
        # The older event name still goes out for the parsing card.
        assert "statement_parse_complete" in by_type
        assert "transactions_updated" in by_type

    @pytest.mark.asyncio
    async def test_local_model_outage_raises_a_transient_failure_and_keeps_processing(
        self, db, tmp_path
    ):
        """A dead model is the queue's problem: wait, do not fail the statement."""
        async def _down(*args, **kwargs):
            raise StatementLocalEndpointDown()

        with patch("core.llm_statement_parser.parse_statement_with_llm", _down), \
             patch("server.api.events.send_event"):
            with pytest.raises(JobFailure) as exc_info:
                await _run(db, tmp_path)

        failure = exc_info.value
        assert failure.transient is True
        assert failure.error_code == LOCAL_LLM_UNREACHABLE_CODE
        assert failure.message == LOCAL_LLM_UNREACHABLE_MESSAGE
        db.update_statement_status.assert_not_called()
        db.update_statement_parsed.assert_not_called()
        db.insert_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unparseable_statement_is_a_hard_failure(self, db, tmp_path):
        from core.llm_statement_parser import StatementParseError

        async def _garbage(*args, **kwargs):
            raise StatementParseError("model returned no transactions")

        with patch("core.llm_statement_parser.parse_statement_with_llm", _garbage), \
             patch("server.api.events.send_event"):
            with pytest.raises(JobFailure) as exc_info:
                await _run(db, tmp_path)

        assert exc_info.value.transient is False
        assert "model returned no transactions" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_a_crash_after_the_parse_propagates_so_the_queue_can_retry(
        self, db, tmp_path
    ):
        async def _fake_parse(*args, **kwargs):
            return _parse_result(rows=1)

        db.update_statement_parsed.side_effect = RuntimeError("database exploded")

        with patch("core.llm_statement_parser.parse_statement_with_llm", _fake_parse), \
             patch("server.api.events.send_event"):
            with pytest.raises(RuntimeError, match="database exploded"):
                await _run(db, tmp_path)

    @pytest.mark.asyncio
    async def test_the_users_stored_document_is_never_deleted(self, db, tmp_path):
        """The source path is the file store's copy, not a temp file."""
        pdf = tmp_path / "bmo.pdf"
        pdf.write_bytes(PDF_BYTES)

        async def _down(*args, **kwargs):
            raise StatementLocalEndpointDown()

        with patch("core.llm_statement_parser.parse_statement_with_llm", _down), \
             patch("server.api.events.send_event"):
            with pytest.raises(JobFailure):
                await txn_api.run_statement_parse_job(
                    db=db,
                    user_id=FAKE_USER.id,
                    statement_id="stmt-uuid-1",
                    filename="bmo.pdf",
                    file_hash="deadbeef",
                    source_path=pdf,
                )

        assert pdf.exists()

    @pytest.mark.asyncio
    async def test_a_progress_callback_is_forwarded_when_the_engine_takes_one(
        self, db, tmp_path
    ):
        """The contract with the engine: progress_cb(pages_done, pages_total)."""
        seen: list = []

        async def _fake_parse(*args, progress_cb=None, **kwargs):
            assert progress_cb is not None, "the parser declares it, so pass it"
            progress_cb(1, 4)
            return _parse_result(rows=1)

        with patch("core.llm_statement_parser.parse_statement_with_llm", _fake_parse), \
             patch("server.api.events.send_event"):
            await txn_api.run_statement_parse_job(
                db=db,
                user_id=FAKE_USER.id,
                statement_id="stmt-uuid-1",
                filename="bmo.pdf",
                file_hash="deadbeef",
                source_path=tmp_path,
                progress_cb=lambda done, total: seen.append((done, total)),
            )

        assert seen == [(1, 4)]


class TestFailStatementRow:
    def test_it_writes_failed_and_both_event_names(self, db):
        events: list[tuple[str, dict]] = []
        with patch("server.api.events.send_event",
                   side_effect=lambda uid, t, d=None: events.append((t, d or {}))):
            txn_api.fail_statement_row(
                db, FAKE_USER.id, "stmt-uuid-1", "bmo.pdf",
                error_code=LOCAL_LLM_UNREACHABLE_CODE,
                message=LOCAL_LLM_UNREACHABLE_MESSAGE,
            )

        kwargs = db.update_statement_status.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["status_message"] == LOCAL_LLM_UNREACHABLE_MESSAGE

        by_type = dict(events)
        assert by_type["statement_failed"]["message"] == LOCAL_LLM_UNREACHABLE_MESSAGE
        assert by_type["statement_failed"]["statement_id"] == "stmt-uuid-1"
        assert by_type["statement_failed"]["status"] == "failed"
        # Legacy name kept so the existing error surface still fires.
        assert by_type["statement_parse_error"]["error"] == LOCAL_LLM_UNREACHABLE_MESSAGE

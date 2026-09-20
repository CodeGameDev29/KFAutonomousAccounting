"""Data ingestion tests — bank statement upload, receipt upload, dedup.

Fixtures come from `lib/fixtures.py` by name. Nothing here globs a directory for
"a PDF": that is how a harness ends up uploading the file that exists to be
rejected as its receipt, taking the resulting HTTP 400, and passing.
"""

from __future__ import annotations

from lib import fixtures
from lib.api_client import APIClient
from lib.test_framework import (
    NotExercised,
    assert_key,
    assert_status,
    assert_status_in,
    not_applicable_to_persona,
)
from profiles import UserProfile

# Statuses that mean a receipt upload *worked*. 400 is not one of them: accept it
# and the test passes on a flat rejection while every downstream reconciliation
# test fails for want of data.
RECEIPT_OK = (200, 202, 409)
STATEMENT_OK = (200, 202, 409)


def test_upload_bank_csv(client: APIClient, profile: UserProfile):
    """Upload a bank CSV and verify transactions are imported."""
    if not profile.uploads_bank_csv:
        not_applicable_to_persona(profile, "uploads_bank_csv", "upload CSV bank statements")

    csv_path = fixtures.bank_csv_for_this_run()
    resp = client.upload("/api/transactions/upload-statement", csv_path)
    assert_status_in(resp, STATEMENT_OK, "Bank CSV upload")
    if resp.status_code == 200:
        data = resp.json()
        assert_key(data, "imported", "Bank upload response")
        # Every row is unique per run (see fixtures.bank_csv_for_this_run), so a
        # short count is a dropped row, not a duplicate.
        assert data["imported"] == fixtures.BANK_CSV_ROWS, (
            f"Bank CSV upload imported {data['imported']} of {fixtures.BANK_CSV_ROWS} rows "
            f"(duplicates={data.get('duplicates')}, replaced={data.get('replaced')}, "
            f"flagged={data.get('flagged_row_count')})."
        )


def test_upload_bank_pdf(client: APIClient, profile: UserProfile):
    """Upload a bank PDF statement and verify the background parse completes."""
    if not profile.uploads_bank_pdf:
        not_applicable_to_persona(profile, "uploads_bank_pdf", "upload PDF bank statements")

    pdf_path = fixtures.require(fixtures.BANK_STATEMENT_PDF, "Bank statement PDF")
    resp = client.upload("/api/transactions/upload-statement", pdf_path)
    assert_status_in(resp, STATEMENT_OK, "Bank PDF upload")
    if resp.status_code == 409:
        raise NotExercised(
            "this statement PDF is already imported on the account (content-hash dedup, HTTP 409) "
            "so its parse did not re-run; run without --no-reset to exercise the parse"
        )
    if resp.status_code == 202:
        # The parse runs in a background task; 202 alone proves only that the
        # file was accepted, so follow it to a terminal state.
        statement_id = resp.json().get("statement_id")
        assert statement_id, f"202 from upload-statement carried no statement_id: {resp.text[:200]}"
        row = client.wait_for_statement(statement_id)
        assert row.get("status") == "imported", (
            f"Statement {statement_id} finished as {row.get('status')!r}: "
            f"{row.get('status_message')}"
        )


def test_upload_receipt(client: APIClient, profile: UserProfile):
    """Upload a receipt and verify extraction actually produced a document.

    `POST /api/receipts/upload` answers **202 the moment the file is stored** —
    extraction is a queue job, not part of the request. Returning on that 202
    asserts that a file was accepted and nothing about whether a document came
    out of it, which is the one thing reconciliation then depends on. So the job
    is followed to a terminal state and the document it wrote is read back.
    """
    if not profile.uploads_receipts:
        not_applicable_to_persona(profile, "uploads_receipts", "upload receipts")

    pdf_path = fixtures.receipt_pdf_for_this_run()
    resp = client.upload("/api/receipts/upload", pdf_path)
    assert_status_in(resp, RECEIPT_OK, "Receipt upload")
    data = resp.json()

    if resp.status_code == 202:
        job_id = data.get("job_id") or data.get("id")
        assert job_id, f"202 from /api/receipts/upload carried no job_id: {str(data)[:200]}"
        job = client.wait_for_job(str(job_id))
        assert job.get("status") == "done", (
            f"Receipt job {job_id} finished as {job.get('status')!r} "
            f"({job.get('error_code')}: {job.get('error_message')}) for {pdf_path.name} — "
            "the upload was accepted but extraction never produced a document."
        )
        result = job.get("result") or {}
        # "skipped" is the worker's answer for NOT_A_FINANCIAL_DOCUMENT. On the
        # generated retail receipt that is a real extraction failure, not a skip.
        assert not result.get("skipped"), (
            f"Receipt extraction returned skipped ({result.get('reason')}) for "
            f"{pdf_path.name} — the extractor did not recognise a receipt that reads "
            f"'Example Retailer / {fixtures.RECEIPT_DATE_ISO} / {fixtures.RECEIPT_TOTAL}'."
        )
        doc_id = result.get("document_id")
    else:
        # 200: the same bytes are already on file (hash dedup), and the body is
        # the document they resolved to.
        assert_key(data, "doc_id", "Receipt response")
        doc_id = data.get("doc_id")

    assert doc_id, (
        f"Receipt upload for {pdf_path.name} completed with no document id: "
        f"{str(data)[:200]}"
    )

    doc = client.get_document(doc_id)
    assert doc.get("vendor"), (
        f"Document {doc_id} was written with no vendor (total={doc.get('total')}, "
        f"date={doc.get('date')}) — extraction produced a row but not a receipt."
    )
    assert doc.get("total") is not None, (
        f"Document {doc_id} was written with no total (vendor={doc.get('vendor')!r})."
    )


def test_upload_duplicate_receipt(client: APIClient, profile: UserProfile):
    """Uploading the same bytes twice must be handled, not crash.

    The file is a blank (non-financial) PDF on purpose: that path returns early,
    before the `documents` row is written, so it is where the dedup bookkeeping
    is actually load-bearing.

    Three answers mean the server deduplicated: **200** (the bytes already
    resolved to a document), **409**, and **202 carrying `duplicate: true`** —
    the queue contract, where the same bytes are still in flight and the answer
    is the job the first upload created. All three are correct behaviour.
    """
    if not profile.test_duplicate_upload:
        not_applicable_to_persona(profile, "test_duplicate_upload", "test duplicate uploads")

    pdf_path = fixtures.blank_pdf_for_this_run()
    first = client.upload("/api/receipts/upload", pdf_path)
    assert_status_in(first, RECEIPT_OK, "First upload of the duplicate pair")
    first_body = first.json()
    first_job = first_body.get("job_id") or first_body.get("id")

    second = client.upload("/api/receipts/upload", pdf_path)
    assert_status_in(second, (200, 202, 409), "Second (duplicate) upload")
    second_body = second.json()

    if second.status_code in (200, 409):
        return  # answered from the documents table: deduplicated.

    if second_body.get("duplicate"):
        assert str(second_body.get("job_id") or second_body.get("id")) == str(first_job), (
            f"Second upload was flagged duplicate but points at job "
            f"{second_body.get('job_id')}, not the first upload's job {first_job} — the dedup "
            "resolved to the wrong row."
        )
        return

    # A 202 with no duplicate flag means the server queued the same bytes twice.
    # That is a real dedup miss *unless* the first job has already finished as a
    # non-financial document: that path writes no `documents` row on purpose, so
    # by then there is genuinely nothing left on file to match the hash against.
    first_row = client.get_job(str(first_job)) if first_job else {}
    first_result = first_row.get("result") or {}
    if first_row.get("status") in ("done", "failed", "cancelled") and first_result.get("skipped"):
        raise NotExercised(
            f"the first upload's job finished as a non-financial document "
            f"({first_result.get('reason')}) before the second upload arrived, and that path "
            "writes no `documents` row by design — so there was no document hash and no "
            "in-flight job left to deduplicate against. The dedup bookkeeping this test aims "
            "at was not reachable in this run."
        )
    raise AssertionError(
        f"Second (duplicate) upload of {pdf_path.name} was accepted as new work: HTTP 202 "
        f"with no `duplicate` flag (job {second_body.get('job_id')}, first job {first_job}, "
        f"first job status {first_row.get('status')!r}). The same bytes bought a second "
        "extraction."
    )


def test_upload_invalid_file_type(client: APIClient, profile: UserProfile):
    """An unsupported file type is rejected before any extraction happens."""
    if not profile.test_invalid_file:
        not_applicable_to_persona(profile, "test_invalid_file", "test invalid uploads")

    bad = fixtures.require(fixtures.UNSUPPORTED_FILE, "Unsupported-file")
    resp = client.upload("/api/receipts/upload", bad)
    assert_status(resp, 400, "Unsupported extension should be rejected")

    # Same check one layer down: a supported extension whose bytes are not that
    # format must also be refused (magic-byte validation, CWE-434).
    fake = fixtures.not_a_pdf_for_this_run()
    resp = client.upload("/api/receipts/upload", fake)
    assert_status(resp, 400, "Plain text renamed .pdf should be rejected")


def test_list_transactions(client: APIClient, profile: UserProfile):
    """List transactions returns paginated results."""
    data = client.list_transactions()
    assert_key(data, "transactions", "Transaction list")
    assert_key(data, "total", "Transaction list")
    assert isinstance(data["transactions"], list), "Transactions should be a list"


def test_list_documents(client: APIClient, profile: UserProfile):
    """List documents returns paginated results."""
    data = client.list_documents()
    assert_key(data, "documents", "Document list")
    assert_key(data, "total", "Document list")
    assert isinstance(data["documents"], list), "Documents should be a list"


def test_transaction_count(client: APIClient, profile: UserProfile):
    """Transaction count endpoint returns a number."""
    resp = client.get("/api/transactions/count")
    assert_status(resp, 200, "Transaction count")
    data = resp.json()
    assert_key(data, "count", "Count response")
    assert isinstance(data["count"], int), "Count should be integer"

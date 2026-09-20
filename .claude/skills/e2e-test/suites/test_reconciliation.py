"""Reconciliation workflow tests — run, review, approve, reject.

Tests verify that reconciliation actually changes transaction statuses
from UNMATCHED → MATCHED, links transactions to documents, and produces
meaningful match data (confidence, explanation, etc.).

Every run here goes through `client.run_reconciliation()`, which starts the run
in the background and polls `/api/reconciliation/progress` to a terminal state.
The LLM pass takes minutes on a local model; holding one request open for that
produces `httpx.ReadTimeout` failures against work the server actually completes.
"""

from __future__ import annotations

import logging

from lib.api_client import APIClient
from lib.test_framework import (
    NotExercised,
    assert_eq,
    assert_key,
    assert_status,
    not_applicable_to_persona,
)
from profiles import UserProfile

logger = logging.getLogger(__name__)


def _count_by_status(client: APIClient, status: str) -> int:
    """Count transactions with a given status via the list endpoint.

    Raises rather than returning 0 on a failed call: "the request did not work"
    and "there are none" are different findings, and reporting the first as the
    second is what made an expired session look like an engine that matched
    nothing.
    """
    resp = client.get("/api/transactions", {"status": status, "per_page": 1})
    assert_status(resp, 200, f"Count transactions with status={status}")
    return resp.json().get("total", 0)


def test_pre_reconciliation_baseline(client: APIClient, profile: UserProfile):
    """Verify unmatched transactions and documents exist before reconciliation.

    This establishes the baseline: we need BOTH unmatched transactions (from bank
    upload) AND unmatched documents (from receipt upload) for reconciliation to
    produce matches. If either is zero, reconciliation has nothing to work with —
    and since both uploads are this test's declared preconditions, a zero here is
    a real ingestion failure, not a missing fixture.
    """
    if not profile.runs_reconciliation:
        not_applicable_to_persona(profile, "runs_reconciliation", "run reconciliation")

    summary = client.reconciliation_summary()
    unmatched_txns = summary.get("unmatched_transactions", 0)
    unmatched_docs = summary.get("unmatched_documents", 0)

    assert unmatched_txns > 0, (
        f"No unmatched transactions found ({unmatched_txns}) even though the statement upload "
        "passed. Reconciliation would produce 0 matches."
    )
    assert unmatched_docs > 0, (
        f"No unmatched documents found ({unmatched_docs}) even though the receipt upload "
        "passed — the document is not in EXTRACTED state. Reconciliation would produce 0 matches."
    )


def test_run_reconciliation(client: APIClient, profile: UserProfile):
    """Run reconciliation and verify it stores at least 1 match.

    0 matches = failure. The fixtures make this a fair bar: the generated receipt
    reads Example Retailer / 2026-01-18 / 40.32 and the uploaded statement carries
    an EXAMPLE RETAILER row with exactly that date and amount (`lib/fixtures.py`),
    so a correct engine has one honest pair to find.
    """
    data = client.run_reconciliation()
    assert_key(data, "matched", "Reconciliation result")
    assert_key(data, "rate", "Reconciliation result")
    assert_key(data, "status", "Reconciliation result")
    assert isinstance(data["matched"], int), "matched should be int"

    # Core assertion: reconciliation must actually match something.
    # `matched` counts matches STORED by this run — a pair only re-scored comes
    # back as `refreshed`, so a first run reporting 0 here really stored nothing.
    assert data["matched"] >= 1, (
        f"Reconciliation stored 0 new matches (status={data.get('status')}). "
        f"Reconcilable txns: {data.get('reconcilable')}, "
        f"Remaining docs: {data.get('remaining_documents')}, "
        f"proposed: {data.get('proposed')}, refreshed: {data.get('refreshed')}, "
        f"flagged_for_review: {data.get('flagged_for_review')}, "
        f"pass counts: {data.get('pass_counts')}. "
        "The receipt/transaction pair in the fixtures is an exact vendor+date+amount match."
    )

    # Status must reflect success
    assert data["status"] in ("success", "partial"), (
        f"Expected status 'success' or 'partial', got '{data['status']}'. "
        f"matched={data['matched']}, rate={data.get('rate')}%"
    )

    assert data["rate"] > 0, f"Match rate should be > 0, got {data['rate']}"


def test_transaction_statuses_changed(client: APIClient, profile: UserProfile):
    """Verify that transactions actually moved from UNMATCHED → MATCHED.

    After reconciliation runs, at least some transactions must have status=MATCHED.
    This catches phantom successes where the engine returns a positive count but
    the database was never updated.
    """
    matched_count = _count_by_status(client, "MATCHED")
    assert matched_count >= 1, (
        f"Expected at least 1 MATCHED transaction in database, found {matched_count}. "
        "Reconciliation reported matches but may have failed to persist status changes."
    )


def test_matched_transaction_has_receipt(client: APIClient, profile: UserProfile):
    """Verify a matched transaction is linked to a document with real data.

    Fetch a MATCHED transaction and check that it has a matched_document block
    containing vendor, total, and file reference — proving the match is a real
    receipt↔transaction link, not a ghost entry.
    """
    resp = client.get("/api/transactions", {"status": "MATCHED", "per_page": 5})
    assert_status(resp, 200, "List matched transactions")
    txns = resp.json().get("transactions", [])
    assert txns, (
        "No MATCHED transactions to inspect, although the status count above found some — "
        "the list endpoint and the count disagree."
    )

    # Fetch full details for the first matched transaction
    txn_id = txns[0]["id"]
    detail_resp = client.get(f"/api/transactions/{txn_id}")
    assert_status(detail_resp, 200, "Transaction detail")
    detail = detail_resp.json()

    assert detail.get("status") == "MATCHED", (
        f"Transaction {txn_id} status should be MATCHED, got {detail.get('status')}"
    )

    # Must have a matched document
    matched_doc = detail.get("matched_document")
    assert matched_doc is not None, (
        f"Transaction {txn_id} has status=MATCHED but no matched_document. "
        "The reconciliation match was not properly linked."
    )

    # Document must have real data
    assert matched_doc.get("vendor"), (
        f"Matched document for txn {txn_id} has no vendor name"
    )
    assert matched_doc.get("total") is not None, (
        f"Matched document for txn {txn_id} has no total amount"
    )


def test_match_has_confidence_and_explanation(client: APIClient, profile: UserProfile):
    """Verify reconciliation matches have confidence scores and explanations.

    Each match should have a non-zero confidence score and a human-readable
    explanation of why the transaction was matched to the document.
    """
    data = client.list_matches()
    matches = data.get("matches", [])
    assert matches, (
        f"/api/reconciliation/matches returned no rows (total={data.get('total')}) after a run "
        "that reported matches — nothing was persisted to reconciliation_matches."
    )

    match = matches[0]
    assert_key(match, "confidence_score", "Match")
    assert match["confidence_score"] > 0, (
        f"Match confidence should be > 0, got {match['confidence_score']}"
    )

    # Verify the match links a real transaction to a real document
    assert match.get("transaction_id") is not None, "Match missing transaction_id"
    assert match.get("document_id") is not None, "Match missing document_id"

    # Transaction details should be populated (from the JOIN)
    txn = match.get("transaction", {})
    assert txn.get("description"), (
        f"Match {match['id']} has no transaction description — JOIN may be broken"
    )
    assert txn.get("amount"), (
        f"Match {match['id']} has no transaction amount"
    )

    # Document details should be populated
    doc = match.get("document", {})
    assert doc.get("vendor"), (
        f"Match {match['id']} has no document vendor — JOIN may be broken"
    )


def test_reconciliation_summary(client: APIClient, profile: UserProfile):
    """Reconciliation summary reflects the state after matching.

    After reconciliation has run, the summary should show matched > 0 and
    a positive match rate.
    """
    data = client.reconciliation_summary()
    assert_key(data, "matched", "Recon summary")
    assert_key(data, "reconcilable", "Recon summary")
    assert_key(data, "rate", "Recon summary")

    # After reconciliation, summary should reflect matches
    if data["reconcilable"] > 0:
        assert data["matched"] >= 1, (
            f"Summary shows 0 matched out of {data['reconcilable']} reconcilable. "
            "Either reconciliation didn't run or didn't persist results."
        )
        assert data["rate"] > 0, (
            f"Match rate should be > 0 after successful reconciliation, got {data['rate']}"
        )


def test_list_matches(client: APIClient, profile: UserProfile):
    """List reconciliation matches with pagination — must have actual entries."""
    data = client.list_matches()
    assert_key(data, "matches", "Match list")
    assert_key(data, "total", "Match list")
    assert isinstance(data["matches"], list), "matches should be list"

    # After reconciliation, there should be matches
    assert data["total"] >= 1, (
        f"Expected at least 1 match in list, got {data['total']}. "
        "Reconciliation may not have persisted matches to the database."
    )


def test_list_pending_matches(client: APIClient, profile: UserProfile):
    """Filter matches to pending review status."""
    data = client.list_matches(status="PENDING_REVIEW")
    assert_key(data, "matches", "Pending matches")


def test_approve_match(client: APIClient, profile: UserProfile):
    """Approve a pending match and verify status changes.

    NOT-EXERCISED here has exactly one meaning: the server answered, and its
    answer was that the review queue is empty. `list_matches` raises on a
    non-2xx instead of handing back a body with no `matches` key, and the
    `assert_key` below refuses a 200 that is not the list shape — so a request
    that *failed* cannot be read as "nothing to approve".
    """
    if not profile.reviews_matches:
        not_applicable_to_persona(profile, "reviews_matches", "review matches")

    pending = client.list_matches(status="PENDING_REVIEW")
    assert_key(pending, "matches", "Pending matches")
    matches = pending["matches"]
    if not matches:
        raise NotExercised(
            "reconciliation left no PENDING_REVIEW match to approve — every match it made "
            "scored above the auto-approve threshold. Nothing to approve is a state of the "
            "data, not a broken endpoint"
        )

    match_id = matches[0]["id"]
    data = client.approve_match(match_id)
    assert_eq(data.get("status"), "ok", "Approve match")
    assert_eq(data.get("new_status"), "USER_APPROVED", "Approved status")


def test_reject_match(client: APIClient, profile: UserProfile):
    """Reject a pending match and verify transaction reverts to UNMATCHED.

    As in `approve_match`: the only NOT-EXERCISED this can report is a queue the
    server said was too short, never a call that did not come back.
    """
    if not profile.reviews_matches:
        not_applicable_to_persona(profile, "reviews_matches", "review matches")
    if profile.approves_all:
        raise NotExercised(
            f"persona '{profile.name}' bulk-approves its queue (profile flag approves_all=True) "
            "and never rejects; the individual-review personas cover reject"
        )

    pending = client.list_matches(status="PENDING_REVIEW")
    assert_key(pending, "matches", "Pending matches")
    matches = pending["matches"]
    if len(matches) < 2:
        raise NotExercised(
            f"only {len(matches)} PENDING_REVIEW match(es) left after approve_match took one — "
            "rejecting needs a second one so the approve path keeps its evidence"
        )

    match = matches[-1]
    match_id = match["id"]
    txn_id = match.get("transaction_id")

    data = client.reject_match(match_id)
    assert_eq(data.get("status"), "ok", "Reject match")
    assert_eq(data.get("new_status"), "USER_REJECTED", "Rejected status")

    # Verify the transaction reverted to UNMATCHED
    if txn_id:
        detail = client.get(f"/api/transactions/{txn_id}")
        if detail.status_code == 200:
            txn_data = detail.json()
            assert txn_data.get("status") == "UNMATCHED", (
                f"Transaction {txn_id} should revert to UNMATCHED after match rejection, "
                f"got {txn_data.get('status')}"
            )


def test_bulk_approve_all(client: APIClient, profile: UserProfile):
    """Bulk approve all pending reviews.

    The only NOT-EXERCISED here is the persona flag. Whatever the queue held, the
    endpoint has to answer 200 with a count — an empty queue is `approved: 0`,
    which is a working endpoint, and any other status is this test's failure.
    """
    if not profile.approves_all:
        not_applicable_to_persona(profile, "approves_all", "bulk-approve its review queue")

    pending_before = client.list_matches(status="PENDING_REVIEW")
    assert_key(pending_before, "total", "Pending matches before bulk approve")

    resp = client.post("/api/transactions/reviews/approve-all")
    assert_status(resp, 200, "Bulk approve")
    data = resp.json()
    assert_key(data, "approved", "Bulk approve response")
    logger.info("  bulk approve: %s pending before, %s approved",
                pending_before.get("total"), data.get("approved"))


def test_unmatch_approved_match(client: APIClient, profile: UserProfile):
    """Unmatch a previously approved match — transaction reverts to UNMATCHED.

    NOT-EXERCISED only when the server answered and listed no approved match.
    """
    approved = client.list_matches(status="USER_APPROVED,AUTO_APPROVED,APPROVED")
    assert_key(approved, "matches", "Approved matches")
    matches = approved["matches"]
    if not matches:
        raise NotExercised(
            "no approved match exists to unmatch — reconciliation produced none above the "
            "auto-approve threshold and the review steps above approved none"
        )

    match = matches[0]
    match_id = match["id"]
    txn_id = match.get("transaction_id")

    resp = client.post(f"/api/reconciliation/matches/{match_id}/unmatch")
    assert_status(resp, 200, f"Unmatch match {match_id}")

    if txn_id:
        detail = client.get(f"/api/transactions/{txn_id}")
        if detail.status_code == 200:
            txn_data = detail.json()
            assert txn_data.get("status") == "UNMATCHED", (
                f"Transaction {txn_id} should be UNMATCHED after unmatch, "
                f"got {txn_data.get('status')}"
            )


def test_reconciliation_idempotent(client: APIClient, profile: UserProfile):
    """Running reconciliation again produces no new matches.

    Everything matchable was matched by the first run, so a second pass that
    *writes* more pairs is finding them in data it already judged.

    `matched` is deliberately the count of matches this run STORED. A flagged
    pair (below the confidence floor) stays in the candidate pool by design, so
    the engine proposes it again on every run and the row already on file is
    re-scored — that comes back as `refreshed` and is not a new match.
    """
    result = client.run_reconciliation()
    # Printed on the pass as well as the failure: the verdict is only readable
    # next to the numbers it was reached on.
    logger.info(
        "  idempotency check [%s]: second run stored matched=%s, refreshed=%s, proposed=%s, "
        "flagged_for_review=%s, rate=%s, status=%s",
        profile.name, result.get("matched"), result.get("refreshed"), result.get("proposed"),
        result.get("flagged_for_review"), result.get("rate"), result.get("status"),
    )
    assert_key(result, "matched", "Second reconciliation result")
    assert result["matched"] == 0, (
        f"A second reconciliation run stored {result['matched']} new match(es) (expected 0) — "
        f"status={result.get('status')}, rate={result.get('rate')}, "
        f"proposed={result.get('proposed')}, refreshed={result.get('refreshed')}, "
        f"flagged_for_review={result.get('flagged_for_review')}. Reconciliation is not "
        "idempotent: the same input yields new matches on a re-run."
    )


def test_audit_log_records_actions(client: APIClient, profile: UserProfile):
    """Audit log captures reconciliation actions (approve, reject, unmatch)."""
    resp = client.get("/api/reconciliation/audit-log", {"per_page": 5})
    assert_status(resp, 200, "Audit log")
    data = resp.json()
    assert_key(data, "entries", "Audit log response")

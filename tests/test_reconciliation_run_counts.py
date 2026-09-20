"""What a run is allowed to call a match: `matched` counts only new writes.

A flagged pair (below ``MATCH_CONFIDENCE_FLOOR``) is stored PENDING_REVIEW and
both sides are deliberately left in the candidate pool — so the next run
proposes the very same pair again, and ``_store_matches`` re-scores the row it
already has. That is ``refreshed``, not ``stored``.

So ``matched`` is not ``len(mp_result.matches)``: that is the engine's
*proposal* count, and reporting it lets a second run over the same statement
claim a new match while it has written nothing. A run that changes nothing has
to say so, or "re-run to be sure" becomes a lie the user cannot check.

These tests drive both paths (sync ``/run`` + ``/actions/sync``, and the
background thread behind ``/start`` and ``/start-statement``) against a fake
database and assert the reported counts.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from models.document import Document, DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus
from server.api.reconciliation import (
    _reconciliation_progress,
    _run_reconciliation_background_locked,
    _run_reconciliation_internal,
)
from server.auth import AuthUser

USER = AuthUser(id="test-uuid-run-counts", email="counts@example.com", role="user",
                email_verified=True)
THRESHOLD = 0.90

TXN_ID = 1
DOC_ID = 3

# Below MATCH_CONFIDENCE_FLOOR (0.60) with a contradicting vendor: the engine
# proposes it, _store_matches flags it, both sides stay in the pool.
FLAGGED_CONFIDENCE = 0.535


class FakeDb:
    """Only the surface a reconciliation run touches."""

    user_id = USER.id

    def __init__(self, txns, docs, existing: list[dict] | None = None,
                 status_counts: dict[str, int] | None = None) -> None:
        self.txns = txns
        self.docs = docs
        self.existing = existing or []
        self.status_counts = status_counts or {"MATCHED": 0, "LINKED": 0, "UNMATCHED": 1}
        self.inserted: list[ReconciliationMatch] = []
        self.refreshed: list[tuple] = []
        self.superseded: list[tuple[int, str]] = []
        self.txn_status: dict[int, str] = {}
        self.doc_status: dict[int, str] = {}
        self._next_id = 9000

    # -- reads
    def get_unmatched_transactions(self, since=None, until=None, source_file=None):
        return list(self.txns)

    def get_unmatched_documents(self, since=None, until=None):
        return list(self.docs)

    def get_business_profile(self):
        return None

    def get_user_rejected_pairs(self) -> set[tuple[int, int]]:
        return set()

    def get_active_matches_for(self, transaction_ids, document_ids) -> list[dict]:
        return [
            row for row in self.existing
            if row["transaction_id"] in set(transaction_ids)
            or row["document_id"] in set(document_ids)
        ]

    def get_all_transactions_for_linking(self):
        return []

    def get_linkable_transactions(self):
        return []

    def get_user_rejected_link_pairs(self) -> set[tuple[int, int]]:
        return set()

    def get_actively_linked_transaction_ids(self) -> set[int]:
        return set()

    def count_transactions_by_status(self, status: str) -> int:
        return self.status_counts.get(status, 0)

    # -- writes
    def insert_match(self, match) -> int:
        self.inserted.append(match)
        self._next_id += 1
        return self._next_id

    def refresh_match_scores(self, *args) -> None:
        self.refreshed.append(args)

    def supersede_match(self, match_id, reason) -> dict:
        self.superseded.append((match_id, reason))
        return {"match_id": match_id}

    def update_transaction_status(self, txn_id, status) -> None:
        self.txn_status[txn_id] = status

    def update_document_status(self, doc_id, status) -> None:
        self.doc_status[doc_id] = status

    def create_transaction_link(self, **kwargs) -> None:  # pragma: no cover
        raise AssertionError("the linker is stubbed out in these tests")


class FakeMultiPassResult:
    def __init__(self, matches: list[ReconciliationMatch]) -> None:
        self.matches = matches
        self.pass_counts = {"pass_1_strict": len(matches)}
        self.llm_stats = {"calls": 0, "fallbacks": 0, "by_label": {}}


def _txn() -> Transaction:
    return Transaction(
        id=TXN_ID, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=date(2026, 1, 15), amount=Decimal("-41.25"), currency="CAD",
        description="EXAMPLE RETAILER", source_file="test_bank_cad.csv", source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _doc() -> Document:
    return Document(
        id=DOC_ID, original_filename="orchid-telecom-invoice-2026-01-19.pdf",
        file_hash="h3", vendor="ORCHID TELECOM", document_date=date(2026, 1, 15),
        currency="CAD", total=Decimal("56.00"), status=DocumentStatus.EXTRACTED,
    )


def _agreeing_doc() -> Document:
    """A receipt whose vendor actually agrees with the bank line.

    ``_doc()`` names a different merchant than the transaction does, on purpose,
    which is what the flagged cases need. A run that legitimately *stores and
    auto-approves* a match needs a pair whose names agree instead:
    ``_store_matches`` flags a description that names another merchant at any
    vendor sub-score, so reusing the contradictory receipt here would be
    asserting that a wrong pair gets written down as reconciled — the opposite
    of the contract.
    """
    return Document(
        id=DOC_ID, original_filename="20260115-example-retailer-receipt.pdf",
        file_hash="h3b", vendor="Example Retailer", document_date=date(2026, 1, 15),
        currency="CAD", total=Decimal("41.25"), status=DocumentStatus.EXTRACTED,
    )


def _match(confidence: float, vendor_score: float = 0.8) -> ReconciliationMatch:
    return ReconciliationMatch(
        transaction_id=TXN_ID, document_id=DOC_ID, confidence_score=confidence,
        amount_score=1.0, date_score=1.0, vendor_score=vendor_score,
        match_type=MatchType.ONE_TO_ONE, status=MatchStatus.PENDING_REVIEW,
    )


def _already_flagged_row() -> list[dict]:
    """The row a previous run left behind for this same pair."""
    return [{
        "id": 4, "transaction_id": TXN_ID, "document_id": DOC_ID,
        "confidence_score": FLAGGED_CONFIDENCE, "status": "PENDING_REVIEW",
        "match_source": "AUTO", "match_type": "ONE_TO_ONE",
    }]


@pytest.fixture()
def _stub_engine():
    """Stub everything a run reaches for except the storage rule under test."""
    no_links = MagicMock(links=[])
    with patch("core.transaction_linker.run_transaction_linking", return_value=no_links), \
         patch("core.categorization_agent.run_categorization_agent"), \
         patch("server.api.events.send_event"):
        yield


def _run_sync(db) -> dict:
    return asyncio.run(_run_reconciliation_internal(USER, db, THRESHOLD))


def _run_background(db) -> dict:
    _reconciliation_progress.pop(USER.id, None)
    _run_reconciliation_background_locked(
        USER.id, db, auto_approve_threshold=THRESHOLD, source_file="test_bank_cad.csv",
    )
    progress = _reconciliation_progress.get(USER.id) or {}
    assert progress.get("status") == "complete", progress
    return progress["result"]


RUNNERS = [pytest.param(_run_sync, id="sync"), pytest.param(_run_background, id="background")]


@pytest.mark.parametrize("run", RUNNERS)
@pytest.mark.usefixtures("_stub_engine")
def test_a_run_that_only_re_scores_reports_no_new_matches(run):
    """A second run that writes nothing must report `matched` as 0."""
    db = FakeDb([_txn()], [_doc()], existing=_already_flagged_row())
    fake = FakeMultiPassResult([_match(FLAGGED_CONFIDENCE, vendor_score=0.0)])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake):
        result = run(db)

    assert result["matched"] == 0
    # Nothing is hidden: the pair was proposed, and it was re-scored in place.
    assert result["refreshed"] == 1
    assert result["proposed"] == 1
    assert result["flagged_for_review"] == 1
    assert result["status"] == "no_matches"
    # And the database agrees — one UPDATE, no INSERT, both sides still in the pool.
    assert db.inserted == []
    assert db.refreshed and db.refreshed[0][0] == 4
    assert db.txn_status == {}
    assert result["remaining_transactions"] == 1


@pytest.mark.parametrize("run", RUNNERS)
@pytest.mark.usefixtures("_stub_engine")
def test_a_run_that_stores_a_match_still_reports_it(run):
    """A run that genuinely stores a match still counts it."""
    db = FakeDb([_txn()], [_agreeing_doc()],
                status_counts={"MATCHED": 1, "LINKED": 0, "UNMATCHED": 0})
    fake = FakeMultiPassResult([_match(0.96)])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake):
        result = run(db)

    assert result["matched"] == 1
    assert result["refreshed"] == 0
    assert result["proposed"] == 1
    assert result["auto_approved"] == 1
    assert result["status"] == "success"
    assert len(db.inserted) == 1
    assert db.txn_status[TXN_ID] == "MATCHED"
    assert result["remaining_transactions"] == 0


@pytest.mark.parametrize("run", RUNNERS)
@pytest.mark.usefixtures("_stub_engine")
def test_a_proposal_blocked_by_an_existing_match_is_not_a_new_match(run):
    """A pair the storage rule refused to write is not `matched` either."""
    settled_elsewhere = [{
        "id": 77, "transaction_id": 999, "document_id": DOC_ID,
        "confidence_score": 0.95, "status": "AUTO_APPROVED",
        "match_source": "AUTO", "match_type": "ONE_TO_ONE",
    }]
    db = FakeDb([_txn()], [_doc()], existing=settled_elsewhere)
    fake = FakeMultiPassResult([_match(0.97)])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake):
        result = run(db)

    assert result["matched"] == 0
    assert result["refreshed"] == 0
    assert result["proposed"] == 1
    assert db.inserted == []


@pytest.mark.usefixtures("_stub_engine")
def test_the_sse_payload_carries_the_same_counts():
    """The event the UI reacts to must not disagree with the result block."""
    db = FakeDb([_txn()], [_doc()], existing=_already_flagged_row())
    fake = FakeMultiPassResult([_match(FLAGGED_CONFIDENCE, vendor_score=0.0)])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake), \
         patch("server.api.events.send_event") as send_event:
        result = _run_sync(db)

    payload = send_event.call_args[0][2]
    assert payload["matched"] == result["matched"] == 0
    assert payload["refreshed"] == result["refreshed"] == 1
    assert payload["flagged_for_review"] == result["flagged_for_review"] == 1

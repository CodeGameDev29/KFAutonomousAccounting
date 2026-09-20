"""A pair the user rejected is never auto-proposed again.

Every row in this file is invented; none of it is a copy of anyone's records.
The scenario is one synthetic January for ``Example Corp Inc.``:

* transaction **#101** ``WISE TRANSFER −$1,800.00 CAD`` (2026-01-20) is proposed
  against document **#201**, a US$1,800.00 contractor invoice, at 0.70
  confidence, and the user rejects it. Nothing about the input changes, so the
  next run proposes the identical pair — and with no memory of the rejection it
  writes the row again, which makes a second reconciliation over unchanged input
  report "new" matches and makes a rejection mean nothing;
* transaction **#102** ``EXAMPLE RETAILER PURCHASE −$104.55 CAD`` (2026-01-15)
  is rejected against document **#211**, and re-proposed with **#210** and
  **#212** — further uploads of the *same* receipt (same vendor, same total,
  same date; different bytes, so different file hashes).

What is pinned here:

* the rejection is read off the existing match row (status ``USER_REJECTED`` +
  ``user_action``), and the engine's own ``superseded`` withdrawals are NOT
  treated as the user's verdict;
* a rejection covers the document's content twins;
* the exclusion happens at **candidate generation** — the rejected pair is not
  scored, so it cannot claim the transaction and block a better candidate;
* a genuinely different, better document for the same transaction is still
  proposed;
* the manual picker still lists a rejected document, labelled.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.reconciliation import find_matches
from db.database_pg import DatabasePg, _document_identity_keys, _normalized_amount_key
from models.document import Document, DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus

USER = "user-rejected-pairs"


# ── The invented rows ──────────────────────────────────────────────────────

def _transfer_txn() -> Transaction:
    """#101 WISE TRANSFER −$1,800.00 CAD, 2026-01-20."""
    return Transaction(
        id=101, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=date(2026, 1, 20), amount=Decimal("-1800.00"),
        currency="CAD", description="WISE TRANSFER",
        source_file="test_bank_cad.csv", source_row=3,
        status=TransactionStatus.UNMATCHED,
    )


def _retailer_txn() -> Transaction:
    """#102 EXAMPLE RETAILER PURCHASE −$104.55 CAD, 2026-01-15."""
    return Transaction(
        id=102, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=date(2026, 1, 15), amount=Decimal("-104.55"),
        currency="CAD", description="EXAMPLE RETAILER PURCHASE",
        source_file="test_bank_cad.csv", source_row=7,
        status=TransactionStatus.UNMATCHED,
    )


def _contractor_doc() -> Document:
    """#201 — a US$1,800.00 invoice, the pair the user rejected."""
    return Document(
        id=201, original_filename="20260201-jordan-avery.pdf", file_hash="hash-201",
        vendor="Jordan Avery", document_date=date(2026, 2, 1),
        currency="USD", total=Decimal("1800.00"),
        status=DocumentStatus.EXTRACTED,
    )


def _telecom_doc() -> Document:
    """#220 ORCHID TELECOM $56.00 — a candidate for the −$104.55 row, but a poor one."""
    return Document(
        id=220, original_filename="20260119-orchid-telecom.pdf",
        file_hash="hash-220", vendor="ORCHID TELECOM", document_date=date(2026, 1, 19),
        currency="CAD", total=Decimal("56.00"), status=DocumentStatus.EXTRACTED,
    )


def _retailer_doc(doc_id: int, filename: str) -> Document:
    """One of the byte-different uploads of the same retailer receipt."""
    return Document(
        id=doc_id, original_filename=filename, file_hash=f"hash-{doc_id}",
        vendor="Example Retailer", document_date=date(2026, 1, 15), currency="CAD",
        total=Decimal("104.55"), status=DocumentStatus.EXTRACTED,
    )


# ── Document identity: what counts as the same piece of paper ─────────────

class TestDocumentIdentity:
    def test_text_money_column_normalizes(self):
        """documents.total is TEXT: "104.55", "104.550" and "$104.55" are one number."""
        assert (
            _normalized_amount_key("104.55")
            == _normalized_amount_key("104.550")
            == _normalized_amount_key("$104.55")
            == _normalized_amount_key("-104.55")
        )
        assert _normalized_amount_key("not money") is None
        assert _normalized_amount_key(None) is None

    def test_byte_different_uploads_of_one_receipt_share_a_content_key(self):
        """Different bytes, so different hashes — and the same receipt."""
        a = _document_identity_keys("hash-210", "Example Retailer", "104.55", "CAD", "2026-01-15")
        b = _document_identity_keys("hash-212", "Example Retailer", "104.55", "CAD", "2026-01-15")
        assert set(a) & set(b), "two uploads of one receipt must share a key"
        assert ("hash", "hash-210") in a and ("hash", "hash-212") in b

    def test_case_and_currency_are_normalized_but_still_required(self):
        upper = _document_identity_keys(None, "EXAMPLE RETAILER ", "104.55", "cad", "2026-01-15")
        lower = _document_identity_keys(None, "example retailer", "104.5500", "CAD", "2026-01-15")
        assert set(upper) & set(lower)

    def test_a_different_amount_is_not_a_twin(self):
        a = _document_identity_keys(None, "Example Retailer", "104.55", "CAD", "2026-01-15")
        b = _document_identity_keys(None, "Example Retailer", "81.45", "CAD", "2026-01-15")
        assert not (set(a) & set(b))

    def test_an_unextracted_document_has_no_content_key(self):
        """No vendor / no total / no date means no twin — never guess."""
        assert _document_identity_keys("h", None, "104.55", "CAD", "2026-01-15") == [("hash", "h")]
        assert _document_identity_keys("h", "Example Retailer", None, "CAD", "2026-01-15") == [("hash", "h")]
        assert _document_identity_keys("h", "Example Retailer", "104.55", "CAD", None) == [("hash", "h")]


# ── Reading the rejection off the match rows ──────────────────────────────

class _Cursor:
    """Answers the two queries get_user_rejected_pairs runs, in order."""

    def __init__(self, rejected_rows, document_rows):
        self.rejected_rows = rejected_rows
        self.document_rows = document_rows
        self.executed: list[tuple[str, tuple]] = []
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        if "FROM reconciliation_matches" in flat:
            self._rows = list(self.rejected_rows)
        elif "FROM documents" in flat:
            self._rows = list(self.document_rows)
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _db_with(rejected_rows, document_rows) -> tuple[DatabasePg, _Cursor]:
    cursor = _Cursor(rejected_rows, document_rows)

    class _Conn:
        def cursor(self_inner):
            return cursor

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *exc):
            return False

    db = DatabasePg.__new__(DatabasePg)
    db.user_id = USER
    db._conn = lambda: _Conn()
    return db, cursor


# (id, file_hash, vendor, total, currency, document_date)
_DOC_ROWS = [
    (210, "hash-210", "Example Retailer", "104.55", "CAD", "2026-01-15"),
    (211, "hash-211", "Example Retailer", "104.55", "CAD", "2026-01-15"),
    (212, "hash-212", "Example Retailer", "104.55", "CAD", "2026-01-15"),
    (201, "hash-201", "Jordan Avery", "1800.00", "USD", "2026-02-01"),
    (220, "hash-220", "ORCHID TELECOM", "56.00", "CAD", "2026-01-19"),
]


class TestRejectedPairsRead:
    def test_only_human_rejections_are_honoured(self):
        """`superseded` is the engine withdrawing its own weak match."""
        db, cursor = _db_with([(101, 201)], _DOC_ROWS)
        pairs = db.get_user_rejected_pairs()
        assert (101, 201) in pairs
        sql, params = cursor.executed[0]
        assert "status = 'USER_REJECTED'" in sql
        assert "user_action = ANY(%s)" in sql
        actions = params[1]
        assert "rejected" in actions and "unmatched" in actions
        assert "remove_proof" in actions and "reassigned" in actions
        assert "superseded" not in actions

    def test_a_rejection_covers_the_documents_content_twins(self):
        """txn 102 rejected with doc 211 rules out docs 210 and 212 too."""
        db, _ = _db_with([(102, 211)], _DOC_ROWS)
        pairs = db.get_user_rejected_pairs()
        assert pairs == {(102, 210), (102, 211), (102, 212)}

    def test_twins_do_not_leak_to_unrelated_receipts(self):
        db, _ = _db_with([(101, 201)], _DOC_ROWS)
        assert db.get_user_rejected_pairs() == {(101, 201)}

    def test_no_rejections_costs_one_query(self):
        db, cursor = _db_with([], _DOC_ROWS)
        assert db.get_user_rejected_pairs() == set()
        assert len(cursor.executed) == 1

    def test_the_picker_asks_only_about_one_transaction(self):
        db, cursor = _db_with([(102, 211)], _DOC_ROWS)
        assert db.get_user_rejected_document_ids(102) == {210, 211, 212}
        sql, params = cursor.executed[0]
        assert "transaction_id = %s" in sql
        assert params[-1] == 102


# ── The engine: excluded at candidate generation ──────────────────────────

class TestEngineExcludesRejectedPairs:
    """Deterministic (no-LLM) runs, which is how the suite runs the engine.

    Note on the pair #101 ↔ #201: the deterministic scorer already refuses it
    (CAD 1,800 against US$1,800 implies a rate of 1.0000, outside every CAD/USD
    band), so a re-proposal of that one could only have come from the LLM amount
    agent. The pair used below — txn 102 against the retailer receipts — is one
    the deterministic engine does propose, so these tests can actually fail.
    """

    def test_the_rejected_pair_is_not_proposed_again(self):
        txn = _retailer_txn()
        doc = _retailer_doc(211, "receipt-example-retailer-b.pdf")

        proposed_before = find_matches([txn], [doc])
        assert [m.document_id for m in proposed_before] == [211], (
            "precondition: without the memory the engine does propose this pair"
        )

        assert find_matches([txn], [doc], rejected_pairs={(102, 211)}) == []

    def test_a_twin_upload_is_not_proposed_either(self):
        """Rejecting doc 211 for txn 102 also rules out docs 210 and 212."""
        txn = _retailer_txn()
        docs = [
            _retailer_doc(210, "receipt-example-retailer-a.pdf"),
            _retailer_doc(211, "receipt-example-retailer-b.pdf"),
            _retailer_doc(212, "receipt-example-retailer-c.pdf"),
        ]
        assert find_matches([txn], docs), "precondition: these do match otherwise"

        rejected = {(102, 210), (102, 211), (102, 212)}
        assert find_matches([txn], docs, rejected_pairs=rejected) == []

    def test_a_better_document_for_the_same_transaction_still_matches(self):
        """The rejection is about a pair, not about the transaction."""
        txn = _retailer_txn()
        docs = [_telecom_doc(), _retailer_doc(210, "receipt-example-retailer-a.pdf")]

        matches = find_matches([txn], docs, rejected_pairs={(102, 220)})

        assert len(matches) == 1
        assert matches[0].document_id == 210
        assert matches[0].transaction_id == 102

    @pytest.mark.parametrize("doc_order", [0, 1])
    def test_the_rejected_pair_cannot_claim_the_transaction(self, doc_order):
        """Exclusion is at candidate generation, not a filter after scoring.

        Doc 230 is a *settled* near-twin (same vendor and amount, one day
        earlier — so not a content twin). A settled claim is never displaced
        inside a run, so had the rejected pair still been scored it could have
        taken txn 102 and kept the exact-date receipt away from it — which is
        the "cannot claim the transaction and block a better candidate" half of
        the rule. Both orders, because the claim registry is order-independent
        only when the rejected pair never asks.
        """
        txn = _retailer_txn()
        near_twin = Document(
            id=230, original_filename="20260114-example-retailer.pdf", file_hash="hash-230",
            vendor="Example Retailer", document_date=date(2026, 1, 14), currency="CAD",
            total=Decimal("104.55"), status=DocumentStatus.EXTRACTED,
        )
        docs = [near_twin, _retailer_doc(210, "receipt-example-retailer-a.pdf")]
        if doc_order:
            docs.reverse()

        matches = find_matches([txn], docs, rejected_pairs={(102, 230)})

        assert [m.document_id for m in matches] == [210]

    def test_no_rejections_changes_nothing(self):
        txn = _retailer_txn()
        doc = _retailer_doc(211, "receipt-example-retailer-b.pdf")
        assert len(find_matches([txn], [doc], rejected_pairs=set())) == 1
        assert len(find_matches([txn], [doc])) == 1


# ── The storage gate ──────────────────────────────────────────────────────

class _StoreDb:
    """Only the surface `_store_matches` touches."""

    def __init__(self, rejected_pairs):
        self.rejected_pairs = rejected_pairs
        self.inserted: list[ReconciliationMatch] = []
        self.txn_status: dict[int, str] = {}
        self.doc_status: dict[int, str] = {}

    def get_user_rejected_pairs(self):
        return self.rejected_pairs

    def get_active_matches_for(self, transaction_ids, document_ids):
        return []

    def insert_match(self, match):
        self.inserted.append(match)
        return 9000 + len(self.inserted)

    def update_transaction_status(self, txn_id, status):
        self.txn_status[txn_id] = status

    def update_document_status(self, doc_id, status):
        self.doc_status[doc_id] = status


def _match(txn_id: int, doc_id: int, confidence: float) -> ReconciliationMatch:
    return ReconciliationMatch(
        transaction_id=txn_id, document_id=doc_id,
        confidence_score=confidence, amount_score=1.0, date_score=1.0,
        vendor_score=0.8, match_type=MatchType.ONE_TO_ONE,
        status=MatchStatus.PENDING_REVIEW,
    )


class TestStorageRefusesRejectedPairs:
    """Second gate: the dedup index excludes USER_REJECTED rows, so an insert of
    a rejected pair succeeds at the database level. The storage step is what has
    to refuse it, or a later run recreates the pair the user threw out."""

    def _maps(self):
        txn, doc = _transfer_txn(), _contractor_doc()
        return {txn.id: txn}, {doc.id: doc}

    def test_a_rejected_pair_is_not_written(self):
        from server.api.reconciliation import _store_matches

        txn_map, doc_map = self._maps()
        db = _StoreDb({(101, 201)})

        counts = _store_matches(db, [_match(101, 201, 0.70)], txn_map, doc_map, 0.90)

        assert db.inserted == []
        assert counts["user_rejected_skipped"] == 1
        assert counts["stored"] == 0
        assert db.txn_status == {} and db.doc_status == {}

    def test_an_unrejected_pair_is_still_written(self):
        from server.api.reconciliation import _store_matches

        txn_map, doc_map = self._maps()
        db = _StoreDb(set())

        counts = _store_matches(db, [_match(101, 201, 0.70)], txn_map, doc_map, 0.90)

        assert len(db.inserted) == 1
        assert counts["user_rejected_skipped"] == 0

    def test_the_caller_may_pass_the_set_it_already_read(self):
        """One read per run: the run passes its set instead of re-reading."""
        from server.api.reconciliation import _store_matches

        txn_map, doc_map = self._maps()
        db = _StoreDb(set())  # a stale/empty DB read must not override it

        counts = _store_matches(
            db, [_match(101, 201, 0.70)], txn_map, doc_map, 0.90,
            rejected_pairs={(101, 201)},
        )

        assert db.inserted == []
        assert counts["user_rejected_skipped"] == 1


# ── The manual picker still offers it, labelled ───────────────────────────

class TestPickerLabelsRejectedDocuments:
    #: The columns ``get_available_matches_for_transaction`` selects, in order.
    _PICKER_COLS = [
        "id", "original_filename", "vendor", "document_date", "total",
        "currency", "extraction_confidence", "stored_path", "status",
        "currency_source", "subtotal", "tax_gst", "tax_hst", "tax_pst",
        "tax_other", "existing_match_count",
    ]

    def _picker_db(self, rejected_rows):
        """A cursor that serves the picker query, then the rejection queries."""
        picker_rows = [
            (201, "20260201-jordan-avery.pdf", "Jordan Avery", "2026-02-01",
             "1800.00", "USD", 0.95, "/p/201.pdf", "EXTRACTED",
             "stated", "1800.00", None, None, None, None, 0),
            (240, "20260120-wise-transfer.pdf", "Wise Payments", "2026-01-20",
             "1800.00", "CAD", 0.95, "/p/240.pdf", "EXTRACTED",
             "stated", "1800.00", None, None, None, None, 0),
        ]
        cols = self._PICKER_COLS

        class _PickerCursor:
            description = [(c,) for c in cols]

            def __init__(self_inner):
                self_inner._rows: list[tuple] = []
                self_inner.executed: list[str] = []

            def execute(self_inner, sql, params=None):
                flat = " ".join(str(sql).split())
                self_inner.executed.append(flat)
                if "existing_match_count" in flat:
                    self_inner.description = [(c,) for c in cols]
                    self_inner._rows = list(picker_rows)
                elif "FROM reconciliation_matches" in flat:
                    self_inner._rows = list(rejected_rows)
                elif "FROM documents" in flat:
                    self_inner._rows = list(_DOC_ROWS)
                else:
                    self_inner._rows = []

            def fetchall(self_inner):
                return self_inner._rows

            def fetchone(self_inner):
                return self_inner._rows[0] if self_inner._rows else None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        cursor = _PickerCursor()

        class _Conn:
            def cursor(self_inner):
                return cursor

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        db = DatabasePg.__new__(DatabasePg)
        db.user_id = USER
        db._conn = lambda: _Conn()
        return db

    def test_the_rejected_document_is_still_listed_and_labelled(self):
        db = self._picker_db([(101, 201)])

        rows = db.get_available_matches_for_transaction(101)

        by_id = {r["id"]: r for r in rows}
        assert 201 in by_id, "a rejection must not hide the receipt from the picker"
        assert by_id[201]["previously_rejected"] is True
        assert by_id[240]["previously_rejected"] is False

    def test_nothing_is_labelled_when_nothing_was_rejected(self):
        db = self._picker_db([])
        rows = db.get_available_matches_for_transaction(101)
        assert all(r["previously_rejected"] is False for r in rows)

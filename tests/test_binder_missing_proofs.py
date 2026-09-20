"""Matched rows whose proof file never reaches the binder.

SYNTHETIC DATA. The user id, the transactions, the storage paths and the counts
below are invented for this test — see ``tests/fixtures/README.md``.

A binder can ship a README line reading "Matched, proof file not in this ZIP: 6"
and be wrong twice over. Transactions can sit at status MATCHED with no
reconciliation match record at all — their proof records are gone, so there was
never a file to archive — and folding that into the same count as a file the
store genuinely could not produce leaves one sentence describing two different
situations, fitting neither: "not in this ZIP".

What is pinned here:

  * the path resolver understands every layout ``documents.stored_path`` has been
    written in, and refuses an empty one instead of resolving it to the storage
    root directory;
  * a fetch says WHY it failed, in a sentence the README can show;
  * every matched row with no proof in the ZIP is listed by date, amount and
    description with its own reason, and the list can never disagree with the
    count printed above it.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

import core.file_storage as file_storage
from core.export_binder import (
    _FILE_GONE,
    _NO_MATCH_RECORD,
    _NO_PATH_ON_RECORD,
    _build_readme,
    _fetch_receipt_bytes,
    _format_missing_proofs,
    _missing_proof_rows,
    _storage_path_candidates,
)
from core.html_binder import binder_proof_counts
from models.transaction import AccountType, Transaction, TransactionStatus

#: An invented user id, in the shape the app stores but belonging to nobody.
USER = "00000000-0000-4000-8000-000000000001"

COMPANY = "Example Corp Inc."


def _txn(
    txn_id: int,
    description: str,
    amount: str,
    status: TransactionStatus = TransactionStatus.MATCHED,
    posted: date = date(2026, 2, 3),
) -> Transaction:
    return Transaction(
        id=txn_id, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=posted, amount=Decimal(amount), currency="CAD",
        description=description, source_file="statement_feb2026_cad.pdf", source_row=1,
        status=status,
    )


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    """A real storage root on disk — the resolver is the thing under test."""
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    return tmp_path


# ── The resolver ──────────────────────────────────────────────────────────

class TestStoragePathCandidates:
    def test_a_current_path_is_itself(self, storage):
        assert _storage_path_candidates(f"{USER}/202602/a1b2c3_orchid-telecom.pdf") == [
            f"{USER}/202602/a1b2c3_orchid-telecom.pdf"
        ]

    def test_windows_backslashes_are_normalised(self, storage):
        assert _storage_path_candidates(rf"{USER}\202602\a1b2c3_orchid-telecom.pdf") == [
            f"{USER}/202602/a1b2c3_orchid-telecom.pdf"
        ]

    def test_the_legacy_uploads_prefix_is_also_tried(self, storage):
        assert _storage_path_candidates("uploads/u1/202602/x.pdf") == [
            "uploads/u1/202602/x.pdf",
            "u1/202602/x.pdf",
        ]

    def test_an_absolute_path_under_the_root_is_the_same_file(self, storage):
        absolute = f"{storage.as_posix()}/{USER}/202602/x.pdf"
        assert _storage_path_candidates(absolute) == [
            absolute.strip("/"),
            f"{USER}/202602/x.pdf",
        ]

    @pytest.mark.parametrize("empty", [None, "", "   ", "\t"])
    def test_an_empty_path_yields_nothing_to_try(self, storage, empty):
        """An empty stored_path yields no candidate — never the storage root."""
        assert _storage_path_candidates(empty) == []

    def test_resolve_path_refuses_an_empty_path_outright(self, storage):
        with pytest.raises(ValueError):
            file_storage.resolve_path("")
        # ...and the root is still resolvable for a real prefix.
        assert file_storage.resolve_path(USER).name == USER


@pytest.mark.asyncio
class TestFetchReceiptBytes:
    async def test_a_stored_file_comes_back_with_no_reason(self, storage):
        target = storage / USER / "202602"
        target.mkdir(parents=True)
        (target / "a1b2c3_orchid-telecom.pdf").write_bytes(b"%PDF-1.4 proof")

        data, reason = await _fetch_receipt_bytes(
            f"{USER}/202602/a1b2c3_orchid-telecom.pdf", USER
        )

        assert data == b"%PDF-1.4 proof"
        assert reason is None

    async def test_a_legacy_windows_path_still_resolves(self, storage):
        target = storage / USER / "202602"
        target.mkdir(parents=True)
        (target / "a1b2c3_orchid-telecom.pdf").write_bytes(b"%PDF-1.4 proof")

        data, reason = await _fetch_receipt_bytes(
            rf"{USER}\202602\a1b2c3_orchid-telecom.pdf", USER
        )

        assert data == b"%PDF-1.4 proof"
        assert reason is None

    async def test_a_deleted_file_says_the_file_is_gone(self, storage):
        data, reason = await _fetch_receipt_bytes(
            f"{USER}/202602/nothing_here.pdf", USER
        )

        assert data is None
        assert reason == _FILE_GONE

    async def test_an_empty_path_is_its_own_reason_and_never_reads_the_root(
        self, storage
    ):
        data, reason = await _fetch_receipt_bytes("", USER)

        assert data is None
        assert reason == _NO_PATH_ON_RECORD


# ── Which rows are missing, and why ───────────────────────────────────────

class TestMissingProofRows:
    def test_the_three_causes_are_told_apart(self):
        stale = _txn(724, "Pre-AuthorizedPayment,ORCHIDTELECOMBPY/FAC", "-56.00")
        gone = _txn(687, "MERIDIAN COURIER SERVICES ANYTOWN MB", "-41.25")
        pathless = _txn(731, "Pre-AuthorizedPayment,CEDARVIEWSUPPLIES/DIV", "-81.45")

        rows = _missing_proof_rows(
            [stale, gone, pathless],
            verified_receipt_map={},
            doc_paths={687: [f"{USER}/202602/d4e5f6_receipt.pdf"], 731: [""]},
            proof_failures={f"{USER}/202602/d4e5f6_receipt.pdf": _FILE_GONE},
        )

        assert [txn.id for txn, _ in rows] == [724, 687, 731]
        assert dict((txn.id, reason) for txn, reason in rows) == {
            724: _NO_MATCH_RECORD,
            687: _FILE_GONE,
            731: _NO_PATH_ON_RECORD,
        }

    def test_a_row_whose_proof_reached_the_zip_is_not_listed(self):
        rows = _missing_proof_rows(
            [_txn(1, "CEDARVIEW SUPPLIES LTD", "-104.55")],
            verified_receipt_map={1: "20260203-CedarviewSupplies-104.55-CAD.pdf"},
            doc_paths={1: ["u/202602/a.pdf"]},
            proof_failures={},
        )
        assert rows == []

    def test_only_matched_rows_are_listed(self):
        """A row that never claimed to be matched is not a missing proof."""
        rows = _missing_proof_rows(
            [
                _txn(1, "MONTHLY PLAN FEE", "-4.00", status=TransactionStatus.IGNORED),
                _txn(2, "TRANSFER TO USD CHEQUING", "-120.00", status=TransactionStatus.LINKED),
                _txn(3, "UNKNOWN", "-9.00", status=TransactionStatus.UNMATCHED),
                _txn(4, "REVIEW", "-9.00", status=TransactionStatus.PENDING_REVIEW),
            ],
            verified_receipt_map={},
            doc_paths={},
            proof_failures={},
        )
        assert rows == []

    def test_the_list_can_never_disagree_with_the_count_above_it(self):
        """Same predicate as binder_proof_counts, deliberately."""
        txns = [
            _txn(1, "STALE ONE", "-41.25"),
            _txn(2, "STALE TWO", "-56.00"),
            _txn(3, "ARCHIVED", "-81.45"),
            _txn(4, "LINKED", "-120.00", status=TransactionStatus.LINKED),
        ]
        verified = {3: "20260203-CedarviewSupplies-81.45-CAD.pdf"}

        rows = _missing_proof_rows(txns, verified, {}, {})
        counts = binder_proof_counts(txns, verified, proof_file_count=1)

        assert len(rows) == counts["matched_no_proof_file"] == 2


# ── The README section ────────────────────────────────────────────────────

class TestReadmeMissingProofSection:
    def _readme(self, rows):
        """A README for an invented 12-transaction month, one proof archived.

        The counts are internally consistent on purpose: twelve transactions,
        three of which need no receipt, leaves nine that do, and the single
        archived proof substantiates one — 11.1%.
        """
        return _build_readme(
            COMPANY, "Feb2026", 2026, 2,
            txn_count=12, receipt_count=1, statement_count=1,
            counts={
                "with_proof": 1, "proof_files": 1, "matched_no_proof_file": len(rows),
                "linked_to_transfer": 2, "pending_review": 0, "no_document": 6,
                "no_receipt_needed": 3, "excluded": 0, "reconcilable": 9,
                "substantiated_rate": 11.1,
            },
            missing_proofs=rows,
        )

    def test_every_missing_transaction_is_named_with_its_reason(self):
        rows = [
            (_txn(724, "Pre-AuthorizedPayment,ORCHIDTELECOMBPY/FAC", "-56.00"), _NO_MATCH_RECORD),
            (_txn(687, "MERIDIAN COURIER SERVICES ANYTOWN MB", "-41.25"), _FILE_GONE),
        ]
        readme = self._readme(rows)

        assert "Matched, proof file missing\n---" in readme
        assert re.search(r"Matched, proof file missing:\s+2", readme)
        for text in (
            "2026-02-03  -56.00 CAD  Pre-AuthorizedPayment,ORCHIDTELECOMBPY/FAC",
            "2026-02-03  -41.25 CAD  MERIDIAN COURIER SERVICES ANYTOWN MB",
            _NO_MATCH_RECORD,
            _FILE_GONE,
        ):
            assert text in readme, text

    def test_a_stale_row_is_not_described_as_a_lost_file(self):
        """A proof that never existed is not reported as a file missing from the ZIP.

        "not in this ZIP" tells a reader to go looking for a file; a stale
        MATCHED status needs the pair resolved in Review instead.
        """
        readme = self._readme(
            [(_txn(724, "ORCHID TELECOM PREAUTH PMT", "-56.00"), _NO_MATCH_RECORD)]
        )
        assert "proof file not in this ZIP" not in readme
        assert "its matched status is stale" in readme
        assert "Approve or reject the pair in Review" in readme
        assert "1 transaction is marked matched" in readme

    def test_a_clean_binder_says_nothing_about_missing_proofs(self):
        readme = self._readme([])

        assert "Matched, proof file missing\n---" not in readme
        assert "Listed one by one" not in readme
        # The tally line still prints, at zero.
        assert re.search(r"Matched, proof file missing:\s+0", readme)

    def test_the_section_is_empty_when_there_is_nothing_to_say(self):
        assert _format_missing_proofs([]) == ""

"""README.txt and index.html count proofs from one source of truth.

A binder could ship a README reading ``Proofs of Transaction: 0`` beside an
index.html that badged rows "Linked" in a document headed CRA audit format,
with no ``proof_of_transaction/`` folder in the ZIP at all. Two writers, two
tallies, and "Linked" reading as "substantiated".

These tests hold: both writers call
:func:`core.html_binder.binder_proof_counts`, the count equals the proofs
actually archived, and a transfer link is never presented as a receipt.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from core.export_binder import _build_readme, generate_audit_binder
from core.html_binder import binder_proof_counts, generate_html_report
from models.transaction import Transaction, TransactionStatus


def _txn(txn_id, status, *, amount="100.00", currency="CAD", account="CAD"):
    return Transaction(
        id=txn_id,
        account=account,
        transaction_type="DEBIT",
        date_posted=date(2026, 5, 4),
        amount=Decimal(amount),
        currency=currency,
        description=f"row {txn_id}",
        source_file="may2026.csv",
        source_row=txn_id,
        status=status,
        category="Office supplies",
    )


class TestBinderProofCounts:
    def test_linked_rows_are_not_counted_as_substantiated(self):
        txns = [_txn(i, TransactionStatus.LINKED) for i in range(1, 15)]
        counts = binder_proof_counts(txns, receipt_name_map={}, proof_file_count=0)
        assert counts["proof_files"] == 0
        assert counts["with_proof"] == 0
        assert counts["linked_to_transfer"] == 14
        assert counts["substantiated_rate"] == 0.0

    def test_only_archived_proofs_count(self):
        """A matched row whose file could not be fetched is reported as a gap,
        never folded into the substantiated count."""
        txns = [_txn(1, TransactionStatus.MATCHED), _txn(2, TransactionStatus.MATCHED)]
        counts = binder_proof_counts(
            txns, receipt_name_map={1: "20260504-CedarviewSupplies-100.00-CAD.pdf"},
        )
        assert counts["with_proof"] == 1
        assert counts["matched_no_proof_file"] == 1
        assert counts["proof_files"] == 1

    def test_multi_proof_bundle_counts_the_transaction_once(self):
        txns = [_txn(1, TransactionStatus.MATCHED)]
        counts = binder_proof_counts(txns, receipt_name_map={1: ["a.pdf", "b.pdf"]})
        assert counts["with_proof"] == 1
        assert counts["proof_files"] == 2

    def test_ignored_and_excluded_rows_leave_the_denominator(self):
        txns = [
            _txn(1, TransactionStatus.MATCHED),
            _txn(2, TransactionStatus.IGNORED),
            _txn(3, TransactionStatus.EXCLUDED),
            _txn(4, TransactionStatus.UNMATCHED),
        ]
        counts = binder_proof_counts(txns, receipt_name_map={1: "a.pdf"})
        assert counts["reconcilable"] == 2
        assert counts["no_receipt_needed"] == 1
        assert counts["excluded"] == 1
        assert counts["no_document"] == 1
        assert counts["substantiated_rate"] == 50.0


class TestReadmeAgreesWithTheCounts:
    def test_readme_prints_the_archived_file_count(self):
        txns = [_txn(i, TransactionStatus.LINKED) for i in range(1, 15)]
        counts = binder_proof_counts(txns, receipt_name_map={}, proof_file_count=0)
        readme = _build_readme("Demo Co", "May 2026", 2026, 5, len(txns), 0, counts=counts)
        assert re.search(r"Proofs of Transaction:\s+0", readme)
        assert re.search(r"Linked to a transfer counterpart:\s+14", readme)
        assert "NOT a receipt" in readme
        assert "NOT PRESENT" in readme        # no proof_of_transaction/ folder

    def test_readme_count_follows_the_archived_files_not_the_status(self):
        txns = [_txn(1, TransactionStatus.MATCHED), _txn(2, TransactionStatus.MATCHED)]
        counts = binder_proof_counts(txns, receipt_name_map={1: "a.pdf"})
        readme = _build_readme("Demo Co", "May 2026", 2026, 5, 2, 1, counts=counts)
        assert re.search(r"Proofs of Transaction:\s+1", readme)
        assert re.search(r"Matched, proof file missing:\s+1", readme)

    def test_readme_describes_storage_honestly(self):
        """The binder must not promise encryption or a cloud it does not have."""
        readme = _build_readme("Demo Co", "May 2026", 2026, 5, 0, 0)
        assert "encrypted cloud storage" not in readme
        assert "a single self-hosted machine" in readme


@pytest.mark.asyncio
class TestHtmlReportAgreesWithReadme:
    async def test_same_numbers_in_both_writers(self):
        txns = [_txn(i, TransactionStatus.LINKED) for i in range(1, 15)]
        html = await generate_html_report(
            "Demo Co", 2026, 5, "May 2026", txns, {}, {},
            receipt_name_map={}, linked_in_month_map={1: [2], 2: [1]},
            proof_file_count=0,
        )
        assert "Files in proof_of_transaction/" in html
        assert "Linked to a transfer counterpart (no receipt)" in html
        # The status badge must not read as "substantiated".
        assert ">Linked<" not in html
        assert "Linked to transfer" in html

    async def test_zip_readme_matches_the_zip_contents(self):
        """End-to-end: the number in README.txt equals the files in the ZIP."""
        txns = [
            _txn(1, TransactionStatus.MATCHED),
            _txn(2, TransactionStatus.LINKED),
        ]

        async def mock_fetch(path: str, user_id: str) -> tuple[bytes | None, str | None]:
            return b"%PDF-1.4 proof", None

        with patch("core.export_binder._fetch_receipt_bytes", side_effect=mock_fetch):
            zip_bytes = await generate_audit_binder(
                company_name="Demo Co",
                year=2026,
                month=5,
                label="May 2026",
                transactions=txns,
                doc_paths={1: ["receipts/one.pdf"]},
                vendors={1: ["Cedarview Supplies"]},
                user_id="u1",
            )

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            readme = zf.read("README.txt").decode("utf-8")
        archived = [n for n in names if n.startswith("proof_of_transaction/")]
        assert len(archived) == 1
        assert re.search(rf"Proofs of Transaction:\s+{len(archived)}\b", readme)
        assert re.search(r"Linked to a transfer counterpart:\s+1", readme)

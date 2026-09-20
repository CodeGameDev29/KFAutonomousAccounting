"""Tests for the QBO and Xero CSV export functions in core/ledger.py.

Each accounting package wants its own column set, date format and sign
convention; these pin both formats so a change to one cannot quietly
reshape the other.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from core.ledger import export_qbo_csv, export_xero_csv
from models.transaction import AccountType, Transaction

# ─── Helpers ────────────────────────────────────────────────────────


def _make_transaction(
    *,
    id: int = 1,
    account: AccountType = AccountType.CAD,
    transaction_type: str = "DEBIT",
    date_posted: date = date(2026, 1, 15),
    amount: Decimal = Decimal("81.45"),
    currency: str = "CAD",
    description: str = "EXAMPLE RETAILER PURCHASE",
    source_file: str = "bank_jan2026.csv",
    source_row: int = 1,
) -> Transaction:
    return Transaction(
        id=id,
        account=account,
        transaction_type=transaction_type,
        date_posted=date_posted,
        amount=amount,
        currency=currency,
        description=description,
        source_file=source_file,
        source_row=source_row,
    )


def _read_csv(path: Path) -> list[list[str]]:
    """Read a CSV file and return rows as lists, handling BOM."""
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def _sample_transactions() -> list[Transaction]:
    """Return a set of transactions for testing."""
    return [
        _make_transaction(
            id=1,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 15),
            amount=Decimal("81.45"),
            currency="CAD",
            description="EXAMPLE RETAILER PURCHASE",
        ),
        _make_transaction(
            id=2,
            transaction_type="CREDIT",
            date_posted=date(2026, 1, 10),
            amount=Decimal("5000.00"),
            currency="CAD",
            description="WIRE TRANSFER IN",
        ),
        _make_transaction(
            id=3,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 20),
            amount=Decimal("150.00"),
            currency="USD",
            account=AccountType.USD,
            description="BLUEPEAK SOFTWARE HOSTING",
        ),
    ]


# ═══════════════════════════════════════════════════════════════════
#  Date format is MM/DD/YYYY
# ═══════════════════════════════════════════════════════════════════


class TestQBODateFormat:
    def test_date_format_mm_dd_yyyy(self, tmp_path: Path):
        txns = [_make_transaction(date_posted=date(2026, 3, 5))]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        # Row 1 is header, row 2 is data
        assert rows[1][0] == "03/05/2026"

    def test_date_format_leading_zeros(self, tmp_path: Path):
        txns = [_make_transaction(date_posted=date(2026, 1, 1))]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][0] == "01/01/2026"


# ═══════════════════════════════════════════════════════════════════
#  Header row correct
# ═══════════════════════════════════════════════════════════════════


class TestQBOHeaderRow:
    def test_header_columns(self, tmp_path: Path):
        out = tmp_path / "qbo.csv"
        export_qbo_csv([], out)
        rows = _read_csv(out)
        assert rows[0] == ["Date", "Description", "Amount", "Category", "Memo"]


# ═══════════════════════════════════════════════════════════════════
#  Debit amounts are negative
# ═══════════════════════════════════════════════════════════════════


class TestQBODebitNegative:
    def test_debit_is_negative(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="DEBIT", amount=Decimal("50.00"))]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][2])
        assert amount < 0
        assert amount == -50.00


# ═══════════════════════════════════════════════════════════════════
#  Credit amounts are positive
# ═══════════════════════════════════════════════════════════════════


class TestQBOCreditPositive:
    def test_credit_is_positive(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="CREDIT", amount=Decimal("5000.00"))]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][2])
        assert amount > 0
        assert amount == 5000.00


# ═══════════════════════════════════════════════════════════════════
#  Category from dict
# ═══════════════════════════════════════════════════════════════════


class TestQBOCategoryFromDict:
    def test_category_from_categories_dict(self, tmp_path: Path):
        txns = [_make_transaction(id=1)]
        categories = {1: "Software and IT"}
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out, categories=categories)
        rows = _read_csv(out)
        assert rows[1][3] == "Software and IT"


# ═══════════════════════════════════════════════════════════════════
#  Default "Uncategorized" when no category
# ═══════════════════════════════════════════════════════════════════


class TestQBODefaultUncategorized:
    def test_uncategorized_when_missing(self, tmp_path: Path):
        txns = [_make_transaction(id=99)]
        categories = {1: "Software and IT"}  # id=99 not in dict
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out, categories=categories)
        rows = _read_csv(out)
        assert rows[1][3] == "Uncategorized"

    def test_uncategorized_when_no_dict(self, tmp_path: Path):
        txns = [_make_transaction()]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][3] == "Uncategorized"


# ═══════════════════════════════════════════════════════════════════
#  Memo contains matched doc filename
# ═══════════════════════════════════════════════════════════════════


class TestQBOMemoDocFilename:
    def test_memo_has_filename(self, tmp_path: Path):
        txns = [_make_transaction(id=1)]
        matched_docs = {1: "/path/to/20260115-ExampleRetailer-Invoice.pdf"}
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out, matched_docs=matched_docs)
        rows = _read_csv(out)
        assert rows[1][4] == "20260115-ExampleRetailer-Invoice.pdf"

    def test_memo_empty_when_no_match(self, tmp_path: Path):
        txns = [_make_transaction(id=1)]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][4] == ""


# ═══════════════════════════════════════════════════════════════════
#  Sorted by date ascending
# ═══════════════════════════════════════════════════════════════════


class TestQBOSortedByDate:
    def test_rows_sorted_by_date(self, tmp_path: Path):
        txns = _sample_transactions()
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        # Skip header; dates should be ascending
        dates = [rows[i][0] for i in range(1, len(rows))]
        assert dates == sorted(dates)
        # Verify specific order: Jan 10, Jan 15, Jan 20
        assert dates[0] == "01/10/2026"
        assert dates[1] == "01/15/2026"
        assert dates[2] == "01/20/2026"


# ═══════════════════════════════════════════════════════════════════
#  Date format is DD/MM/YYYY
# ═══════════════════════════════════════════════════════════════════


class TestXeroDateFormat:
    def test_date_format_dd_mm_yyyy(self, tmp_path: Path):
        txns = [_make_transaction(date_posted=date(2026, 3, 5))]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][0] == "05/03/2026"

    def test_date_format_leading_zeros(self, tmp_path: Path):
        txns = [_make_transaction(date_posted=date(2026, 1, 1))]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][0] == "01/01/2026"


# ═══════════════════════════════════════════════════════════════════
#  Header row correct
# ═══════════════════════════════════════════════════════════════════


class TestXeroHeaderRow:
    def test_header_columns(self, tmp_path: Path):
        out = tmp_path / "xero.csv"
        export_xero_csv([], out)
        rows = _read_csv(out)
        assert rows[0] == [
            "Date", "Amount", "Payee", "Description",
            "Reference", "Transaction Type",
        ]


# ═══════════════════════════════════════════════════════════════════
#  Amounts always positive
# ═══════════════════════════════════════════════════════════════════


class TestXeroAmountsPositive:
    def test_debit_amount_positive(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="DEBIT", amount=Decimal("50.00"))]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][1])
        assert amount > 0
        assert amount == 50.00

    def test_credit_amount_positive(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="CREDIT", amount=Decimal("5000.00"))]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][1])
        assert amount > 0
        assert amount == 5000.00

    def test_negative_stored_amount_becomes_positive(self, tmp_path: Path):
        """Even if the Decimal is negative internally, Xero gets abs()."""
        txns = [_make_transaction(transaction_type="DEBIT", amount=Decimal("-75.00"))]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][1])
        assert amount == 75.00


# ═══════════════════════════════════════════════════════════════════
#  Transaction type "Debit"/"Credit"
# ═══════════════════════════════════════════════════════════════════


class TestXeroTransactionType:
    def test_debit_type(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="DEBIT")]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][5] == "Debit"

    def test_credit_type(self, tmp_path: Path):
        txns = [_make_transaction(transaction_type="CREDIT")]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][5] == "Credit"


# ═══════════════════════════════════════════════════════════════════
#  Reference contains matched doc filename
# ═══════════════════════════════════════════════════════════════════


class TestXeroReference:
    def test_reference_has_filename(self, tmp_path: Path):
        txns = [_make_transaction(id=1)]
        matched_docs = {1: "/data/receipts/20260115-ExampleRetailer-Invoice.pdf"}
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out, matched_docs=matched_docs)
        rows = _read_csv(out)
        assert rows[1][4] == "20260115-ExampleRetailer-Invoice.pdf"

    def test_reference_empty_when_no_match(self, tmp_path: Path):
        txns = [_make_transaction(id=1)]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        assert rows[1][4] == ""


# ═══════════════════════════════════════════════════════════════════
#  Multi-currency included
# ═══════════════════════════════════════════════════════════════════


class TestXeroMultiCurrency:
    def test_cad_and_usd_transactions(self, tmp_path: Path):
        txns = _sample_transactions()
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        # Should have 3 data rows + 1 header
        assert len(rows) == 4

    def test_sorted_by_date(self, tmp_path: Path):
        """Multi-currency transactions are still sorted by date."""
        txns = _sample_transactions()
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        dates = [rows[i][0] for i in range(1, len(rows))]
        # DD/MM/YYYY: Jan 10 → 10/01/2026, Jan 15 → 15/01/2026, Jan 20 → 20/01/2026
        assert dates[0] == "10/01/2026"
        assert dates[1] == "15/01/2026"
        assert dates[2] == "20/01/2026"

    def test_all_amounts_present(self, tmp_path: Path):
        txns = _sample_transactions()
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        amounts = [float(rows[i][1]) for i in range(1, len(rows))]
        assert 5000.00 in amounts
        assert 81.45 in amounts
        assert 150.00 in amounts


# ═══════════════════════════════════════════════════════════════════
#  Xero export with commas and quotes in description
# ═══════════════════════════════════════════════════════════════════


class TestXeroSpecialCharsInDescription:
    def test_description_with_commas_and_quotes(self, tmp_path: Path):
        """Descriptions containing commas and double quotes must be properly
        escaped in the CSV so that a standard CSV reader round-trips them."""
        txns = [_make_transaction(
            description='Example Retailer "Gift Card", 50% off',
        )]
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out)
        rows = _read_csv(out)
        # The description column (index 3) must preserve the original string
        assert rows[1][3] == 'Example Retailer "Gift Card", 50% off'


# ═══════════════════════════════════════════════════════════════════
#  QBO export with empty transactions → header only
# ═══════════════════════════════════════════════════════════════════


class TestQBOEmptyTransactions:
    def test_empty_transactions_produces_header_only(self, tmp_path: Path):
        """An empty transactions list should produce a CSV with exactly 1 line
        (the header row) and no data rows."""
        out = tmp_path / "qbo.csv"
        export_qbo_csv([], out)
        rows = _read_csv(out)
        assert len(rows) == 1
        assert rows[0] == ["Date", "Description", "Amount", "Category", "Memo"]


# ═══════════════════════════════════════════════════════════════════
#  Zero amount: QBO export with amount=0
# ═══════════════════════════════════════════════════════════════════


class TestQBOZeroAmount:
    def test_zero_amount_debit(self, tmp_path: Path):
        """A transaction with amount=0 should produce a row with amount 0."""
        txns = [_make_transaction(transaction_type="DEBIT", amount=Decimal("0"))]
        out = tmp_path / "qbo.csv"
        export_qbo_csv(txns, out)
        rows = _read_csv(out)
        amount = float(rows[1][2])
        assert amount == 0.0

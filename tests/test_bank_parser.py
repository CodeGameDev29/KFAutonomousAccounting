"""Tests for core.bank_parser.

Tests cover BMO CAD/USD, BMO Credit Card, and Wise CSV parsing,
format auto-detection, and edge cases (empty files, malformed rows).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.bank_parser import (
    detect_csv_format,
    parse_bmo_credit_card_csv,
    parse_bmo_csv,
    parse_csv,
    parse_wise_csv,
)
from models.transaction import AccountType, Transaction

# ========================== CSV fixture helpers ============================

BMO_CAD_HEADER = "Account Number,Transaction Type,Date Posted,Transaction Amount,Description"
BMO_CC_HEADER = "Transaction Date,Posting Date,Description,Amount,Card Number"
WISE_HEADER = (
    "ID,Status,Direction,Created on,Finished on,"
    "Source amount,Source currency,Target amount,Target currency,"
    "Exchange rate,Fee,Reference"
)


def _write_csv(tmp_path: Path, name: str, header: str, rows: list[str]) -> Path:
    """Write a CSV file under tmp_path and return its Path."""
    p = tmp_path / name
    p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


# ========================== BMO CAD / USD ==================================


def _bmo_cad_rows() -> list[str]:
    return [
        "000123456,DEBIT,20260106,-481.25,PAYPAL *BLUEPEAK",
        "000123456,CREDIT,20260115,5000.00,WIRE PAYMENT NORTHWIND",
        "000123456,DEBIT,20260118,-29.99,EXAMPLE RETAILER MARKETPLACE",
        "000123456,CREDIT,20260120,12000.00,WIRE PAYMENT CONTOSO STUDIOS",
        "000123456,DEBIT,20260125,-150.00,TRANSFER TO WISE",
    ]


class TestParseBMOCadCsv:
    """BMO CAD chequing CSV parsing."""

    def test_parse_bmo_cad_csv(self, tmp_path: Path) -> None:
        """Parse a 5-row BMO CAD CSV and verify all Transaction fields."""
        csv_file = _write_csv(tmp_path, "bmo_cad.csv", BMO_CAD_HEADER, _bmo_cad_rows())
        txns = parse_bmo_csv(csv_file, AccountType.CAD)

        assert len(txns) == 5

        # First row — a debit
        t0 = txns[0]
        assert isinstance(t0, Transaction)
        assert t0.account == AccountType.CAD
        assert t0.transaction_type == "DEBIT"
        assert t0.date_posted == date(2026, 1, 6)
        assert t0.amount == Decimal("-481.25")
        assert t0.currency == "CAD"
        assert t0.description == "PAYPAL *BLUEPEAK"
        assert t0.source_file == str(csv_file)
        assert t0.source_row == 1  # 1-based row index (header excluded)

        # Second row — a credit
        t1 = txns[1]
        assert t1.transaction_type == "CREDIT"
        assert t1.amount == Decimal("5000.00")
        assert t1.description == "WIRE PAYMENT NORTHWIND"
        assert t1.source_row == 2

        # Last row
        t4 = txns[4]
        assert t4.date_posted == date(2026, 1, 25)
        assert t4.amount == Decimal("-150.00")
        assert t4.source_row == 5

    def test_parse_bmo_usd_csv(self, tmp_path: Path) -> None:
        """BMO USD CSV produces transactions with currency=USD."""
        rows = [
            "000789012,CREDIT,20260110,8500.00,WIRE PAYMENT FABRIKAM GAMES",
            "000789012,DEBIT,20260112,-50.00,BANK FEE WIRE INCOMING",
        ]
        csv_file = _write_csv(tmp_path, "bank_usd.csv", BMO_CAD_HEADER, rows)
        txns = parse_bmo_csv(csv_file, AccountType.USD)

        assert len(txns) == 2
        assert all(t.currency == "USD" for t in txns)
        assert all(t.account == AccountType.USD for t in txns)

    def test_bmo_csv_debit_credit_types(self, tmp_path: Path) -> None:
        """DEBIT rows produce negative amounts; CREDIT rows produce positive."""
        rows = [
            "000123456,DEBIT,20260101,-200.00,SOME PURCHASE",
            "000123456,CREDIT,20260102,300.00,SOME DEPOSIT",
        ]
        csv_file = _write_csv(tmp_path, "bmo_types.csv", BMO_CAD_HEADER, rows)
        txns = parse_bmo_csv(csv_file, AccountType.CAD)

        debit = txns[0]
        credit = txns[1]
        assert debit.transaction_type == "DEBIT"
        assert debit.amount < 0
        assert credit.transaction_type == "CREDIT"
        assert credit.amount > 0

    def test_bmo_csv_date_parsing(self, tmp_path: Path) -> None:
        """YYYYMMDD date strings are parsed into date objects correctly."""
        rows = [
            "000123456,DEBIT,20261231,-10.00,NEW YEARS EVE PURCHASE",
            "000123456,CREDIT,20260301,100.00,MARCH DEPOSIT",
        ]
        csv_file = _write_csv(tmp_path, "bmo_dates.csv", BMO_CAD_HEADER, rows)
        txns = parse_bmo_csv(csv_file, AccountType.CAD)

        assert txns[0].date_posted == date(2026, 12, 31)
        assert txns[1].date_posted == date(2026, 3, 1)

    def test_bmo_csv_empty_file(self, tmp_path: Path) -> None:
        """A CSV with only a header (no data rows) returns an empty list."""
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text(BMO_CAD_HEADER + "\n", encoding="utf-8")
        txns = parse_bmo_csv(csv_file, AccountType.CAD)

        assert txns == []

    def test_bmo_csv_malformed_row(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Rows with missing columns are skipped and a warning is logged."""
        rows = [
            "000123456,DEBIT,20260106,-481.25,PAYPAL *BLUEPEAK",
            "000123456,DEBIT,20260107",  # malformed — missing columns
            "000123456,CREDIT,20260108,100.00,VALID ROW",
        ]
        csv_file = _write_csv(tmp_path, "bmo_bad.csv", BMO_CAD_HEADER, rows)
        txns = parse_bmo_csv(csv_file, AccountType.CAD)

        # Malformed row is skipped; two valid rows remain
        assert len(txns) == 2
        assert txns[0].description == "PAYPAL *BLUEPEAK"
        assert txns[1].description == "VALID ROW"

        # A warning should have been logged about the skipped row
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("row" in msg.lower() or "skip" in msg.lower() for msg in warning_messages)


# ========================== BMO Credit Card ================================


def _bmo_cc_rows() -> list[str]:
    return [
        "01/05/2026,01/06/2026,ORCHID TELECOM,-56.00,1234",
        "01/10/2026,01/12/2026,EXAMPLE RETAILER,-129.99,1234",
        "01/15/2026,01/16/2026,PAYMENT THANK YOU,500.00,1234",
    ]


class TestParseBMOCreditCardCsv:
    """BMO credit card CSV parsing."""

    def test_parse_bmo_credit_card_csv(self, tmp_path: Path) -> None:
        """Parse a 3-row CC CSV and verify Transaction fields."""
        csv_file = _write_csv(tmp_path, "bmo_cc.csv", BMO_CC_HEADER, _bmo_cc_rows())
        txns = parse_bmo_credit_card_csv(csv_file)

        assert len(txns) == 3

        t0 = txns[0]
        assert t0.account == AccountType.CREDIT_CARD
        assert t0.currency == "CAD"
        assert t0.description == "ORCHID TELECOM"
        assert t0.amount == Decimal("-56.00")
        # CC convention matching XLSX: negative = CREDIT (payment/refund)
        assert t0.transaction_type == "CREDIT"
        assert t0.source_file == str(csv_file)
        assert t0.source_row == 1

        # Positive amount on CC = DEBIT (charge)
        t2 = txns[2]
        assert t2.transaction_type == "DEBIT"
        assert t2.amount == Decimal("500.00")
        assert t2.description == "PAYMENT THANK YOU"

    def test_credit_card_date_uses_posting_date(self, tmp_path: Path) -> None:
        """date_posted should come from the Posting Date column, not Transaction Date."""
        csv_file = _write_csv(tmp_path, "bmo_cc.csv", BMO_CC_HEADER, _bmo_cc_rows())
        txns = parse_bmo_credit_card_csv(csv_file)

        # First row: Transaction Date=01/05/2026, Posting Date=01/06/2026
        assert txns[0].date_posted == date(2026, 1, 6)
        # Second row: Posting Date=01/12/2026
        assert txns[1].date_posted == date(2026, 1, 12)


# ========================== Wise ==========================================


def _wise_rows() -> list[str]:
    return [
        (
            "12345,COMPLETED,DEBIT,2026-01-10 10:00:00,2026-01-10 10:05:00,"
            "1500.00,CAD,7890.00,CNY,5.26,7.50,Contractor Jan payment"
        ),
        (
            "12346,COMPLETED,CREDIT,2026-01-12 14:30:00,2026-01-12 14:35:00,"
            "3000.00,USD,3000.00,USD,1.0,0.00,Client payment received"
        ),
        (
            "12347,COMPLETED,DEBIT,2026-01-15 09:00:00,2026-01-15 09:10:00,"
            "500.00,CAD,370.00,USD,0.74,3.75,Transfer to BMO USD"
        ),
    ]


def _wise_rows_with_pending() -> list[str]:
    """Includes a non-COMPLETED row that should be filtered out."""
    return _wise_rows() + [
        (
            "12348,PENDING,DEBIT,2026-01-20 08:00:00,,200.00,CAD,,,,"
            "Pending transfer"
        ),
        (
            "12349,CANCELLED,DEBIT,2026-01-21 08:00:00,,100.00,CAD,,,,"
            "Cancelled transfer"
        ),
    ]


class TestParseWiseCsv:
    """Wise CSV parsing."""

    def test_parse_wise_csv(self, tmp_path: Path) -> None:
        """Parse 3-row Wise CSV with exchange rate data and verify fields."""
        csv_file = _write_csv(tmp_path, "wise.csv", WISE_HEADER, _wise_rows())
        txns = parse_wise_csv(csv_file)

        assert len(txns) == 3

        t0 = txns[0]
        assert isinstance(t0, Transaction)
        assert t0.account == AccountType.WISE
        assert t0.transaction_type == "DEBIT"
        assert t0.date_posted == date(2026, 1, 10)
        assert t0.amount == Decimal("-1500.00")
        assert t0.currency == "CAD"
        assert t0.source_file == str(csv_file)
        assert t0.source_row == 1

        # CREDIT row
        t1 = txns[1]
        assert t1.transaction_type == "CREDIT"
        assert t1.amount == Decimal("3000.00")
        assert t1.currency == "USD"

    def test_wise_csv_completed_only(self, tmp_path: Path) -> None:
        """Only rows with Status=COMPLETED are included; PENDING/CANCELLED skipped."""
        csv_file = _write_csv(
            tmp_path, "wise_mixed.csv", WISE_HEADER, _wise_rows_with_pending()
        )
        txns = parse_wise_csv(csv_file)

        assert len(txns) == 3  # 5 rows total, 2 non-COMPLETED skipped

    def test_wise_csv_direction_mapping(self, tmp_path: Path) -> None:
        """Wise Direction column maps: DEBIT->DEBIT, CREDIT->CREDIT."""
        csv_file = _write_csv(tmp_path, "wise.csv", WISE_HEADER, _wise_rows())
        txns = parse_wise_csv(csv_file)

        assert txns[0].transaction_type == "DEBIT"
        assert txns[1].transaction_type == "CREDIT"
        assert txns[2].transaction_type == "DEBIT"

    def test_wise_csv_preserves_exchange_info(self, tmp_path: Path) -> None:
        """Exchange rate info should be captured in the transaction description."""
        csv_file = _write_csv(tmp_path, "wise.csv", WISE_HEADER, _wise_rows())
        txns = parse_wise_csv(csv_file)

        # First row: CAD -> CNY at rate 5.26
        desc = txns[0].description
        assert "Contractor Jan payment" in desc
        # Exchange info should be present in some form
        assert "5.26" in desc or "CNY" in desc or "7890" in desc


# ========================== Format Detection ==============================


class TestDetectCsvFormat:
    """Auto-detection of CSV format from header row."""

    def test_detect_bmo_cad_format(self, tmp_path: Path) -> None:
        csv_file = _write_csv(tmp_path, "bmo.csv", BMO_CAD_HEADER, _bmo_cad_rows()[:1])
        fmt = detect_csv_format(csv_file)
        # BMO CAD and USD share the same CSV format; detection returns
        # a generic BMO type (actual account is determined by user context).
        assert fmt in ("bmo_cad", "bmo_usd")

    def test_detect_wise_format(self, tmp_path: Path) -> None:
        csv_file = _write_csv(tmp_path, "wise.csv", WISE_HEADER, _wise_rows()[:1])
        fmt = detect_csv_format(csv_file)
        assert fmt == "wise"

    def test_detect_unknown_format_raises(self, tmp_path: Path) -> None:
        """An unrecognised CSV header should raise ValueError."""
        csv_file = tmp_path / "unknown.csv"
        csv_file.write_text("Foo,Bar,Baz\n1,2,3\n", encoding="utf-8")
        with pytest.raises(ValueError, match="[Uu]nknown|[Uu]nrecogni"):
            detect_csv_format(csv_file)


# ========================== Auto-detect entry point =======================


class TestParseCsvAutoDetect:
    """parse_csv() auto-detects format and delegates to the right parser."""

    def test_parse_csv_auto_detects_bmo(self, tmp_path: Path) -> None:
        csv_file = _write_csv(tmp_path, "bmo.csv", BMO_CAD_HEADER, _bmo_cad_rows())
        txns = parse_csv(csv_file)

        assert len(txns) == 5
        # Should have parsed as BMO; exact account may default to CAD
        assert all(t.account in (AccountType.CAD, AccountType.USD) for t in txns)

    def test_parse_csv_auto_detects_wise(self, tmp_path: Path) -> None:
        csv_file = _write_csv(tmp_path, "wise.csv", WISE_HEADER, _wise_rows())
        txns = parse_csv(csv_file)

        assert len(txns) == 3
        assert all(t.account == AccountType.WISE for t in txns)

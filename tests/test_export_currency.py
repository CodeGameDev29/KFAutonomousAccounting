"""An export carries one currency per file, or it refuses to write.

Every transaction, amount and description in this module is invented — see
``tests/fixtures/README.md``.

Why it matters: a month's batch can hold CAD card charges beside USD client
wires, and Xero reads a bank-statement CSV's currency from the account the file
is imported into rather than from the file itself. A US$9,876.54 client wire
imported into a CAD account is booked as C$9,876.54 — at a ~1.38 rate that
overstates the month's revenue by roughly 38%, straight into a filing.

These tests hold the rule: the exporters refuse a mixed batch outright, and
every file they do write states its currency on every row.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from core.ledger import (
    LEDGER_HEADERS_STANDARD,
    QBO_HEADERS,
    XERO_HEADERS,
    MixedCurrencyExportError,
    export_ledger,
    export_qbo_csv,
    export_transactions_csv,
    export_xero_csv,
    group_by_currency,
)
from models.ledger_entry import LedgerEntry
from models.transaction import AccountType, Transaction


def _txn(txn_id, currency, amount, *, account="CAD", ttype="CREDIT", description="row"):
    return Transaction(
        id=txn_id,
        account=account,
        transaction_type=ttype,
        date_posted=date(2026, 2, 1 + txn_id),
        amount=Decimal(amount),
        currency=currency,
        description=description,
        source_file="statement_feb2026.csv",
        source_row=txn_id,
    )


# The two rows that cannot share a file: a USD client wire settled into the USD
# account, and a CAD card charge for a foreign supply billed in USD.
MIXED = [
    _txn(1, "USD", "9876.54", account="USD",
         description="IncomingWirePayment,INCOMINGWIRE PAYMENT,US,HARBOURLINE LOGISTICS,"),
    _txn(2, "CAD", "41.25", account="CreditCard", ttype="DEBIT",
         description="USD 29.50@1.3983 BLUEPEAK SOFTWARE INC SAMPLETOWNCA"),
]


class TestMixedCurrencyIsRefused:
    def test_xero_refuses_a_mixed_batch(self, tmp_path: Path):
        with pytest.raises(MixedCurrencyExportError) as exc:
            export_xero_csv(MIXED, tmp_path / "xero.csv")
        assert exc.value.currencies == ["CAD", "USD"]
        assert "one currency per file" in str(exc.value)

    def test_qbo_refuses_a_mixed_batch(self, tmp_path: Path):
        with pytest.raises(MixedCurrencyExportError):
            export_qbo_csv(MIXED, tmp_path / "qbo.csv")

    def test_transactions_csv_keeps_currency_on_every_row(self, tmp_path: Path):
        """The CRA-format transactions CSV does not split, so it must label."""
        out = tmp_path / "txns.csv"
        export_transactions_csv(MIXED, out)
        with open(out, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert "Currency" in LEDGER_HEADERS_STANDARD
        assert {r["Currency"] for r in rows} == {"CAD", "USD"}

    def test_no_usd_amount_is_ever_written_into_a_cad_file(self, tmp_path: Path):
        """The end-to-end guarantee: split, then check each file's own rows."""
        for currency, rows in group_by_currency(MIXED).items():
            out = tmp_path / f"xero_{currency}.csv"
            written = export_xero_csv(rows, out)
            assert written == currency
            with open(out, encoding="utf-8-sig", newline="") as f:
                data = list(csv.DictReader(f))
            assert {r["Currency"] for r in data} == {currency}
        cad = (tmp_path / "xero_CAD.csv").read_text(encoding="utf-8-sig")
        assert "9876.54" not in cad


class TestCurrencyIsStated:
    def test_xero_header_has_a_currency_column(self, tmp_path: Path):
        out = tmp_path / "xero.csv"
        export_xero_csv([], out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            assert next(csv.reader(f)) == XERO_HEADERS
        assert "Currency" in XERO_HEADERS

    def test_qbo_header_has_a_currency_column(self, tmp_path: Path):
        out = tmp_path / "qbo.csv"
        export_qbo_csv([], out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            assert next(csv.reader(f)) == QBO_HEADERS
        assert "Currency" in QBO_HEADERS

    def test_amounts_are_native_never_converted(self, tmp_path: Path):
        usd = [r for r in MIXED if r.currency == "USD"]
        out = tmp_path / "xero_usd.csv"
        export_xero_csv(usd, out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        assert row["Amount"] == "9876.54"
        assert row["Currency"] == "USD"

    def test_group_by_currency_puts_cad_first(self):
        keys = list(group_by_currency(MIXED))
        assert keys == ["CAD", "USD"]


class TestLedgerWorkbookCurrency:
    def _entry(self, currency, amount, ttype="CREDIT"):
        return LedgerEntry(
            account=AccountType.WISE,
            month="Feb2026",
            transaction_type=ttype,
            date_posted=date(2026, 2, 5),
            amount=Decimal(amount),
            currency=currency,
            description="row",
            category="Sales of goods and services",
        )

    def test_account_sheet_carries_currency(self, tmp_path: Path):
        out = tmp_path / "ledger.xlsx"
        export_ledger(
            {"Feb2026_Wise": [self._entry("CAD", "120.00"), self._entry("USD", "300.00")]},
            out, year=2026,
        )
        ws = openpyxl.load_workbook(out)["Feb2026_Wise"]
        header = [c.value for c in ws[1]]
        assert header == LEDGER_HEADERS_STANDARD
        cur_col = header.index("Currency") + 1
        assert {ws.cell(row=r, column=cur_col).value for r in (2, 3)} == {"CAD", "USD"}

    def test_net_income_never_sums_two_currencies(self, tmp_path: Path):
        """A Wise tab holds CAD and USD; one blended total would be a number
        that is neither, so NET INCOME reports a row per currency.

        C$120.00 + US$300.00 is not 420 of anything — that figure is the one
        this test exists to keep out of the workbook.
        """
        out = tmp_path / "ledger.xlsx"
        export_ledger(
            {"Feb2026_Wise": [self._entry("CAD", "120.00"), self._entry("USD", "300.00")]},
            out, year=2026,
        )
        ws = openpyxl.load_workbook(out)["NET INCOME"]
        rows = {
            ws.cell(row=r, column=2).value: float(ws.cell(row=r, column=3).value)
            for r in (2, 3)
        }
        assert rows == {"CAD": 120.00, "USD": 300.00}
        assert 420.00 not in rows.values()

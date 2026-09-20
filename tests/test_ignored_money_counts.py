"""IGNORED money counts; EXCLUDED money does not.

The `status` column is a statement about PAPERWORK, not about dollars — with
exactly one exception. Two distinct meanings have to stay apart, because a
single status value cannot carry both:

* reconciliation auto-sets IGNORED on bank fees / interest / transfers to mean
  "self-evident — no receipt or note needed". The money is still spent: an
  ignored $14.95 bank fee is a $14.95 deductible expense and must appear in
  every total, chart, ledger and tax figure.
* a statement-import reject means "this row never happened" (mis-parse,
  duplicate, belongs to another account). Because every money aggregation is
  status-blind, a rejected row filed under IGNORED would book phantom dollars
  into expenses, net income, the category charts, the record binder and the
  QBO/Xero exports.

Migration 034 split them: reject now writes EXCLUDED, the only status that
gates money (core.transfer_exclusion.NON_COUNTABLE_STATUSES).

These tests are the guard rail in both directions. If someone ever "optimises"
a money query by filtering out IGNORED rows, the first class fails.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl

from core.transfer_exclusion import (
    NON_COUNTABLE_STATUSES,
    countable_sql,
    is_countable,
)
from models.transaction import AccountType, Transaction, TransactionStatus

# A self-evident bank fee: needs no receipt, but is a real deductible expense.
BANK_FEE = Decimal("14.95")


def _make_txn(
    txn_id: int,
    desc: str,
    amount: Decimal,
    txn_type: str = "DEBIT",
    category: str | None = None,
    status: TransactionStatus = TransactionStatus.UNMATCHED,
    acct: AccountType = AccountType.CAD,
    dt: date = date(2026, 1, 2),
) -> Transaction:
    txn = Transaction(
        account=acct,
        transaction_type=txn_type,
        date_posted=dt,
        amount=amount,
        currency="CAD",
        description=desc,
        source_file="bank.csv",
        source_row=1,
        status=status,
        category=category,
    )
    txn.id = txn_id
    return txn


def _summary_totals(xlsx_bytes: bytes) -> tuple[float, float, float]:
    """(income, expenses, net) from the binder Summary sheet's TOTAL row."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb["Summary"]
    for row in ws.iter_rows(min_col=1, max_col=6):
        if isinstance(row[0].value, str) and row[0].value.startswith("TOTAL"):
            return (
                float(row[3].value or 0),
                float(row[4].value or 0),
                float(row[5].value or 0),
            )
    raise AssertionError("TOTAL row not found in Summary sheet")


class TestCountability:
    def test_only_excluded_is_non_countable(self):
        assert NON_COUNTABLE_STATUSES == ("EXCLUDED",)

    def test_ignored_money_counts(self):
        # The whole point: "no receipt needed" says nothing about dollars.
        assert is_countable("IGNORED")
        assert is_countable(TransactionStatus.IGNORED)

    def test_every_status_but_excluded_counts(self):
        for status in TransactionStatus:
            if status is TransactionStatus.EXCLUDED:
                assert not is_countable(status)
            else:
                assert is_countable(status), status

    def test_countable_sql_binds_the_status_list(self):
        sql, params = countable_sql(alias="t")
        assert "t.status" in sql
        assert params == [["EXCLUDED"]]
        # Must not smuggle IGNORED into the money gate.
        assert "IGNORED" not in sql


class TestIgnoredMoneyReachesTheBinder:
    """An ignored bank fee is a real expense and must show up in the totals."""

    def test_ignored_bank_fee_is_still_an_expense(self):
        from core.export_binder import _build_xlsx

        txns = [
            _make_txn(1, "CLIENT WIRE", Decimal("5000.00"), txn_type="CREDIT",
                      category="Income"),
            # Auto-ignored by the reconciliation heuristic. Real money.
            _make_txn(2, "BANK MONTHLY FEE", BANK_FEE, txn_type="DEBIT",
                      category="Interest and bank charges",
                      status=TransactionStatus.IGNORED),
        ]

        income, expenses, net = _summary_totals(
            _build_xlsx("TestCo", 2026, 1, "Jan2026", txns, {}, {},
                        transfer_txn_ids=set())
        )
        assert income == 5000.00
        assert expenses == float(BANK_FEE), (
            "an IGNORED bank fee was dropped from expenses — IGNORED means "
            "'no receipt needed', not 'this money did not happen'"
        )
        assert net == 5000.00 - float(BANK_FEE)

    def test_ignored_interest_is_still_income(self):
        from core.export_binder import _build_xlsx

        txns = [
            _make_txn(1, "INTEREST EARNED", Decimal("42.00"), txn_type="CREDIT",
                      category="All other revenues",
                      status=TransactionStatus.IGNORED),
        ]

        income, _expenses, net = _summary_totals(
            _build_xlsx("TestCo", 2026, 1, "Jan2026", txns, {}, {},
                        transfer_txn_ids=set())
        )
        assert income == 42.00
        assert net == 42.00

    def test_ignored_row_still_appears_as_a_ledger_row(self):
        """CRA needs the row itself, not just its dollars."""
        from core.export_binder import _build_xlsx

        txns = [
            _make_txn(1, "BANK MONTHLY FEE", BANK_FEE, txn_type="DEBIT",
                      category="Interest and bank charges",
                      status=TransactionStatus.IGNORED),
        ]
        wb = openpyxl.load_workbook(io.BytesIO(
            _build_xlsx("TestCo", 2026, 1, "Jan2026", txns, {}, {},
                        transfer_txn_ids=set())
        ))
        cells = [
            c.value
            for sheet in wb.sheetnames
            for row in wb[sheet].iter_rows()
            for c in row
        ]
        assert any(
            isinstance(v, str) and "BANK MONTHLY FEE" in v for v in cells
        ), "the ignored fee vanished from the binder entirely"

    def test_binder_never_claims_ignored_money_was_excluded(self):
        """An accountant reading the binder must see why a row carries no
        receipt, not a claim that the user excluded it."""
        from core.export_binder import _build_xlsx

        txns = [
            _make_txn(1, "BANK MONTHLY FEE", BANK_FEE, txn_type="DEBIT",
                      status=TransactionStatus.IGNORED),
        ]
        wb = openpyxl.load_workbook(io.BytesIO(
            _build_xlsx("TestCo", 2026, 1, "Jan2026", txns, {}, {},
                        transfer_txn_ids=set())
        ))
        text = " ".join(
            str(c.value)
            for row in wb["Summary"].iter_rows()
            for c in row
            if isinstance(c.value, str)
        )
        assert "excluded by user" not in text.lower()
        assert "no receipt needed" in text.lower()

"""Guards for reconciliation's sign compatibility rule.

Covers:

- ``_is_transaction_expense`` must honor the ``transaction_type`` field
  (``DEBIT`` = expense, ``CREDIT`` = deposit/payment). The LLM statement
  parser (``core/llm_statement_parser.py``) emits bank debits as
  ``transaction_type="DEBIT"`` with a POSITIVE ``amount``, so a sign-only
  heuristic reads them as NOT-expense — and then
  ``check_sign_compatibility`` rejects every candidate pair and the amount
  agent reports "no amount match, skipped" for a whole statement's worth of
  receipts. A parser convention must not be able to silence the matcher.

- ``check_sign_compatibility`` must return True for (DEBIT transaction,
  expense receipt) and (CREDIT transaction, income invoice) regardless of
  the raw amount sign.

These are pure-Python unit tests — no DB, no LLM, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.reconciliation import (
    _is_income_document,
    _is_transaction_expense,
    check_sign_compatibility,
)
from models.document import Document, DocumentStatus
from models.transaction import Transaction, TransactionStatus


def _txn(
    *,
    id: int = 1,
    account: str = "CAD",
    transaction_type: str = "DEBIT",
    amount: Decimal = Decimal("100.00"),
    currency: str = "CAD",
    description: str = "TEST VENDOR",
    date_posted: date = date(2026, 3, 4),
) -> Transaction:
    return Transaction(
        id=id,
        account=account,
        transaction_type=transaction_type,
        date_posted=date_posted,
        amount=amount,
        currency=currency,
        description=description,
        source_file="test.csv",
        source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _doc(
    *,
    id: int = 1,
    vendor: str = "Test Vendor",
    total: Decimal = Decimal("100.00"),
    currency: str = "CAD",
    document_date: date = date(2026, 3, 4),
    original_filename: str = "20260304-test-Receipt.pdf",
    invoice_number: str | None = None,
) -> Document:
    return Document(
        id=id,
        original_filename=original_filename,
        file_hash="a" * 32,
        vendor=vendor,
        document_date=document_date,
        currency=currency,
        total=total,
        status=DocumentStatus.EXTRACTED,
        invoice_number=invoice_number,
    )


# ── _is_transaction_expense: transaction_type is authoritative ───────────


def test_debit_with_positive_amount_is_expense_on_bank():
    """The canonical case: a bank DEBIT stored with a positive amount."""
    txn = _txn(account="CAD", transaction_type="DEBIT", amount=Decimal("133.82"))
    assert _is_transaction_expense(txn) is True


def test_debit_with_negative_amount_is_expense_on_bank_legacy():
    """Legacy form (negative amount on bank debit) still classified as expense."""
    txn = _txn(account="CAD", transaction_type="DEBIT", amount=Decimal("-133.82"))
    assert _is_transaction_expense(txn) is True


def test_credit_with_positive_amount_is_not_expense_on_bank():
    """Bank CREDIT (deposit) is not an expense regardless of sign."""
    txn = _txn(account="CAD", transaction_type="CREDIT", amount=Decimal("500.00"))
    assert _is_transaction_expense(txn) is False


def test_credit_with_negative_amount_is_not_expense_on_bank():
    """CREDIT type beats sign — even if stored as a negative credit."""
    txn = _txn(account="CAD", transaction_type="CREDIT", amount=Decimal("-500.00"))
    assert _is_transaction_expense(txn) is False


def test_debit_on_credit_card_is_expense():
    """CC DEBIT (purchase) is an expense — canonical form stores positive."""
    txn = _txn(account="CreditCard", transaction_type="DEBIT", amount=Decimal("24.80"))
    assert _is_transaction_expense(txn) is True


def test_credit_on_credit_card_is_not_expense():
    """CC CREDIT (payment / refund) is not an expense."""
    txn = _txn(account="CreditCard", transaction_type="CREDIT", amount=Decimal("-516.00"))
    assert _is_transaction_expense(txn) is False


# ── check_sign_compatibility end to end ─────────────────────────────────


def test_sign_compat_bank_debit_receipt_compatible():
    """The canonical pair: bank DEBIT (expense) vs receipt (expense doc) → compatible."""
    txn = _txn(account="CAD", transaction_type="DEBIT", amount=Decimal("133.82"))
    doc = _doc(
        vendor="Maple Street Office Supplies",
        total=Decimal("133.82"),
        original_filename="20260303-MapleStreetOfficeSupplies-Receipt.pdf",
    )
    assert _is_income_document(doc) is False
    assert check_sign_compatibility(txn, doc) is True


def test_sign_compat_bank_credit_receipt_rejected():
    """Bank CREDIT (deposit) vs expense receipt → incompatible (deposit doesn't match purchase receipt)."""
    txn = _txn(account="CAD", transaction_type="CREDIT", amount=Decimal("500.00"))
    doc = _doc(original_filename="20260320-something-Receipt.pdf")
    assert check_sign_compatibility(txn, doc) is False


def test_sign_compat_cc_debit_receipt_compatible():
    """CC DEBIT + receipt → compatible (cross-currency card charge)."""
    txn = _txn(
        account="CreditCard",
        transaction_type="DEBIT",
        amount=Decimal("24.80"),
        currency="CAD",
        description="USD 12.34@1.3500 EXAMPLE VENDOR",
    )
    doc = _doc(
        vendor="Example Vendor Inc.",
        total=Decimal("17.67"),
        currency="USD",
        original_filename="vendor_invoice_abcd.pdf",
    )
    # A supplier invoice: the filename has "invoice" but the vendor is NOT the
    # user's own company, so it stays an expense document.
    assert _is_income_document(doc) is False
    assert check_sign_compatibility(txn, doc) is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])

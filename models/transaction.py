from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class AccountType(str, Enum):
    CAD = "CAD"
    USD = "USD"
    WISE = "Wise"
    CREDIT_CARD = "CreditCard"
    PAYPAL = "PayPal"
    AMAZON = "Amazon"


class TransactionStatus(str, Enum):
    """Reconciliation state of a bank/credit-card row.

    IGNORED and EXCLUDED are easy to confuse and mean opposite things about money:

    * ``IGNORED`` — self-evident row (bank fee, interest, internal transfer).
      Needs no receipt and no explanatory note. **Its money still counts** in
      every total, chart, ledger and tax figure. This is a statement about
      paperwork, never about dollars.
    * ``EXCLUDED`` — the row isn't real (mis-parse, duplicate, not the user's).
      Rejected during statement-import review. **Its money must never count.**
    """

    UNMATCHED = "UNMATCHED"
    MATCHED = "MATCHED"
    IGNORED = "IGNORED"
    LINKED = "LINKED"
    PENDING_REVIEW = "PENDING_REVIEW"
    EXCLUDED = "EXCLUDED"


class ImportConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    LEGACY = "LEGACY"


class Transaction(BaseModel):
    id: int | None = None
    # `account` is a free-form string, not validated against AccountType: a
    # statement parsed by the LLM carries whatever institution name it names
    # (e.g. "BMO"). AccountType stays as the set of well-known keys.
    account: str = ""
    transaction_type: str = Field(pattern=r"^(DEBIT|CREDIT)$")
    date_posted: date
    amount: Decimal
    currency: str = Field(pattern=r"^(CAD|USD|CNY|EUR|GBP)$")
    description: str
    source_file: str
    source_row: int
    status: TransactionStatus = TransactionStatus.UNMATCHED
    category: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    # Institution-agnostic fields
    institution: str | None = None
    account_type: str | None = None
    external_id: str | None = None
    import_confidence: ImportConfidence | None = None
    import_flags: list[str] = Field(default_factory=list)
    statement_id: str | None = None


class Statement(BaseModel):
    id: str
    user_id: str
    source_file: str
    source_file_hash: str | None = None
    institution: str
    account_type: str
    account_number: str | None = None
    currency: str
    statement_period_start: date
    statement_period_end: date
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    balance_matches: bool | None = None
    balance_difference: Decimal | None = None
    page_count: int | None = None
    row_count: int | None = None
    import_confidence: ImportConfidence
    import_flags: list[str] = Field(default_factory=list)
    parse_method: str
    llm_model: str | None = None
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_usd: float = 0.0
    created_at: datetime


def account_str(account) -> str:
    """Return the string form of a Transaction.account field.

    ``Transaction.account`` may hold an ``AccountType`` member or a plain
    string (a statement parsed by the LLM carries an arbitrary institution
    name). This helper accepts either.
    """
    if account is None:
        return ""
    if hasattr(account, "value"):
        return account.value
    return str(account)

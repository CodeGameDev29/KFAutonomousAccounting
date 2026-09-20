from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from models.transaction import AccountType


class LedgerEntry(BaseModel):
    id: int | None = None
    account: AccountType
    month: str  # e.g. "Jan2026"
    transaction_type: str
    date_posted: date
    amount: Decimal
    currency: str
    description: str
    category: str | None = None
    document_link: str | None = None
    note: str | None = None
    match_id: int | None = None
    created_at: datetime | None = None

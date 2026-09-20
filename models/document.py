from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel


class DocumentStatus(str, Enum):
    EXTRACTED = "EXTRACTED"
    MATCHED = "MATCHED"
    FLAGGED = "FLAGGED"


class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: Decimal
    amount: Decimal


class TaxBreakdown(BaseModel):
    gst: Decimal | None = None
    hst: Decimal | None = None
    pst: Decimal | None = None
    other_tax: Decimal | None = None
    tax_label: str | None = None


class Document(BaseModel):
    id: int | None = None
    original_filename: str
    stored_path: str | None = None
    file_hash: str
    vendor: str | None = None
    document_date: date | None = None
    currency: str | None = None
    # How `currency` was arrived at: "stated" (the document says so),
    # "inferred" (read off its Canadian tax line), "assumed" (nothing said —
    # `currency` is then None). NULL when a row carries no provenance at all;
    # core/document_currency.py reads NULL as stated.
    currency_source: str | None = None
    subtotal: Decimal | None = None
    tax_gst: Decimal | None = None
    tax_hst: Decimal | None = None
    tax_pst: Decimal | None = None
    tax_other: Decimal | None = None
    total: Decimal | None = None
    payment_method: str | None = None
    invoice_number: str | None = None
    line_items: list[LineItem] | None = None
    extraction_confidence: str | None = None
    extraction_raw_json: str | None = None
    status: DocumentStatus = DocumentStatus.EXTRACTED
    field_confidence: dict | None = None       # {"vendor": "high", "date": "medium", ...}
    review_status: str = "pending"             # auto_accepted | flagged | corrected | pending
    created_at: datetime | None = None

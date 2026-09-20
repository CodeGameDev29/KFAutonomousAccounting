"""Tests for payment date extraction from document metadata."""

from datetime import date
from decimal import Decimal

from core.reconciliation import _extract_payment_date, _get_best_date
from models.document import Document, DocumentStatus


def _doc(payment_method=None, raw_json=None, document_date=None):
    return Document(
        original_filename="test.pdf",
        file_hash="abc123",
        vendor="Test",
        document_date=document_date or date(2026, 1, 19),
        currency="CAD",
        total=Decimal("81.45"),
        payment_method=payment_method,
        extraction_raw_json=raw_json,
        status=DocumentStatus.EXTRACTED,
    )


class TestExtractPaymentDate:
    def test_from_payment_method_text(self):
        """'Bank account on February 02' → 2026-02-02."""
        doc = _doc(payment_method="Bank account on February 02")
        result = _extract_payment_date(doc)
        assert result == date(2026, 2, 2)

    def test_from_payment_method_short_month(self):
        """'Auto-pay on Feb 2' → 2026-02-02."""
        doc = _doc(payment_method="Auto-pay on Feb 2")
        result = _extract_payment_date(doc)
        assert result == date(2026, 2, 2)

    def test_from_raw_json_payment_date_field(self):
        """payment_date field in raw JSON."""
        import json
        raw = json.dumps({"payment_date": "2026-02-02", "vendor": "ORCHID TELECOM"})
        doc = _doc(raw_json=raw)
        result = _extract_payment_date(doc)
        assert result == date(2026, 2, 2)

    def test_no_payment_date(self):
        """Returns None when no payment date clues."""
        doc = _doc(payment_method="Credit Card ending 0000")
        result = _extract_payment_date(doc)
        assert result is None

    def test_none_payment_method(self):
        doc = _doc(payment_method=None)
        result = _extract_payment_date(doc)
        assert result is None


class TestGetBestDate:
    def test_prefers_payment_date(self):
        """Payment date is preferred over document date."""
        doc = _doc(
            payment_method="Bank account on February 02",
            document_date=date(2026, 1, 19),
        )
        result = _get_best_date(doc)
        assert result == date(2026, 2, 2)

    def test_falls_back_to_document_date(self):
        """When no payment date, use document date."""
        doc = _doc(document_date=date(2026, 1, 19))
        result = _get_best_date(doc)
        assert result == date(2026, 1, 19)

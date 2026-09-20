"""Per-field confidence for an extracted document — ``compute_field_confidence``.

SYNTHETIC DATA. The receipt modelled here is the generated
``tests/fixtures/test_receipt_retailer.pdf``; its subtotal, GST, PST and total
are read from ``scripts/gen_fixtures.receipt_totals()`` rather than typed in, so
the "5% GST + 7% PST can only be a Canadian charge" reasoning below is checked
against the arithmetic the fixture actually prints. No real receipt appears.

The cases below are the ones the scorer has to get right. What the
function is for: a review queue that shows the user which fields to look at. So
"low" has to mean *look at this* and "high" has to mean *don't* — a scorer that
calls a guess "high" is worse than one that admits it does not know, which is
why the currency cases below are graded by provenance rather than by presence.
The function must also never raise: it runs on whatever a model emitted, and a
crash there loses a whole document.
"""

from __future__ import annotations

from decimal import Decimal

from core.extraction import compute_field_confidence
from scripts.gen_fixtures import INVOICE_NUMBER, RECEIPT_DATE, RETAILER, receipt_totals

# ─── Helpers ────────────────────────────────────────────────────────

SUBTOTAL, GST, PST, TOTAL = receipt_totals()


def _complete_extraction() -> dict:
    """An extraction dict where every field is valid and present.

    These are the generated retail receipt's own figures: 36.00 + 5% GST + 7%
    PST = 40.32. Two things ride on that arithmetic — the cross-validation
    bonus (subtotal + taxes = total) and the Canadian-tax inference that the
    currency cases below turn on — so both are derived, not asserted blind.
    """
    return {
        "vendor": RETAILER,
        "date": RECEIPT_DATE.isoformat(),
        # A complete extraction quotes the currency text the document prints
        # ("CAD") as well as reading it. The quotation is what earns "stated",
        # and "stated" is what earns "high" — core/document_currency.py.
        "currency_marker": "CAD",
        "currency": "CAD",
        "subtotal": f"{SUBTOTAL:.2f}",
        "gst": f"{GST:.2f}",
        "hst": None,
        "pst": f"{PST:.2f}",
        "other_tax": None,
        "total": f"{TOTAL:.2f}",
        "payment_method": "Credit card",
        "invoice_number": INVOICE_NUMBER,
    }


# ═══════════════════════════════════════════════════════════════════
#  All fields present and valid → all "high"
# ═══════════════════════════════════════════════════════════════════


class TestAllFieldsPresent:
    def test_all_present_fields_are_high(self):
        data = _complete_extraction()
        result = compute_field_confidence(data)

        assert result["vendor"] == "high"
        assert result["date"] == "high"
        assert result["total"] == "high"
        assert result["currency"] == "high"
        assert result["subtotal"] == "high"
        assert result["gst"] == "high"
        assert result["pst"] == "high"
        assert result["payment_method"] == "high"
        assert result["invoice_number"] == "high"


# ═══════════════════════════════════════════════════════════════════
#  Null vendor → vendor "low"
# ═══════════════════════════════════════════════════════════════════


class TestNullVendor:
    def test_null_vendor_is_low(self):
        data = _complete_extraction()
        data["vendor"] = None
        result = compute_field_confidence(data)
        assert result["vendor"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Empty string vendor → "low"
# ═══════════════════════════════════════════════════════════════════


class TestEmptyVendor:
    def test_empty_string_vendor_is_low(self):
        data = _complete_extraction()
        data["vendor"] = ""
        result = compute_field_confidence(data)
        assert result["vendor"] == "low"

    def test_whitespace_only_vendor_is_low(self):
        data = _complete_extraction()
        data["vendor"] = "   "
        result = compute_field_confidence(data)
        assert result["vendor"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  NOT_A_FINANCIAL_DOCUMENT vendor → "low"
# ═══════════════════════════════════════════════════════════════════


class TestNotFinancialDocument:
    def test_not_financial_document_vendor_is_low(self):
        """NOT_A_FINANCIAL_DOCUMENT is the sentinel the extractor emits when the
        file was not a financial document at all. It is not a vendor name, so it
        must score 'low' rather than sail through as a confident reading."""
        data = _complete_extraction()
        data["vendor"] = "NOT_A_FINANCIAL_DOCUMENT"
        result = compute_field_confidence(data)
        assert result["vendor"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Single-char vendor → "medium"
# ═══════════════════════════════════════════════════════════════════


class TestSingleCharVendor:
    def test_single_char_vendor_is_medium(self):
        data = _complete_extraction()
        data["vendor"] = "A"
        result = compute_field_confidence(data)
        assert result["vendor"] == "medium"


# ═══════════════════════════════════════════════════════════════════
#  A date the document printed in words → date "medium"
# ═══════════════════════════════════════════════════════════════════


class TestInvalidDateFormat:
    def test_non_iso_date_is_medium(self):
        """Readable, but not normalized — somebody should look at it."""
        data = _complete_extraction()
        data["date"] = RECEIPT_DATE.strftime("%b %d, %Y")
        result = compute_field_confidence(data)
        assert result["date"] == "medium"

    def test_slash_date_is_medium(self):
        """01/02 is ambiguous between two continents; ISO never is."""
        data = _complete_extraction()
        data["date"] = RECEIPT_DATE.strftime("%m/%d/%Y")
        result = compute_field_confidence(data)
        assert result["date"] == "medium"


# ═══════════════════════════════════════════════════════════════════
#  Null date → "low"
# ═══════════════════════════════════════════════════════════════════


class TestNullDate:
    def test_null_date_is_low(self):
        data = _complete_extraction()
        data["date"] = None
        result = compute_field_confidence(data)
        assert result["date"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Total of zero → total "low"
# ═══════════════════════════════════════════════════════════════════


class TestZeroTotal:
    def test_zero_total_is_low(self):
        data = _complete_extraction()
        data["total"] = "0.00"
        result = compute_field_confidence(data)
        assert result["total"] == "low"

    def test_zero_integer_total_is_low(self):
        data = _complete_extraction()
        data["total"] = "0"
        result = compute_field_confidence(data)
        assert result["total"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Unparseable total "N/A" → "low"
# ═══════════════════════════════════════════════════════════════════


class TestUnparseableTotal:
    def test_na_total_is_low(self):
        data = _complete_extraction()
        data["total"] = "N/A"
        result = compute_field_confidence(data)
        assert result["total"] == "low"

    def test_none_total_is_low(self):
        data = _complete_extraction()
        data["total"] = None
        result = compute_field_confidence(data)
        assert result["total"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Unknown currency "XYZ" → currency "medium"
# ═══════════════════════════════════════════════════════════════════


class TestUnknownCurrency:
    def test_unknown_currency_is_medium(self):
        """A well-formed code the engine holds no opinion about is not a guess,
        but it is not something the matcher can convert either."""
        data = _complete_extraction()
        data["currency_marker"] = "XYZ"
        data["currency"] = "XYZ"
        result = compute_field_confidence(data)
        assert result["currency"] == "medium"


# ═══════════════════════════════════════════════════════════════════
#  Currency confidence is graded by provenance
#
#  Nothing stated and nothing inferable → "low". Nothing stated but a
#  Canadian tax line on the same receipt → "medium": the code is inferred
#  (core/document_currency.py), not read, and it says so.
# ═══════════════════════════════════════════════════════════════════


class TestNullCurrency:
    def test_null_currency_with_no_tax_signal_is_low(self):
        data = _complete_extraction()
        data["currency"] = None
        data["currency_marker"] = ""
        for key in ("gst", "hst", "pst", "other_tax", "subtotal"):
            data[key] = None
        result = compute_field_confidence(data)
        assert result["currency"] == "low"

    def test_null_currency_inferred_from_a_canadian_tax_line_is_medium(self):
        """5% GST + 7% PST on the same subtotal can only be a Canadian charge."""
        data = _complete_extraction()
        data["currency"] = None
        data["currency_marker"] = ""
        # The inference is the fixture's own arithmetic, restated here.
        assert (GST + PST) / SUBTOTAL == Decimal("0.12")
        result = compute_field_confidence(data)
        assert result["currency"] == "medium"

    def test_a_quoted_code_beats_a_model_that_read_the_address(self):
        """The marker is a transcription; `currency` is an opinion.

        A subscription receipt that leads a model astray: it answers "USD" from
        a US mailing address while quoting the bare "$" it was actually shown.
        The quotation wins, the tax line decides, and the grade says "inferred"
        rather than "read".
        """
        data = _complete_extraction()
        data["currency"] = "USD"
        data["currency_marker"] = "$"
        result = compute_field_confidence(data)
        assert result["currency"] == "medium"

    def test_a_bare_dollar_sign_is_not_a_currency(self):
        """A bare "$", no marker quoted, and a 12% tax line: 0.60 on 5.00."""
        data = {
            "vendor": "Bluepeak Software Inc",
            "date": "2026-08-06",
            "currency": "$",
            "subtotal": "5.00",
            "other_tax": "0.60",
            "total": "5.60",
        }
        result = compute_field_confidence(data)
        assert result["currency"] == "medium"


# ═══════════════════════════════════════════════════════════════════
#  Cross-validation bonus upgrades subtotal + total to "high"
# ═══════════════════════════════════════════════════════════════════


class TestCrossValidationBonus:
    def test_cross_validation_upgrades_to_high(self):
        """When subtotal + taxes = total within $0.02, both total and subtotal
        are upgraded to 'high'."""
        data = {
            "vendor": RETAILER,
            "date": RECEIPT_DATE.isoformat(),
            "currency": "CAD",
            "subtotal": f"{SUBTOTAL:.2f}",
            "gst": f"{GST:.2f}",
            "hst": "0",
            "pst": f"{PST:.2f}",
            "other_tax": "0",
            "total": f"{TOTAL:.2f}",
            "payment_method": "Credit card",
            "invoice_number": INVOICE_NUMBER,
        }
        assert SUBTOTAL + GST + PST == TOTAL
        result = compute_field_confidence(data)
        assert result["total"] == "high"
        assert result["subtotal"] == "high"

    def test_cross_validation_within_tolerance(self):
        """A cent out still gets the bonus: receipts round their own tax lines."""
        data = {
            "vendor": RETAILER,
            "date": RECEIPT_DATE.isoformat(),
            "currency": "CAD",
            "subtotal": f"{SUBTOTAL:.2f}",
            "gst": f"{GST:.2f}",
            "hst": "0",
            "pst": f"{PST:.2f}",
            "other_tax": "0",
            "total": f"{TOTAL + Decimal('0.01'):.2f}",  # off by $0.01
            "payment_method": "Credit card",
            "invoice_number": INVOICE_NUMBER,
        }
        result = compute_field_confidence(data)
        assert result["total"] == "high"
        assert result["subtotal"] == "high"


# ═══════════════════════════════════════════════════════════════════
#  Null payment_method and invoice_number → both "low"
# ═══════════════════════════════════════════════════════════════════


class TestNullOptionalFields:
    def test_null_payment_method_is_low(self):
        data = _complete_extraction()
        data["payment_method"] = None
        result = compute_field_confidence(data)
        assert result["payment_method"] == "low"

    def test_null_invoice_number_is_low(self):
        data = _complete_extraction()
        data["invoice_number"] = None
        result = compute_field_confidence(data)
        assert result["invoice_number"] == "low"

    def test_empty_string_payment_method_is_low(self):
        data = _complete_extraction()
        data["payment_method"] = ""
        result = compute_field_confidence(data)
        assert result["payment_method"] == "low"

    def test_single_char_invoice_number_is_low(self):
        """Single char fails the len >= 2 check → 'low'."""
        data = _complete_extraction()
        data["invoice_number"] = "X"
        result = compute_field_confidence(data)
        assert result["invoice_number"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Explicit zero tax "0.00" → "medium", null → "low"
# ═══════════════════════════════════════════════════════════════════


class TestTaxFieldConfidence:
    def test_explicit_zero_gst_is_medium(self):
        """"GST 0.00" printed on the document is information; a missing key is not."""
        data = _complete_extraction()
        data["gst"] = "0.00"
        result = compute_field_confidence(data)
        assert result["gst"] == "medium"

    def test_explicit_zero_string_gst_is_medium(self):
        data = _complete_extraction()
        data["gst"] = "0"
        result = compute_field_confidence(data)
        assert result["gst"] == "medium"

    def test_null_hst_is_low(self):
        data = _complete_extraction()
        data["hst"] = None
        result = compute_field_confidence(data)
        assert result["hst"] == "low"

    def test_positive_gst_is_high(self):
        data = _complete_extraction()
        data["gst"] = f"{GST:.2f}"
        result = compute_field_confidence(data)
        assert result["gst"] == "high"


# ═══════════════════════════════════════════════════════════════════
#  Garbage input — never raises
# ═══════════════════════════════════════════════════════════════════


class TestGarbageInput:
    def test_garbage_vendor_numeric(self):
        """Numeric vendor (12345) has length >= 2 and is not the sentinel,
        so it actually scores 'high'. The function is lenient."""
        data = _complete_extraction()
        data["vendor"] = 12345
        result = compute_field_confidence(data)
        # str(12345).strip() == "12345", len >= 2, not sentinel → "high"
        assert result["vendor"] == "high"

    def test_garbage_date_list(self):
        """A list for date is not a valid ISO string → 'medium' (non-ISO path)."""
        data = _complete_extraction()
        data["date"] = [1, 2, 3]
        result = compute_field_confidence(data)
        # str([1, 2, 3]) is "[1, 2, 3]" which doesn't match YYYY-MM-DD → "medium"
        assert result["date"] == "medium"

    def test_garbage_total_dict(self):
        """A dict for total → _safe_decimal returns 0 → 'low'."""
        data = _complete_extraction()
        data["total"] = {"nested": True}
        result = compute_field_confidence(data)
        assert result["total"] == "low"

    def test_never_raises_on_fully_garbage(self):
        """A completely garbage dict should never raise — all fields handled."""
        data = {
            "vendor": 12345,
            "date": [1, 2, 3],
            "total": {"nested": True},
            "currency": object(),
            "subtotal": False,
            "gst": [],
            "hst": set(),
            "pst": b"bytes",
            "payment_method": None,
            "invoice_number": None,
        }
        # Should not raise
        result = compute_field_confidence(data)
        assert isinstance(result, dict)
        # All expected keys should be present
        for key in ("vendor", "date", "total", "currency", "subtotal", "gst", "hst", "pst",
                    "payment_method", "invoice_number"):
            assert key in result
            assert result[key] in ("high", "medium", "low")

    def test_empty_dict_returns_all_low(self):
        """An empty dict should produce all 'low' confidences."""
        result = compute_field_confidence({})
        assert result["vendor"] == "low"
        assert result["date"] == "low"
        assert result["total"] == "low"
        assert result["currency"] == "low"
        assert result["payment_method"] == "low"
        assert result["invoice_number"] == "low"


# ═══════════════════════════════════════════════════════════════════
#  Total with currency symbol — _safe_decimal strips it
# ═══════════════════════════════════════════════════════════════════


class TestCurrencySymbolInTotal:
    def test_total_with_currency_symbol_and_code(self):
        """'$81.45 CAD' should parse to 81.45 via _safe_decimal, giving 'high'."""
        data = {
            "vendor": "Orchid Telecom",
            "date": "2026-01-15",
            "total": "$81.45 CAD",
            "currency": "CAD",
        }
        result = compute_field_confidence(data)
        assert result["total"] == "high"


# ═══════════════════════════════════════════════════════════════════
#  Boundary: 2-char vendor → "high"
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryTwoCharVendor:
    def test_two_char_vendor_is_high(self):
        """A 2-character vendor satisfies len >= 2 and should be 'high'."""
        data = _complete_extraction()
        data["vendor"] = "Co"
        result = compute_field_confidence(data)
        assert result["vendor"] == "high"


# ═══════════════════════════════════════════════════════════════════
#  Boundary: Cross-validation FAILURE — total NOT upgraded
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryCrossValidationFailure:
    def test_cross_validation_fails_when_mismatch_exceeds_tolerance(self):
        """When subtotal + taxes != total by > $0.02, the cross-validation
        bonus is NOT applied.

        A total that is simply positive is already "high" from the base check,
        so the bonus is invisible there. The way to see it is a field that can
        only reach "high" through the bonus: an explicit ``0.00`` subtotal
        scores "medium" on its own, and must stay "medium" when the arithmetic
        does not add up.
        """
        data = {
            "vendor": RETAILER,
            "date": RECEIPT_DATE.isoformat(),
            "currency": "CAD",
            "subtotal": f"{SUBTOTAL:.2f}",
            "gst": f"{GST:.2f}",
            "hst": "0",
            "pst": f"{PST:.2f}",
            "other_tax": "0",
            "total": f"{TOTAL + Decimal('5.00'):.2f}",  # $5.00 out, not $0.02
            "payment_method": "Credit card",
            "invoice_number": INVOICE_NUMBER,
        }
        result = compute_field_confidence(data)
        assert result["total"] == "high"  # base check: a positive total

        data2 = dict(data)
        data2["subtotal"] = "0.00"  # base check → "medium" for an explicit zero
        data2["total"] = "5.00"     # 0.00 + GST + PST != 5.00
        result2 = compute_field_confidence(data2)
        assert result2["subtotal"] == "medium"
        assert result2["subtotal"] != "high"

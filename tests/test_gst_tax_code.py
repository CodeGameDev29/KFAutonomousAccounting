"""GST/HST tax codes are asserted only on evidence.

Every description, amount, category assignment and supplier in this module is
invented — see ``tests/fixtures/README.md``. The bank-memo shapes are format
facts the parser depends on (a trailing province code, a glued CITYSTATE token,
an ISO-3 country code, a ``,US,`` region token in a wire memo); the names and
figures inside those shapes are invented.

Deriving a tax code from the category alone tags every ``Uncategorized`` row
and every foreign-supplier charge ``GST on Expenses``, and every non-resident
client wire ``GST on Income``. Claiming an input tax credit on a supply that
carried no GST overstates a refund claim.

These tests hold the rule: a GST code appears **only** when the linked source
document actually shows GST or HST. Everything else asserts no tax
(``Zero-rated / No GST``, ``BAS Excluded``) or asserts nothing (``""``).
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.gst_tax_code import (
    CA,
    FOREIGN,
    TAX_BAS_EXCLUDED,
    TAX_GST_EXPENSES,
    TAX_GST_INCOME,
    TAX_NO_GST_FOREIGN,
    TAX_NONE,
    UNKNOWN,
    supplier_jurisdiction,
    xero_tax_code,
)
from core.ledger import XERO_HEADERS, export_xero_csv
from models.transaction import Transaction


def _doc(gst="0", hst="0"):
    return {"has_document": True, "gst": Decimal(gst), "hst": Decimal(hst)}


NO_DOC = None

# ── The invented counterparties ────────────────────────────────────────────
# Canadian: the memo ends in a province code.
CA_SOFTWARE = "BLUEPEAK SOFTWARE INC ANYTOWN MB"
CA_SUPPLIES = "CEDARVIEW SUPPLIES LTD ANYTOWN MB"
CA_MARKETPLACE = "EXMPL MKTP CA ANYTOWN MB"
# Foreign: an ISO-3 country code, a glued CITYSTATE token, a bare US state.
FOREIGN_HOST = "USD 45.00@1.4010 BLUEPEAK SOFTWARE GMBH EXAMPLESTADT DEU"
FOREIGN_COURIER = "USD 62.50@1.3980 MERIDIAN COURIER SAMPLETOWNCA"
FOREIGN_TRADER = "USD 10.00@1.4020 NORTHWIND TRADING CO SAMPLE CITY CA"
FOREIGN_RETAILER = "USD 17.25@1.3950 EXAMPLE RETAILER SAMPLECITYWA"
FOREIGN_WIRE = "IncomingWirePayment,INCOMINGWIRE PAYMENT,US,NORTHWINDTRADINGCO,"


# ── The rules that decide a tax code ────────────────────────────────────────

class TestTaxCodeRules:
    def test_canadian_supplier_with_receipt_gst_gets_gst_code(self):
        code, basis = xero_tax_code(
            "Software and IT",
            doc_tax=_doc(gst="6.00"),
            description=CA_SOFTWARE,
        )
        assert code == TAX_GST_EXPENSES
        assert "linked document" in basis

    def test_hst_on_the_document_also_claims(self):
        """13% HST on an $80.00 supply — an HST line claims like a GST one."""
        code, _ = xero_tax_code(
            "Office supplies", doc_tax=_doc(hst="10.40"),
            description=CA_SUPPLIES,
        )
        assert code == TAX_GST_EXPENSES

    def test_canadian_supplier_no_tax_on_receipt_gets_no_code(self):
        code, basis = xero_tax_code(
            "Office supplies",
            doc_tax=_doc(gst="0", hst="0"),
            description=CA_SUPPLIES,
        )
        assert code == TAX_NONE
        assert "no GST/HST" in basis

    def test_foreign_supplier_gets_no_gst(self):
        for description in (
            FOREIGN_HOST, FOREIGN_COURIER, FOREIGN_TRADER, FOREIGN_RETAILER,
        ):
            code, basis = xero_tax_code(
                "Software and IT", doc_tax=NO_DOC, description=description,
            )
            assert code == TAX_NO_GST_FOREIGN, description
            assert "Non-resident supplier" in basis

    def test_uncategorized_gets_no_code(self):
        """Even with GST on the document: the engine could not classify the row,
        so it has no business asserting the row's tax treatment either."""
        for category in (None, "", "Uncategorized"):
            code, basis = xero_tax_code(
                category, doc_tax=_doc(gst="5.00"),
                description=CA_MARKETPLACE,
            )
            assert code == TAX_NONE, category
            assert "review" in basis

    def test_uncategorized_foreign_row_still_names_the_jurisdiction(self):
        """An uncategorized German supplier: no tax code, but the basis says why
        no Canadian GST is expected, so the reviewer is not starting from zero."""
        code, basis = xero_tax_code(
            "Uncategorized", doc_tax=NO_DOC, description=FOREIGN_HOST,
        )
        assert code == TAX_NONE
        assert "non-resident counterparty" in basis

    def test_non_resident_income_is_not_gst_on_income(self):
        code, basis = xero_tax_code(
            "Sales of goods and services",
            doc_tax=NO_DOC,
            description=FOREIGN_WIRE,
        )
        assert code != TAX_GST_INCOME
        assert code == TAX_NO_GST_FOREIGN
        assert "Non-resident payer" in basis

    def test_income_with_collected_tax_on_the_invoice_is_gst_on_income(self):
        code, _ = xero_tax_code(
            "Sales of goods and services",
            doc_tax=_doc(gst="480.00"),
            description="DirectDeposit HARBOURLINE LOGISTICS ANYTOWN MB",
        )
        assert code == TAX_GST_INCOME

    def test_canadian_income_without_a_document_asserts_nothing(self):
        code, basis = xero_tax_code(
            "Sales of goods and services", doc_tax=NO_DOC,
            description="Direct Deposit, PAYPAL MSP/DIV",
        )
        assert code == TAX_NONE
        assert "No sales document" in basis

    def test_expense_without_a_document_asserts_nothing(self):
        code, basis = xero_tax_code(
            "Labour and commissions", doc_tax=NO_DOC, description="Cheque, NO.0000",
        )
        assert code == TAX_NONE
        assert "No document linked" in basis

    @pytest.mark.parametrize(
        "category",
        ["Internal transfer", "Credit card payment",
         "Income tax and GST/HST remittance", "Owner's draw"],
    )
    def test_non_supplies_are_bas_excluded(self, category):
        code, _ = xero_tax_code(category, doc_tax=NO_DOC, description="x")
        assert code == TAX_BAS_EXCLUDED

    def test_refund_defers_to_review(self):
        code, basis = xero_tax_code("Refund/Credit", doc_tax=NO_DOC, description="REFUND")
        assert code == TAX_NONE
        assert "original supply" in basis

    def test_an_off_taxonomy_name_asserts_no_tax_code(self):
        """A category outside the taxonomy is flagged, never given a tax code.

        It must not fall through to a GST claim even when the document shows
        GST: the engine could not classify the row, so it has no business
        asserting the row's tax treatment either.
        """
        code, basis = xero_tax_code("Office Widgets", doc_tax=_doc(gst="1.00"),
                                    description=CA_SUPPLIES)
        assert code == TAX_NONE
        assert "flagged for review" in basis
        code, _ = xero_tax_code("Office Widgets", doc_tax=NO_DOC,
                                description=CA_SUPPLIES)
        assert code == TAX_NONE

    def test_document_tax_wins_over_a_foreign_reading(self):
        """A registered non-resident that did charge GST keeps its ITC."""
        code, _ = xero_tax_code(
            "Software and IT", doc_tax=_doc(gst="2.50"),
            description=FOREIGN_RETAILER,
        )
        assert code == TAX_GST_EXPENSES


# ── Jurisdiction detection ─────────────────────────────────────────────────

class TestSupplierJurisdiction:
    @pytest.mark.parametrize(
        "description",
        [
            FOREIGN_HOST,
            FOREIGN_COURIER,
            FOREIGN_TRADER,
            FOREIGN_RETAILER,
            "EXAMPLE RETAILER SUBSCRIPTION SAMPLETOWNCA",
            FOREIGN_WIRE,
        ],
    )
    def test_foreign(self, description):
        assert supplier_jurisdiction(description) == FOREIGN

    @pytest.mark.parametrize(
        "description",
        [
            CA_MARKETPLACE,
            "ORCHID TELECOM ANYTOWN NS",
            CA_SUPPLIES,
            "ORCHID TELECOM *AD000000 000-000-0000 ON",
        ],
    )
    def test_canadian(self, description):
        """A trailing province code is Canada — and a bare 'CA' token in a
        Canadian bank memo means Canada, never California."""
        assert supplier_jurisdiction(description) == CA

    @pytest.mark.parametrize(
        "description",
        [
            "Cheque, NO.0000",
            "Transaction Fee, CHEQUE 02 AT $1.50",
            "InterestEarned",
            "Online Transfer, TF000000-1234",
            "",
        ],
    )
    def test_unknown(self, description):
        assert supplier_jurisdiction(description) == UNKNOWN

    def test_three_letter_bank_abbreviations_are_not_states(self):
        """'BUS/ENT' must not read as Northwest Territories, 'MSP/DIV' not as a state."""
        assert supplier_jurisdiction("Pre-Authorized Payment, ORCHID/TEL BUS/ENT") == UNKNOWN
        assert supplier_jurisdiction("Pre-Authorized Payment, WISE MSP/DIV") == UNKNOWN


# ── The rule reaches the exported file ─────────────────────────────────────

def _txn(txn_id, description, *, amount="10.00", currency="CAD", ttype="DEBIT"):
    return Transaction(
        id=txn_id,
        account="CreditCard",
        transaction_type=ttype,
        date_posted=date(2026, 5, 4),
        amount=Decimal(amount),
        currency=currency,
        description=description,
        source_file="card_may2026.csv",
        source_row=txn_id,
    )


class TestXeroCsvTaxColumn:
    def test_uncategorized_and_foreign_rows_carry_no_gst(self, tmp_path: Path):
        txns = [
            _txn(1, FOREIGN_COURIER),
            _txn(2, FOREIGN_HOST),
            _txn(3, CA_SUPPLIES),
            _txn(4, CA_MARKETPLACE),
        ]
        categories = {
            1: "Uncategorized",
            2: "Uncategorized",
            3: "Office supplies",
            4: "Office supplies",
        }
        doc_tax = {3: _doc(gst="1.30")}
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out, categories=categories, doc_tax=doc_tax)

        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        assert list(rows[0]) == XERO_HEADERS
        by_id = {r["Description"]: r for r in rows}
        assert by_id[FOREIGN_COURIER]["Tax Type"] == ""
        assert by_id[FOREIGN_HOST]["Tax Type"] == ""
        assert by_id[CA_SUPPLIES]["Tax Type"] == TAX_GST_EXPENSES
        # Canadian supplier, categorized, but no receipt: still nothing asserted.
        assert by_id[CA_MARKETPLACE]["Tax Type"] == ""
        # Every row explains itself.
        assert all(r["Tax Basis"] for r in rows)

    def test_no_row_claims_gst_without_doc_tax(self, tmp_path: Path):
        """With no document evidence anywhere, the exported file must not
        contain a single GST claim."""
        txns = [
            _txn(1, FOREIGN_COURIER),
            _txn(2, "Pre-Authorized Payment, ORCHID TELECOM BPY/FAC"),
            _txn(3, "Cheque, NO.0000"),
        ]
        categories = {
            1: "Uncategorized",
            2: "Utilities and telephone/telecommunication",
            3: "Labour and commissions",
        }
        out = tmp_path / "xero.csv"
        export_xero_csv(txns, out, categories=categories)
        text = out.read_text(encoding="utf-8-sig")
        assert TAX_GST_EXPENSES not in text
        assert TAX_GST_INCOME not in text

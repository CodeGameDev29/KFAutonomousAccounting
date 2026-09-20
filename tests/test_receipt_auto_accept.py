"""An *inferred* currency is accept-grade; an *assumed* one is not.

Currency provenance (`core/document_currency.py`) grades the currency field by
what the document supports: `stated` (the document printed a marker) is "high",
`inferred` (read off a Canadian tax line — a GST/HST/PST figure, or a
5/12/13/14/15% tax ratio) is "medium", `assumed` (nothing written down anywhere,
currency stored NULL) is "low".

The review gate in `server/api/receipts.py` only auto-accepts a receipt whose
every field is "high". Read literally that would send every Canadian receipt
printing a bare "$" to the review queue, which is not what its medium means.
Medium-because-inferred is *evidence*: the tax line settles the currency, the
grade only records that it was inferred rather than read, so the review panel
can say why.

So `review_status_for` treats an inferred currency as accept-grade while
`compute_field_confidence` keeps saying "medium". An assumed currency still
blocks auto-accept, and a low or medium on any other field still blocks it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.api.receipts import process_receipt_document, review_status_for

USER_ID = "user-autoaccept"

# The only shape `compute_field_confidence` currently grades "high" on every
# field: gst, hst AND pst all positive. A receipt hardly ever prints all three
# (a missing tax line grades "low", an explicit zero "medium"), so this is a
# fixture for the gate rather than a document — these tests are about which
# currency provenance is accept-grade, not about how tax lines are graded.
# `_model` is left off deliberately: a model outside the trusted list downgrades
# an auto-accept on its own, and that rule is not what is under test here.
ALL_HIGH = {
    "vendor": "Vendor Inc.",
    "date": "2026-03-04",
    "subtotal": "100.00",
    "gst": "5.00",
    "hst": "1.00",
    "pst": "7.00",
    "total": "113.00",
    "payment_method": "Visa",
    "invoice_number": "INV-77",
    "_provider": "test",
}


def _inferred() -> dict:
    """A subscription receipt, with the model's guess still in it.

    A bare "$" beside the total and "USD" read off a US mailing address: the
    quotation wins, the Canadian tax figures decide, and the currency is CAD by
    inference.
    """
    return dict(ALL_HIGH, currency_marker="$", currency="USD")


def _stated() -> dict:
    """The document printed the code itself."""
    return dict(ALL_HIGH, currency_marker="CAD", currency="CAD")


def _assumed() -> dict:
    """Nothing printed, and nothing Canadian to infer from.

    The tax block has to change with the currency evidence: a named GST/HST/PST
    figure *is* the Canadian signal, so an assumed currency and the all-high tax
    fixture cannot coexist. `review_status_for` is exercised on that isolated
    question in `TestTheGateItself` below.
    """
    return dict(
        ALL_HIGH,
        currency_marker="$",
        currency=None,
        subtotal="5.00",
        gst="0",
        hst="0",
        pst="0",
        other_tax="0.35",   # 8.77% — no Canadian jurisdiction charges that
        total="4.34",
    )


# ── The gate itself, on a confidence blob the test controls ──────────────────────


class TestTheGateItself:
    """`review_status_for` with the currency isolated from every other field."""

    @staticmethod
    def _blob(currency: str) -> dict:
        return {
            "vendor": "high", "date": "high", "total": "high", "subtotal": "high",
            "gst": "high", "hst": "high", "pst": "high",
            "payment_method": "high", "invoice_number": "high",
            "currency": currency,
        }

    def test_an_inferred_currency_is_accept_grade(self):
        assert review_status_for(self._blob("medium"), _inferred()) == "auto_accepted"

    def test_an_assumed_currency_blocks_auto_accept(self):
        """Graded low by the blob — the same low any other field would flag on."""
        assert review_status_for(self._blob("low"), _assumed()) == "flagged"

    def test_a_medium_currency_is_not_upgraded_when_nothing_was_written_down(self):
        """The gate reads the provenance, not the grade: no evidence, no accept."""
        assert review_status_for(self._blob("medium"), _assumed()) == "pending"

    def test_a_stated_currency_auto_accepts(self):
        assert review_status_for(self._blob("high"), _stated()) == "auto_accepted"

    def test_a_medium_on_another_field_still_holds_the_receipt(self):
        blob = self._blob("medium")
        blob["date"] = "medium"
        assert review_status_for(blob, _inferred()) == "pending"

    def test_a_low_on_another_field_still_flags_the_receipt(self):
        blob = self._blob("medium")
        blob["vendor"] = "low"
        assert review_status_for(blob, _inferred()) == "flagged"

    def test_an_unknown_but_stated_code_is_still_only_medium(self):
        """"XYZ" grades medium because the engine holds no opinion about it — and
        that medium is doubt, not evidence. Only `inferred` is upgraded."""
        payload = dict(ALL_HIGH, currency_marker="XYZ", currency="XYZ")
        assert review_status_for(self._blob("medium"), payload) == "pending"


# ── The same three cases through the real extraction path ─────────────────


class _Cursor:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append((" ".join(str(sql).split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def cursor(self):
        return _Cursor(self._sink)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDb:
    """Only the surface `process_receipt_document` touches."""

    def __init__(self) -> None:
        self.user_id = USER_ID
        self.executed: list[tuple] = []

    def insert_processed_file(self, file_hash, original_filename, stored_path=None):
        return 1

    def insert_document(self, doc):
        self.document = doc
        return 909

    def insert_audit_log(self, **kwargs):
        return 1

    def _conn(self):
        return _Conn(self.executed)

    def stored_review(self) -> tuple[dict, str]:
        """(field_confidence, review_status) as written to the documents row."""
        sql, params = next(
            (s, p) for s, p in self.executed if s.startswith("UPDATE documents")
        )
        assert "field_confidence = %s, review_status = %s" in sql
        return json.loads(params[0]), params[1]


async def _process(payload: dict, tmp_path: Path) -> tuple[dict, FakeDb]:
    import core.extraction

    async def _extract(path, db=None, **kwargs):
        return dict(payload)

    original = core.extraction.extract_document
    core.extraction.extract_document = _extract
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4 auto-accept-fixture")
    db = FakeDb()
    try:
        result = await process_receipt_document(
            db=db,
            user_id=USER_ID,
            filename="receipt.pdf",
            file_hash="feedface",
            file_path=receipt,
            stored_path=f"{USER_ID}/202603/feedface_receipt.pdf",
        )
    finally:
        core.extraction.extract_document = original
    return result, db


@pytest.mark.asyncio
async def test_an_inferred_currency_auto_accepts_and_still_reads_medium(tmp_path):
    """(i) A Canadian receipt that prints a bare "$"."""
    result, db = await _process(_inferred(), tmp_path)

    assert result["review_status"] == "auto_accepted"
    assert result["field_confidence"]["currency"] == "medium", (
        "the blob keeps saying medium so the review panel can show why it is CAD"
    )
    assert db.document.currency == "CAD"
    assert db.document.currency_source == "inferred"

    stored_confidence, stored_status = db.stored_review()
    assert stored_status == "auto_accepted"
    assert stored_confidence["currency"] == "medium"


@pytest.mark.asyncio
async def test_an_assumed_currency_stays_in_the_review_queue(tmp_path):
    """(ii) Nothing said the currency and nothing could infer it."""
    result, db = await _process(_assumed(), tmp_path)

    assert result["review_status"] != "auto_accepted"
    assert result["review_status"] == "flagged", "a low field flags; it never accepts"
    assert result["field_confidence"]["currency"] == "low"
    assert db.document.currency is None
    assert db.document.currency_source == "assumed"
    assert db.stored_review()[1] == "flagged"


@pytest.mark.asyncio
async def test_a_stated_currency_auto_accepts_through_the_extraction_path(tmp_path):
    """(iii) The document printed the code itself."""
    result, db = await _process(_stated(), tmp_path)

    assert result["review_status"] == "auto_accepted"
    assert result["field_confidence"]["currency"] == "high"
    assert db.document.currency == "CAD"
    assert db.document.currency_source == "stated"
    assert db.stored_review()[1] == "auto_accepted"

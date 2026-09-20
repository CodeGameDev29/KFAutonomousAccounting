"""The statement detector: every statement in, no receipt or invoice out.

SYNTHETIC DATA. Every document and every line of text below was generated for
this repository — see ``tests/fixtures/README.md`` and ``scripts/gen_fixtures.py``.

``core/statement_detector.py`` is what stops the Transactions "auto" upload zone
from extracting a month of bank statement as a single receipt. It decides inside
the upload request, from the text layer of the first few pages, with no LLM and
no network — so it is exactly the kind of rule that can drift quietly. These
tests are the tripwire.

The asymmetry in the assertions is deliberate and is the whole design:

* **every** bank statement must be detected. A miss means the statement gets
  read as a receipt and a month of transactions never arrives.
* **no** receipt and **no** invoice may be. A false positive is worse: it takes
  a document the user can see in front of them and feeds it to a bank-statement
  parser.
* the signals that fired are asserted by name on a statement and on an invoice,
  so loosening the rule until invoices start matching fails here loudly rather
  than showing up as a support question.

Two fixture sets. The three generated PDFs in ``tests/fixtures`` ship with this
repository and run on every machine. The volume tests run over a corpus you
supply yourself — a directory of statements, receipts and invoices of your own,
which is the only way to check precision at volume on documents this repository
cannot carry. Point ``AA_BULK_FIXTURES`` at that directory to run them; they
skip with a reason when it is unset. Nothing here touches the network, an LLM,
or the database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.statement_detector import (
    MAX_PAGES_SCANNED,
    MIN_LEDGER_ROWS,
    REQUIRED_SIGNALS,
    SIGNAL_CLOSING_BALANCE,
    SIGNAL_DATE_RANGE,
    SIGNAL_LEDGER_ROWS,
    SIGNAL_OPENING_BALANCE,
    SIGNAL_STATEMENT_PERIOD,
    detect_bank_statement,
    detect_statement_text,
    looks_like_bank_statement,
)
from scripts.gen_fixtures import (
    STATEMENT_FX_RATE,
    STATEMENT_OPENING,
    STATEMENT_ROWS,
    USD_ACCOUNT,
    statement_running_balances,
)

LOCAL_FIXTURES = Path(__file__).parent / "fixtures"

#: An optional corpus of your own documents; this repository ships none. Point
#: ``AA_BULK_FIXTURES`` at a directory holding ``*-statement.pdf``,
#: ``receipt-*.pdf`` and ``invoice-*.pdf`` to run the volume tests over it.
#: There is no default: unset means the volume tests skip.
_BULK_DIR = os.environ.get("AA_BULK_FIXTURES", "").strip()
BULK_FIXTURES = Path(_BULK_DIR) if _BULK_DIR else None

BULK_SKIP_REASON = (
    "set AA_BULK_FIXTURES to a directory of your own statement/receipt/invoice "
    "PDFs to run the volume tests"
)

requires_bulk_fixtures = pytest.mark.skipif(
    BULK_FIXTURES is None or not BULK_FIXTURES.is_dir(), reason=BULK_SKIP_REASON
)


def _bulk(pattern: str) -> list[Path]:
    if BULK_FIXTURES is None or not BULK_FIXTURES.is_dir():
        return []
    return sorted(BULK_FIXTURES.glob(pattern))


def _bulk_params(pattern: str) -> list:
    """Parameters for a per-document test, or one skip that names the dependency.

    An empty ``parametrize`` would skip with pytest's own "got empty parameter
    set" wording, which says nothing about *why* the set is empty. This keeps
    the reason attached to the corpus.
    """
    found = _bulk(pattern)
    if found:
        return found
    return [
        pytest.param(
            None,
            marks=pytest.mark.skip(reason=BULK_SKIP_REASON),
            id="bulk-corpus-absent",
        )
    ]


def _local(name: str) -> Path:
    return LOCAL_FIXTURES / name


# A statement detection needs MIN_TEXT_CHARS of text before it will say anything
# at all, so the text-rule tests pad with this rather than asserting on a
# three-word document the detector is entitled to ignore.
_FILLER = "filler text to clear the minimum length. " * 5


# ---------------------------------------------------------------------------
# The statements — the generated one here, the corpus where it is available
# ---------------------------------------------------------------------------

class TestStatementsAreDetected:
    @requires_bulk_fixtures
    def test_the_fixture_set_is_actually_there(self):
        """Guard against a green run over an empty glob."""
        assert _bulk("*-statement.pdf"), "no statements in the corpus"
        assert _bulk("receipt-*.pdf"), "no receipts in the corpus"
        assert _bulk("invoice-*.pdf"), "no invoices in the corpus"

    @pytest.mark.parametrize(
        "pdf", _bulk_params("*-statement.pdf"), ids=lambda p: getattr(p, "name", p)
    )
    def test_every_corpus_statement_is_detected(self, pdf: Path):
        detection = detect_bank_statement(pdf)
        assert detection.is_statement is True, f"{pdf.name}: {detection.summary}"
        assert len(detection.signals) >= REQUIRED_SIGNALS

    @requires_bulk_fixtures
    def test_no_statement_is_missed(self):
        """Reported as one list, so a regression names every file it broke."""
        missed = [
            p.name for p in _bulk("*-statement.pdf")
            if not looks_like_bank_statement(p)
        ]
        assert missed == []

    def test_the_two_page_business_statement_is_detected(self):
        """The generated statement, whose text layer is the awkward kind.

        pdfplumber returns it with the columns run together and the spaces gone
        — "Openingbalance", "Closingtotals", "For the period ending February 27,
        2026" — and it carries only four transactions, so the row-count signal
        never fires. It is the reason the phrase signals match ``\\s*`` between
        words and accept "period ending" rather than only "statement period".
        """
        detection = detect_bank_statement(_local("test_bank_statement.pdf"))

        assert detection.is_statement is True, detection.summary
        assert set(detection.signals) == {
            SIGNAL_STATEMENT_PERIOD,
            SIGNAL_OPENING_BALANCE,
            SIGNAL_CLOSING_BALANCE,
        }
        # Four transactions: proof the verdict does not lean on the row count.
        assert detection.row_count < MIN_LEDGER_ROWS
        assert SIGNAL_LEDGER_ROWS not in detection.signals
        assert detection.pages_scanned == 2


# ---------------------------------------------------------------------------
# The things that must never be mistaken for one
# ---------------------------------------------------------------------------

class TestReceiptsAndInvoicesAreNotStatements:
    @pytest.mark.parametrize(
        "pdf", _bulk_params("receipt-*.pdf"), ids=lambda p: getattr(p, "name", p)
    )
    def test_no_corpus_receipt_is_detected_as_a_statement(self, pdf: Path):
        detection = detect_bank_statement(pdf)
        assert detection.is_statement is False, f"{pdf.name}: {detection.summary}"

    @pytest.mark.parametrize(
        "pdf", _bulk_params("invoice-*.pdf"), ids=lambda p: getattr(p, "name", p)
    )
    def test_no_corpus_invoice_is_detected_as_a_statement(self, pdf: Path):
        detection = detect_bank_statement(pdf)
        assert detection.is_statement is False, f"{pdf.name}: {detection.summary}"

    @requires_bulk_fixtures
    def test_no_false_positives_at_all(self):
        """One list, so a loosened rule names every document it would misroute."""
        false_positives = [
            f"{p.name} ({detect_bank_statement(p).summary})"
            for p in _bulk("receipt-*.pdf") + _bulk("invoice-*.pdf")
            if looks_like_bank_statement(p)
        ]
        assert false_positives == []

    @requires_bulk_fixtures
    def test_a_multipage_invoice_does_not_reach_the_row_threshold(self):
        """A long invoice has dozens of line items and still zero ledger rows.

        Its line items start with a description, not a date — which is exactly
        what separates a ledger from a priced list, and why the row signal is
        allowed to be as cheap as it is.
        """
        multipage = sorted(_bulk("invoice-*.pdf"))
        assert multipage, "no invoices in the corpus"
        for pdf in multipage:
            detection = detect_bank_statement(pdf)
            assert detection.row_count == 0, pdf.name
            assert detection.is_statement is False, pdf.name

    def test_the_contractor_invoice_is_not_a_statement(self):
        detection = detect_bank_statement(_local("test_invoice_contractor.pdf"))
        assert detection.is_statement is False, detection.summary
        assert detection.signals == ()

    def test_the_retail_receipt_is_not_a_statement(self):
        detection = detect_bank_statement(_local("test_receipt_retailer.pdf"))
        assert detection.is_statement is False, detection.summary
        assert detection.signals == ()


# ---------------------------------------------------------------------------
# The evidence, pinned by name
# ---------------------------------------------------------------------------

class TestTheEvidenceIsWhatItClaims:
    @requires_bulk_fixtures
    def test_a_dense_statement_fires_all_five_signals(self):
        """A dense month: period phrase, both balances, a range and ledger rows.

        The generated fixture cannot stand in for this one — it has four
        transactions, so the ledger-row signal can never fire on it, and the
        run-together columns of its balance lines are not a date range. If any
        of the five stops firing on a full statement the rule changed, and this
        is where that gets noticed.
        """
        statements = _bulk("*-statement.pdf")
        assert statements, "no statements in the corpus"
        all_five = {
            SIGNAL_STATEMENT_PERIOD,
            SIGNAL_OPENING_BALANCE,
            SIGNAL_CLOSING_BALANCE,
            SIGNAL_DATE_RANGE,
            SIGNAL_LEDGER_ROWS,
        }
        fired = [set(detect_bank_statement(p).signals) for p in statements]
        assert any(s == all_five for s in fired), (
            "no statement in the corpus fires all five signals any more"
        )

    @requires_bulk_fixtures
    def test_a_corpus_invoice_fires_none_of_them(self):
        """Zero signals, not one, on every invoice in the corpus."""
        for pdf in _bulk("invoice-*.pdf"):
            detection = detect_bank_statement(pdf)
            assert detection.signals == (), f"{pdf.name}: {detection.summary}"
            assert detection.reason == "0 of 5 signals (need 2)", pdf.name

    def test_the_evidence_serializes_for_a_log_line_and_a_job_row(self):
        """``as_dict`` is what the API logs and seeds onto a job's ``result``."""
        payload = detect_bank_statement(_local("test_bank_statement.pdf")).as_dict()

        assert payload["is_statement"] is True
        assert isinstance(payload["signals"], list)
        assert all(isinstance(s, str) for s in payload["signals"])
        assert isinstance(payload["row_count"], int)
        assert isinstance(payload["text_chars"], int)
        assert isinstance(payload["pages_scanned"], int)
        assert isinstance(payload["reason"], str)
        assert payload["reason"] == f"{len(payload['signals'])} of 5 signals (need 2)"

    def test_a_long_statement_is_not_read_to_the_end(self, tmp_path: Path):
        """Cheapness is part of the contract — this runs inside the request."""
        long_statement = _write_padded_statement(
            tmp_path / "long_statement.pdf", pages=MAX_PAGES_SCANNED + 2
        )
        detection = detect_bank_statement(long_statement)

        assert detection.pages_scanned == MAX_PAGES_SCANNED
        assert detection.is_statement is True, detection.summary


def _write_padded_statement(path: Path, *, pages: int) -> Path:
    """A statement longer than the detector will read. Built here, not checked in.

    Only the page count matters, so this is the cheapest document that carries
    two signals on page one and keeps going.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as _canvas

    closing = statement_running_balances()[-1]
    c = _canvas.Canvas(str(path), pagesize=LETTER)
    for page in range(1, pages + 1):
        c.setFont("Courier", 9)
        y = 740.0
        lines = [
            "BMO Business Banking",
            "Business Chequing Statement",
            f"For the period ending February 27, 2026    Page {page} of {pages}",
            f"Jan31  Openingbalance                     {STATEMENT_OPENING:,.2f}",
            f"Feb27  Closingtotals                      {closing:,.2f}",
            "This document is a synthetic test fixture. It is not a bank record.",
        ]
        for line in lines:
            c.drawString(54.0, y, line)
            y -= 12.0
        c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------------------
# Unreadable input
# ---------------------------------------------------------------------------

class TestUnreadableInputNeverRaises:
    def test_a_corrupt_pdf_answers_false_instead_of_raising(self, tmp_path: Path):
        """A ``%PDF`` header and no usable object graph behind it."""
        shredded = tmp_path / "shredded.pdf"
        shredded.write_bytes(
            b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"trailer\n<< /Size 2 >>\nstartxref\n0\n%%EOF\n"
        )
        detection = detect_bank_statement(shredded)

        assert detection.is_statement is False
        assert detection.signals == ()
        assert detection.text_chars == 0
        assert detection.reason == "no usable text layer"

    def test_a_missing_file_answers_false(self):
        assert looks_like_bank_statement(Path("no-such-file-anywhere.pdf")) is False

    def test_a_text_file_named_pdf_answers_false(self, tmp_path: Path):
        masquerading = tmp_path / "statement.pdf"
        masquerading.write_text(
            "Statement period: 2026-01-01 to 2026-01-31\nOpening balance: 1.00\n",
            encoding="utf-8",
        )
        # The words are all there; there is no PDF to read them out of.
        assert looks_like_bank_statement(masquerading) is False

    def test_an_image_only_pdf_has_no_text_layer_to_judge(self):
        """A scanned statement is a miss, on purpose: this rule is text-only."""
        detection = detect_bank_statement(_local("test_receipt_scan.pdf"))
        assert detection.is_statement is False
        assert detection.text_chars == 0
        assert detection.reason == "no usable text layer"

    def test_empty_text_is_the_same_answer_without_the_file(self):
        detection = detect_statement_text("")
        assert detection.is_statement is False
        assert detection.reason == "no usable text layer"


# ---------------------------------------------------------------------------
# The rule itself, over text — the cases no fixture covers
# ---------------------------------------------------------------------------

CREDIT_CARD = """BMO Mastercard
Statement date Feb. 24, 2026
Statement period Jan. 25, 2026 - Feb. 24, 2026
Previous balance 1,234.56
Jan. 27 Jan. 28 EXMPL MKTP CA ANYTOWN MB 41.25
Feb. 02 Feb. 03 TRSF FROM/DE ACCT/CPT 0000-XXXX-000 300.00 CR
"""

CONSULTING_INVOICE = """INVOICE
Jordan Avery Consulting
Invoice #: INV-2026-0042
Invoice date: February 1, 2026   Payment due: March 3, 2026
Bill To: Example Corp Inc.
Description Hours Amount
Software development for the period January 1, 2026 to January 31, 2026 48 $3,600.00
Subtotal: $3,600.00
GST (5%): $180.00
Total Due (CAD): $3,780.00
Payment Terms: Net 30. Remaining balance due on receipt.
"""

UTILITY_BILL = """Orchid Telecom
Account 0000 1234
Service period: 2026-01-01 to 2026-01-31
Previous reading 4,210.00  Current reading 4,394.00
Amount due: $81.45
Due date: 2026-02-15
"""


class TestTheRuleOverText:
    def test_a_credit_card_statement_is_a_statement(self):
        """Two signals and no ledger rows at all — the 2-of-5 floor earning its keep."""
        detection = detect_statement_text(CREDIT_CARD)

        assert detection.is_statement is True, detection.summary
        assert SIGNAL_STATEMENT_PERIOD in detection.signals
        assert SIGNAL_DATE_RANGE in detection.signals
        # A card statement prints one amount per row, never a running balance.
        assert detection.row_count == 0

    def test_an_invoice_that_mentions_a_billing_period_is_not_a_statement(self):
        """The nearest miss there is: a date range, and nothing to corroborate it."""
        detection = detect_statement_text(CONSULTING_INVOICE)

        assert detection.is_statement is False, detection.summary
        assert detection.signals == (SIGNAL_DATE_RANGE,)

    def test_a_utility_bill_with_a_service_period_is_not_a_statement(self):
        detection = detect_statement_text(UTILITY_BILL)

        assert detection.is_statement is False, detection.summary
        assert detection.signals == (SIGNAL_DATE_RANGE,)

    def test_one_signal_alone_is_never_enough(self):
        text = "CLOSING BALANCE 1,234.56\n" + _FILLER
        detection = detect_statement_text(text)

        assert detection.signals == (SIGNAL_CLOSING_BALANCE,)
        assert detection.is_statement is False

    def test_two_unrelated_dates_on_a_line_are_not_a_range(self):
        text = "Invoice date: 2026-01-04 Payment due: 2026-02-03\n" + _FILLER
        assert SIGNAL_DATE_RANGE not in detect_statement_text(text).signals

    @pytest.mark.parametrize(
        "line",
        [
            "Statement period: 2026-01-01 to 2026-01-31",
            "Period: January 1, 2026 to January 31, 2026",
            "From 2026-01-01 To 2026-01-31",
            "2026-01-01 - 2026-01-31",
            "2026-01-01 – 2026-01-31",
            "Dec. 29, 2025 - Jan. 28, 2026",
        ],
    )
    def test_date_range_shapes_that_must_be_recognised(self, line: str):
        text = line + "\n" + _FILLER
        assert SIGNAL_DATE_RANGE in detect_statement_text(text).signals

    def test_collapsed_whitespace_still_matches_the_phrases(self):
        """pdfplumber emits "Openingbalance" when the column is narrow.

        The two lines are the generated statement's own balance rows, printed
        the way its text layer comes back.
        """
        debits = sum(-delta for _d, _desc, delta in STATEMENT_ROWS if delta < 0)
        closing = statement_running_balances()[-1]
        text = (
            f"Jan31 Openingbalance {STATEMENT_OPENING:,.2f}\n"
            f"Feb27 Closingtotals {debits:,.2f} {closing:,.2f}\n"
            + _FILLER
        )
        signals = detect_statement_text(text).signals

        assert SIGNAL_OPENING_BALANCE in signals
        assert SIGNAL_CLOSING_BALANCE in signals

    def test_a_phrase_split_across_a_line_break_still_matches(self):
        text = "Statement\nperiod 2026-01-01\n" + _FILLER
        assert SIGNAL_STATEMENT_PERIOD in detect_statement_text(text).signals

    def test_reopening_balance_does_not_count_as_an_opening_balance(self):
        text = "Reopening balance discussion\n" + _FILLER
        assert SIGNAL_OPENING_BALANCE not in detect_statement_text(text).signals

    def test_ledger_rows_need_a_date_and_two_amounts(self):
        rows = "\n".join(
            f"2026-01-{day:02d} POS PURCHASE ANYTOWN MB 1,1{day:02d}.25 4,2{day:02d}.60"
            for day in range(1, MIN_LEDGER_ROWS + 1)
        )
        detection = detect_statement_text(rows)
        assert detection.row_count == MIN_LEDGER_ROWS
        assert SIGNAL_LEDGER_ROWS in detection.signals

        # One short of the threshold, the signal does not fire.
        fewer = "\n".join(rows.splitlines()[:-1])
        assert SIGNAL_LEDGER_ROWS not in detect_statement_text(fewer).signals

        # A single amount per line is an invoice line item, not a ledger row.
        one_amount = "\n".join(line.rsplit(" ", 1)[0] for line in rows.splitlines())
        assert detect_statement_text(one_amount).row_count == 0

    def test_priced_line_items_are_not_ledger_rows(self):
        text = "\n".join(
            f"Anodised 4mm steel bracket (lot 2{n:02d}) 3 13.75 41.25"
            for n in range(1, 40)
        )
        assert detect_statement_text(text).row_count == 0

    def test_an_fx_rate_is_not_a_second_amount(self):
        """"AT1.3500" read as the amount "1.35" would fake a ledger row."""
        closing = statement_running_balances()[-1]
        text = "\n".join(
            f"Feb{day:02d} US$Transfer,USDTFR{USD_ACCOUNT},AT{STATEMENT_FX_RATE} {closing:,.2f}"
            for day in range(1, 15)
        )
        assert detect_statement_text(text).row_count == 0

    def test_the_boolean_shortcut_agrees_with_the_evidence(self):
        assert bool(detect_statement_text(CREDIT_CARD)) is True
        assert bool(detect_statement_text(CONSULTING_INVOICE)) is False

"""The PDF bank statement parser: dates, amounts, and one whole statement.

SYNTHETIC DATA. Every figure below is either written by this module or read from
``scripts/gen_fixtures.py``, which generated ``tests/fixtures/test_bank_statement.pdf``.
No real bank, account, counterparty or amount appears anywhere in it.

Two halves, for two reasons:

* ``_parse_date`` and ``_clean_amount`` are pure string rules and carry no data
  at all. They are tested on the date and money shapes a statement PDF can
  carry — an ISO date, a slashed one, a month name, a dollar sign, a thousands
  separator — and on the two "there is nothing here" spellings (``""`` and
  ``"-"``) that must come back empty rather than raise.
* the statement tests run the checked-in fixture through ``pdf_to_csv`` and then
  through ``core.bank_parser.parse_csv``, which is the path a user's upload
  takes. Their expected values are **derived from the generator's constants**
  (``STATEMENT_ROWS``, ``STATEMENT_WIRE_IN``, ...), so regenerating the fixture
  with different numbers moves the assertions with it instead of breaking them.

The quirks that fixture was built around are the ones worth pinning: the table
crosses a page break, pdfplumber returns the columns run together
(``Openingbalance``, ``Closingtotals``), the FX line carries an ``AT1.3500``
token that must not read as a second amount, and the opening/closing rows have a
transaction's shape without being transactions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.bank_parser import parse_csv
from core.pdf_statement_parser import (
    _clean_amount,
    _parse_date,
    _write_bmo_csv,
    detect_account_type,
    extract_text_from_pdf,
    is_bank_statement_pdf,
    pdf_to_csv,
)
from scripts.gen_fixtures import (
    STATEMENT_FX_OUT,
    STATEMENT_FX_RATE,
    STATEMENT_INTEREST,
    STATEMENT_PERIOD_END,
    STATEMENT_ROWS,
    STATEMENT_WIRE_IN,
)

FIXTURE = Path(__file__).parent / "fixtures" / "test_bank_statement.pdf"


def _expected_date(day_token: str):
    """``"Feb03"`` → ``date(2026, 2, 3)``, the year taken from the period end.

    The fixture prints run-together ``MonDD`` days and states its year once, in
    "For the period ending February 27, 2026". Resolving the two here is the
    same arithmetic the parser has to do, done independently.
    """
    return datetime.strptime(
        f"{day_token}{STATEMENT_PERIOD_END.year}", "%b%d%Y"
    ).date()


class TestDateParsing:
    """Every date shape the table extractor can hand ``_parse_date``."""

    def test_iso_date(self):
        assert _parse_date("2026-03-09") == "20260309"

    def test_slash_date(self):
        assert _parse_date("03/09/2026") == "20260309"

    def test_month_name_date(self):
        assert _parse_date("Mar 9, 2026") == "20260309"

    def test_invalid_date(self):
        assert _parse_date("not a date") is None

    def test_empty_date(self):
        assert _parse_date("") is None


class TestAmountCleaning:
    """Money as a PDF prints it, reduced to something ``Decimal`` accepts."""

    def test_plain_amount(self):
        assert _clean_amount("81.45") == "81.45"

    def test_dollar_sign(self):
        assert _clean_amount("$81.45") == "81.45"

    def test_negative(self):
        assert _clean_amount("-$81.45") == "-81.45"

    def test_commas(self):
        assert _clean_amount("$1,234.56") == "1234.56"

    def test_empty(self):
        assert _clean_amount("") == ""

    def test_none_like(self):
        """An empty cell in a bank table is a dash, and means no amount."""
        assert _clean_amount("-") == ""


class TestWriteBMOCSV:
    """The hand-off: rows out of a PDF have to be readable by the CSV parser."""

    def test_writes_valid_csv(self, tmp_path: Path):
        rows = [
            ("20260304", "DEBIT", Decimal("104.55"), "CEDARVIEW SUPPLIES LTD"),
            ("20260311", "CREDIT", Decimal("9876.54"), "INCOMING WIRE HARBOURLINE LOGISTICS"),
        ]
        csv_path = _write_bmo_csv(rows)
        try:
            assert csv_path.exists()

            transactions = parse_csv(csv_path)
            assert len(transactions) == len(rows)

            # The writer's whole job is the sign convention: it stores a DEBIT
            # as a negative amount and a CREDIT as a positive one, and the
            # reader has to come back with exactly what was written.
            assert transactions[0].description == "CEDARVIEW SUPPLIES LTD"
            assert transactions[0].date_posted == _expected_date("Mar04")
            assert transactions[0].amount == -rows[0][2]
            assert transactions[1].description == "INCOMING WIRE HARBOURLINE LOGISTICS"
            assert transactions[1].amount == rows[1][2]
        finally:
            csv_path.unlink(missing_ok=True)


@pytest.fixture()
def statement_transactions():
    """The synthetic statement, all the way through ``pdf_to_csv`` → ``parse_csv``."""
    result = pdf_to_csv(FIXTURE)
    assert result is not None, "the synthetic statement produced no CSV at all"
    csv_path, _account_type = result
    try:
        return parse_csv(csv_path)
    finally:
        csv_path.unlink(missing_ok=True)


class TestSyntheticBusinessStatement:
    """The generated two-page business-chequing statement, end to end."""

    def test_is_detected_as_bank_statement(self):
        assert is_bank_statement_pdf(FIXTURE) is True

    def test_pdf_to_csv_produces_output(self):
        result = pdf_to_csv(FIXTURE)
        assert result is not None
        csv_path, _account_type = result
        assert csv_path.exists()
        csv_path.unlink(missing_ok=True)

    def test_detects_usd_account(self):
        """``Account Type: USD`` in the header, not a guess from the amounts."""
        text = extract_text_from_pdf(FIXTURE)
        assert detect_account_type(text) == "USD"

        result = pdf_to_csv(FIXTURE)
        assert result is not None
        csv_path, account_type = result
        assert account_type == "USD"
        csv_path.unlink(missing_ok=True)

    def test_correct_transaction_count(self, statement_transactions):
        """Four rows: the opening balance and the closing totals are not transactions.

        Both of those lines have a transaction's shape — a date at the front and
        money at the end — which is exactly why the count is worth asserting.
        """
        assert len(statement_transactions) == len(STATEMENT_ROWS)

    def test_every_row_keeps_its_own_date_and_signed_amount(self, statement_transactions):
        """Row for row against the generator, in statement order."""
        actual = [(t.date_posted, t.amount) for t in statement_transactions]
        expected = [
            (_expected_date(day), delta) for day, _desc, delta in STATEMENT_ROWS
        ]
        assert actual == expected

    def test_wire_payment_extracted(self, statement_transactions):
        wire = [
            t for t in statement_transactions
            if "Wire" in t.description or "WIRE" in t.description
        ]
        assert len(wire) == 1
        assert wire[0].amount == STATEMENT_WIRE_IN

    def test_debit_transaction_is_negative(self, statement_transactions):
        debits = [t for t in statement_transactions if t.transaction_type == "DEBIT"]
        assert len(debits) == sum(1 for _d, _desc, delta in STATEMENT_ROWS if delta < 0)
        assert all(t.amount < 0 for t in debits)

    def test_interest_earned_extracted(self, statement_transactions):
        """An interest credit has no receipt behind it and still has to arrive."""
        interest = [
            t for t in statement_transactions if "nterest" in t.description
        ]
        assert len(interest) == 1
        assert interest[0].amount == STATEMENT_INTEREST

    def test_an_fx_rate_is_not_read_as_the_amount(self, statement_transactions):
        """``AT1.3500`` sits mid-description on the transfer row.

        Read as money it would be "1.35", and the transfer would post at a
        hundredth of its value with the real amount silently dropped.
        """
        transfer = [t for t in statement_transactions if "Transfer" in t.description]
        assert len(transfer) == 1
        assert transfer[0].amount == -STATEMENT_FX_OUT
        assert abs(transfer[0].amount) != Decimal(STATEMENT_FX_RATE)
        assert STATEMENT_FX_RATE in transfer[0].description


class TestPdfDetection:
    def test_non_pdf_returns_false(self, tmp_path: Path):
        """A text file is not a statement, and asking must not raise."""
        not_a_pdf = tmp_path / "notes.txt"
        not_a_pdf.write_text("Hello world, this is not a bank statement", encoding="utf-8")
        assert is_bank_statement_pdf(not_a_pdf) is False

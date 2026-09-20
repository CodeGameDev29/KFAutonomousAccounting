"""The QA harness's index, and the workbook reader it feeds.

``scripts/qa/manifest.py`` is how the offline harness finds a month's statements,
receipts and hand-reconciled workbook. It reads a documented layout under a
configurable directory, indexes whichever accounts the filenames name, and
answers an absent directory with an empty manifest rather than an error, because
the harness has to run where no data directory was ever set up.

Every file these tests touch is either created in ``tmp_path`` or is the
synthetic corpus in ``tests/fixtures`` (see that directory's README).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.qa.manifest import (
    MonthData,
    account_of,
    build_manifest,
    fixture_manifest,
    month_of,
)
from scripts.qa.xlsx_parser import parse_xlsx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEDGER = FIXTURES / "ledger-2026-01.xlsx"


# ── Reading a filename ──────────────────────────────────────────────────────

class TestAccountFromFilename:
    """Which account a statement is about is a filename convention, not a guess.

    The answer is an account key — the same string ``config/accounts.yaml``
    names and ``transactions.account`` stores — and the spellings that reach it
    are a documented, overridable table."""

    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("cad-2026-01", "CAD"),
            ("chequing-2026-01", "CAD"),
            ("usd-2026-01", "USD"),
            ("us-dollar-statement-2026-01", "USD"),
            ("cc-2026-01", "CreditCard"),
            ("credit-card-2026-01", "CreditCard"),
            ("credit_card_2026_01", "CreditCard"),
            ("mastercard-2026-01", "CreditCard"),
        ],
    )
    def test_the_conventions_the_layout_documents(self, stem, expected):
        assert account_of(stem) == expected

    def test_a_credit_card_is_never_read_as_a_chequing_account(self):
        """"credit card" also contains "card"; order in the pattern table matters."""
        assert account_of("credit-card-cad-2026-01") == "CreditCard"
        # and the table is a parameter, not a fixed set of accounts
        assert account_of("transfers-2026-01", (("Wise", (r"transfers?",)),)) == "Wise"

    def test_a_filename_that_names_no_account_is_not_guessed(self):
        assert account_of("statement-2026-01") is None


class TestMonthFromFilename:
    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("cad-2026-01", (2026, 1)),
            ("cad_2026_01", (2026, 1)),
            ("cad-202612", (2026, 12)),
            ("cad-January-2026", (2026, 1)),
            ("cad-Sep 2026", (2026, 9)),
        ],
    )
    def test_the_month_spellings_the_layout_accepts(self, stem, expected):
        assert month_of(stem) == expected

    def test_a_thirteenth_month_is_not_a_month(self):
        """``2026-13`` is a reference number, not a date; fall through to the name."""
        assert month_of("invoice-2026-13") is None

    def test_no_year_is_no_month(self):
        assert month_of("january-statement") is None


# ── Building over a directory ───────────────────────────────────────────────

def _layout(root: Path) -> Path:
    """The documented layout, with one month of invented files in it."""
    year = root / "2026"
    statements = year / "statements"
    statements.mkdir(parents=True)
    for name in ("cad-2026-01.pdf", "usd-2026-01.pdf", "cc-2026-01.pdf"):
        (statements / name).write_bytes(b"%PDF-1.4 synthetic")
    receipts = year / "receipts" / "2026-01"
    receipts.mkdir(parents=True)
    (receipts / "20260118-example-retailer.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (receipts / "notes.txt").write_text("not a receipt", encoding="utf-8")
    (receipts / "sub").mkdir()
    (receipts / "sub" / "20260122-bluepeak.png").write_bytes(b"\x89PNG synthetic")
    (year / "ledger-2026-01.xlsx").write_bytes(LEDGER.read_bytes())
    return root


class TestBuildManifest:
    def test_a_missing_data_directory_is_an_empty_manifest_not_an_error(self, tmp_path):
        """A data directory nobody has created yet is the normal case, not a failure."""
        assert build_manifest(data_dir=tmp_path / "nothing-here") == {}

    def test_one_month_indexes_every_statement_and_its_workbook(self, tmp_path):
        manifest = build_manifest(data_dir=_layout(tmp_path))

        assert list(manifest) == ["2026-01"]
        month = manifest["2026-01"]
        assert month.label == "Jan 2026"
        assert month.statement_for("CAD").name == "cad-2026-01.pdf"
        assert month.statement_for("USD").name == "usd-2026-01.pdf"
        assert month.statement_for("CreditCard").name == "cc-2026-01.pdf"
        assert month.has_bank_pdfs() is True
        assert month.xlsx is not None and month.xlsx.name == "ledger-2026-01.xlsx"

    def test_only_months_with_something_in_them_are_listed(self, tmp_path):
        """Twelve empty MonthData rows would make the summary table a lie."""
        manifest = build_manifest(data_dir=_layout(tmp_path))
        assert "2026-02" not in manifest

    def test_receipts_are_walked_recursively_and_non_receipts_ignored(self, tmp_path):
        month = build_manifest(data_dir=_layout(tmp_path))["2026-01"]
        names = sorted(p.name for p in month.receipt_files())
        assert names == ["20260118-example-retailer.pdf", "20260122-bluepeak.png"]

    def test_an_open_workbook_lock_file_is_not_the_ground_truth(self, tmp_path):
        """Excel leaves ``~$name.xlsx`` behind; it is not a workbook."""
        root = _layout(tmp_path)
        (root / "2026" / "ledger-2026-02.xlsx").write_bytes(LEDGER.read_bytes())
        (root / "2026" / "~$ledger-2026-02.xlsx").write_bytes(b"lock")
        month = build_manifest(data_dir=root)["2026-02"]
        assert month.xlsx is not None
        assert not month.xlsx.name.startswith("~$")

    def test_a_statement_whose_name_says_nothing_is_skipped_not_misfiled(self, tmp_path):
        root = _layout(tmp_path)
        (root / "2026" / "statements" / "scan001.pdf").write_bytes(b"%PDF-1.4")
        month = build_manifest(data_dir=root)["2026-01"]
        assert month.statements["CAD"].name == "cad-2026-01.pdf"
        assert "scan001" not in str(month.statements)


class TestFixtureManifest:
    """The synthetic corpus, so the harness runs with no data directory configured."""

    def test_it_points_at_the_generated_statement_and_workbook(self):
        manifest = fixture_manifest()
        assert list(manifest) == ["2026-01"]
        month = manifest["2026-01"]
        assert month.statement_for("USD") is not None
        assert month.statement_for("USD").name == "test_bank_statement.pdf"
        assert month.xlsx is not None
        assert month.xlsx.name == "ledger-2026-01.xlsx"
        assert month.has_bank_pdfs() is True


class TestMonthDataItself:
    def test_the_key_is_sortable_and_the_label_is_readable(self):
        md = MonthData(year=2026, month=9)
        assert md.key == "2026-09"
        assert md.label == "Sep 2026"

    def test_a_month_with_nothing_in_it_says_so(self):
        md = MonthData(year=2026, month=9)
        assert md.has_bank_pdfs() is False
        assert md.statement_for("CAD") is None
        assert md.receipt_files() == []


# ── The workbook reader, over the synthetic workbook ────────────────────────

class TestGroundTruthWorkbook:
    """``tests/fixtures/ledger-2026-01.xlsx``, whose contents the generator fixes.

    The expected rows below are the generator's own
    ``XLSX_CAD_ROWS`` / ``XLSX_USD_ROWS`` / ``XLSX_CC_ROWS``, read back out of
    the file — so this asserts the reader's behaviour, not a transcription.
    """

    @pytest.fixture(scope="class")
    def rows(self):
        return parse_xlsx(LEDGER)

    def test_both_currency_sections_of_one_sheet_are_read(self, rows):
        """CAD and USD sit in the same tab, separated by a SUM row."""
        from scripts.gen_fixtures import XLSX_CAD_ROWS, XLSX_USD_ROWS

        cad = [r for r in rows if r.account == "CAD"]
        usd = [r for r in rows if r.account == "USD"]
        assert len(cad) == len(XLSX_CAD_ROWS)
        assert len(usd) == len(XLSX_USD_ROWS)

    def test_the_sum_row_is_not_a_transaction(self, rows):
        """It carries a date-less total; reading it would double the CAD section."""
        from scripts.gen_fixtures import XLSX_CAD_ROWS

        cad_total = sum(r.amount for r in rows if r.account == "CAD")
        assert cad_total == sum(r[2] for r in XLSX_CAD_ROWS)

    def test_the_credit_card_sheet_uses_the_opposite_sign_convention(self, rows):
        """A positive card amount is a charge (DEBIT); a negative one is a payment."""
        card = {r.description: r for r in rows if r.account == "CreditCard"}
        charge = card["Example Retailer MARKETPLACE"]
        payment = card["PAYMENT THANK YOU"]
        assert charge.amount > 0 and charge.transaction_type == "DEBIT"
        assert payment.amount < 0 and payment.transaction_type == "CREDIT"

    def test_the_posting_date_is_what_lands_in_the_account(self, rows):
        """The card sheet has both dates; the posting date is the one that counts."""
        charge = next(
            r for r in rows
            if r.account == "CreditCard" and r.description == "Example Retailer MARKETPLACE"
        )
        assert charge.date_posted == date(2026, 1, 19)

    def test_numeric_yyyymmdd_dates_survive_the_round_trip(self, rows):
        wire = next(r for r in rows if r.description.startswith("INCOMING WIRE"))
        assert wire.date_posted == date(2026, 1, 25)
        assert wire.amount == Decimal("9600")
        assert wire.transaction_type == "CREDIT"

    def test_every_row_says_which_sheet_and_line_it_came_from(self, rows):
        """Without provenance a mismatch report cannot be argued with."""
        assert rows, "the synthetic workbook should not be empty"
        for row in rows:
            assert row.sheet_name
            assert row.row_number > 0

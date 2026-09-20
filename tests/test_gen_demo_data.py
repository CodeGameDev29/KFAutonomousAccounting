"""``scripts/gen_demo_data.py`` -- the synthetic demo month a new user uploads.

Three promises the script makes, held here:

* it is deterministic: two runs into two directories are byte-identical, so the
  set is reproducible and carries no wall clock;
* the statement it writes is one the project's own bank parser reads, to the
  row count the script declares;
* nothing it writes names an email address outside ``example.com`` -- the
  cheapest tripwire for a real record having been pasted into the cast.
"""

from __future__ import annotations

import re
from decimal import Decimal

from core.bank_parser import detect_csv_format, parse_csv
from scripts.gen_demo_data import BANK_ROWS, DOCS, generate_all

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _pdf_text(path) -> str:
    import fitz  # PyMuPDF

    with fitz.open(path) as pdf:
        meta = " ".join(str(v) for v in (pdf.metadata or {}).values() if v)
        return meta + "\n" + "\n".join(page.get_text() for page in pdf)


def test_two_runs_are_byte_identical(tmp_path):
    first = generate_all(tmp_path / "a")
    second = generate_all(tmp_path / "b")

    assert [p.name for p in first] == [p.name for p in second]
    assert len(first) == 1 + len(DOCS) + 1  # statement, documents, README.txt
    for a, b in zip(first, second):
        assert a.read_bytes() == b.read_bytes(), f"{a.name} differs between runs"


def test_statement_parses_with_the_bank_parser(tmp_path):
    generate_all(tmp_path)
    statement = tmp_path / "demo_bank_cad.csv"

    assert detect_csv_format(statement) == "bmo_cad"
    txns = parse_csv(statement)

    assert len(txns) == len(BANK_ROWS)
    assert {t.currency for t in txns} == {"CAD"}
    assert [t.amount for t in txns] == [amount for _d, _t, amount, _desc, _e in BANK_ROWS]
    assert [t.transaction_type for t in txns] == [kind for _d, kind, _a, _desc, _e in BANK_ROWS]
    # Direction and sign agree on every row, as a chequing export prints them.
    for t in txns:
        assert (t.amount < 0) == (t.transaction_type == "DEBIT")


def test_every_matched_document_has_a_bank_line_for_its_total(tmp_path):
    """The set is a story: the documents and the statement are about the same money."""
    bank = {abs(amount) for _d, _t, amount, _desc, _e in BANK_ROWS}
    same_currency = [d for d in DOCS if d.currency == "CAD" and "NO MATCH" not in d.expect]
    assert same_currency
    for doc in same_currency:
        assert doc.total in bank, doc.filename
    # Amounts on the statement are unique, so no pair is ambiguous by design.
    assert len(bank) == len(BANK_ROWS)
    assert all(isinstance(d.total, Decimal) for d in DOCS)


def test_no_email_outside_example_com(tmp_path):
    written = generate_all(tmp_path)
    found: set[str] = set()
    for path in written:
        if path.suffix == ".pdf":
            text = _pdf_text(path)
        elif path.suffix in {".csv", ".txt"}:
            text = path.read_text(encoding="utf-8")
        else:
            continue  # the PNG has no text layer; its words come from the same Doc rows
        found.update(_EMAIL.findall(text))

    assert found, "expected the documents to carry example.com contact addresses"
    assert all(addr.lower().endswith("@example.com") for addr in found), found


def test_every_document_prints_the_synthetic_notice(tmp_path):
    for path in generate_all(tmp_path):
        if path.suffix == ".pdf":
            text = _pdf_text(path)
            assert "synthetic test fixture" in text
            assert "scripts/gen_demo_data.py" in text  # the author field

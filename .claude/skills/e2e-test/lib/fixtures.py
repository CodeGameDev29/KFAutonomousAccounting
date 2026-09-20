"""The files the suites upload — chosen by name, generated per run, never committed.

This skill ships **no fixture files of its own**. Static inputs come from the
repository's synthetic set under `tests/fixtures/` (written by
`scripts/gen_fixtures.py`; read `tests/fixtures/README.md`). Everything that has
to be unique per run is generated here into a temp directory. Nothing in either
place is, or may ever be, a real document.

Three lessons are baked into this module.

**Never discover a fixture by glob.** Taking "the first `*.pdf` in a directory"
is how a harness ends up uploading the file that exists to be *rejected* as its
receipt, collecting an HTTP 400, and passing anyway. Fixtures are named
constants; a missing one is a FAIL with its path, because having the file is
ours to arrange.

**Uploads are content-hash deduped, and statements replace by filename.** A
byte-identical receipt is swallowed and the run reports success against the last
run's data, so the receipt is generated per run with its own order number.
Statements are the opposite: the same *filename* deletes and replaces its own
transactions, so the bank CSV keeps a fixed name and each run starts from a
known set of rows.

**The receipt and the CSV agree on purpose.** The CSV carries an EXAMPLE
RETAILER row with the receipt's exact date and total, so a *correct* matcher has
one honest pair to find. Without it, "reconciliation stored at least one match"
is a bar no honest engine could clear.

Run this file directly to write the same material for the browser journeys:

    python .claude/skills/e2e-test/lib/fixtures.py --out <dir>
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
REPO_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

# ── Named fixtures from the repository's synthetic set ────────────────────
# A two-page synthetic statement in a bank-export layout (the LLM parse path).
BANK_STATEMENT_PDF = REPO_FIXTURES_DIR / "test_bank_statement.pdf"
# An unsupported extension: one line of prose, no data.
UNSUPPORTED_FILE = REPO_FIXTURES_DIR / "test_unsupported.txt"
# The image ingest path, for the journeys.
RECEIPT_IMAGE = REPO_FIXTURES_DIR / "test_receipt_retailer.png"

# ── What the generated receipt says on its face ───────────────────────────
# The same invented cast as tests/fixtures: a Manitoba company, so 5% GST and a
# separate 7% provincial line are a valid combination. The CSV below is built to
# agree with these three values.
RECEIPT_VENDOR = "EXAMPLE RETAILER"
RECEIPT_SUBTOTAL = "36.00"
RECEIPT_GST = "1.80"
RECEIPT_RST = "2.52"
RECEIPT_TOTAL = "40.32"
RECEIPT_DATE_ISO = "2026-01-18"
RECEIPT_DATE_YYYYMMDD = "20260118"

# The statement the API suite owns. Fixed name: upload-statement replaces the
# transactions of a file with the same name, which keeps a re-run deterministic.
# The stem ends in `cad` because the parser takes the account key from it.
# Deliberately *not* the journeys' file name — re-uploading that here would
# delete a journey's statement.
BANK_CSV_UPLOAD_NAME = "e2e_api_bank_cad.csv"
JOURNEY_BANK_CSV_NAME = "e2e_journey_bank_cad.csv"

# How many transaction rows the generated CSV carries. The upload test asserts
# every one of them imported: a silently dropped row is a real defect.
BANK_CSV_ROWS = 6

_RUN_TAG = str(int(time.time()))


def run_tag() -> str:
    """One tag per process, shared by every fixture it writes."""
    return _RUN_TAG


def require(path: Path, what: str) -> Path:
    """Return `path`, or fail loudly — a missing fixture is ours to arrange."""
    if not path.is_file():
        raise AssertionError(
            f"{what} fixture is missing: {path}. This is a harness setup failure, not a "
            "product failure — regenerate the synthetic set with `python scripts/gen_fixtures.py` "
            "or point the constant in lib/fixtures.py at the right file."
        )
    return path


def _temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="aa-e2e-"))


def write_receipt_pdf(out: Path) -> Path:
    """Write a one-page synthetic retail receipt, unique to this run.

    The order number carries the run tag, so the content hash differs from
    every earlier run and dedup cannot swallow it, while vendor, date and total
    stay fixed for the CSV to agree with. PyMuPDF is already a dependency of the
    app (`requirements.txt`).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - environment problem
        raise AssertionError(
            "PyMuPDF is not importable, so the receipt fixture cannot be generated. Run the "
            "suite with the project's virtualenv (`pip install -r requirements.txt`)."
        ) from exc

    lines = [
        ("Example Retailer", 16),
        ("Order Confirmation / Receipt", 11),
        ("", 11),
        (f"Order #: E2E-{_RUN_TAG}", 11),
        (f"Order placed: {RECEIPT_DATE_ISO}", 11),
        ("Payment: Card ending in 0000", 11),
        ("", 11),
        ("Ship to:", 11),
        ("Example Corp Inc.", 11),
        ("123 Example St, Anytown, MB R0A 0A0", 11),
        ("", 11),
        ("Items                              Qty      Amount", 11),
        (f"Ergonomic keyboard tray              1       {RECEIPT_SUBTOTAL}", 11),
        ("", 11),
        (f"Item subtotal                                {RECEIPT_SUBTOTAL}", 11),
        (f"GST (5%)                                      {RECEIPT_GST}", 11),
        (f"RST (7%)                                      {RECEIPT_RST}", 11),
        (f"Order total (CAD)                            {RECEIPT_TOTAL}", 12),
    ]
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 72.0
    for text, size in lines:
        if text:
            page.insert_text((72, y), text, fontname="cour", fontsize=size)
        y += size + 7
    doc.set_metadata({"title": "Synthetic e2e receipt", "author": "", "creator": "", "producer": ""})
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    doc.close()
    return out


def write_bank_csv(out: Path) -> Path:
    """Write a bank-export-layout CSV whose EXAMPLE RETAILER row matches the receipt.

    Header names are load-bearing: `core/bank_parser.py` detects the format from
    them and raises on anything it does not recognise. Dates are YYYYMMDD and
    debits are negative, as in `tests/fixtures/test_bank_cad.csv`.

    Every description carries a per-run reference, the way a bank memo carries a
    store or auth number. Transaction dedup ignores the source file, so without
    the reference a re-run's rows are swallowed as duplicates of whatever an
    earlier run or journey already imported.
    """
    ref = f"REF{_RUN_TAG[-6:]}"
    acct = "000000-1234"
    rows = [
        ("DEBIT", RECEIPT_DATE_YYYYMMDD, f"-{RECEIPT_TOTAL}", f"{RECEIPT_VENDOR} PURCHASE {ref}"),
        ("DEBIT", "20260119", "-12.50", f"ANYTOWN COFFEE HOUSE {ref}"),
        ("DEBIT", "20260120", "-89.99", f"PINEGROVE MOBILE PREAUTH PMT {ref}"),
        ("DEBIT", "20260122", "-300.00", f"BLUEPEAK SOFTWARE INC {ref}"),
        ("CREDIT", "20260125", "9600.00", f"INCOMING WIRE HARBOURLINE LOGISTICS {ref}"),
        ("DEBIT", "20260128", "-67.30", f"MERIDIAN FUEL {ref}"),
    ]
    assert len(rows) == BANK_CSV_ROWS
    text = ['"Account Number","Transaction Type","Date Posted","Transaction Amount","Description"']
    text += [f'"{acct}","{kind}","{date}","{amount}","{desc}"' for kind, date, amount, desc in rows]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(text) + "\n", encoding="utf-8")
    return out


def write_not_a_pdf(out: Path) -> Path:
    """Plain text under a `.pdf` name: must be refused on its magic bytes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "This file is not a PDF. It is plain text with a .pdf extension, and an upload\n"
        f"endpoint must refuse it before any extraction happens. (e2e run {_RUN_TAG})\n",
        encoding="utf-8",
    )
    return out


def receipt_pdf_for_this_run() -> Path:
    return write_receipt_pdf(_temp_dir() / f"e2e_api_receipt_{_RUN_TAG}.pdf")


def bank_csv_for_this_run() -> Path:
    return write_bank_csv(_temp_dir() / BANK_CSV_UPLOAD_NAME)


def not_a_pdf_for_this_run() -> Path:
    return write_not_a_pdf(_temp_dir() / f"e2e_api_not_a_pdf_{_RUN_TAG}.pdf")


def blank_pdf_for_this_run() -> Path:
    """A valid but empty one-page PDF, unique per run.

    Used only by the duplicate-upload test: it needs the *same bytes* twice
    inside one run, and it must not collide with a previous run's row, or the
    first of the two uploads is already the duplicate.
    """
    body = (
        b"%PDF-1.0\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n"
        b"0000000058 00000 n\n0000000115 00000 n\n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n206\n%%EOF\n"
        + f"%% e2e duplicate probe {_RUN_TAG}\n".encode()
    )
    out = _temp_dir() / f"e2e_api_blank_{_RUN_TAG}.pdf"
    out.write_bytes(body)
    return out


def write_journey_set(out_dir: Path) -> dict[str, Path]:
    """Everything a browser journey uploads, in one directory, fresh for this run."""
    out_dir = out_dir.resolve()
    return {
        "bank_csv": write_bank_csv(out_dir / JOURNEY_BANK_CSV_NAME),
        "receipt_pdf": write_receipt_pdf(out_dir / f"e2e_journey_receipt_{_RUN_TAG}.pdf"),
        "not_a_pdf": write_not_a_pdf(out_dir / "e2e_journey_not_a_pdf.pdf"),
        "bank_statement_pdf": require(BANK_STATEMENT_PDF, "Bank statement PDF"),
        "receipt_image": require(RECEIPT_IMAGE, "Receipt image"),
        "unsupported_file": require(UNSUPPORTED_FILE, "Unsupported-file"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write this run's synthetic upload files for the browser journeys.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Directory to write into (default: a new temp directory). "
                             "Keep it outside the repository.")
    args = parser.parse_args()
    written = write_journey_set(args.out or _temp_dir())
    width = max(len(k) for k in written)
    for key, path in written.items():
        print(f"{key.ljust(width)}  {path}")


if __name__ == "__main__":
    main()

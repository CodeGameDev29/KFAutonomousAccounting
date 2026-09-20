#!/usr/bin/env python3
"""Generate a coherent demo month. SYNTHETIC, GENERATED, FIXED DATES, NO REAL RECORDS.

``tests/fixtures/`` holds per-parser unit fixtures: each one keeps a single code
path honest and none of them belongs to the same story, so uploading them
through the UI matches nothing. This script writes the other thing a new user
needs -- one invented month (January 2026) of one invented company's books, in
which the bank statement and the documents are about the same payments, so the
whole pipeline has something to show: extraction, matching, review, exclusion
of fees and transfers, categorization, and an export with proof documents.

Nothing here is a real document. The company, the vendors, the addresses, the
account number, the invoice numbers and every amount are invented, and the cast
is the one ``scripts/gen_fixtures.py`` already uses (Example Corp Inc., Anytown,
Manitoba, ``@example.com``). Every generated document prints a demo notice on
its face.

Usage::

    python scripts/gen_demo_data.py                 # writes data/demo/
    python scripts/gen_demo_data.py --out /tmp/demo # somewhere else

``data/`` is gitignored, so the default output is never committed. The output is
byte-for-byte reproducible: there is no randomness and no wall clock in it
(reportlab invariant mode, fixed dates). Only packages already in
``requirements.txt`` are used (reportlab, Pillow).

What is in the set, and what each piece is there to show -- the expectations
below are what the matcher's documented scoring predicts, and ``README.txt`` in
the output directory repeats them next to the upload order:

===============================================  ==========================================
file                                             what it demonstrates
===============================================  ==========================================
``demo_bank_cad.csv``                            a CAD chequing statement in the BMO export
                                                 layout (the built-in CSV parser), 12 rows
``demo_receipt_orchid_telecom.pdf``              exact amount, same day
``demo_invoice_bluepeak_software.pdf``           exact amount, document dated six days
                                                 before the bank line
``demo_invoice_jordan_avery_consulting.pdf``     a services invoice, GST only, paid in full
``demo_receipt_cedarview_supplies.pdf``          a retail receipt with GST and RST lines
``demo_receipt_sample_street_cafe.png``          the image path: a photographed receipt
``demo_invoice_example_corp_to_harbourline.pdf`` an invoice ISSUED BY the company, matching
                                                 the incoming wire -- needs
                                                 ``OWN_COMPANY_PATTERNS=example corp``
``demo_invoice_northwind_hosting_usd.pdf``       a USD invoice paid from the CAD account
                                                 at a rate inside the CAD/USD band
``demo_receipt_harbourview_print_cash.pdf``      a cash receipt with no bank line at all
===============================================  ==========================================
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

try:  # imported as ``scripts.gen_demo_data`` (pytest, from the repo root)
    from scripts.gen_fixtures import (
        CAD_ACCOUNT,
        CAD_CSV_HEADER,
        CLIENT_WIRE,
        COMPANY,
        COMPANY_ADDRESS,
        COMPANY_EMAIL,
        CONTRACTOR_ADDRESS,
        CONTRACTOR_BUSINESS,
        CONTRACTOR_EMAIL,
        SOFTWARE,
        TELECOM,
        _grouped,
        _money,
    )
except ModuleNotFoundError:  # run as ``python scripts/gen_demo_data.py``
    from gen_fixtures import (
        CAD_ACCOUNT,
        CAD_CSV_HEADER,
        CLIENT_WIRE,
        COMPANY,
        COMPANY_ADDRESS,
        COMPANY_EMAIL,
        CONTRACTOR_ADDRESS,
        CONTRACTOR_BUSINESS,
        CONTRACTOR_EMAIL,
        SOFTWARE,
        TELECOM,
        _grouped,
        _money,
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "demo"

AUTHOR = "scripts/gen_demo_data.py (synthetic)"
NOTICE = "This document is a synthetic test fixture (demo data). It is not a real {kind}."

GST = Decimal("0.05")
#: Manitoba's retail sales tax -- provincial, so never a GST/HST input tax credit.
RST = Decimal("0.07")
CENT = Decimal("0.01")

# The extra members of the invented cast. Same town, same province, so every
# tax line on every document agrees about where the company is.
SUPPLIES = "Cedarview Supplies Ltd"
SUPPLIES_ADDRESS = "88 Sample Rd, Anytown, MB R0C 0C0"
CAFE = "Sample Street Cafe"
CAFE_ADDRESS = "12 Sample St, Anytown, MB R0A 0A0"
PRINT_SHOP = "Harbourview Print Shop"
PRINT_SHOP_ADDRESS = "5 Harbourview Lane, Anytown, MB R0D 0D0"
TELECOM_ADDRESS = "400 Orchid Way, Anytown, MB R0E 0E0"
SOFTWARE_ADDRESS = "77 Bluepeak Cres, Anytown, MB R0F 0F0"
CLIENT_NAME = "Harbourline Logistics"
CLIENT_ADDRESS = "900 Harbourline Dr, Anytown, MB R0G 0G0"
# A US supplier billing in US dollars. "Hosting" is in the name on purpose: the
# categorization agent sees the bank line, and a line that says what was bought
# can be categorized where a bare trading name is honestly left uncategorized.
USD_VENDOR = "Northwind Hosting LLC"
USD_VENDOR_ADDRESS = "789 Sample Blvd, Anytown, WA 00000, USA"
COURIER_LINE = "MERIDIAN COURIER SERVICES"
PARKING_LINE = "ANYTOWN PARKING AUTHORITY"

#: CAD per USD on the day the USD invoice was paid. Invented, and inside the
#: engine's default FX_CAD_USD_MIN/MAX band of 1.20-1.55.
FX_RATE = Decimal("1.3725")


# ── The documents ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Doc:
    """One document of the demo month, and the bank line (if any) that paid it."""

    filename: str
    kind: str                      # "receipt" | "invoice"
    heading: str                   # the word printed at the top
    issuer: str
    issuer_lines: tuple[str, ...]
    number_label: str
    number: str
    doc_date: date
    items: tuple[tuple[str, Decimal], ...]
    currency: str = "CAD"
    gst: bool = True
    rst: bool = False
    bill_to: tuple[str, ...] = (COMPANY, COMPANY_ADDRESS)
    extra: tuple[str, ...] = field(default_factory=tuple)
    expect: str = ""

    @property
    def subtotal(self) -> Decimal:
        return sum((amount for _d, amount in self.items), Decimal("0"))

    @property
    def gst_amount(self) -> Decimal:
        return (self.subtotal * GST).quantize(CENT) if self.gst else Decimal("0")

    @property
    def rst_amount(self) -> Decimal:
        return (self.subtotal * RST).quantize(CENT) if self.rst else Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.gst_amount + self.rst_amount


DOC_TELECOM = Doc(
    filename="demo_receipt_orchid_telecom.pdf",
    kind="receipt", heading="PAYMENT RECEIPT",
    issuer=TELECOM.title(),
    issuer_lines=(TELECOM_ADDRESS, "billing@example.com"),
    number_label="Receipt #", number="OT-2026-000101",
    doc_date=date(2026, 1, 5),
    items=(("Business mobile plan - January 2026", Decimal("80.00")),),
    rst=True,
    extra=("Payment: Pre-authorized debit, account ending 1234",),
    expect="MATCH, auto-approved: same amount, same day, vendor named on the bank line.",
)

DOC_SOFTWARE = Doc(
    filename="demo_invoice_bluepeak_software.pdf",
    kind="invoice", heading="INVOICE",
    issuer=SOFTWARE.title(),
    issuer_lines=(SOFTWARE_ADDRESS, "accounts@example.com"),
    number_label="Invoice #", number="BP-2026-0117",
    doc_date=date(2026, 1, 2),
    items=(("Team plan, 10 seats - January 2026", Decimal("300.00")),),
    rst=True,
    extra=("Terms: Due on receipt",),
    expect=("MATCH, sent to review: same amount, but the invoice is dated six days "
            "before the bank line, which costs enough date score to stay under the "
            "auto-approve threshold."),
)

DOC_CONTRACTOR = Doc(
    filename="demo_invoice_jordan_avery_consulting.pdf",
    kind="invoice", heading="INVOICE",
    issuer=CONTRACTOR_BUSINESS,
    issuer_lines=(CONTRACTOR_ADDRESS, CONTRACTOR_EMAIL),
    number_label="Invoice #", number="JA-2026-0003",
    doc_date=date(2026, 1, 9),
    items=(
        ("Design production services, 40.0 h @ 75.00", Decimal("3000.00")),
        ("Asset preparation and handoff, 8.0 h @ 75.00", Decimal("600.00")),
    ),
    extra=("Terms: Due on receipt",),
    expect=("MATCH, auto-approved: a services invoice (GST only, no RST) paid in full "
            "three days after it was issued."),
)

DOC_SUPPLIES = Doc(
    filename="demo_receipt_cedarview_supplies.pdf",
    kind="receipt", heading="SALES RECEIPT",
    issuer=SUPPLIES,
    issuer_lines=(SUPPLIES_ADDRESS,),
    number_label="Receipt #", number="CV-0000-4410",
    doc_date=date(2026, 1, 13),
    items=(
        ("Copy paper, case of 10", Decimal("64.00")),
        ("Toner cartridge, black", Decimal("50.00")),
    ),
    rst=True,
    extra=("Payment: Debit card ending 1234",),
    expect="MATCH, auto-approved: same amount, posted the day after the purchase.",
)

DOC_CAFE = Doc(
    filename="demo_receipt_sample_street_cafe.png",
    kind="receipt", heading="RECEIPT",
    issuer=CAFE,
    issuer_lines=(CAFE_ADDRESS,),
    number_label="Check #", number="0000-0217",
    doc_date=date(2026, 1, 16),
    items=(
        ("Lunch special x2", Decimal("28.00")),
        ("Coffee x2", Decimal("6.00")),
    ),
    rst=True,
    bill_to=(),
    extra=("Payment: Debit card ending 1234",),
    expect=("MATCH, auto-approved: the image path -- a receipt with no text layer, read "
            "by the vision model."),
)

DOC_OUTGOING = Doc(
    filename="demo_invoice_example_corp_to_harbourline.pdf",
    kind="invoice", heading="INVOICE",
    issuer=COMPANY,
    issuer_lines=(COMPANY_ADDRESS, COMPANY_EMAIL),
    number_label="Invoice #", number="EC-2026-0001",
    doc_date=date(2026, 1, 6),
    items=(("Brand and packaging design program - milestone 1", Decimal("9500.00")),),
    bill_to=(CLIENT_NAME, CLIENT_ADDRESS),
    extra=("Payment due: 2026-01-16", "Payment by wire transfer to the account on file."),
    expect=("An invoice the company ISSUED. With OWN_COMPANY_PATTERNS=example corp it "
            "pairs with the incoming wire and goes to review (same amount, nine days "
            "between invoice and wire). With the variable unset every document is an "
            "expense, so it stays unmatched and the wire stays 'needs a receipt'."),
)

DOC_USD = Doc(
    filename="demo_invoice_northwind_hosting_usd.pdf",
    kind="invoice", heading="INVOICE",
    issuer=USD_VENDOR,
    issuer_lines=(USD_VENDOR_ADDRESS, "billing-us@example.com"),
    number_label="Invoice #", number="NW-2026-0058",
    doc_date=date(2026, 1, 20),
    items=(("Cloud hosting, production cluster - January 2026", Decimal("790.00")),
           ("Managed backups - January 2026", Decimal("50.00"))),
    currency="USD", gst=False,
    extra=("All amounts are in US dollars (USD).", "Terms: Due on receipt"),
    expect=("FX MATCH: USD 840.00 against a CAD 1,152.90 bank line, implied rate "
            f"{FX_RATE} inside the 1.20-1.55 band. A cross-currency pair is never "
            "called exact; expect review or the FX-relaxed pass."),
)

DOC_CASH = Doc(
    filename="demo_receipt_harbourview_print_cash.pdf",
    kind="receipt", heading="SALES RECEIPT",
    issuer=PRINT_SHOP,
    issuer_lines=(PRINT_SHOP_ADDRESS,),
    number_label="Receipt #", number="HP-0000-0932",
    doc_date=date(2026, 1, 24),
    items=(("Business cards, 250", Decimal("45.00")),),
    rst=True,
    extra=("Payment: Cash",),
    expect="NO MATCH: paid in cash, so no bank line exists. Stays unmatched.",
)

DOCS: tuple[Doc, ...] = (
    DOC_TELECOM, DOC_SOFTWARE, DOC_CONTRACTOR, DOC_SUPPLIES,
    DOC_CAFE, DOC_OUTGOING, DOC_USD, DOC_CASH,
)

USD_PAID_CAD = (DOC_USD.total * FX_RATE).quantize(CENT)


# ── The bank statement ──────────────────────────────────────────────────────
#
# (date, type, signed amount, description, what should happen to the row).
# The fee, interest and card-payment descriptions are worded to hit the
# ``non_reconcilable`` patterns shipped in ``config/category_rules.yaml``.

BANK_ROWS: tuple[tuple[str, str, Decimal, str, str], ...] = (
    ("20260105", "DEBIT", -DOC_TELECOM.total, f"{TELECOM} PREAUTH PMT",
     f"matches {DOC_TELECOM.filename}"),
    ("20260108", "DEBIT", -DOC_SOFTWARE.total, SOFTWARE,
     f"matches {DOC_SOFTWARE.filename} (review)"),
    ("20260112", "DEBIT", -DOC_CONTRACTOR.total, f"BILL PAYMENT {CONTRACTOR_BUSINESS.upper()}",
     f"matches {DOC_CONTRACTOR.filename}"),
    ("20260114", "DEBIT", -DOC_SUPPLIES.total, SUPPLIES.upper(),
     f"matches {DOC_SUPPLIES.filename}"),
    ("20260115", "CREDIT", DOC_OUTGOING.total, f"INCOMING WIRE {CLIENT_WIRE}",
     f"matches {DOC_OUTGOING.filename} when OWN_COMPANY_PATTERNS is set"),
    ("20260116", "DEBIT", -DOC_CAFE.total, f"{CAFE.upper()} ANYTOWN",
     f"matches {DOC_CAFE.filename}"),
    ("20260121", "DEBIT", -USD_PAID_CAD, "NORTHWIND HOSTING",
     f"matches {DOC_USD.filename} through the FX band"),
    ("20260122", "DEBIT", Decimal("-58.80"), COURIER_LINE,
     "no document in the set: stays 'needs a receipt'"),
    ("20260126", "DEBIT", Decimal("-450.00"), "OnlineTransfer,TF0000001234",
     "credit-card payment: excluded from matching, no receipt needed"),
    ("20260127", "DEBIT", Decimal("-18.00"), PARKING_LINE,
     "no document in the set: stays 'needs a receipt' (rule-categorized as motor vehicle)"),
    ("20260130", "DEBIT", Decimal("-16.00"), "MONTHLY FEE BUSINESS PLAN",
     "bank fee: excluded from matching, no receipt needed"),
    ("20260130", "CREDIT", Decimal("3.42"), "INTEREST EARNED",
     "interest: excluded from matching, no receipt needed"),
)


def gen_bank_csv(out: Path) -> Path:
    """The chequing CSV, in exactly the layout of ``tests/fixtures/test_bank_cad.csv``."""
    path = out / "demo_bank_cad.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(CAD_CSV_HEADER)
        for date_str, txn_type, amount, description, _expect in BANK_ROWS:
            w.writerow([CAD_ACCOUNT, txn_type, date_str, _money(amount), description])
    return path


# ── Rendering ───────────────────────────────────────────────────────────────

def _doc_rows(doc: Doc) -> list[tuple[str, str]]:
    """The body of a document as ``(left text, right-aligned amount)`` rows.

    ``"-"`` on the left with nothing on the right is a rule across the page.
    """
    rows: list[tuple[str, str]] = [
        (f"{doc.number_label} {doc.number}", ""),
        (f"Date: {doc.doc_date.isoformat()}", ""),
    ]
    if doc.bill_to:
        rows += [("", ""), ("Bill To:" if doc.kind == "invoice" else "Sold To:", "")]
        rows += [(line, "") for line in doc.bill_to]
    rows += [("", ""), ("Description", "Amount"), ("-", "")]
    rows += [(desc, _grouped(amount)) for desc, amount in doc.items]
    rows += [("-", ""), ("Subtotal", _grouped(doc.subtotal))]
    if doc.gst:
        rows.append(("GST (5%)", _grouped(doc.gst_amount)))
    if doc.rst:
        rows.append(("RST (7%)", _grouped(doc.rst_amount)))
    total_label = "Total" if doc.kind == "receipt" else "Total Due"
    rows.append((f"{total_label} ({doc.currency})", _grouped(doc.total)))
    if doc.extra:
        rows += [("", "")] + [(line, "") for line in doc.extra]
    return rows


def _doc_lines(doc: Doc, *, width: int) -> list[str]:
    """``_doc_rows`` as monospaced lines, ``width`` characters wide."""
    amount_col = 14
    label_col = width - amount_col
    return [
        "-" * width if (left, right) == ("-", "")
        else f"{left:<{label_col}}{right:>{amount_col}}".rstrip()
        for left, right in _doc_rows(doc)
    ]


def _canvas(path: Path, *, title: str):
    """A reportlab canvas whose bytes do not change between runs."""
    import reportlab.rl_config as rl_config
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as canvas_mod

    rl_config.invariant = 1  # fixed /ID and /CreationDate -> reproducible bytes
    c = canvas_mod.Canvas(str(path), pagesize=LETTER, invariant=1)
    c.setTitle(title)
    c.setAuthor(AUTHOR)
    c.setSubject("Synthetic demo data -- no real records")
    return c


def gen_pdf(out: Path, doc: Doc) -> Path:
    """One letter-size page: a letterhead, the monospaced body, the demo notice."""
    path = out / doc.filename
    c = _canvas(path, title=f"{doc.heading.title()} - {doc.issuer} (synthetic demo)")

    c.setFont("Helvetica-Bold", 18)
    c.drawString(54, 738, doc.issuer)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(558, 738, doc.heading)
    c.setFont("Helvetica", 9)
    y = 722.0
    for line in doc.issuer_lines:
        c.drawString(54, y, line)
        y -= 12
    c.setLineWidth(0.8)
    c.line(54, y - 2, 558, y - 2)

    c.setFont("Courier", 10)
    y -= 24
    for line in _doc_lines(doc, width=84):
        c.drawString(54, y, line)
        y -= 14

    c.setFont("Helvetica-Oblique", 8)
    c.drawString(54, 60, NOTICE.format(kind=doc.kind))
    c.showPage()
    c.save()
    return path


def gen_png(out: Path, doc: Doc) -> Path:
    """The same shape as a narrow till receipt image -- no text layer at all."""
    from PIL import Image, ImageDraw, ImageFont

    path = out / doc.filename
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # a Pillow without the scalable default font
        font = ImageFont.load_default()
    rows = [(doc.issuer, ""), *((line, "") for line in doc.issuer_lines), ("", ""),
            (doc.heading, ""), *_doc_rows(doc), ("", ""),
            ("Synthetic test fixture (demo data).", ""), ("Not a real receipt.", "")]
    width, line_h, margin = 560, 26, 24
    img = Image.new("RGB", (width, margin * 2 + line_h * len(rows)), (252, 252, 248))
    draw = ImageDraw.Draw(img)
    ink = (25, 25, 25)
    y = margin
    for left, right in rows:
        if (left, right) == ("-", ""):
            draw.line((margin, y + line_h // 2, width - margin, y + line_h // 2), fill=ink, width=1)
        else:
            draw.text((margin, y), left, fill=ink, font=font)
            if right:
                draw.text((width - margin - draw.textlength(right, font=font), y), right,
                          fill=ink, font=font)
        y += line_h
    img.save(path, format="PNG", optimize=True)
    return path


# ── The README the script leaves next to the data ───────────────────────────

def gen_readme(out: Path) -> Path:
    path = out / "README.txt"
    lines = [
        "Autonomous Accounting -- demo data",
        "==================================",
        "",
        "Written by scripts/gen_demo_data.py. SYNTHETIC: every company, person, address,",
        "account number, invoice number and amount here is invented. It is one made-up",
        f"month (January 2026) of {COMPANY}, a made-up Manitoba company, built so",
        "that the statement and the documents are about the same payments.",
        "",
        "Before you start",
        "----------------",
        "One document is an invoice the company ISSUED. The engine recognises those by",
        "the issuer's name, so put this in .env and restart the server, or that invoice",
        "is treated as an expense and will not match the incoming wire:",
        "",
        "    OWN_COMPANY_PATTERNS=example corp",
        "",
        "Upload order",
        "------------",
        "1. Transactions -> upload the statement:  demo_bank_cad.csv  (12 rows, instant).",
        "2. Receipts -> upload all eight documents at once. Each is read by your vision",
        "   model; the queue shows progress. Wait until every job is done.",
        "3. Transactions -> \"Match Receipts\". Matching runs, then categorization.",
        "4. Review -> approve or reject whatever was sent to review. A pair waiting",
        "   for review is not yet proof: its document is left out of the binder until",
        "   you approve it.",
        "5. Reports -> January 2026 -> download the binder (ZIP: XLSX workbook,",
        "   proof_of_transaction/ documents, HTML report).",
        "",
        "The statement",
        "-------------",
    ]
    for date_str, txn_type, amount, description, expect in BANK_ROWS:
        day = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        lines.append(f"  {day}  {txn_type:<6} {_grouped(amount):>10}  {description}")
        lines.append(f"      -> {expect}")
    lines += ["", "The documents", "-------------"]
    for doc in DOCS:
        lines.append(f"  {doc.filename}")
        lines.append(f"      {doc.issuer}, {doc.doc_date.isoformat()}, "
                     f"{doc.currency} {_grouped(doc.total)}")
        lines.append(f"      -> {doc.expect}")
    lines += [
        "",
        "What you see depends on the model you pointed LLM_BASE_URL at: a document it",
        "misreads will land in review or stay unmatched rather than be guessed. That is",
        "the engine working as intended.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


# ── Entry point ─────────────────────────────────────────────────────────────

def generate_all(out: Path) -> list[Path]:
    """Write the whole demo set into ``out`` and return the paths, in upload order."""
    out.mkdir(parents=True, exist_ok=True)
    written = [gen_bank_csv(out)]
    for doc in DOCS:
        written.append(gen_png(out, doc) if doc.filename.endswith(".png") else gen_pdf(out, doc))
    written.append(gen_readme(out))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output directory (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    for path in generate_all(args.out):
        shown = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
        print(f"wrote {shown}  ({path.stat().st_size:,} bytes)")
    print("\nAll demo data is synthetic: invented names and amounts, no real records. "
          "Read README.txt in the output directory for the upload order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

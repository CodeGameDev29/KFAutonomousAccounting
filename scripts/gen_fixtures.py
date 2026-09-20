#!/usr/bin/env python3
"""Regenerate every checked-in test fixture. SYNTHETIC, GENERATED, SEED 42, NO REAL RECORDS.

Nothing this script writes is a real document. Every company, person, address,
account number, invoice number, amount and date below was invented for this
repository. The books of a real business are never used, sampled from, or
paraphrased here -- see ``tests/fixtures/README.md``.

What it produces, and why each one exists:

===========================================  ============================================
fixture                                      the code path it keeps honest
===========================================  ============================================
``test_bank_statement.pdf``                  ``core/pdf_statement_parser.py`` -- the
                                             two-page business-chequing layout with
                                             run-together columns, an FX transfer line
                                             carrying an ``AT<rate>`` token, an incoming
                                             wire memo, an interest line, and opening /
                                             closing rows that are not transactions.
                                             Also ``core/statement_detector.py``.
``test_invoice_contractor.pdf``              ``core/statement_detector.py`` -- an invoice
                                             with dates and totals must fire **no**
                                             statement signal.
``test_receipt_retailer.pdf``                same, for a retail receipt -- and a
                                             document with separate GST and
                                             provincial-tax lines, which
                                             ``core/gst_tax_code.py`` must not
                                             confuse for one another.
``test_receipt_retailer.png``                the image ingest path (a photographed /
                                             scanned receipt, no text layer).
``test_receipt_scan.pdf``                    an image-only PDF: a statement that was
                                             scanned has no text layer to judge.
``test_bank_cad.csv``                        ``core/bank_parser.py`` ``detect_csv_format``
                                             + ``parse_bmo_csv`` (chequing signature,
                                             ``YYYYMMDD`` dates, signed amounts, a wire
                                             row that names a client).
``test_wise.csv``                            ``parse_wise_csv`` -- COMPLETED-only filter,
                                             DEBIT/CREDIT direction, fee column, FX
                                             legs whose rate lands in the description.
``test_paypal.csv``                          ``detect_csv_format`` (the
                                             ``TimeZone``/``Gross``/``Net`` signature) +
                                             ``parse_paypal_csv`` -- Completed-only
                                             filter, a skipped internal transfer type,
                                             a fee that lands in the description, and a
                                             credit as well as debits. Times are UTC, so
                                             the file names no region.
``test_unsupported.txt``                     ``detect_csv_format`` raising on a file
                                             whose header matches nothing -- the error
                                             path. One line of prose, no data.
``ised_benchmarks_sample.json``              ``core/reasonableness.py`` ``load_benchmarks``
                                             -- the shape of the (gitignored)
                                             ``config/ised_benchmarks.json``, with
                                             invented ratios.
``ised_benchmarks_cohorts_sample.json``      the same shape carried across every
                                             cohort the engine selects between:
                                             the whole-band cell, four revenue
                                             cohorts, four profit-margin cohorts
                                             and one cell too thin to read.
                                             Invented ratios.
``ledger-2026-01.xlsx``                      ``scripts/qa/xlsx_parser.py`` -- a
                                             hand-kept ground-truth workbook:
                                             CAD and USD sections in one sheet, a SUM
                                             row between them, a credit-card sheet with
                                             a metadata row above its header.
===========================================  ============================================

Usage::

    python scripts/gen_fixtures.py                    # rewrite tests/fixtures/
    python scripts/gen_fixtures.py --seed 7           # a different invented set
    python scripts/gen_fixtures.py --rows 500         # bigger CSV / XLSX, same shapes
    python scripts/gen_fixtures.py --out /tmp/f       # somewhere else

The default output is byte-for-byte reproducible: ``reportlab``'s invariant mode
and fixed document timestamps mean a second run leaves ``git status`` clean.

Only dependencies already in ``requirements.txt`` are used (reportlab, Pillow,
openpyxl), so there is no separate dev requirements file to install.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "tests" / "fixtures"
DEFAULT_SEED = 42

# ── The invented world ──────────────────────────────────────────────────────
#
# One cast of made-up names, shared by every fixture so a transaction and the
# document that proves it can be about the same thing.

# The company and its contractor both sit in Manitoba. The province is load
# bearing, not decoration: the retail receipt charges 5% GST and 7% provincial
# sales tax on separate lines, and Manitoba is a province where that split is
# the real combination (an HST province charges one blended line instead). The
# whole cast shares it so a supplier's invoice, the company's own statement and
# a receipt shipped to the company cannot disagree about where the company is.

COMPANY = "Example Corp Inc."
COMPANY_ADDRESS = "123 Example St, Anytown, MB R0A 0A0"
COMPANY_EMAIL = "ap@example.com"

# The generated statements are a synthetic statement in the BMO export layout:
# the bank name and the product string are what `core/pdf_statement_parser.py`
# and `core/statement_detector.py` key on, so they are format facts the fixture
# has to carry. Every other value on the page — the company, the account
# numbers, the amounts, the counterparties — is invented, and each page prints
# "This document is a synthetic test fixture".
BANK = "BMO Business Banking"
CAD_ACCOUNT = "000000-1234"
USD_ACCOUNT = "000000-5678"
CARD_LAST4 = "0000"

CONTRACTOR = "Jordan Avery"
CONTRACTOR_BUSINESS = "Jordan Avery Consulting"
CONTRACTOR_ADDRESS = "456 Sample Ave, Anytown, MB R0B 0B0"
CONTRACTOR_EMAIL = "jordan.avery@example.com"

CLIENT_WIRE = "HARBOURLINE LOGISTICS"
CLIENT_PLATFORM = "Northwind Trading"
RETAILER = "Example Retailer"
TELECOM = "ORCHID TELECOM"
SOFTWARE = "BLUEPEAK SOFTWARE INC"

# Descriptions the generator draws from when asked for more rows than the
# canonical small set. Invented merchants only.
EXTRA_MERCHANTS = [
    "CEDARVIEW SUPPLIES LTD",
    "ANYTOWN PARKING AUTHORITY",
    "EXAMPLE RETAILER MARKETPLACE",
    "BLUEPEAK SOFTWARE INC",
    "ORCHID TELECOM PREAUTH PMT",
    "HARBOURVIEW PRINT SHOP",
    "SAMPLE STREET CAFE",
    "MERIDIAN COURIER SERVICES",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _money(value: Decimal | float | str) -> str:
    """Two decimal places, no thousands separator -- how a CSV prints money."""
    return f"{Decimal(str(value)):.2f}"


def _grouped(value: Decimal | float | str) -> str:
    """Two decimal places with thousands separators -- how a statement prints it."""
    return f"{Decimal(str(value)):,.2f}"


def _canvas(path: Path, *, title: str):
    """A reportlab canvas whose bytes do not change between runs."""
    import reportlab.rl_config as rl_config
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as _canvas_mod

    rl_config.invariant = 1  # fixed /ID and /CreationDate -> reproducible bytes
    c = _canvas_mod.Canvas(str(path), pagesize=LETTER, invariant=1)
    c.setTitle(title)
    c.setAuthor("scripts/gen_fixtures.py (synthetic)")
    c.setSubject("Synthetic test fixture -- no real records")
    return c


def _rewrite_zip_deterministic(path: Path) -> None:
    """Re-pack a zip container with fixed member timestamps.

    openpyxl stamps every zip member with ``time.localtime()`` and overwrites
    ``dcterms:modified`` with the wall clock at save time, so an otherwise
    identical workbook gets different bytes on every run and the fixture shows up
    as a spurious diff. Nothing about the workbook's content changes here.
    """
    import io
    import re
    import zipfile

    fixed = (2026, 1, 31, 12, 0, 0)
    fixed_iso = "2026-01-31T12:00:00Z"
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as zin, \
            zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in sorted(zin.infolist(), key=lambda i: i.filename):
            data = zin.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = re.sub(
                    rb">[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z</dcterms:modified>",
                    f">{fixed_iso}</dcterms:modified>".encode(),
                    data,
                )
            member = zipfile.ZipInfo(info.filename, date_time=fixed)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = info.external_attr
            zout.writestr(member, data)
    path.write_bytes(buf.getvalue())


def _draw(c, lines: list[str], *, font: str = "Courier", size: int = 9) -> None:
    """Draw one page of monospaced lines, top-down.

    Monospace on purpose: pdfplumber decides where a word ends from the gap
    between glyphs, and the parsers under test read columns that a real bank PDF
    runs together. A proportional font would make the extracted text depend on
    the font metrics of whoever regenerated the fixture.
    """
    c.setFont(font, size)
    y = 740.0
    for line in lines:
        c.drawString(54.0, y, line)
        y -= size + 3.0


# ── 1. The two-page bank statement ──────────────────────────────────────────
#
# Arithmetic, so the fixture is internally consistent and a test can re-derive
# every expected value from these five numbers rather than hardcoding them.

STATEMENT_OPENING = Decimal("4000.00")
STATEMENT_WIRE_IN = Decimal("6250.00")      # incoming wire, a CREDIT
STATEMENT_FX_OUT = Decimal("1500.00")       # outgoing FX transfer, a DEBIT
STATEMENT_FEE = Decimal("25.00")            # monthly plan fee, a DEBIT
STATEMENT_INTEREST = Decimal("12.40")       # interest earned, a CREDIT
STATEMENT_FX_RATE = "1.3500"
STATEMENT_PERIOD_END = date(2026, 2, 27)

STATEMENT_ROWS = [
    # (day, description, signed delta)
    ("Feb03", f"IncomingWirePayment,{CLIENT_WIRE}", STATEMENT_WIRE_IN),
    ("Feb09", f"US$Transfer,USDTFR{USD_ACCOUNT},AT{STATEMENT_FX_RATE}", -STATEMENT_FX_OUT),
    ("Feb18", "ServiceCharge,MONTHLYPLANFEE", -STATEMENT_FEE),
    ("Feb27", "InterestEarned", STATEMENT_INTEREST),
]


def statement_running_balances() -> list[Decimal]:
    """Balance after each row of ``STATEMENT_ROWS``."""
    balance = STATEMENT_OPENING
    out = []
    for _day, _desc, delta in STATEMENT_ROWS:
        balance += delta
        out.append(balance)
    return out


def gen_bank_statement_pdf(out: Path) -> Path:
    """A two-page USD business-chequing statement.

    Every quirk here is one the parser handles and would otherwise go untested:
    the transaction table spans a page break, the columns are run together the
    way pdfplumber returns them, the FX line carries an ``AT1.3500`` token that
    must not read as a second amount, and the opening/closing rows sit in the
    same column shape as real transactions without being transactions.
    """
    path = out / "test_bank_statement.pdf"
    balances = statement_running_balances()
    debits = sum(-d for _, _, d in STATEMENT_ROWS if d < 0)
    credits = sum(d for _, _, d in STATEMENT_ROWS if d > 0)
    closing = STATEMENT_OPENING - debits + credits
    period = STATEMENT_PERIOD_END.strftime("%B %d, %Y").replace(" 0", " ")

    c = _canvas(path, title="Business Chequing Statement (synthetic)")
    _draw(c, [
        f"{BANK}",
        "Business Chequing Statement",
        "",
        COMPANY,
        COMPANY_ADDRESS,
        "",
        "Account Type: USD",
        f"Account Number: {USD_ACCOUNT}",
        f"For the period ending {period}",
        "Page 1 of 2",
        "",
        "Date   Description                              Amountsdebited  Amountscredited      Balance",
        f"Jan31  Openingbalance                                                          {_grouped(STATEMENT_OPENING):>12}",
        f"{STATEMENT_ROWS[0][0]}  {STATEMENT_ROWS[0][1]:<44}"
        f"{_grouped(STATEMENT_ROWS[0][2]):>15}  {_grouped(balances[0]):>12}",
        f"{STATEMENT_ROWS[1][0]}  {STATEMENT_ROWS[1][1]:<44}"
        f"{_grouped(-STATEMENT_ROWS[1][2]):>15}  {_grouped(balances[1]):>12}",
        "",
        "continued on page 2",
    ])
    c.showPage()
    _draw(c, [
        f"{BANK}",
        f"Account Number: {USD_ACCOUNT}",
        f"For the period ending {period}",
        "Page 2 of 2",
        "",
        "Date   Description                              Amountsdebited  Amountscredited      Balance",
        f"{STATEMENT_ROWS[2][0]}  {STATEMENT_ROWS[2][1]:<44}"
        f"{_grouped(-STATEMENT_ROWS[2][2]):>15}  {_grouped(balances[2]):>12}",
        f"{STATEMENT_ROWS[3][0]}  {STATEMENT_ROWS[3][1]:<44}"
        f"{_grouped(STATEMENT_ROWS[3][2]):>15}  {_grouped(balances[3]):>12}",
        "",
        f"Feb27  Closingtotals                            {_grouped(debits):>14}"
        f" {_grouped(credits):>15}  {_grouped(closing):>12}",
        "",
        "Numberofitems 4",
        "",
        "This document is a synthetic test fixture. It is not a bank record.",
    ])
    c.showPage()
    c.save()
    return path


# ── 2. The contractor invoice ───────────────────────────────────────────────

INVOICE_NUMBER = "INV-2026-0042"
INVOICE_DATE = date(2026, 2, 1)
INVOICE_DUE = date(2026, 3, 3)
INVOICE_LINES = [
    ("Design production services", Decimal("40.0"), Decimal("75.00")),
    ("Asset preparation and handoff", Decimal("8.0"), Decimal("75.00")),
]
#: GST only, with no provincial line: professional work billed between two
#: Manitoba businesses carries the 5% federal tax and no retail sales tax.
INVOICE_GST_RATE = Decimal("0.05")


def invoice_totals() -> tuple[Decimal, Decimal, Decimal]:
    subtotal = sum((hours * rate for _d, hours, rate in INVOICE_LINES), Decimal("0"))
    gst = (subtotal * INVOICE_GST_RATE).quantize(Decimal("0.01"))
    return subtotal, gst, subtotal + gst


def gen_invoice_pdf(out: Path) -> Path:
    """A one-page contractor invoice: dates and totals, and no statement signal.

    The dates deliberately sit on their own lines. An invoice date and a due
    date on one line, joined by a dash, is a date *range* -- which is one of the
    five signals the statement detector counts, and the nearest miss there is.
    """
    path = out / "test_invoice_contractor.pdf"
    subtotal, gst, total = invoice_totals()
    lines = [
        "INVOICE",
        "",
        CONTRACTOR_BUSINESS,
        CONTRACTOR_ADDRESS,
        CONTRACTOR_EMAIL,
        "",
        f"Invoice #: {INVOICE_NUMBER}",
        f"Invoice date: {INVOICE_DATE.isoformat()}",
        f"Payment due: {INVOICE_DUE.isoformat()}",
        "",
        "Bill To:",
        COMPANY,
        COMPANY_ADDRESS,
        COMPANY_EMAIL,
        "",
        "Description                                  Hours      Rate       Amount",
    ]
    for desc, hours, rate in INVOICE_LINES:
        lines.append(f"{desc:<44}{hours:>6}{_grouped(rate):>10}{_grouped(hours * rate):>13}")
    lines += [
        "",
        f"{'Subtotal':<60}{_grouped(subtotal):>13}",
        f"{'GST (5%)':<60}{_grouped(gst):>13}",
        f"{'Total Due (CAD)':<60}{_grouped(total):>13}",
        "",
        f"Remit to: {CONTRACTOR_BUSINESS}",
        "Institution (test): 000     Transit (test): 00000",
        "Account (test): 000000-9876",
        "Terms: Net 30",
        "",
        "This document is a synthetic test fixture. It is not an invoice.",
    ]
    c = _canvas(path, title="Contractor invoice (synthetic)")
    _draw(c, lines)
    c.showPage()
    c.save()
    return path


# ── 3. The retail receipt (PDF and image) ───────────────────────────────────

RECEIPT_ORDER_ID = "ORD-0000-0000"
RECEIPT_DATE = date(2026, 1, 18)
RECEIPT_ITEMS = [("Ergonomic keyboard tray", 1, Decimal("36.00"))]
RECEIPT_GST_RATE = Decimal("0.05")
#: Manitoba's retail sales tax. Named RST on the document because that is the
#: statutory name there; it is a provincial sales tax, so it is never
#: recoverable as a GST/HST input tax credit.
RECEIPT_RST_RATE = Decimal("0.07")
RECEIPT_RST_LABEL = "RST (7%)"


def receipt_totals() -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """``(subtotal, gst, provincial_tax, total)`` for the retail receipt."""
    subtotal = sum((qty * price for _d, qty, price in RECEIPT_ITEMS), Decimal("0"))
    gst = (subtotal * RECEIPT_GST_RATE).quantize(Decimal("0.01"))
    rst = (subtotal * RECEIPT_RST_RATE).quantize(Decimal("0.01"))
    return subtotal, gst, rst, subtotal + gst + rst


def _receipt_lines() -> list[str]:
    subtotal, gst, rst, total = receipt_totals()
    lines = [
        RETAILER,
        "Order Confirmation",
        "",
        f"Order #: {RECEIPT_ORDER_ID}",
        f"Order placed: {RECEIPT_DATE.isoformat()}",
        f"Payment: Card ending in {CARD_LAST4}",
        "",
        "Ship to:",
        COMPANY,
        COMPANY_ADDRESS,
        "",
        "Items                                        Qty        Amount",
    ]
    for desc, qty, price in RECEIPT_ITEMS:
        lines.append(f"{desc:<44}{qty:>4}{_grouped(price * qty):>14}")
    lines += [
        "",
        f"{'Item subtotal':<48}{_grouped(subtotal):>14}",
        f"{'GST (5%)':<48}{_grouped(gst):>14}",
        f"{RECEIPT_RST_LABEL:<48}{_grouped(rst):>14}",
        f"{'Order total (CAD)':<48}{_grouped(total):>14}",
        "",
        "This document is a synthetic test fixture. It is not a receipt.",
    ]
    return lines


def gen_receipt_pdf(out: Path) -> Path:
    """A one-page retail receipt with separate GST and provincial-tax lines.

    The two tax lines are the point: ``core/gst_tax_code.py`` only claims an
    input tax credit on positive evidence of GST or HST, so a provincial sales
    tax must never be mistakable for one. The ship-to address and the rates
    have to agree for that to be a real case — 5% GST plus a separate 7%
    provincial line is Manitoba, and the address says Manitoba.
    """
    path = out / "test_receipt_retailer.pdf"
    c = _canvas(path, title="Retail receipt (synthetic)")
    _draw(c, _receipt_lines())
    c.showPage()
    c.save()
    return path


def gen_receipt_image(out: Path) -> Path:
    """The same receipt as a photographed/scanned image -- no text layer."""
    from PIL import Image, ImageDraw

    path = out / "test_receipt_retailer.png"
    width, height = 620, 460
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    y = 16
    for line in _receipt_lines():
        draw.text((14, y), line, fill=(20, 20, 20))
        y += 16
    img.save(path, format="PNG", optimize=True)
    return path


def gen_scanned_receipt_pdf(out: Path, image: Path) -> Path:
    """An image-only PDF: a scan, with nothing for a text rule to read.

    The image is handed over as an ``ImageReader`` around its *bytes*, never as
    a path. reportlab names the embedded XObject from a digest of whatever it
    was given, so passing ``str(image)`` writes a hash of the absolute path it
    was handed into the PDF: the same fixture then differs between two
    checkouts, and `python scripts/gen_fixtures.py` looks like an uncommitted
    change on every fresh clone. Digesting the bytes instead keeps the output
    byte-identical wherever the repository happens to live.
    """
    import io

    from reportlab.lib.utils import ImageReader

    path = out / "test_receipt_scan.pdf"
    c = _canvas(path, title="Scanned receipt, image only (synthetic)")
    c.drawImage(ImageReader(io.BytesIO(image.read_bytes())), 54, 380, width=460, height=342)
    c.showPage()
    c.save()
    return path


# ── 4. The chequing CSV ─────────────────────────────────────────────────────

CAD_CSV_HEADER = [
    "Account Number", "Transaction Type", "Date Posted",
    "Transaction Amount", "Description",
]

# (date, type, signed amount, description). The wire row is the one that names a
# counterparty, because that is the row the matcher has to reason about.
CAD_CSV_ROWS = [
    ("20260115", "DEBIT", Decimal("-67.20"), f"{TELECOM} PREAUTH PMT"),
    ("20260122", "DEBIT", Decimal("-300.00"), SOFTWARE),
    ("20260125", "CREDIT", Decimal("9600.00"), f"INCOMING WIRE {CLIENT_WIRE}"),
    ("20260128", "DEBIT", Decimal("-4.00"), "MONTHLY PLAN FEE"),
]


def gen_bank_cad_csv(out: Path, *, rows: int, rng: random.Random) -> Path:
    """The chequing CSV signature: ``Transaction Type`` / ``Date Posted`` / ``Transaction Amount``."""
    path = out / "test_bank_cad.csv"
    data = list(CAD_CSV_ROWS)
    for i in range(len(data), rows):
        day = (i % 28) + 1
        amount = Decimal(str(rng.randrange(500, 45000))) / 100
        data.append((
            f"202601{day:02d}",
            "DEBIT",
            -amount,
            rng.choice(EXTRA_MERCHANTS),
        ))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(CAD_CSV_HEADER)
        for date_str, txn_type, amount, description in data:
            w.writerow([CAD_ACCOUNT, txn_type, date_str, _money(amount), description])
    return path


# ── 5. The transfer-platform CSV ────────────────────────────────────────────

WISE_CSV_HEADER = [
    "ID", "Status", "Direction", "Created on", "Finished on",
    "Source amount", "Source currency", "Target amount", "Target currency",
    "Exchange rate", "Fee", "Reference",
]

WISE_CSV_ROWS = [
    ("50000001", "COMPLETED", "DEBIT", "2026-01-12 09:00:00", "2026-01-12 09:06:00",
     "1800.00", "USD", "1800.00", "USD", "1.0", "9.00", f"Contractor payment {CONTRACTOR}"),
    ("50000002", "COMPLETED", "CREDIT", "2026-01-19 15:20:00", "2026-01-19 15:24:00",
     "2400.00", "USD", "2400.00", "USD", "1.0", "0.00", f"Client payment {CLIENT_PLATFORM}"),
    ("50000003", "COMPLETED", "DEBIT", "2026-01-26 11:00:00", "2026-01-26 11:07:00",
     "1000.00", "CAD", "740.00", "USD", "0.74", "4.50", "Transfer to USD chequing"),
    # Not COMPLETED: the parser must drop both, and their short rows must not
    # raise on the way past.
    ("50000004", "PENDING", "DEBIT", "2026-01-28 08:00:00", "",
     "250.00", "CAD", "", "", "", "", "Pending transfer"),
    ("50000005", "CANCELLED", "DEBIT", "2026-01-29 08:00:00", "",
     "120.00", "CAD", "", "", "", "", "Cancelled transfer"),
]


def gen_wise_csv(out: Path, *, rows: int, rng: random.Random) -> Path:
    path = out / "test_wise.csv"
    data = list(WISE_CSV_ROWS)
    for i in range(len(data), rows):
        day = (i % 28) + 1
        amount = Decimal(str(rng.randrange(2500, 300000))) / 100
        data.append((
            f"5000{i:04d}", "COMPLETED", "DEBIT",
            f"2026-02-{day:02d} 10:00:00", f"2026-02-{day:02d} 10:05:00",
            _money(amount), "CAD", _money(amount * Decimal("0.74")), "USD",
            "0.74", "3.75", rng.choice(EXTRA_MERCHANTS).title(),
        ))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(WISE_CSV_HEADER)
        w.writerows(data)
    return path


# ── 5b. The payment-processor CSV ───────────────────────────────────────────
#
# PayPal's "Activity Download" shape. Every merchant, subject line, amount and
# transaction id below is invented. The timezone column says ``UTC`` so the file
# names no region; nothing in the parser depends on the value.

PAYPAL_CSV_HEADER = [
    "Date", "Time", "TimeZone", "Name", "Type", "Status", "Currency",
    "Gross", "Fee", "Net", "Subject", "Transaction ID",
]

#: (day, time, name, type, status, gross, fee, subject, txn_id)
#: Gross is the signed amount; Net is derived as Gross + Fee so the row's own
#: arithmetic closes, which is what a parser change would break first.
PAYPAL_CSV_ROWS = [
    ("01/10/2026", "12:15:00", TELECOM.title(), "Website Payment", "Completed",
     Decimal("-74.20"), Decimal("0.00"), "Business line - January", "PP-0000-0001"),
    ("01/18/2026", "09:42:00", SOFTWARE.title(), "Subscription Payment", "Completed",
     Decimal("-129.00"), Decimal("0.00"), "Team plan - January", "PP-0000-0002"),
    ("01/22/2026", "14:05:00", CLIENT_PLATFORM, "Express Checkout Payment", "Completed",
     Decimal("640.00"), Decimal("-18.56"), "Invoice INV-2026-0007", "PP-0000-0003"),
    # Dropped by the parser: an internal transfer type in _PAYPAL_SKIP_TYPES.
    ("01/24/2026", "14:06:00", "", "General Currency Conversion", "Completed",
     Decimal("-640.00"), Decimal("0.00"), "Conversion to CAD", "PP-0000-0004"),
    # Dropped by the parser: not Completed.
    ("01/27/2026", "08:30:00", RETAILER, "Website Payment", "Pending",
     Decimal("-41.30"), Decimal("0.00"), "Awaiting clearance", "PP-0000-0005"),
]

#: How many rows of PAYPAL_CSV_ROWS survive parse_paypal_csv. Tests re-derive
#: their expectations from the constants above rather than hard-coding values.
PAYPAL_IMPORTED_ROWS = 3


def gen_paypal_csv(out: Path, *, rows: int, rng: random.Random) -> Path:
    path = out / "test_paypal.csv"
    data = list(PAYPAL_CSV_ROWS)
    for i in range(len(data), rows):
        day = (i % 28) + 1
        gross = -(Decimal(str(rng.randrange(500, 40000))) / 100)
        data.append((
            f"02/{day:02d}/2026", "10:00:00", rng.choice(EXTRA_MERCHANTS).title(),
            "Website Payment", "Completed", gross, Decimal("0.00"),
            "Business purchase", f"PP-0000-{i + 1:04d}",
        ))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(PAYPAL_CSV_HEADER)
        for day, clock, name, kind, status, gross, fee, subject, txn_id in data:
            w.writerow([
                day, clock, "UTC", name, kind, status, "USD",
                _money(gross), _money(fee), _money(gross + fee), subject, txn_id,
            ])
    return path


# ── 5c. The file the importer must refuse ───────────────────────────────────

def gen_unsupported_txt(out: Path) -> Path:
    """A file whose first line matches no CSV signature, for the error path."""
    path = out / "test_unsupported.txt"
    path.write_text("test unsupported file\n", encoding="utf-8", newline="")
    return path


# ── 6. The peer-benchmark sample ────────────────────────────────────────────
#
# The same JSON shape as the ``config/ised_benchmarks.json`` that
# ``scripts/ingest_ised_benchmarks.py`` builds (and which is gitignored because
# it is far too large to check in), cut down to a few cells. The NAICS codes are
# the public Canadian taxonomy; every ratio below is invented and must not be
# read as a real industry figure.

BENCHMARK_CELLS = [
    # (naics, label, band, gross_margin, net_margin, businesses, pct_profitable, lines)
    ("5414", "Specialized design services", "30K-5M",
     0.720, 0.160, 1450, 0.770,
     {"salaries_wages": 0.290, "subcontracts": 0.110, "professional_fees": 0.052,
      "rent": 0.038, "advertising": 0.036}),
    ("5414", "Specialized design services", "5M-20M",
     0.690, 0.130, 260, 0.820,
     {"salaries_wages": 0.330, "subcontracts": 0.095, "professional_fees": 0.044,
      "rent": 0.031, "advertising": 0.028}),
    ("5416", "Management, scientific and technical consulting services", "30K-5M",
     0.760, 0.190, 980, 0.800,
     {"salaries_wages": 0.265, "subcontracts": 0.088, "professional_fees": 0.064,
      "rent": 0.033, "advertising": 0.022}),
    ("2361", "Residential building construction", "30K-5M",
     0.380, 0.090, 640, 0.730,
     {"cost_of_sales": 0.620, "subcontracts": 0.480, "salaries_wages": 0.095,
      "rent": 0.014, "advertising": 0.011}),
]


def gen_ised_benchmarks_sample(out: Path) -> Path:
    path = out / "ised_benchmarks_sample.json"
    cells = {}
    for naics, label, band, gross, net, count, profitable, lines in BENCHMARK_CELLS:
        cells[f"{naics}|{band}|all"] = {
            "naics": naics,
            "naics_label": label,
            "revenue_band": band,
            "cohort": "all",
            "business_count": count,
            "sample_size": count,
            "quality": "A",
            "cohort_revenue_low": None,
            "cohort_revenue_high": None,
            "pct_profitable": profitable,
            "ratios": {"gross_margin": gross, "net_profit_margin": net},
            "source_url": None,
            "lines": {"operating_revenue": 1.0, **lines},
        }
    payload = {
        "meta": {
            "source": "SYNTHETIC -- generated by scripts/gen_fixtures.py",
            "synthetic": True,
            "source_year": 2026,
            "geography": "Nowhere (invented)",
            "notice": (
                "Invented ratios in the shape of config/ised_benchmarks.json. "
                "Not industry data. Run scripts/ingest_ised_benchmarks.py for the real table."
            ),
            "revenue_bands": sorted({c[2] for c in BENCHMARK_CELLS}),
            "quartiles": ["all"],
            "cohorts": ["all"],
            "min_business_count": 20,
            "revenue_floor_cad": 30000,
        },
        "cells": cells,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# ── 6b. The peer-benchmark cohort sample ────────────────────────────────────
#
# A second, richer benchmark sample: one industry carried across the cohort
# cells the reasonableness engine actually selects between -- the whole-band
# ``all`` cell, four by-revenue cohorts and four by-profit-margin cohorts.
# Every figure is invented. NAICS 5414 is a public taxonomy code, not a claim
# about that industry; no number here is industry data, and no Open Government
# Licence data file ships with this repository.
#
# The shape mirrors what ``scripts/ingest_ised_benchmarks.py`` emits, because
# that is what the engine has to read:
#
#   * the ``all`` cell carries a per-line {pct_of_revenue, q1, median, q3}
#     band, except where a band would be unreliable -- there the line is a
#     bare float, exactly as the ingest's monotonicity gate writes it;
#   * ``rev_q*`` / ``pm_q*`` are point cells: every line is a bare float;
#   * ``rev_q*`` carry the revenue range that defines the cohort, ``pm_q*``
#     do not (they are margin cohorts, not size cohorts);
#   * one thin ``5M-20M`` cell sits under ``min_business_count`` so the
#     density gate has something to suppress.
#
# Two shape choices are deliberate, so the tests that read this file stay
# meaningful rather than lucky:
#
#   * the four revenue cohorts leave the $180K-$450K window uncovered, so the
#     "no size cohort brackets this revenue" fallback to the whole-band cell
#     has a case to exercise;
#   * spending falls as profitability rises across ``pm_q1``..``pm_q4``, so a
#     comparison against the most-profitable cohort has a direction to it.

BENCHMARK_COHORT_NAICS = "5414"
BENCHMARK_COHORT_LABEL = "Specialized design services"
#: Invented count of businesses behind the whole-band cell. The four revenue
#: cohorts split it evenly, the way a quartile split does.
BENCHMARK_COHORT_BUSINESSES = 38_600
#: Invented count for the thin high-revenue cell: below ``min_business_count``
#: on purpose, so reading it is suppressed rather than compared.
BENCHMARK_THIN_BUSINESSES = 11
#: A reserved, non-resolvable example domain. These cells have no real source
#: to cite, and citing a government page for invented figures would be a lie.
BENCHMARK_SOURCE_URL = f"https://example.invalid/peer-benchmarks/{BENCHMARK_COHORT_NAICS}"

#: cohort -> (revenue_low, revenue_high) in dollars. ``None`` means the cohort
#: is not defined by revenue at all.
BENCHMARK_REV_RANGES = {
    "rev_q1": (30_000, 180_000),
    "rev_q2": (450_000, 1_200_000),
    "rev_q3": (1_200_000, 2_800_000),
    "rev_q4": (2_800_000, 5_000_000),
}

#: The whole-band cell's lines. A 4-tuple is a band (pct, q1, median, q3); a
#: bare float is a line with no reliable band, which the engine reads as a
#: central value only and never widens into a fabricated range.
BENCHMARK_ALL_LINES: dict[str, object] = {
    "operating_revenue": 1.0,
    "salaries_wages": (0.29, 0.22, 0.29, 0.38),
    "employee_benefits": (0.035, 0.018, 0.035, 0.058),
    "subcontracts": (0.11, 0.07, 0.11, 0.18),
    "professional_fees": (0.052, 0.024, 0.052, 0.086),
    "rent": (0.038, 0.016, 0.038, 0.062),
    "cca_depreciation": (0.024, 0.008, 0.024, 0.041),
    "advertising": 0.036,
}

#: cohort -> (gross_margin, net_profit_margin, lines). Point cells only.
BENCHMARK_COHORT_CELLS: dict[str, tuple[float, float, dict[str, float]]] = {
    "rev_q1": (0.70, 0.11, {
        "salaries_wages": 0.24, "employee_benefits": 0.026, "subcontracts": 0.145,
        "professional_fees": 0.058, "advertising": 0.041, "rent": 0.044,
        "cca_depreciation": 0.026,
    }),
    "rev_q2": (0.71, 0.14, {
        "salaries_wages": 0.27, "employee_benefits": 0.032, "subcontracts": 0.125,
        "professional_fees": 0.053, "advertising": 0.037, "rent": 0.039,
        "cca_depreciation": 0.024,
    }),
    "rev_q3": (0.73, 0.17, {
        "salaries_wages": 0.31, "employee_benefits": 0.037, "subcontracts": 0.105,
        "professional_fees": 0.049, "advertising": 0.033, "rent": 0.034,
        "cca_depreciation": 0.021,
    }),
    "rev_q4": (0.75, 0.20, {
        "salaries_wages": 0.34, "employee_benefits": 0.041, "subcontracts": 0.088,
        "professional_fees": 0.045, "advertising": 0.029, "rent": 0.029,
        "cca_depreciation": 0.019,
    }),
    "pm_q1": (0.66, 0.015, {
        "salaries_wages": 0.36, "employee_benefits": 0.040, "subcontracts": 0.155,
        "professional_fees": 0.057, "advertising": 0.062, "rent": 0.048,
    }),
    "pm_q2": (0.69, 0.095, {
        "salaries_wages": 0.33, "employee_benefits": 0.036, "subcontracts": 0.135,
        "professional_fees": 0.052, "advertising": 0.047, "rent": 0.041,
    }),
    "pm_q3": (0.72, 0.185, {
        "salaries_wages": 0.30, "employee_benefits": 0.032, "subcontracts": 0.115,
        "professional_fees": 0.048, "advertising": 0.035, "rent": 0.035,
    }),
    "pm_q4": (0.76, 0.285, {
        "salaries_wages": 0.26, "employee_benefits": 0.027, "subcontracts": 0.092,
        "professional_fees": 0.043, "advertising": 0.024, "rent": 0.028,
    }),
}

#: The thin high-revenue cell. One banded line and one bare one, so the
#: suppression path is exercised on both shapes.
BENCHMARK_THIN_LINES: dict[str, object] = {
    "operating_revenue": 1.0,
    "salaries_wages": (0.33, 0.25, 0.33, 0.42),
    "subcontracts": 0.098,
}


def _benchmark_line(value: object) -> object:
    """A 4-tuple becomes a band dict; anything else passes through as-is."""
    if isinstance(value, tuple):
        pct, q1, median, q3 = value
        return {"pct_of_revenue": pct, "q1": q1, "median": median, "q3": q3}
    return value


def _benchmark_cell(
    *, band: str, cohort: str, business_count: int, quality: str,
    gross: float, net: float, lines: dict[str, object],
    pct_profitable: float | None = None,
    revenue_low: float | None = None, revenue_high: float | None = None,
) -> dict:
    return {
        "naics": BENCHMARK_COHORT_NAICS,
        "naics_label": BENCHMARK_COHORT_LABEL,
        "revenue_band": band,
        "cohort": cohort,
        "business_count": business_count,
        "sample_size": business_count,
        "quality": quality,
        "cohort_revenue_low": revenue_low,
        "cohort_revenue_high": revenue_high,
        "pct_profitable": pct_profitable,
        "ratios": {"gross_margin": gross, "net_profit_margin": net},
        "source_url": BENCHMARK_SOURCE_URL,
        "lines": {k: _benchmark_line(v) for k, v in lines.items()},
    }


def gen_benchmark_cohorts_sample(out: Path) -> Path:
    """One industry across every cohort the engine chooses between."""
    path = out / "ised_benchmarks_cohorts_sample.json"
    per_cohort = round(BENCHMARK_COHORT_BUSINESSES / 4)
    naics, band = BENCHMARK_COHORT_NAICS, "30K-5M"

    cells = {
        f"{naics}|{band}|all": _benchmark_cell(
            band=band, cohort="all", business_count=BENCHMARK_COHORT_BUSINESSES,
            quality="A", gross=0.72, net=0.16, pct_profitable=0.77,
            lines=BENCHMARK_ALL_LINES,
        )
    }
    for cohort, (gross, net, lines) in BENCHMARK_COHORT_CELLS.items():
        low, high = BENCHMARK_REV_RANGES.get(cohort, (None, None))
        cells[f"{naics}|{band}|{cohort}"] = _benchmark_cell(
            band=band, cohort=cohort, business_count=per_cohort,
            quality="A", gross=gross, net=net,
            lines={"operating_revenue": 1.0, **lines},
            revenue_low=low, revenue_high=high,
        )
    cells[f"{naics}|5M-20M|all"] = _benchmark_cell(
        band="5M-20M", cohort="all", business_count=BENCHMARK_THIN_BUSINESSES,
        quality="E", gross=0.69, net=0.13, pct_profitable=0.88,
        lines=BENCHMARK_THIN_LINES,
    )

    payload = {
        "meta": {
            "source": "SYNTHETIC -- generated by scripts/gen_fixtures.py",
            "synthetic": True,
            "source_year": 2026,
            "geography": "Canada (national)",
            "notice": (
                "Invented peer figures in the shape of config/ised_benchmarks.json. "
                "Not industry data, and not a sample of any published release: no "
                "Open Government Licence data file ships with this repository. Run "
                "scripts/ingest_ised_benchmarks.py to download and build the real table."
            ),
            "shape_note": (
                "The revenue cohorts deliberately leave the $180K-$450K window "
                "uncovered, so the fallback from a size cohort to the whole-band "
                "cell has a case to exercise."
            ),
            "revenue_bands": ["30K-5M", "5M-20M"],
            "cohorts": ["all", "rev_q1", "rev_q2", "rev_q3", "rev_q4",
                        "pm_q1", "pm_q2", "pm_q3", "pm_q4"],
            "quartiles": ["all"],
            "min_business_count": 20,
            "revenue_floor_cad": 30000,
        },
        "cells": cells,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# ── 7. The ground-truth workbook ────────────────────────────────────────────

#: The category column carries canonical names from ``config/ised_categories.py``
#: -- the closed list the app itself stores -- so the workbook reader is
#: exercised against the vocabulary a comparison actually has to agree with.
XLSX_CAD_ROWS = [
    (20260115, "DEBIT", Decimal("-67.20"), f"{TELECOM} PREAUTH PMT",
     "Utilities and telephone/telecommunication"),
    (20260122, "DEBIT", Decimal("-300.00"), SOFTWARE, "Software and IT"),
    (20260125, "CREDIT", Decimal("9600.00"), f"INCOMING WIRE {CLIENT_WIRE}",
     "Sales of goods and services"),
    (20260128, "DEBIT", Decimal("-4.00"), "MONTHLY PLAN FEE", "Interest and bank charges"),
]
XLSX_USD_ROWS = [
    (20260112, "DEBIT", Decimal("-1800.00"), f"WISE CONTRACTOR {CONTRACTOR}",
     "Purchases, materials and sub-contracts"),
    (20260119, "CREDIT", Decimal("2400.00"), f"WISE CLIENT {CLIENT_PLATFORM}",
     "Sales of goods and services"),
]
XLSX_CC_ROWS = [
    (20260118, 20260119, Decimal("40.32"), f"{RETAILER} MARKETPLACE", "Office supplies"),
    (20260124, 20260125, Decimal("-150.00"), "PAYMENT THANK YOU", "Credit card payment"),
]


def gen_ledger_xlsx(out: Path, *, rows: int, rng: random.Random) -> Path:
    """A hand-kept ground-truth workbook for ``scripts/qa/xlsx_parser.py``.

    The awkward parts are deliberate, because they are what the parser exists to
    survive: two currency sections in one sheet separated by a SUM row, a
    metadata timestamp above the credit-card header, and a credit-card sheet
    whose sign convention is the opposite of the chequing one.
    """
    import openpyxl

    path = out / "ledger-2026-01.xlsx"
    wb = openpyxl.Workbook()

    main = wb.active
    main.title = "CAD and USD Jan2026"
    main.append(["Generated by scripts/gen_fixtures.py -- synthetic"])
    main.append([])
    main.append(["Item #", "Transaction Type", "Date Posted", "Transaction Amount CAD",
                 "Description", "Category", "Invoice/Cheque", "Note"])
    cad = list(XLSX_CAD_ROWS)
    for i in range(len(cad), max(rows, len(cad))):
        day = (i % 28) + 1
        cad.append((
            int(f"202601{day:02d}"), "DEBIT",
            -(Decimal(str(rng.randrange(500, 45000))) / 100),
            rng.choice(EXTRA_MERCHANTS), "Other indirect expenses",
        ))
    for n, (d, t, amount, desc, cat) in enumerate(cad, start=1):
        main.append([n, t, d, float(amount), desc, cat, "", ""])
    main.append([])
    main.append(["SUM", "", "", float(sum(r[2] for r in cad)), "", "", "", ""])
    main.append([])
    main.append(["Item #", "Transaction Type", "Date Posted", "Transaction Amount USD",
                 "Description", "Category", "Invoice/Cheque", "Note"])
    for n, (d, t, amount, desc, cat) in enumerate(XLSX_USD_ROWS, start=1):
        main.append([n, t, d, float(amount), desc, cat, "", ""])

    card = wb.create_sheet(f"Credit Card {CARD_LAST4} Jan2026")
    card.append(["Generated by scripts/gen_fixtures.py -- synthetic"])
    card.append([])
    card.append(["Item #", "Card #", "Transaction Date", "Posting Date",
                 "Transaction Amount", "Description", "Category", "Invoice", "Note"])
    for n, (txn_d, post_d, amount, desc, cat) in enumerate(XLSX_CC_ROWS, start=1):
        card.append([n, CARD_LAST4, txn_d, post_d, float(amount), desc, cat, "", ""])

    fixed = datetime(2026, 1, 31, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    wb.properties.created = fixed
    wb.properties.modified = fixed
    wb.properties.creator = "scripts/gen_fixtures.py (synthetic)"
    wb.properties.lastModifiedBy = "scripts/gen_fixtures.py (synthetic)"
    wb.save(path)
    _rewrite_zip_deterministic(path)
    return path


# ── Entry point ─────────────────────────────────────────────────────────────

def generate_all(out: Path, *, seed: int = DEFAULT_SEED, rows: int = 0) -> list[Path]:
    """Write every fixture into ``out`` and return the paths, in order."""
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    written = [
        gen_bank_statement_pdf(out),
        gen_invoice_pdf(out),
        gen_receipt_pdf(out),
    ]
    image = gen_receipt_image(out)
    written.append(image)
    written.append(gen_scanned_receipt_pdf(out, image))
    written.append(gen_bank_cad_csv(out, rows=rows, rng=rng))
    written.append(gen_wise_csv(out, rows=rows, rng=rng))
    written.append(gen_paypal_csv(out, rows=rows, rng=rng))
    written.append(gen_unsupported_txt(out))
    written.append(gen_ised_benchmarks_sample(out))
    written.append(gen_benchmark_cohorts_sample(out))
    written.append(gen_ledger_xlsx(out, rows=rows, rng=rng))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"random seed (default: {DEFAULT_SEED})")
    parser.add_argument("--rows", type=int, default=0,
                        help="minimum rows in the CSV / XLSX fixtures; the checked-in "
                             "set is the small canonical one (default: 0)")
    args = parser.parse_args()

    for path in generate_all(args.out, seed=args.seed, rows=args.rows):
        print(f"wrote {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}"
              f"  ({path.stat().st_size:,} bytes)")
    print("\nAll fixtures are synthetic: seed "
          f"{args.seed}, invented names and amounts, no real records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

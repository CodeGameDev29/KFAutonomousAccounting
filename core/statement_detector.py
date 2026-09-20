"""Is this PDF a bank statement? A cheap, precise answer from the text layer.

This is what keeps the Transactions "auto" upload zone from routing on file
extension alone. Without it every PDF in a dropped folder goes to
``POST /api/receipts/upload``, and a month of bank statement comes back as one
receipt — one vendor, one total, every transaction on it thrown away.

The detector runs **inside the upload request**, which has to answer in well
under a second whether the user dropped one file or a whole folder of them. So
it is deliberately the cheapest thing that can work:

* text layer only — no LLM, no page rendering, no network, no database;
* at most ``MAX_PAGES_SCANNED`` pages, not the whole document;
* pure: a path in, evidence out, nothing written anywhere.

It is also deliberately **strict**. A false positive sends a receipt down the
statement pipeline, which is worse than a miss: a missed statement leaves the
routing where it started, while a misrouted receipt loses a document the user
can see in front of them. So the rule is a conjunction of a usable text layer
and two independent statement signals, and nothing here fires on an invoice's
"Subtotal / GST / TOTAL DUE" shape.

**This is not** ``core.pdf_statement_parser.is_bank_statement_pdf``, and does not
replace it. That one matches two of {transaction, balance, deposit, withdrawal,
debit, credit, account, statement, bmo, wise} — an invoice matches it — and it is
used for a different job on purpose: the permissive "you dropped a receipt in the
statement box" guard on ``POST /api/transactions/upload-statement``, where a
stricter rule would start rejecting statements the user has explicitly declared.
Loose is right there; strict is right here.

The evidence comes back with the verdict because a misfire has to be
debuggable — the API logs it and seeds it onto the job row, so "why did this
receipt end up in Transactions?" is answerable from the server log and from
``GET /api/jobs`` without re-running anything.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# How much of the document to look at. A statement announces itself on page one;
# the row-count signal wants a page or two of the table behind it. Capping the
# scan at three pages of pdfplumber text is what keeps this inside the request.
MAX_PAGES_SCANNED = 3

# Below this there is no text layer worth reasoning about — a scanned statement,
# a shredded file, an image-only PDF. Those answer False and take the receipt
# path, which is the routing default.
MIN_TEXT_CHARS = 120

# How many date-amount-balance rows make a ledger. Ten is well past what any
# receipt or invoice header block produces and well under a month of statement
# rows.
MIN_LEDGER_ROWS = 10

# Two of the five. One signal alone is a coincidence: "closing balance" shows up in a
# loan payoff letter, and a line with two dates shows up on an invoice with
# terms. Two independent ones is a statement.
REQUIRED_SIGNALS = 2

SIGNAL_STATEMENT_PERIOD = "statement_period"
SIGNAL_OPENING_BALANCE = "opening_balance"
SIGNAL_CLOSING_BALANCE = "closing_balance"
SIGNAL_DATE_RANGE = "date_range"
SIGNAL_LEDGER_ROWS = "ledger_rows"

ALL_SIGNALS: tuple[str, ...] = (
    SIGNAL_STATEMENT_PERIOD,
    SIGNAL_OPENING_BALANCE,
    SIGNAL_CLOSING_BALANCE,
    SIGNAL_DATE_RANGE,
    SIGNAL_LEDGER_ROWS,
)

# --- Signal 1: the statement-period phrase -----------------------------------
#
# "statement period" is the canonical wording, but plenty of business statements
# say "For the period ending February 27, 2026" and never put the two words
# together, and credit-card statements head with "Statement date" instead.
# All three are the same claim: this document covers a
# window of an account's life, which is what a receipt or an invoice never says.
# ``\s*`` everywhere because pdfplumber cheerfully emits "Openingbalance".
_RE_STATEMENT_PERIOD = re.compile(
    r"\bstatement\s*period"
    r"|\bstatement\s*date"
    r"|\bperiod\s*(?:ending|ended|covered)",
    re.IGNORECASE,
)

# --- Signals 2 and 3: the balances that bracket a statement ------------------
#
# The BMO export format labels that line "closing totals" on business
# statements, so both wordings count as the closing signal.
_RE_OPENING_BALANCE = re.compile(r"\bopening\s*balance", re.IGNORECASE)
_RE_CLOSING_BALANCE = re.compile(
    r"\bclosing\s*balance|\bclosing\s*totals", re.IGNORECASE
)

# --- Dates ------------------------------------------------------------------
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"

# The date shapes Canadian bank statements use, plus the run-together
# "Feb02" pdfplumber produces when the column is narrow.
_DATE = (
    r"(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"                 # 2000-01-31
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"              # 01/31/2000
    rf"|{_MONTH}\s*\d{{1,2}}(?:\s*,?\s*\d{{4}})?"    # January 31, 2000 / Feb02
    rf"|\d{{1,2}}\s*[-/]?\s*{_MONTH}(?:\s*,?\s*\d{{4}})?"  # 31 Jan 2000
    r")"
)
_RE_DATE = re.compile(_DATE, re.IGNORECASE)
_RE_DATE_AT_START = re.compile(rf"^{_DATE}", re.IGNORECASE)

# What has to sit between two dates for them to be a range rather than two
# unrelated fields ("Invoice date: ... Payment due: ..." is not a range).
_RE_RANGE_JOINER = re.compile(
    r"^[\s:]*(?:to|through|thru|till|until|and|[-‐‑‒–—―])[\s:]*$",
    re.IGNORECASE,
)

# A line already carrying "period" is saying what its two dates are.
_RE_PERIOD_WORD = re.compile(r"\bperiod\b", re.IGNORECASE)

# --- Amounts ----------------------------------------------------------------
#
# Two decimal places, required. The trailing \b is what stops "AT1.2345" (a bank
# FX rate) from reading as the amount "1.23".
_RE_MONEY = re.compile(r"-?\$?\s?\d{1,3}(?:,\d{3})+\.\d{2}\b|-?\$?\s?\d+\.\d{2}\b")

_RE_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class StatementDetection:
    """The verdict plus why — everything needed to argue with it later."""

    is_statement: bool
    signals: tuple[str, ...]
    row_count: int
    text_chars: int
    pages_scanned: int
    reason: str

    def __bool__(self) -> bool:  # so `if detect_bank_statement(p):` reads right
        return self.is_statement

    def as_dict(self) -> dict:
        """JSON-safe evidence, for a log line or a job's ``result``."""
        return {
            "is_statement": self.is_statement,
            "signals": list(self.signals),
            "row_count": self.row_count,
            "text_chars": self.text_chars,
            "pages_scanned": self.pages_scanned,
            "reason": self.reason,
        }

    @property
    def summary(self) -> str:
        """One line for the server error log."""
        return (
            f"statement={self.is_statement} "
            f"signals={'+'.join(self.signals) or 'none'} "
            f"rows={self.row_count} chars={self.text_chars} "
            f"pages={self.pages_scanned} ({self.reason})"
        )


def extract_leading_text(file_path: Path | str, max_pages: int = MAX_PAGES_SCANNED) -> tuple[str, int]:
    """Text from the first ``max_pages`` pages, and how many were readable.

    Never raises: an unreadable or non-PDF file is ``("", 0)``. A whole-document
    read would be wrong here as well as slow — a 40-page statement costs the same
    verdict as a 2-page one.
    """
    parts: list[str] = []
    pages_scanned = 0
    try:
        import pdfplumber

        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages[:max_pages]:
                pages_scanned += 1
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                if page_text:
                    parts.append(page_text)
    except Exception as exc:
        logger.debug("statement detector could not read %s: %s", file_path, exc)
        return "", pages_scanned
    return "\n".join(parts), pages_scanned


def _normalized_lines(text: str) -> list[str]:
    """One entry per line, inner whitespace collapsed, blanks dropped."""
    out = []
    for raw in text.splitlines():
        line = _RE_WHITESPACE.sub(" ", raw).strip()
        if line:
            out.append(line)
    return out


def _line_has_date_range(line: str) -> bool:
    """Does this line carry a date *range* (not just two dates)?"""
    spans = [(m.start(), m.end()) for m in _RE_DATE.finditer(line)]
    if len(spans) < 2:
        return False
    # "Statement period: 2000-01-01 2000-01-31" — the word does the joining.
    if _RE_PERIOD_WORD.search(line):
        return True
    return any(
        _RE_RANGE_JOINER.match(line[spans[i][1]:spans[i + 1][0]])
        for i in range(len(spans) - 1)
    )


def _count_ledger_rows(lines: list[str]) -> int:
    """Lines that look like a transaction: a date, then two or more amounts.

    Two amounts is the point — an amount and a running balance. One amount is an
    invoice line item or a receipt total; a date at the front of the line is what
    a ledger has and a document's header block does not.
    """
    rows = 0
    for line in lines:
        if not _RE_DATE_AT_START.match(line):
            continue
        if len(_RE_MONEY.findall(line)) >= 2:
            rows += 1
    return rows


def detect_statement_text(text: str, *, pages_scanned: int = 0) -> StatementDetection:
    """The rule itself, over already-extracted text. No I/O — the testable half."""
    text_chars = len(text.strip())
    if text_chars < MIN_TEXT_CHARS:
        return StatementDetection(
            is_statement=False,
            signals=(),
            row_count=0,
            text_chars=text_chars,
            pages_scanned=pages_scanned,
            reason="no usable text layer",
        )

    lines = _normalized_lines(text)
    # Phrase searches run over the whole text with every whitespace run
    # collapsed, so a phrase split across a line break still matches.
    flat = _RE_WHITESPACE.sub(" ", text)

    row_count = _count_ledger_rows(lines)

    fired: list[str] = []
    if _RE_STATEMENT_PERIOD.search(flat):
        fired.append(SIGNAL_STATEMENT_PERIOD)
    if _RE_OPENING_BALANCE.search(flat):
        fired.append(SIGNAL_OPENING_BALANCE)
    if _RE_CLOSING_BALANCE.search(flat):
        fired.append(SIGNAL_CLOSING_BALANCE)
    if any(_line_has_date_range(line) for line in lines):
        fired.append(SIGNAL_DATE_RANGE)
    if row_count >= MIN_LEDGER_ROWS:
        fired.append(SIGNAL_LEDGER_ROWS)

    is_statement = len(fired) >= REQUIRED_SIGNALS
    reason = (
        f"{len(fired)} of {len(ALL_SIGNALS)} signals"
        f" (need {REQUIRED_SIGNALS})"
    )
    return StatementDetection(
        is_statement=is_statement,
        signals=tuple(fired),
        row_count=row_count,
        text_chars=text_chars,
        pages_scanned=pages_scanned,
        reason=reason,
    )


def detect_bank_statement(file_path: Path | str) -> StatementDetection:
    """Does this PDF look like a bank statement? Verdict plus evidence.

    Never raises. A corrupt, encrypted, image-only or not-actually-a-PDF file
    answers ``is_statement=False`` with ``reason="no usable text layer"``.
    """
    text, pages_scanned = extract_leading_text(file_path)
    return detect_statement_text(text, pages_scanned=pages_scanned)


def looks_like_bank_statement(file_path: Path | str) -> bool:
    """``detect_bank_statement`` when only the boolean is wanted."""
    return detect_bank_statement(file_path).is_statement

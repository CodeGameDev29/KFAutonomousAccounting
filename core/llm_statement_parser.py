"""LLM-based generic bank statement parser.

One generic extraction pipeline for any Canadian bank PDF, rather than a
hand-coded regex parser per institution. A locally hosted model is the primary
parser, so a statement never has to leave the machine it was uploaded to; hosted
models are optional fallbacks that only engage when their key is non-empty.

Pipeline:
  PDF → pdfplumber text-density gate
      ├─ ≥85% text pages → text mode
      └─ <85% (scanned)  → vision mode at 150 DPI
  Providers in order: local → Gemini (if keyed) → OpenAI (if keyed)
  Deterministic balance validator (±$0.02 tolerance)
  Fails? → one retry with BALANCE_CORRECTION_PROMPT on the same provider
  Returns StatementParseResult with confidence tier (HIGH / MEDIUM / LOW)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from core.extraction import (
    _TRANSIENT_RETRY_BACKOFF_S,
    LLM_CALL_TIMEOUT_S,
    _finish_reason,
    _PageProgress,
)
from core.llm_errors import (
    LOCAL_LLM_UNREACHABLE_MESSAGE,
    LocalLLMUnreachableError,
    is_local_endpoint_down,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TransactionRow(BaseModel):
    """A single transaction extracted from a bank statement."""

    date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Transaction date in YYYY-MM-DD format",
    )
    amount: str = Field(
        ...,
        description="Absolute decimal string, e.g. '1234.56'. Sign conveyed by `type`.",
    )
    description: str = Field(..., description="Original description from statement")
    type: str = Field(
        ...,
        pattern=r"^(debit|credit)$",
        description=(
            "'debit' = money out, 'credit' = money in. Not emitted by the model: "
            "_normalize_statement_payload derives it from the sign of the "
            "compact row's `amt`, which is where the prompt puts the direction."
        ),
    )
    running_balance: Optional[str] = Field(
        None,
        description="Balance after this transaction, if visible on statement",
    )
    balance_delta: Optional[str] = Field(
        None,
        description="Signed delta from prior running balance, if visible",
    )
    external_id: Optional[str] = Field(
        None,
        description="Bank-assigned transaction reference (e.g. Wise transactionNumber)",
    )


class StatementMetadata(BaseModel):
    """Statement-level metadata extracted from the header/footer of the PDF."""

    institution: str = Field(
        ...,
        description="Bank or platform name, e.g. 'BMO', 'RBC', 'TD', 'Wise', 'PayPal'",
    )
    account_type: str = Field(
        ...,
        description=(
            "One of: CHECKING, SAVINGS, CREDIT_CARD, FX_MULTI_CURRENCY, MERCHANT, OTHER"
        ),
    )
    account_number: Optional[str] = Field(
        None,
        description="Last 4 digits of account number if visible, else null",
    )
    statement_period_start: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Start date of statement period in YYYY-MM-DD",
    )
    statement_period_end: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="End date of statement period in YYYY-MM-DD",
    )
    opening_balance: str = Field(
        ...,
        description="Opening balance as decimal string, e.g. '1500.00'",
    )
    closing_balance: str = Field(
        ...,
        description="Closing balance as decimal string, e.g. '2300.00'",
    )
    currency: str = Field(
        ...,
        pattern=r"^(CAD|USD|EUR|GBP|CNY)$",
        description="Primary currency of the statement",
    )
    multi_account: bool = Field(
        False,
        description="True if the PDF covers more than one sub-account or currency section",
    )
    page_count: int = Field(
        0,
        description=(
            "Total pages in the statement PDF. NOT asked of the model: the "
            "count is read from the file itself, and parse_statement_with_llm "
            "stamps it over whatever arrives here."
        ),
    )


class BankStatementExtraction(BaseModel):
    """Full extraction output from an LLM call — includes transactions, metadata,
    and an optional chain-of-thought reasoning field (discarded after validation)."""

    transactions: list[TransactionRow] = Field(default_factory=list)
    metadata: StatementMetadata
    reasoning: Optional[str] = Field(
        None,
        description="Chain-of-thought audit trail. Discarded after validation.",
    )


class StatementParseResult(BaseModel):
    """Final result returned to callers after validation and confidence scoring."""

    transactions: list[TransactionRow]
    metadata: StatementMetadata
    confidence: str = Field(..., pattern=r"^(HIGH|MEDIUM|LOW)$")
    flags: list[str] = Field(default_factory=list)
    parse_method: str = Field(
        ...,
        pattern=(
            r"^(local_text|local_vision|gemini_text|gemini_vision"
            r"|openai_fallback|legacy_pdf_to_csv)$"
        ),
    )
    balance_matches: bool
    balance_difference: str  # Decimal string
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)


class StatementParseError(Exception):
    """Raised when bank statement parsing fails across all LLM paths."""
    pass


class StatementLocalEndpointDown(StatementParseError, LocalLLMUnreachableError):
    """The local endpoint was the only rung, and it never answered.

    Subclasses both so existing `except StatementParseError` handlers keep
    working while the upload endpoint can still recognise an outage and answer
    503 with the honest message instead of 422 "unable to parse".
    """

    def __init__(self, message: str = LOCAL_LLM_UNREACHABLE_MESSAGE) -> None:
        LocalLLMUnreachableError.__init__(self, message)


# Upper bound on pages rendered to images for vision parsing. Beyond this a
# scanned PDF risks OOM-killing the worker and overflows the LLM context, so the
# parser fails fast with a clean error instead. Text-mode parsing
# (machine-generated statements) is unaffected by this cap.
_MAX_VISION_PAGES = 50

# Vision-mode page batching, shared by every provider. Up to this many pages go
# in one call; beyond it the statement is split into non-overlapping batches and
# the rows deduplicated across boundaries.
_MAX_SINGLE_BATCH_PAGES = 15
_VISION_BATCH_SIZE = 10

# Render DPI for scanned statements. Vision models downsample an image to a
# fixed tile budget before reading it, so rendering far above this buys no
# detail the model can see and costs peak render memory.
DEFAULT_STATEMENT_DPI = 150

# ── Output budget ───────────────────────────────────────────────────────────
# Sized to the compact row shape. A schema that emits every key on every row,
# nulls included, plus a multi-sentence `reasoning`, costs far more output
# tokens per row than the compact shape this prompt asks for. 25 rows/page is
# the upper bound for a dense Canadian statement page, so the budget is
# "base + 40 per expected row", floored so a one-page statement still has room
# for a long correction. A truncated answer is detected via finish_reason and
# re-asked once at double the budget rather than parsed half-complete.
STATEMENT_OUTPUT_BASE_TOKENS = 256
STATEMENT_OUTPUT_TOKENS_PER_ROW = 40
STATEMENT_EXPECTED_ROWS_PER_PAGE = 25
STATEMENT_MIN_OUTPUT_TOKENS = 1024


def statement_max_output_tokens(page_count: int) -> int:
    """Output-token budget for one statement call covering `page_count` pages."""
    pages = max(1, int(page_count or 1))
    expected_rows = pages * STATEMENT_EXPECTED_ROWS_PER_PAGE
    return max(
        STATEMENT_MIN_OUTPUT_TOKENS,
        STATEMENT_OUTPUT_BASE_TOKENS + STATEMENT_OUTPUT_TOKENS_PER_ROW * expected_rows,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BANK_STATEMENT_SYSTEM_PROMPT = """\
You are an expert financial document parser specializing in Canadian bank statements.
Your job is to extract every transaction from a bank statement PDF and return the
result as a single JSON object that matches the schema described below.

═══════════════════════════════════════════════════════
1. OUTPUT SHAPE — COMPACT JSON. READ THE KEY LEGEND ONCE.
═══════════════════════════════════════════════════════

Return ONE JSON object: statement-level "metadata" ONCE, then every transaction as
a compact row in "transactions". Transaction keys are ABBREVIATED:

   "d"    date, YYYY-MM-DD (see Date Normalization below)
   "desc" description exactly as printed on the statement row
   "amt"  SIGNED amount, as a JSON number:
            NEGATIVE = money LEAVING the account (withdrawal, purchase, fee,
                       pre-authorized debit, interest charged, card purchase)
            POSITIVE = money ENTERING it (deposit, refund, transfer in,
                       interest paid, a payment made to a credit card)
   "bal"  running balance printed after this row, as a JSON number
   "ref"  bank reference number for this row (Wise transactionNumber, etc.)

One row looks EXACTLY like this — nothing else:
  {"d":"2000-01-15","desc":"POS PURCHASE EXAMPLE RETAILER 0000","amt":-41.25,"bal":1234.56}

OMIT any key you cannot fill. Never emit null, "", or a placeholder 0, and never
repeat metadata inside a row. Most rows are just d/desc/amt, plus bal when the
statement prints a running balance.

{
  "metadata": {
    "institution":            <string>  Bank or platform name e.g. "BMO", "RBC",
                                        "TD", "Scotiabank", "CIBC", "Wise",
                                        "PayPal". Use the exact name printed on
                                        the PDF.
    "account_type":           <string>  One of exactly: CHECKING | SAVINGS | CREDIT_CARD
                                        | FX_MULTI_CURRENCY | MERCHANT | OTHER
    "account_number":         <string>  Last 4 digits — OMIT the key if not visible.
    "statement_period_start": <YYYY-MM-DD>
    "statement_period_end":   <YYYY-MM-DD>
    "opening_balance":        <number>  e.g. 1500.00
    "closing_balance":        <number>  e.g. 2300.00
    "currency":               <string>  Exactly one of: CAD | USD | EUR | GBP | CNY
    "multi_account":          true      OMIT this key unless the PDF really has
                                        multiple sub-accounts or currency sections
                                        (e.g. Wise CAD + USD sections)
  },
  "transactions": [ <compact rows, in statement order> ],
  "notes": <string>  ONE short sentence, and ONLY when something was genuinely
                     ambiguous — a row you could not read, a sign you had to infer
                     from the balance, a section you skipped. OMIT the key when
                     nothing was ambiguous. Do not narrate your method.
}

═══════════════════════════════════════════════════════
2. DATE NORMALIZATION RULES
═══════════════════════════════════════════════════════

- Always output dates as YYYY-MM-DD.
- If the statement shows only MM-DD (e.g. "Jan 15" or "01/15") without a year,
  infer the year from `statement_period_start` and `statement_period_end`:
    • If the month falls within the statement period, use that year.
    • If the month is December and the period starts in December but ends in
      January, dates with month=Dec get the start year; dates with month=Jan
      get the end year.
- If the statement shows DD/MM/YYYY, reorder to YYYY-MM-DD.
- If the statement shows MM/DD/YYYY, reorder to YYYY-MM-DD.
- Never guess a year — use the statement period as your anchor.
- If a date is completely unparseable, set it to `statement_period_end` and
  add a note in `reasoning`. Do NOT omit the row.

═══════════════════════════════════════════════════════
3. DEBIT / CREDIT SIGN INFERENCE RULES
═══════════════════════════════════════════════════════

Banks use different layouts. Determine the sign of "amt" by examining ALL of:
  a) Column layout: if the statement has separate "Debits" and "Credits" columns,
     use which column the amount falls in.
  b) Description keywords: "withdrawal", "purchase", "payment to", "fee", "interest
     charged", "NSF", "pre-authorized debit" → NEGATIVE. "deposit", "credit",
     "transfer in", "refund", "cashback", "interest paid" → POSITIVE.
  c) Running balance delta: if bal[i] < bal[i-1], that row is NEGATIVE; if greater,
     POSITIVE.
  d) Credit cards: purchases increase the balance owed → NEGATIVE. Payments reduce
     the balance owed → POSITIVE.
- When multiple signals agree, you have HIGH confidence. When they conflict, prefer
  the running balance delta signal and say so in `notes`.
- The sign of "amt" is the ONLY place direction is recorded. Never emit a positive
  "amt" for money leaving the account, and never add a separate type/direction key.

═══════════════════════════════════════════════════════
4. RUNNING BALANCE DELTA CROSS-CHECK
═══════════════════════════════════════════════════════

After determining every row's sign and amount, verify:

  Deposit accounts (CHECKING / SAVINGS / FX_MULTI_CURRENCY / MERCHANT / OTHER):
    opening_balance + sum(credits) - sum(debits) ≈ closing_balance (within $0.05)

  CREDIT_CARD accounts — the equation INVERTS, because the balance is money you
  OWE: purchases (debits) increase it and payments (credits) reduce it:
    opening_balance + sum(debits) - sum(credits) ≈ closing_balance (within $0.05)
  Here opening_balance is the "previous balance" and closing_balance is the
  "new balance" / "total balance" printed on the statement.

If the difference exceeds $0.05:
  1. Re-read the statement for rows you may have missed or duplicated.
  2. Check for any row where the sign might be inverted.
  3. Note the discrepancy in `notes` and provide your best-effort output anyway.

═══════════════════════════════════════════════════════
5. MULTI-PAGE HANDLING
═══════════════════════════════════════════════════════

- Treat all pages as a single continuous statement.
- Do NOT duplicate rows that appear at both the bottom of one page and the top
  of the next page (page-carry rows).
- Do NOT include page totals or sub-totals as transaction rows.
- Rows that span two lines (description wraps) should be merged into a single row.

═══════════════════════════════════════════════════════
6. SKIP-PHRASE FILTER (MANDATORY)
═══════════════════════════════════════════════════════

EXCLUDE any row whose description (trimmed) matches ANY of the following phrases
(case-insensitive, exact match of full description):

  Opening Balance       Closing Balance        Brought Forward
  Carried Forward       Previous Balance       Balance Forward
  Total Withdrawals     Total Deposits         Total Debits
  Total Credits         Subtotal               Statement Period
  Minimum Payment       Interest Summary       Daily Balance Summary

These are summary rows, not individual transactions. Including them corrupts
the balance reconciliation. If you encounter such a row, skip it entirely (no
need to mention it).

═══════════════════════════════════════════════════════
7. MULTI-ACCOUNT STATEMENTS
═══════════════════════════════════════════════════════

- If a PDF covers multiple sub-accounts (e.g. BMO CAD chequing + USD chequing,
  or Wise CAD + USD + EUR sections), set `multi_account: true`.
- Extract ONLY the primary/largest account's transactions in the main
  `transactions` array. Name the secondary accounts in `notes`.
- Tag the primary account's currency in `metadata.currency`.
- If the accounts are truly inseparable (e.g. a combined statement with
  interleaved rows), extract all rows and set the most common currency.

═══════════════════════════════════════════════════════
8. CURRENCY RULES
═══════════════════════════════════════════════════════

- Only these 5 currencies are permitted in `metadata.currency`:
  CAD, USD, EUR, GBP, CNY
- If the statement is in a different currency, use "CAD" as a safe fallback
  and note this in `notes`.
- For Wise and PayPal multi-currency statements, use the primary account currency.

═══════════════════════════════════════════════════════
9. QUALITY REQUIREMENTS
═══════════════════════════════════════════════════════

- Include EVERY transaction row — do not omit rows due to ambiguity.
- If a row's amount is genuinely illegible, still include the row with "amt":0 and
  name it in `notes`. That is the one case where 0 is allowed.
- Do not fabricate transactions. If you cannot read a page, say so in `notes`.
- Return ONLY the JSON object. No markdown fences, no explanatory text outside JSON.
"""

BALANCE_CORRECTION_PROMPT = """\
═══════════════════════════════════════════════════════
BALANCE CORRECTION REQUIRED
═══════════════════════════════════════════════════════

Your previous extraction has a balance discrepancy that exceeds the $0.02 tolerance:

  Stated opening balance:   ${opening}
  Stated closing balance:   ${closing}
  Computed closing balance: ${computed}
  Difference:               ${difference}

This means one or more of the following errors occurred:
  1. You missed one or more transaction rows — re-read the PDF carefully.
  2. You included a summary row (Opening Balance, Total Withdrawals, etc.) as a
     transaction — remove it.
  3. You inverted the sign on one or more rows (debit vs credit) — re-check using
     the running balance delta.
  4. You duplicated a row that appeared on two consecutive pages — remove the duplicate.

Please re-extract the complete transaction list, correcting any of the above issues.
Apply the same JSON schema, date normalization, sign inference, and skip-phrase filter
rules as before. In `notes`, say in one sentence what you changed.

Return ONLY the corrected JSON object. The target is:
  {formula} = closing_balance ± $0.02
"""

# The reconciliation identity differs by account type — see the running
# balance cross-check section of the system prompt.
_DEPOSIT_FORMULA = "opening_balance + sum(credits) - sum(debits)"
_CREDIT_CARD_FORMULA = "opening_balance + sum(debits) - sum(credits)"


def _balance_formula(account_type: str) -> str:
    """Return the balance identity that applies to `account_type`."""
    return _CREDIT_CARD_FORMULA if account_type == "CREDIT_CARD" else _DEPOSIT_FORMULA



# ---------------------------------------------------------------------------
# PDF utility functions
# ---------------------------------------------------------------------------

def _pdfplumber_text_density(pdf_path: Path) -> float:
    """Return fraction of pages with ≥100 non-whitespace characters.

    ≥0.85 → text-based PDF (text mode).
    <0.85  → scanned PDF (vision mode).
    Returns 0.0 on any error (triggers vision path as safe fallback).
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return 0.0
            total = len(pdf.pages)
            rich = 0
            for page in pdf.pages:
                text = page.extract_text() or ""
                non_ws = len(re.sub(r"\s", "", text))
                if non_ws >= 100:
                    rich += 1
            return rich / total
    except Exception as exc:
        logger.warning("pdfplumber text density check failed for %s: %s", pdf_path.name, exc)
        return 0.0


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF, joining pages with PAGE markers.

    Returns a single string with ``--- PAGE N OF M ---`` separators.
    """
    import pdfplumber

    pages_text: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            pages_text.append(f"--- PAGE {i} OF {total} ---\n{page_text}")
    return "\n\n".join(pages_text)


def _statement_page_count(pdf_path: Path) -> int:
    """Page count of the PDF, read from the file rather than asked of the model."""
    from core.extraction import _pdf_page_count

    return _pdf_page_count(pdf_path)


def _prepare_statement_images(pdf_path: Path, dpi: int = DEFAULT_STATEMENT_DPI) -> list[bytes]:
    """Render each PDF page as PNG bytes using PyMuPDF at the given DPI.

    Vision models downsample an image to a fixed tile budget before reading
    it, so a higher DPI does not change the tokens they receive or the rows
    they return — it only raises peak render memory. The default DPI is chosen
    to be the lowest that keeps statement text legible.

    Returns a list of PNG bytes, one per page.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    images: list[bytes] = []
    try:
        # Guard against pathological inputs: rendering a very long scanned PDF
        # at high DPI can exhaust memory (worker OOM-kill → 502) and
        # produces an LLM payload too large to parse reliably. Fail fast with a
        # clean error the endpoint converts to a 422 instead of crashing.
        if doc.page_count > _MAX_VISION_PAGES:
            raise StatementParseError(
                f"Statement has {doc.page_count} pages; the scanned-document limit "
                f"is {_MAX_VISION_PAGES}. Please split it and upload in parts."
            )
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
            # Drop the raw pixmap before rendering the next page. The decoded
            # RGB buffer is orders of magnitude larger than the PNG that is
            # kept, and the same process is serving every other request, so
            # holding it any longer than needed is waste.
            pix = None
    finally:
        doc.close()
    return images


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if the exception looks like a 429 / rate-limit error.

    Delegates to the extraction module so "capacity rejection" means exactly
    one thing across the whole LLM ingestion path.
    """
    from core.extraction import _is_quota_error

    return _is_quota_error(exc)


# Compact row keys (what the prompt asks for) → the TransactionRow field names the
# rest of the engine uses. Long names are accepted too, so a correction retry,
# a hosted provider, or a model that ignores the legend still validates.
_ROW_KEY_ALIASES = {
    "d": "date",
    "desc": "description",
    "amt": "amount",
    "bal": "running_balance",
    "ref": "external_id",
    "balance": "running_balance",
    "reference": "external_id",
}

_METADATA_KEY_ALIASES = {
    "period_start": "statement_period_start",
    "period_end": "statement_period_end",
    "start": "statement_period_start",
    "end": "statement_period_end",
    "opening": "opening_balance",
    "closing": "closing_balance",
    "account": "account_number",
}


def _decimal_or_none(value) -> Decimal | None:
    """Parse a number or a money-ish string to Decimal. None when not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text or text in ("-", "null", "none", "N/A", "n/a"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _money_str(value) -> str | None:
    """Canonical 2-dp decimal string, sign preserved. None when not numeric."""
    parsed = _decimal_or_none(value)
    if parsed is None:
        return None
    return f"{parsed:.2f}"


def _iso_date_str(value) -> str:
    """YYYY-MM-DD, zero-padding a model that wrote 2026-1-5. Untouched otherwise."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return text


def _normalize_statement_row(raw: dict) -> dict | None:
    """Expand one compact row to the TransactionRow shape.

    The sign of `amt` is where the prompt puts the direction, so it decides
    debit/credit. An explicit long-form `type` key is honoured only when the amount
    carries no sign information (i.e. it is not negative) — a negative number is
    an unambiguous statement about direction, a positive one is not.
    """
    row = {_ROW_KEY_ALIASES.get(key, key): value for key, value in raw.items()}

    signed = _decimal_or_none(row.get("amount"))
    declared = str(row.get("type") or "").strip().lower()
    if signed is not None and signed < 0:
        row["type"] = "debit"
    elif declared in ("debit", "credit"):
        row["type"] = declared
    else:
        row["type"] = "credit" if (signed is not None and signed > 0) else "debit"

    if signed is not None:
        row["amount"] = f"{abs(signed):.2f}"
    elif row.get("amount") is not None:
        row["amount"] = str(row["amount"])

    for key in ("running_balance", "balance_delta"):
        if key in row:
            row[key] = _money_str(row[key])

    if row.get("external_id") is not None:
        row["external_id"] = str(row["external_id"]).strip() or None

    row["date"] = _iso_date_str(row.get("date"))
    row["description"] = str(row.get("description") or "").strip()
    if not row["description"] and row.get("amount") is None:
        return None  # an entirely empty row is noise, not a transaction
    return row


def _normalize_statement_payload(parsed: dict) -> dict:
    """Map the compact wire shape onto the schema the rest of the engine consumes.

    Everything downstream — the balance validator, the statements table, the
    review UI, the reconciler — sees TransactionRow/StatementMetadata, whichever
    of the two shapes arrived on the wire.
    """
    if not isinstance(parsed, dict):
        return parsed
    out = dict(parsed)

    meta = out.get("metadata")
    if isinstance(meta, dict):
        meta = {_METADATA_KEY_ALIASES.get(key, key): value for key, value in meta.items()}
        for key in ("opening_balance", "closing_balance"):
            as_money = _money_str(meta.get(key))
            if as_money is not None:
                meta[key] = as_money
        for key in ("statement_period_start", "statement_period_end"):
            if meta.get(key) is not None:
                meta[key] = _iso_date_str(meta[key])
        for key in ("account_type", "currency"):
            if isinstance(meta.get(key), str):
                meta[key] = meta[key].strip().upper()
        if meta.get("account_number") is not None:
            meta["account_number"] = str(meta["account_number"]).strip() or None
        if not isinstance(meta.get("multi_account"), bool):
            raw_multi = str(meta.get("multi_account") or "").strip().lower()
            meta["multi_account"] = raw_multi in ("true", "yes", "1")
        out["metadata"] = meta

    raw_rows = out.get("transactions")
    if raw_rows is None:
        raw_rows = out.get("txns") or out.get("rows") or []
    rows: list[dict] = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        row = _normalize_statement_row(raw)
        if row is not None:
            rows.append(row)
    out["transactions"] = rows

    # `notes` is the compact stand-in for the multi-sentence `reasoning` field.
    if out.get("notes") and not out.get("reasoning"):
        out["reasoning"] = str(out["notes"])
    return out


def _parse_llm_json(raw: str) -> dict:
    """Strip markdown fences, parse JSON, and normalize the compact wire shape.

    Raises ValueError on parse failure.
    """
    text = raw.strip()
    # Strip ```json ... ``` fences the model may emit despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first and last fence lines
        inner = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(inner).strip()
    return _normalize_statement_payload(json.loads(text))


def _dedup_rows(rows: list[TransactionRow]) -> list[TransactionRow]:
    """Deduplicate rows with identical (date, amount, description[:40]) tuples.

    Keeps the first occurrence and logs if any duplicates are found.
    """
    seen: set[tuple] = set()
    unique: list[TransactionRow] = []
    for row in rows:
        key = (row.date, row.amount, row.description[:40])
        if key not in seen:
            seen.add(key)
            unique.append(row)
        else:
            logger.debug("Duplicate row suppressed: %s %s %s", row.date, row.amount, row.description[:40])
    return unique


# ---------------------------------------------------------------------------
# Gemini extraction — text mode
# ---------------------------------------------------------------------------

async def _extract_statement_with_gemini_text(
    text: str,
    prompt: str,
    db,
    model_name: str,
) -> tuple[BankStatementExtraction, int, int, float]:
    """Call Gemini in text mode to extract structured statement data.

    Returns (BankStatementExtraction, tokens_in, tokens_out, cost_usd).
    Retries up to 3x on rate-limit errors with exponential backoff.
    """
    from google import genai
    from google.genai import types

    from config.settings import gemini_config, statement_parsing_config

    client = genai.Client(api_key=gemini_config.api_key)

    full_content = (
        f"{prompt}\n\nExtract all transactions from this bank statement:\n\n{text}"
    )
    parts = [types.Part.from_text(text=full_content)]

    thinking_cfg = None
    if "2.5" in model_name:
        thinking_cfg = types.ThinkingConfig(
            thinking_budget=statement_parsing_config.thinking_budget
        )

    gen_cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
        thinking_config=thinking_cfg,
    )

    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model_name,
            contents=types.Content(role="user", parts=parts),
            config=gen_cfg,
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )

    # Token counts and cost
    tok_in = tok_out = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tok_in = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        tok_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
    cost = tok_in * 1.5e-7 + tok_out * 6e-7  # Gemini 2.5 Flash pricing

    if db is not None:
        try:
            db.log_api_call("statement_parsing", model_name, tok_in, tok_out, cost)
        except Exception as log_exc:
            logger.debug("Failed to log Gemini statement cost: %s", log_exc)

    parsed = _parse_llm_json(response.text)
    extraction = BankStatementExtraction(**parsed)
    return extraction, tok_in, tok_out, cost


# ---------------------------------------------------------------------------
# Gemini extraction — vision mode
# ---------------------------------------------------------------------------

async def _extract_statement_with_gemini_vision(
    images: list[bytes],
    prompt: str,
    db,
    model_name: str,
) -> tuple[BankStatementExtraction, int, int, float]:
    """Call Gemini in vision mode (PNG parts) to extract structured statement data.

    Beyond ``_MAX_SINGLE_BATCH_PAGES`` pages, splits into non-overlapping
    batches of ``_VISION_BATCH_SIZE`` and deduplicates by
    (date, amount, description[:40]).

    Returns (BankStatementExtraction, tokens_in, tokens_out, cost_usd).
    """
    from google import genai
    from google.genai import types

    from config.settings import gemini_config, statement_parsing_config

    client = genai.Client(api_key=gemini_config.api_key)

    thinking_cfg = None
    if "2.5" in model_name:
        thinking_cfg = types.ThinkingConfig(
            thinking_budget=statement_parsing_config.thinking_budget
        )

    gen_cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
        thinking_config=thinking_cfg,
    )

    _BATCH_SIZE = _VISION_BATCH_SIZE
    _MAX_SINGLE = _MAX_SINGLE_BATCH_PAGES

    async def _call_batch(batch_images: list[bytes], batch_label: str) -> tuple[BankStatementExtraction, int, int, float]:
        parts = [types.Part.from_text(
            text=f"{prompt}\n\nExtract all transactions from these {len(batch_images)} pages of the bank statement ({batch_label})."
        )]
        for img in batch_images:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))

        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model_name,
                contents=types.Content(role="user", parts=parts),
                config=gen_cfg,
            ),
            timeout=LLM_CALL_TIMEOUT_S,
        )

        ti = to = 0
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            ti = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            to = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
        c = ti * 1.5e-7 + to * 6e-7  # Gemini 2.5 Flash pricing

        if db is not None:
            try:
                db.log_api_call("statement_parsing", model_name, ti, to, c)
            except Exception as log_exc:
                logger.debug("Failed to log Gemini batch cost: %s", log_exc)

        parsed = _parse_llm_json(resp.text)
        extraction = BankStatementExtraction(**parsed)
        return extraction, ti, to, c

    total_pages = len(images)

    if total_pages <= _MAX_SINGLE:
        return await _call_batch(images, f"pages 1-{total_pages}")

    # Batched path: split into non-overlapping batches of 10
    all_rows: list[TransactionRow] = []
    first_metadata: StatementMetadata | None = None
    total_tok_in = total_tok_out = 0
    total_cost = 0.0

    for batch_start in range(0, total_pages, _BATCH_SIZE):
        batch_end = min(batch_start + _BATCH_SIZE, total_pages)
        batch = images[batch_start:batch_end]
        label = f"pages {batch_start + 1}-{batch_end} of {total_pages}"

        batch_extraction, ti, to, c = await _call_batch(batch, label)
        total_tok_in += ti
        total_tok_out += to
        total_cost += c

        if first_metadata is None:
            first_metadata = batch_extraction.metadata
        all_rows.extend(batch_extraction.transactions)

    # Deduplicate across batch boundaries
    deduped = _dedup_rows(all_rows)
    assert first_metadata is not None  # guaranteed — at least one batch ran
    combined = BankStatementExtraction(transactions=deduped, metadata=first_metadata)
    return combined, total_tok_in, total_tok_out, total_cost


# ---------------------------------------------------------------------------
# OpenAI extraction — fallback
# ---------------------------------------------------------------------------

_SCHEMA_CORRECTION_MESSAGE = (
    "The previous response could not be validated against the required schema. "
    "Please try again and return ONLY a valid JSON object. Required: "
    "metadata.institution, metadata.account_type, metadata.statement_period_start, "
    "metadata.statement_period_end, metadata.opening_balance, "
    "metadata.closing_balance, metadata.currency, and a `transactions` array whose "
    'rows use the compact keys {"d","desc","amt","bal"} — "amt" SIGNED (negative '
    "for money out), omitting any key you cannot fill."
)


def _build_statement_messages(
    text_content: str | None,
    images: list[bytes] | None,
    prompt: str,
    batch_label: str | None = None,
) -> list[dict]:
    """Build chat-completions messages for a statement (text or vision mode)."""
    import base64

    if text_content and not images:
        return [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    "Extract all transactions from this bank statement text:\n\n"
                    + text_content
                ),
            },
        ]

    image_parts = []
    for img_bytes in (images or []):
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    instruction = "Extract all transactions from this bank statement."
    if batch_label:
        instruction = (
            f"Extract all transactions from these {len(images or [])} pages "
            f"of the bank statement ({batch_label})."
        )
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                *image_parts,
            ],
        },
    ]


async def _statement_via_chat_completions(
    text_content: str | None,
    images: list[bytes] | None,
    prompt: str,
    client,
    db,
    *,
    model: str,
    provider: str,
    timeout_s: float,
    input_cost_per_token: float,
    output_cost_per_token: float,
    create_kwargs: dict | None = None,
    batch_label: str | None = None,
) -> tuple[BankStatementExtraction, int, int, float]:
    """Run one statement (or one page batch) through an OpenAI-protocol endpoint.

    The whole call/parse/retry/cost body, parameterized by client, model and
    pricing, so the local provider and hosted OpenAI share it rather than
    drifting apart in two copies.

    Returns (BankStatementExtraction, tokens_in, tokens_out, cost_usd).
    """
    messages = _build_statement_messages(text_content, images, prompt, batch_label)
    kwargs = dict(create_kwargs or {})
    kwargs.setdefault("response_format", {"type": "json_object"})

    def _log_cost(resp) -> tuple[int, int, float]:
        ti = to = 0
        if hasattr(resp, "usage") and resp.usage:
            try:
                ti = int(resp.usage.prompt_tokens or 0)
                to = int(resp.usage.completion_tokens or 0)
            except (TypeError, ValueError):
                ti = to = 0
        c = ti * input_cost_per_token + to * output_cost_per_token
        if db is not None:
            try:
                # Zero-cost providers log too, so usage still shows up.
                db.log_api_call("statement_parsing", model, ti, to, c)
            except Exception as log_exc:
                logger.debug("Failed to log %s statement cost: %s", provider, log_exc)
        return ti, to, c

    started = time.monotonic()

    # First attempt
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                ),
                timeout=timeout_s,
            )
            break
        except Exception as exc:
            err_s = str(exc).lower()
            is_server = any(c in err_s for c in ("500", "502", "503", "529"))
            # A couple of short retries are worth it — but they stay inside the
            # latency budget. Sleeping 30-60s for a rate limit only guarantees
            # the browser times out before the answer arrives.
            if (_is_rate_limit_error(exc) or is_server) and attempt < 2:
                wait = _TRANSIENT_RETRY_BACKOFF_S * (attempt + 1)
                logger.warning(
                    "%s statement attempt %d failed, retrying in %.0fs: %s",
                    provider, attempt + 1, wait, exc,
                )
                await asyncio.sleep(wait)
            else:
                raise

    raw = response.choices[0].message.content
    ti, to, c = _log_cost(response)

    # A statement truncated by its output budget is recoverable: the JSON stops
    # mid-row. Doubling once and re-asking beats handing half a transaction list
    # to the balance validator, which would fail a correct extraction.
    if _finish_reason(response) == "length" and kwargs.get("max_tokens"):
        budget = int(kwargs["max_tokens"])
        kwargs = {**kwargs, "max_tokens": budget * 2}
        logger.warning(
            "%s/%s statement hit the %d-token output budget - one retry at %d",
            provider, model, budget, budget * 2,
        )
        response = await asyncio.wait_for(
            client.chat.completions.create(model=model, messages=messages, **kwargs),
            timeout=timeout_s,
        )
        raw = response.choices[0].message.content
        ti2, to2, c2 = _log_cost(response)
        ti += ti2
        to += to2
        c += c2

    try:
        parsed = _parse_llm_json(raw)
        extraction = BankStatementExtraction(**parsed)
        logger.info(
            "statement: provider=%s model=%s latency_ms=%d tokens_in=%d tokens_out=%d cost_usd=%.6f",
            provider, model, int((time.monotonic() - started) * 1000), ti, to, c,
        )
        return extraction, ti, to, c
    except Exception:
        pass

    # One corrective retry
    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": _SCHEMA_CORRECTION_MESSAGE},
    ]
    response2 = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=retry_messages,
            **kwargs,
        ),
        timeout=timeout_s,
    )
    raw2 = response2.choices[0].message.content
    ti2, to2, c2 = _log_cost(response2)

    parsed2 = _parse_llm_json(raw2)
    extraction2 = BankStatementExtraction(**parsed2)
    logger.info(
        "statement: provider=%s model=%s latency_ms=%d tokens_in=%d tokens_out=%d cost_usd=%.6f (after schema retry)",
        provider, model, int((time.monotonic() - started) * 1000),
        ti + ti2, to + to2, c + c2,
    )
    return extraction2, ti + ti2, to + to2, c + c2


# ---------------------------------------------------------------------------
# Local extraction — PRIMARY provider
# ---------------------------------------------------------------------------

async def _extract_statement_with_local(
    text_content: str | None,
    images: list[bytes] | None,
    prompt: str,
    db,
    client=None,
    progress=None,
    pages_total: int | None = None,
    **_ignored,
) -> tuple[BankStatementExtraction, int, int, float]:
    """Parse a statement with the locally hosted model (PRIMARY provider).

    Same batching rule as the Gemini vision path: beyond
    ``_MAX_SINGLE_BATCH_PAGES`` pages, render in non-overlapping batches of
    ``_VISION_BATCH_SIZE`` and deduplicate across batch boundaries, so a long
    statement doesn't depend on one enormous context window.

    `progress` is the caller's ``_PageProgress``, advanced after each batch so a
    queued upload can show genuine movement instead of a spinner. The output
    budget is sized per call from the pages that call actually covers.

    Returns (BankStatementExtraction, tokens_in, tokens_out, cost_usd) —
    cost_usd is always 0.0 for a self-hosted endpoint.
    """
    from config.settings import local_llm_config
    from core.extraction import _get_local_client, _get_local_semaphore

    cfg = local_llm_config
    if client is None:
        client = _get_local_client()

    def _common(pages: int) -> dict:
        return dict(
            model=cfg.model,
            provider="local",
            timeout_s=cfg.timeout_s,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
            create_kwargs={
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "extra_body": cfg.extra_body,
                "max_tokens": statement_max_output_tokens(pages),
            },
        )

    async def _advance(pages: int) -> None:
        if progress is not None:
            await progress.advance(pages)

    async with _get_local_semaphore():
        # Text mode, or a page count one call can carry.
        if not images or len(images) <= _MAX_SINGLE_BATCH_PAGES:
            label = f"pages 1-{len(images)}" if images else None
            pages = len(images) if images else (pages_total or 1)
            result = await _statement_via_chat_completions(
                text_content, images, prompt, client, db, batch_label=label,
                **_common(pages),
            )
            await _advance(pages)
            return result

        total_pages = len(images)
        all_rows: list[TransactionRow] = []
        first_metadata: StatementMetadata | None = None
        total_tok_in = total_tok_out = 0
        total_cost = 0.0

        for batch_start in range(0, total_pages, _VISION_BATCH_SIZE):
            batch_end = min(batch_start + _VISION_BATCH_SIZE, total_pages)
            batch = images[batch_start:batch_end]
            label = f"pages {batch_start + 1}-{batch_end} of {total_pages}"
            batch_extraction, ti, to, c = await _statement_via_chat_completions(
                None, batch, prompt, client, db, batch_label=label,
                **_common(len(batch)),
            )
            total_tok_in += ti
            total_tok_out += to
            total_cost += c
            if first_metadata is None:
                first_metadata = batch_extraction.metadata
            all_rows.extend(batch_extraction.transactions)
            await _advance(len(batch))

        deduped = _dedup_rows(all_rows)
        assert first_metadata is not None  # guaranteed — at least one batch ran
        combined = BankStatementExtraction(transactions=deduped, metadata=first_metadata)
        return combined, total_tok_in, total_tok_out, total_cost


async def _extract_statement_with_openai(
    text_content: str | None,
    images: list[bytes] | None,
    prompt: str,
    client,
    db,
) -> tuple[BankStatementExtraction, int, int, float]:
    """Call OpenAI GPT-4.1 as an optional last-resort extractor.

    Uses json_object response_format (simpler and works with all gpt-4.1 variants)
    with Pydantic validation of the output.

    One auto-retry on JSON parse failure with a corrective message.

    Returns (BankStatementExtraction, tokens_in, tokens_out, cost_usd).
    """
    from config.settings import openai_config

    if client is None:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=openai_config.api_key)

    return await _statement_via_chat_completions(
        text_content, images, prompt, client, db,
        model=openai_config.model,
        provider="openai",
        timeout_s=LLM_CALL_TIMEOUT_S,
        input_cost_per_token=2e-6,   # GPT-4.1 pricing
        output_cost_per_token=8e-6,
    )


# ---------------------------------------------------------------------------
# Skip-phrase filter
# ---------------------------------------------------------------------------

_SKIP_PHRASES_RE = re.compile(
    r"^("
    r"Opening Balance|Closing Balance|Brought Forward|Carried Forward|"
    r"Previous Balance|Balance Forward|"
    r"Total Withdrawals|Total Deposits|Total Debits|Total Credits|"
    r"Subtotal|Statement Period|Minimum Payment|"
    r"Interest Summary|Daily Balance Summary"
    r")$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Deterministic validator
# ---------------------------------------------------------------------------

def validate_statement_extraction(
    extraction: BankStatementExtraction,
    source_filename: str,
) -> tuple[str, list[str], dict]:
    """Run deterministic validation checks on an LLM extraction.

    Returns:
        confidence_tier: "HIGH", "MEDIUM", or "LOW"
        flags: list of flag strings (non-empty = something to review)
        summary_dict: numeric summary for balance reconciliation display

    Checks performed (in order):
        1. Skip-phrase filter — drop summary rows, flag if any dropped
        2. Balance reconciliation — opening + credits − debits ≈ closing ±$0.02
        3. Date containment — all dates within statement period ±2 days
        4. Running-balance delta consistency — >5% inconsistent rows → flag
        5. Duplicate detection — identical (date, amount, description[:40]) tuples
        6. Amount outlier — abs(amount) > $100,000
        7. Row count sanity — <3 rows for non-credit-card statements
    """
    meta = extraction.metadata
    rows = list(extraction.transactions)

    flags: list[str] = []

    # ── 1. Skip-phrase filter ────────────────────────────────────────────
    before_count = len(rows)
    rows = [r for r in rows if not _SKIP_PHRASES_RE.match(r.description.strip())]
    dropped_skip = before_count - len(rows)
    if dropped_skip > 0:
        flags.append("skip_phrase_dropped")
        logger.info(
            "%s: dropped %d skip-phrase row(s)", source_filename, dropped_skip
        )

    # Update extraction's transaction list in-place for downstream use
    extraction.transactions.clear()
    extraction.transactions.extend(rows)

    # ── 2. Balance reconciliation ────────────────────────────────────────
    try:
        opening = Decimal(str(meta.opening_balance))
        closing = Decimal(str(meta.closing_balance))
    except InvalidOperation:
        flags.append("date_parse_error")  # the flag name reads "date"; it covers any parse error
        opening = Decimal("0")
        closing = Decimal("0")

    sum_credits = Decimal("0")
    sum_debits = Decimal("0")
    for row in rows:
        try:
            amt = Decimal(str(row.amount))
        except InvalidOperation:
            amt = Decimal("0")
        if row.type == "credit":
            sum_credits += amt
        else:
            sum_debits += amt

    # Credit cards invert the deposit-account equation: a purchase (debit)
    # INCREASES the balance owed and a payment (credit) reduces it. Applying the
    # deposit equation to a card statement puts it out of tolerance by roughly
    # twice the payment total, which would drop every card statement to LOW
    # confidence and into the lossy regex fallback.
    is_credit_card = meta.account_type == "CREDIT_CARD"
    if is_credit_card:
        computed_closing = opening + sum_debits - sum_credits
    else:
        computed_closing = opening + sum_credits - sum_debits
    balance_difference = abs(computed_closing - closing)

    if balance_difference > Decimal("0.05"):
        flags.append("balance_out_of_tolerance")
    elif balance_difference > Decimal("0.02"):
        flags.append("balance_mismatch")

    # ── 3. Date containment ──────────────────────────────────────────────
    _date_out_of_period = False
    try:
        from datetime import date as _date
        from datetime import timedelta
        period_start = _date.fromisoformat(meta.statement_period_start)
        period_end = _date.fromisoformat(meta.statement_period_end)
        low = period_start - timedelta(days=2)
        high = period_end + timedelta(days=2)

        for row in rows:
            try:
                txn_date = _date.fromisoformat(row.date)
                if txn_date < low or txn_date > high:
                    _date_out_of_period = True
                    logger.debug(
                        "%s: date %s outside period %s–%s",
                        source_filename, row.date,
                        meta.statement_period_start, meta.statement_period_end,
                    )
            except ValueError:
                _date_out_of_period = True
    except ValueError:
        _date_out_of_period = True

    if _date_out_of_period:
        flags.append("date_out_of_period")

    # ── 4. Running-balance delta consistency ─────────────────────────────
    rb_rows = [r for r in rows if r.running_balance is not None]
    if len(rb_rows) >= 2:
        inconsistent = 0
        for i in range(1, len(rb_rows)):
            try:
                prev_rb = Decimal(str(rb_rows[i - 1].running_balance))
                curr_rb = Decimal(str(rb_rows[i].running_balance))
                amt = Decimal(str(rb_rows[i].amount))
                direction = Decimal("1") if rb_rows[i].type == "credit" else Decimal("-1")
                expected_delta = amt * direction
                actual_delta = curr_rb - prev_rb
                if abs(actual_delta - expected_delta) > Decimal("0.02"):
                    inconsistent += 1
            except InvalidOperation:
                inconsistent += 1
        if inconsistent / len(rb_rows) > 0.05:
            flags.append("running_balance_inconsistent")

    # ── 5. Duplicate detection ───────────────────────────────────────────
    keys: set[tuple] = set()
    for row in rows:
        key = (row.date, row.amount, row.description[:40])
        if key in keys:
            flags.append("duplicate_suspected")
            break  # one flag is enough — don't spam
        keys.add(key)

    # ── 6. Amount outlier ────────────────────────────────────────────────
    for row in rows:
        try:
            if abs(Decimal(str(row.amount))) > Decimal("100000"):
                flags.append("amount_outlier")
                break
        except InvalidOperation:
            pass

    # ── 7. Row count sanity ──────────────────────────────────────────────
    if len(rows) < 3 and not is_credit_card:
        flags.append("low_row_count")
    elif len(rows) < 1:
        flags.append("low_row_count")

    # ── Confidence tier mapping ──────────────────────────────────────────
    _CRITICAL_FLAGS = {"balance_out_of_tolerance", "low_row_count", "date_parse_error"}
    has_critical = bool(_CRITICAL_FLAGS & set(flags))

    if has_critical or balance_difference > Decimal("0.05"):
        confidence = "LOW"
    elif balance_difference <= Decimal("0.02") and not flags:
        confidence = "HIGH"
    else:
        # Has non-critical flags OR balance diff in (0.02, 0.05]
        confidence = "MEDIUM"

    summary = {
        "opening": opening,
        "closing": closing,
        "sum_credits": sum_credits,
        "sum_debits": sum_debits,
        "computed_closing": computed_closing,
        "balance_difference": balance_difference,
        "row_count": len(rows),
        "dropped_skip_phrases": dropped_skip,
    }

    return confidence, flags, summary


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------

async def parse_statement_with_llm(
    file_path: Path,
    user_id: str,
    source_filename: str,
    openai_client=None,
    db=None,
    progress_cb=None,
) -> StatementParseResult:
    """Parse a bank statement PDF using LLM extraction.

    `progress_cb(pages_done, pages_total)` is optional and called after each page
    batch (once, at the end, for a single-call statement). A callback that also
    declares `mode` is additionally called once up front with
    `mode="one_pass"` (the whole statement is one LLM call) or `mode="paged"`
    (real page batches), so a caller can show an elapsed clock rather than a page
    counter that cannot move. Exceptions raised by it are logged and ignored: a
    progress callback must never fail a parse that already succeeded.

    Orchestration steps:
      1. Acquire shared LLM concurrency semaphore (shared with receipt extraction).
      2. Determine text vs vision mode via pdfplumber text-density gate.
      3. Attempt the local model (primary).
      4. On failure, try Gemini — but only if a key is configured.
      5. On failure, try OpenAI GPT-4.1 — but only if a key is configured.
      6. Validate extraction deterministically; compute confidence tier.
      7. If balance mismatch > $0.02, retry once with BALANCE_CORRECTION_PROMPT
         on whichever provider answered.
      8. Return StatementParseResult.

    Raises:
        StatementParseError: if no provider is configured, or all of them fail.
    """
    from config.settings import (
        gemini_config,
        local_llm_config,
        openai_config,
        statement_parsing_config,
    )
    from core.extraction import _get_extraction_semaphore

    async with _get_extraction_semaphore():
        # ── Step 1: text density gate ────────────────────────────────────
        # pdfplumber opens + scans every page (CPU/IO bound). Run it off the
        # event loop so the worker keeps serving SSE and other requests while
        # statements parse — blocking here starves every other request and the
        # upload times out before the answer arrives.
        density = await asyncio.to_thread(_pdfplumber_text_density, file_path)
        text_mode = density >= 0.85
        # The page count is a fact about the file, not something to ask a model
        # for: it is read here and stamped over whatever the extraction carries.
        page_count = await asyncio.to_thread(_statement_page_count, file_path)
        logger.info(
            "%s: %d page(s), density=%.2f → %s mode", source_filename, page_count,
            density, "text" if text_mode else "vision",
        )
        # How the pages actually reach the model, which is what the queue shows.
        # Text mode is always a single call, and so is vision up to
        # ``_MAX_SINGLE_BATCH_PAGES``: in both cases nothing can advance a page
        # counter until the one answer arrives, so the UI says "reading N pages
        # in one pass" and runs an elapsed clock instead of faking movement.
        one_pass = text_mode or page_count <= _MAX_SINGLE_BATCH_PAGES
        progress = _PageProgress(
            progress_cb, page_count, mode="one_pass" if one_pass else "paged"
        )
        await progress.announce()

        extraction: BankStatementExtraction | None = None
        parse_method: str | None = None
        errors: list[str] = []
        local_endpoint_down = False
        tok_in = tok_out = 0
        cost = 0.0

        async def _text() -> str:
            return await asyncio.to_thread(_extract_pdf_text, file_path)

        async def _images() -> list[bytes]:
            return await asyncio.to_thread(
                _prepare_statement_images, file_path, dpi=statement_parsing_config.dpi
            )

        # ── Step 2: local primary ────────────────────────────────────────
        if local_llm_config.enabled:
            try:
                if text_mode:
                    extraction, ti, to, c = await _extract_statement_with_local(
                        await _text(), None, BANK_STATEMENT_SYSTEM_PROMPT, db,
                        progress=progress, pages_total=page_count,
                    )
                    parse_method = "local_text"
                else:
                    extraction, ti, to, c = await _extract_statement_with_local(
                        None, await _images(), BANK_STATEMENT_SYSTEM_PROMPT, db,
                        progress=progress, pages_total=page_count,
                    )
                    parse_method = "local_vision"
                tok_in += ti
                tok_out += to
                cost += c
                logger.info(
                    "%s: local (%s) extracted %d rows (tokens_in=%d, tokens_out=%d)",
                    source_filename, local_llm_config.model,
                    len(extraction.transactions), ti, to,
                )
            except Exception as exc:
                if is_local_endpoint_down(exc):
                    local_endpoint_down = True
                    logger.warning(
                        "%s: local endpoint %s unreachable: %s",
                        source_filename, local_llm_config.base_url, exc,
                    )
                    errors.append(
                        f"local_primary[{local_llm_config.base_url}]: endpoint unreachable ({exc})"
                    )
                else:
                    logger.warning("%s: local statement parse failed: %s", source_filename, exc)
                    errors.append(f"local_primary[{local_llm_config.model}]: {exc}")

        # ── Step 3: Gemini fallback (only when a key is configured) ──────
        # Try each candidate model in turn, but only step down on a capacity
        # rejection. A multi-page statement makes one call per page batch, so a
        # single upload can use up a model's daily capacity by itself.
        if extraction is None and gemini_config.enabled:
            for model_name in statement_parsing_config.gemini_model_candidates:
                try:
                    if text_mode:
                        extraction, ti, to, c = await _extract_statement_with_gemini_text(
                            await _text(), BANK_STATEMENT_SYSTEM_PROMPT, db, model_name
                        )
                        parse_method = "gemini_text"
                    else:
                        extraction, ti, to, c = await _extract_statement_with_gemini_vision(
                            await _images(), BANK_STATEMENT_SYSTEM_PROMPT, db, model_name
                        )
                        parse_method = "gemini_vision"
                    tok_in += ti
                    tok_out += to
                    cost += c
                    logger.info(
                        "%s: Gemini (%s) extracted %d rows (tokens_in=%d, tokens_out=%d, cost=$%.4f)",
                        source_filename, model_name, len(extraction.transactions), ti, to, c,
                    )
                    await progress.advance(page_count)
                    break
                except Exception as exc:
                    logger.warning("%s: Gemini %s failed: %s", source_filename, model_name, exc)
                    errors.append(f"gemini_fallback[{model_name}]: {exc}")
                    if not _is_rate_limit_error(exc):
                        break

        # ── Step 4: OpenAI fallback (only when a key is configured) ──────
        # An explicitly injected client counts as configuration.
        if extraction is None and (openai_config.enabled or openai_client is not None):
            logger.info("%s: falling back to OpenAI %s", source_filename, openai_config.model)
            try:
                text_for_oai = await _text() if text_mode else None
                oai_images = None if text_mode else await _images()
                extraction, ti, to, c = await _extract_statement_with_openai(
                    text_for_oai, oai_images, BANK_STATEMENT_SYSTEM_PROMPT,
                    openai_client, db,
                )
                parse_method = "openai_fallback"
                tok_in += ti
                tok_out += to
                cost += c
                logger.info(
                    "%s: OpenAI extracted %d rows (tokens_in=%d, tokens_out=%d, cost=$%.4f)",
                    source_filename, len(extraction.transactions), ti, to, c,
                )
                await progress.advance(page_count)
            except Exception as exc:
                logger.error("%s: OpenAI fallback failed: %s", source_filename, exc)
                errors.append(f"openai_fallback: {exc}")

        if extraction is not None and page_count:
            # Authoritative: the file says how many pages it has.
            extraction.metadata.page_count = page_count

        if extraction is None or parse_method is None:
            if not errors:
                raise StatementParseError(
                    "no statement parsing provider configured — set LLM_BASE_URL, "
                    "GEMINI_API_KEY or OPENAI_API_KEY"
                )
            if local_endpoint_down and len(errors) == 1:
                # The configured endpoint never answered and no other provider
                # was configured. Say that, rather than implying the statement
                # itself was unreadable. If a hosted fallback was tried and
                # failed too, its error is the more informative one and falls
                # through to the generic message below.
                logger.error(
                    "%s: no statement parse — local endpoint down; %s",
                    source_filename, "; ".join(errors),
                )
                raise StatementLocalEndpointDown()
            raise StatementParseError(
                "All LLM paths failed for %s: %s" % (source_filename, "; ".join(errors))
            )

        # ── Step 4: Validate ─────────────────────────────────────────────
        confidence, flags, summary = validate_statement_extraction(extraction, source_filename)
        balance_difference = summary["balance_difference"]
        tolerance = statement_parsing_config.balance_tolerance
        balance_matches = balance_difference <= tolerance

        logger.info(
            "%s: confidence=%s flags=%s balance_diff=$%s",
            source_filename, confidence, flags, balance_difference,
        )

        # ── Step 5: Balance correction retry ────────────────────────────
        if not balance_matches:
            logger.info(
                "%s: balance mismatch $%s > $%s — retrying with correction prompt",
                source_filename, balance_difference, tolerance,
            )
            correction_prompt = BANK_STATEMENT_SYSTEM_PROMPT + "\n\n" + BALANCE_CORRECTION_PROMPT.format(
                difference=str(balance_difference),
                opening=str(summary["opening"]),
                closing=str(summary["closing"]),
                computed=str(summary["computed_closing"]),
                formula=_balance_formula(extraction.metadata.account_type),
            )
            try:
                # The retry goes back to whichever provider actually answered,
                # in the same mode. Switching provider here would change two
                # variables at once and make the "did it improve?" comparison
                # below meaningless.
                if parse_method == "local_text":
                    retry_extraction, ti, to, c = await _extract_statement_with_local(
                        await _text(), None, correction_prompt, db,
                        pages_total=page_count,
                    )
                elif parse_method == "local_vision":
                    retry_extraction, ti, to, c = await _extract_statement_with_local(
                        None, await _images(), correction_prompt, db,
                        pages_total=page_count,
                    )
                elif parse_method == "gemini_text":
                    retry_extraction, ti, to, c = await _extract_statement_with_gemini_text(
                        await _text(), correction_prompt, db,
                        statement_parsing_config.gemini_model,
                    )
                elif parse_method == "gemini_vision":
                    retry_extraction, ti, to, c = await _extract_statement_with_gemini_vision(
                        await _images(), correction_prompt, db,
                        statement_parsing_config.gemini_model,
                    )
                else:
                    # OpenAI fallback retry
                    text_r = await _text() if text_mode else None
                    imgs_r = None if text_mode else await _images()
                    retry_extraction, ti, to, c = await _extract_statement_with_openai(
                        text_r, imgs_r, correction_prompt, openai_client, db
                    )

                tok_in += ti
                tok_out += to
                cost += c

                confidence2, flags2, summary2 = validate_statement_extraction(
                    retry_extraction, source_filename
                )
                if summary2["balance_difference"] < summary["balance_difference"]:
                    logger.info(
                        "%s: correction retry improved balance diff $%s → $%s",
                        source_filename,
                        summary["balance_difference"],
                        summary2["balance_difference"],
                    )
                    extraction = retry_extraction
                    confidence = confidence2
                    flags = flags2
                    summary = summary2
                    balance_difference = summary2["balance_difference"]
                    balance_matches = balance_difference <= tolerance
                else:
                    logger.info(
                        "%s: correction retry did not improve ($%s ≥ $%s), keeping original",
                        source_filename,
                        summary2["balance_difference"],
                        summary["balance_difference"],
                    )
            except Exception as exc:
                logger.warning("%s: correction retry failed: %s", source_filename, exc)
                errors.append(f"balance_correction_retry: {exc}")

        return StatementParseResult(
            transactions=extraction.transactions,
            metadata=extraction.metadata,
            confidence=confidence,
            flags=flags,
            parse_method=parse_method,
            balance_matches=balance_matches,
            balance_difference=str(summary["balance_difference"]),
            llm_tokens_in=tok_in,
            llm_tokens_out=tok_out,
            llm_cost_usd=cost,
            errors=errors,
        )

"""Document extraction module — file hashing, LLM-based data extraction,
validation, and Document model construction."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import certifi
from openai import APIStatusError, AsyncOpenAI

from config.settings import (
    EXPENSE_CATEGORIES,
    RECEIPT_BATCH_DPI,
    RECEIPT_FIRST_PAGES_DPI,
    RECEIPT_RETRY_DPI,
    openai_config,
)
from core.document_currency import INFERRED as _CURRENCY_INFERRED
from core.document_currency import MARKER_FIELD as _CURRENCY_MARKER_FIELD
from core.document_currency import (
    classify_currency_marker,
    currency_from_extraction,
    marker_from_text,
)
from models.document import Document, DocumentStatus, LineItem

logger = logging.getLogger(__name__)

# ── Latency budget ───────────────────────────────────────────────────────────
# An upload is a *synchronous* HTTP request: the browser holds the connection
# open until extraction returns. Anything the extraction path spends past the
# request timeout in front of it is invisible to the user — the work still
# completes and the receipt still lands, but the browser has already shown a
# timeout. Every retry/backoff below is therefore sized to keep the worst case
# (primary fails → fallback succeeds) comfortably under that ceiling.
LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "45"))

# ── Large / complex receipts ─────────────────────────────────────────────────
# Rendering a long invoice at full DPI into ONE vision call makes a single
# enormous request: every letter-size page adds input tokens, so it can outlive
# the per-call timeout and report no progress on the way.
#
# The rule:
#   ≤ 4 pages                  → one vision call at 200 DPI
#   > 4 pages, text density ≥ 0.85 → the text layer in one call, plus the page-1
#                                    image so the vendor logo/letterhead is read
#   > 4 pages, scanned         → page batches of 4, merged by
#                                _merge_receipt_batches
# Pages beyond the first four render at 150 DPI: a vision tower downsamples to a
# fixed tile budget, so above a certain resolution the extra pixels only cost
# memory — except on a faint scan, where they are the difference between reading
# the page and reading nothing.
# That case is caught after the fact: see _batch_read_nothing / RECEIPT_RETRY_DPI.
RECEIPT_SINGLE_CALL_MAX_PAGES = 4
RECEIPT_BATCH_SIZE = 4
# The DPI ladder lives in config/settings.py so it is one env-tunable knob and
# not a number duplicated per module; the names below are this module's aliases
# for it.
RECEIPT_OVERFLOW_DPI = RECEIPT_BATCH_DPI
RECEIPT_TEXT_DENSITY_THRESHOLD = 0.85
# Ceiling on the pages rendered for the batched vision path. A document past
# this is still extracted from its first RECEIPT_MAX_VISION_PAGES pages, but the
# result is flagged low with pages_used < pages_total — never silently truncated.
RECEIPT_MAX_VISION_PAGES = 40

# Output budgets for the trimmed receipt schema. Omitting null fields keeps what
# a small receipt emits short, and a page batch carrying line items stays well
# inside the batch budget. Generous headroom on top, because an inference server
# only allocates what it generates — and a truncated answer costs a whole extra
# call (see the finish_reason=="length" doubling below).
RECEIPT_MAX_OUTPUT_TOKENS = 512
RECEIPT_BATCH_MAX_OUTPUT_TOKENS = 768

# ── The grand total is never trusted at reduced resolution ───────────────────
# A page batch that read *something* at 150 DPI can still have read the wrong
# thing, and on an invoice there is exactly one number where that is
# unacceptable: the grand total, which is the figure that reaches a CRA filing.
# A faint scan can read a confident, wrong total at 150 DPI and the true figure
# at 200, with nothing in the answer to tell the two apart. So the batch that
# claims the grand total is re-rendered at
# RECEIPT_RETRY_DPI and asked again before its figure is accepted, and when two
# batches disagree both are re-read before the disagreement is reported.
#
# The budget below is the ceiling those two rules share on one document: two
# calls, which covers the two claimants an invoice produces (the total page and
# one page that mistook a carried-forward figure for it) and stops a
# pathological scan where every batch claims a total from spending the document's
# whole latency budget on re-reads.
RECEIPT_TOTAL_VERIFY_MAX_CALLS = 2
# Two readings of the same figure agree when they land this close — the same
# tolerance _merge_receipt_batches uses to decide two batches agree.
RECEIPT_TOTAL_AGREEMENT_TOLERANCE = Decimal("0.02")

# Backoff before re-trying the *same* provider after a transient server error.
# Deliberately short: the retry holds a semaphore slot and the user's connection
# while it sleeps, and a second provider is standing by.
_TRANSIENT_RETRY_BACKOFF_S = 2.0

# Per-token USD rates for the default Gemini model. The cost estimate recorded
# by db.log_api_call is derived from these, so a rate that no longer matches the
# configured model silently mis-reports it — keep them in step with
# GeminiConfig.model in config/settings.py.
_GEMINI_INPUT_COST_PER_TOKEN = 0.0000015
_GEMINI_OUTPUT_COST_PER_TOKEN = 0.0000075


def _is_quota_error(exc: Exception) -> bool:
    """True when a provider rejected the call for capacity rather than correctness.

    Quota/rate-limit rejections are NOT worth sleeping through here. A per-day
    quota will not free up in 30s, and a per-minute one is better served by the
    fallback provider than by holding the caller's connection open. Either way
    the right move is to fail over immediately.
    """
    text = str(exc).lower()
    if "429" in text or "resource_exhausted" in text:
        return True
    if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) == 429:
        return True
    return any(k in text for k in ("quota", "rate limit", "rate_limit", "resource exhausted"))


@dataclass
class ExtractionAttempt:
    """Per-call telemetry captured by _extract_with_gemini / _extract_with_openai.

    Collected into ``result["_telemetry"]`` (a list) by ``extract_document`` and
    persisted to the ``extraction_log`` table by the caller once the parent
    document row has an id.
    """

    model: str
    confidence: float
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    fallback_from: str | None = None
    error: str | None = None


_CONFIDENCE_STRING_TO_NUMERIC = {"high": 0.9, "medium": 0.6, "low": 0.3}


def _string_to_numeric_confidence(value: object) -> float:
    """Map 'high'/'medium'/'low' to a numeric 0..1 value. Default 0.9."""
    if isinstance(value, (int, float)):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.9
        return max(0.0, min(1.0, v))
    if isinstance(value, str):
        return _CONFIDENCE_STRING_TO_NUMERIC.get(value.lower(), 0.9)
    return 0.9

# Server-side concurrency gate for LLM API calls. Prevents overwhelming
# Gemini/OpenAI rate limits when many receipts are uploaded simultaneously.
# Controlled by LLM_MAX_CONCURRENT_EXTRACTIONS env var (default 5).
_extraction_semaphore: asyncio.Semaphore | None = None


def _get_extraction_semaphore() -> asyncio.Semaphore:
    global _extraction_semaphore
    if _extraction_semaphore is None:
        from config.settings import gemini_config
        _extraction_semaphore = asyncio.Semaphore(gemini_config.max_concurrent_extractions)
    return _extraction_semaphore


def _get_default_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI client with proper SSL configuration."""
    import httpx

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    # Without an explicit timeout a stalled connection holds an extraction
    # semaphore slot forever, which wedges every queued upload behind it.
    http_client = httpx.AsyncClient(
        verify=ssl_context,
        timeout=httpx.Timeout(LLM_CALL_TIMEOUT_S, connect=10.0),
    )
    return AsyncOpenAI(
        api_key=openai_config.api_key,
        http_client=http_client,
        max_retries=0,  # retries are handled above, inside the latency budget
    )


# The local endpoint is a single inference server, so it gets its own, tighter
# gate than the hosted-provider semaphore above. Nested inside it: a burst of
# uploads queues here rather than inside the inference server, where it would
# stretch every request's latency at once.
_local_semaphore: asyncio.Semaphore | None = None
_local_semaphore_limit: int | None = None


def _get_local_semaphore() -> asyncio.Semaphore:
    global _local_semaphore, _local_semaphore_limit
    from config.settings import local_llm_config

    limit = max(1, local_llm_config.max_concurrent)
    if _local_semaphore is None or _local_semaphore_limit != limit:
        _local_semaphore = asyncio.Semaphore(limit)
        _local_semaphore_limit = limit
    return _local_semaphore


def _get_local_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI client pointed at the local OpenAI-compatible endpoint.

    No certifi/SSL context: the endpoint is plain HTTP on the LAN or loopback.
    """
    import httpx

    from config.settings import local_llm_config

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(local_llm_config.timeout_s, connect=5.0),
    )
    return AsyncOpenAI(
        base_url=local_llm_config.base_url,
        api_key=local_llm_config.api_key,
        http_client=http_client,
        max_retries=0,  # the provider chain is the retry
    )


def compute_file_hash(file_path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_decimal(value) -> Decimal:
    """Parse a value into a Decimal, handling currency symbols and commas."""
    if value is None or value == "" or value == "null":
        return Decimal(0)
    s = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    # Remove trailing currency codes like "USD", "CAD"
    s = re.sub(r'[A-Za-z]+$', '', s).strip()
    if not s or s == "-":
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


def validate_extraction(data: dict, *, require_amount: bool = True) -> tuple[bool, list[str]]:
    """Validate extracted financial data.

    Lenient validation — an uploaded document is treated as "proof of
    transaction" rather than as a strict receipt. As long as an amount can be
    extracted, the document is valid. Tax breakdowns and line item consistency are optional.

    ``require_amount=False`` is for ONE caller: a single page-batch of a long
    invoice. Pages 5-8 of a 20-page invoice legitimately carry no grand total —
    only the batch that shows one reports it — so demanding an amount there would
    burn a corrective retry per batch and flag every long invoice low. The merged
    result is still validated with require_amount=True, so a document with no
    amount anywhere is still caught.

    Returns (is_valid, list_of_warnings).
    """
    warnings: list[str] = []

    # Must have SOME amount — either total or amount field
    total = _safe_decimal(data.get("total"))
    amount = _safe_decimal(data.get("amount"))
    effective_total = total if total != 0 else amount

    if effective_total == 0 and require_amount:
        raw_total = str(data.get("total", "")).strip()
        raw_amount = str(data.get("amount", "")).strip()
        if raw_total not in ("0", "0.00", "0.0") and raw_amount not in ("0", "0.00", "0.0"):
            return False, ["No amount could be extracted from document"]

    # Promote "amount" to "total" if total is missing
    if total == 0 and amount != 0:
        data["total"] = data["amount"]

    # Tax consistency check is a warning, not a rejection
    subtotal = _safe_decimal(data.get("subtotal"))
    if subtotal != 0 and total != 0:
        gst = _safe_decimal(data.get("gst"))
        hst = _safe_decimal(data.get("hst"))
        pst = _safe_decimal(data.get("pst"))
        other_tax = _safe_decimal(data.get("other_tax"))
        expected_total = subtotal + gst + hst + pst + other_tax
        if abs(total - expected_total) > Decimal("0.02"):
            warnings.append(
                f"Total mismatch: expected {expected_total} "
                f"(subtotal + taxes) but got {total}"
            )

    return (True, warnings)


def parse_extraction_response(raw_json: str) -> dict:
    """Parse a JSON string, stripping markdown code fences if present.

    Raises ValueError if the JSON is invalid.
    """
    cleaned = raw_json.strip()
    cleaned = re.sub(r"^```json\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc


def build_document_from_extraction(
    extraction_data: dict,
    original_filename: str,
    file_hash: str,
    raw_json: str,
) -> Document:
    """Construct a Document model instance from extraction data."""
    is_valid, _warnings = validate_extraction(extraction_data)

    line_items = None
    if extraction_data.get("line_items"):
        line_items = [
            LineItem(
                description=item["description"],
                quantity=item["quantity"],
                unit_price=_safe_decimal(item.get("unit_price")),
                amount=_safe_decimal(item.get("amount")),
            )
            for item in extraction_data["line_items"]
        ]

    doc_date = None
    if extraction_data.get("date"):
        try:
            doc_date = date.fromisoformat(extraction_data["date"])
        except (ValueError, TypeError):
            doc_date = None

    def _to_decimal_or_none(val) -> Decimal | None:
        if val is None or val == "" or val == "null":
            return None
        d = _safe_decimal(val)
        return d if d != 0 or str(val).strip() == "0" else None

    # Use "total" or fall back to "amount" field
    total = _to_decimal_or_none(extraction_data.get("total"))
    if total is None or total == 0:
        total = _to_decimal_or_none(extraction_data.get("amount"))

    # What currency, and on what evidence. The model emits `currency` only when
    # the document states one; a Canadian tax line fills it in; otherwise the
    # column stays NULL and says so, instead of carrying a guess that scores
    # like a reading. See core/document_currency.py.
    currency, currency_source = currency_from_extraction(extraction_data)

    return Document(
        original_filename=original_filename,
        file_hash=file_hash,
        vendor=extraction_data.get("vendor"),
        document_date=doc_date,
        currency=currency,
        currency_source=currency_source,
        subtotal=_to_decimal_or_none(extraction_data.get("subtotal")),
        tax_gst=_to_decimal_or_none(extraction_data.get("gst")),
        tax_hst=_to_decimal_or_none(extraction_data.get("hst")),
        tax_pst=_to_decimal_or_none(extraction_data.get("pst")),
        tax_other=_to_decimal_or_none(extraction_data.get("other_tax")),
        total=total,
        payment_method=extraction_data.get("payment_method"),
        invoice_number=extraction_data.get("invoice_number"),
        line_items=line_items,
        extraction_confidence="high" if is_valid else "low",
        extraction_raw_json=raw_json,
        status=DocumentStatus.EXTRACTED,
    )


def compute_field_confidence(extraction_data: dict) -> dict[str, str]:
    """Compute per-field confidence levels from raw LLM extraction data.

    Returns a dict mapping field names to "high", "medium", or "low".
    Never raises — returns "low" for any field that causes an exception.
    """
    confidence: dict[str, str] = {}
    KNOWN_CURRENCIES = {"CAD", "USD", "CNY", "EUR", "GBP"}

    # --- vendor ---
    try:
        vendor = extraction_data.get("vendor")
        vendor_str = str(vendor).strip() if vendor else ""
        if not vendor or not vendor_str:
            confidence["vendor"] = "low"
        elif vendor_str == "NOT_A_FINANCIAL_DOCUMENT":
            confidence["vendor"] = "low"
        elif len(vendor_str) >= 2:
            confidence["vendor"] = "high"
        else:
            confidence["vendor"] = "medium"
    except Exception:
        confidence["vendor"] = "low"

    # --- date ---
    try:
        raw_date = extraction_data.get("date")
        if raw_date is None:
            confidence["date"] = "low"
        else:
            raw_date_str = str(raw_date).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date_str):
                try:
                    date.fromisoformat(raw_date_str)
                    confidence["date"] = "high"
                except (ValueError, TypeError):
                    confidence["date"] = "medium"
            else:
                confidence["date"] = "medium"
    except Exception:
        confidence["date"] = "low"

    # --- total ---
    try:
        total_dec = _safe_decimal(extraction_data.get("total"))
        confidence["total"] = "high" if total_dec > 0 else "low"
    except Exception:
        confidence["total"] = "low"

    # --- currency ---
    # Graded by provenance, not by presence: a code the document *stated* is
    # high, a code inferred from its Canadian tax line is medium, and nothing
    # at all is low. A code the model volunteers for a bare "$" is a guess
    # rather than a reading, so it never reaches "high" through this path.
    try:
        code, source = currency_from_extraction(extraction_data)
        if code is None:
            confidence["currency"] = "low"
        elif source == _CURRENCY_INFERRED:
            confidence["currency"] = "medium"
        elif code in KNOWN_CURRENCIES:
            confidence["currency"] = "high"
        else:
            # A well-formed code the engine holds no opinion about ("XYZ").
            confidence["currency"] = "medium"
    except Exception:
        confidence["currency"] = "low"

    # --- subtotal / gst / hst / pst ---
    for tax_field in ("subtotal", "gst", "hst", "pst"):
        try:
            raw_val = extraction_data.get(tax_field)
            if raw_val is None:
                confidence[tax_field] = "low"
            else:
                dec_val = _safe_decimal(raw_val)
                if dec_val > 0:
                    confidence[tax_field] = "high"
                elif str(raw_val).strip() in ("0", "0.00", "0.0"):
                    confidence[tax_field] = "medium"
                else:
                    confidence[tax_field] = "low"
        except Exception:
            confidence[tax_field] = "low"

    # --- payment_method / invoice_number ---
    for str_field in ("payment_method", "invoice_number"):
        try:
            val = extraction_data.get(str_field)
            if val and len(str(val).strip()) >= 2:
                confidence[str_field] = "high"
            else:
                confidence[str_field] = "low"
        except Exception:
            confidence[str_field] = "low"

    # --- Cross-validation bonus ---
    try:
        subtotal_dec = _safe_decimal(extraction_data.get("subtotal"))
        gst_dec = _safe_decimal(extraction_data.get("gst"))
        hst_dec = _safe_decimal(extraction_data.get("hst"))
        pst_dec = _safe_decimal(extraction_data.get("pst"))
        other_tax_dec = _safe_decimal(extraction_data.get("other_tax"))
        total_dec = _safe_decimal(extraction_data.get("total"))
        computed_total = subtotal_dec + gst_dec + hst_dec + pst_dec + other_tax_dec
        if subtotal_dec > 0 and total_dec > 0 and abs(total_dec - computed_total) <= Decimal("0.02"):
            confidence["total"] = "high"
            confidence["subtotal"] = "high"
    except Exception:
        pass  # Don't degrade existing confidence on cross-validation failure

    return confidence


def build_extraction_prompt(
    *,
    batch: tuple[int, int] | None = None,
    pages_total: int | None = None,
    text_layer: bool = False,
) -> str:
    """Return the system prompt that instructs the LLM to extract a document as JSON.

    Three shapes, one rule set:

    * default — one receipt, one call. Nulls are OMITTED rather than emitted,
      which is most of what keeps a receipt's output short.
    * ``batch=(first_page, last_page)`` — one 4-page slice of a long invoice.
      Adds `grand_total_page` and `continuation` so the merge in
      ``_merge_receipt_batches`` can tell a page subtotal from the document's
      grand total instead of guessing.
    * ``text_layer=True`` — a long machine-generated invoice sent as text plus
      the page-1 image for the letterhead.
    """
    header = (
        "You are a financial document extraction assistant for a Canadian corporation. "
        "Your job is to extract key transaction data from ANY type of financial document — "
        "receipts, invoices, wire transfer confirmations, bank statements, insurance policies, "
        "payment screenshots, cheques, train tickets, contractor invoices, or any other proof "
        "of a business transaction.\n\n"
        "These documents serve as proof that a transaction occurred. They may be in ANY format: "
        "rotated, multi-page, handwritten, in any language (Chinese, English, etc.), screenshots, "
        "scanned forms, or emails. Extract whatever information you can find.\n\n"
    )

    fields = (
        "Extract these fields into ONE JSON object. OMIT any key you cannot determine — "
        "do not emit null, an empty string, or a placeholder 0. An absent key means "
        '"not shown on the document".\n\n'
        "{\n"
        '  "vendor": "the company, person, or entity on the other side of the transaction '
        "(sender or receiver). For wire transfers, use the beneficiary/recipient name. "
        'For contractor invoices, use the contractor name. For government forms, use the agency name.",\n'
        '  "date": "the transaction or document date in YYYY-MM-DD format. '
        'For any date format (MM/DD/YYYY, YYYY年MM月DD日, DD-Mon-YYYY, etc.), convert to YYYY-MM-DD.",\n'
        '  "payment_date": "the date the payment was/will be charged to the bank account, in YYYY-MM-DD format. '
        "Look for phrases like 'payment date', 'charge date', 'due date', 'will be charged on', 'auto-pay on', "
        "'debit date', 'withdrawal date', 'value date'. Omit the key if not found.\",\n"
        '  "currency_marker": "the currency text printed immediately next to the total, '
        "QUOTED EXACTLY as it appears on the document. Copy it character for character — "
        '"$", "US$", "USD", "CA$", "C$", "CAD", "€", "£", "¥", "RMB". Copy, do not decide: '
        "a bare dollar sign is copied as \"$\", never upgraded to \"US$\" or \"CAD\" because of "
        'an address or a vendor name. Use "" when the amount is printed with no currency text '
        'at all.",\n'
        '  "currency": "ISO 4217 currency code — ONLY when the document itself says which '
        "currency it is in. It says so with a written code (CAD, USD, EUR, CNY), with US$ or "
        'C$/CA$, with words ("Canadian dollars", "US dollars"), or with a symbol that names '
        "exactly one currency (€ -> EUR, £ -> GBP, ¥ -> CNY). "
        'A bare "$" names no currency: OMIT this key. Do not infer the currency from an '
        'address, a phone number, a language, a domain or the vendor\'s country — a bare "$" '
        "on a US company's receipt is very often a Canadian charge, and a wrong currency code "
        'is worse than no code at all.",\n'
        '  "subtotal": "the subtotal before tax",\n'
        '  "gst": "GST amount",\n'
        '  "hst": "HST amount",\n'
        '  "pst": "provincial sales tax amount, however the document labels it '
        '(PST, RST, QST)",\n'
        '  "other_tax": "any other tax amount",\n'
        '  "total": "the primary transaction amount. This is the most important field. '
        "For invoices, use the total including tax. For wire transfers, use the amount transferred. "
        "For cheques, use the cheque amount. For train tickets, use the fare. "
        'If no subtotal/tax breakdown exists, put the full amount here.",\n'
        '  "amount": "alternative amount field if total is ambiguous. '
        'For wire transfers with currency conversion, put the SENT amount here and the RECEIVED amount in total.",\n'
        '  "payment_method": "how the payment was made (wire transfer, credit card, cheque, Wise, PayPal, e-Transfer, etc.)",\n'
        '  "invoice_number": "invoice, receipt, transaction, or reference number",\n'
        '  "purpose": "brief description of what this transaction is for '
        '(e.g. contractor payment, software subscription, office supplies, travel expense, salary)"'
    )

    if batch is not None:
        first_page, last_page = batch
        total_str = str(pages_total) if pages_total else "?"
        fields += (
            ',\n  "grand_total_page": 0,   // the page number (counted across the WHOLE document, '
            f"1-{total_str}) that prints the document's FINAL grand total / amount due / balance "
            "due. OMIT this key when these pages do not show it.\n"
            '  "continuation": true,    // true when these pages continue a table or invoice '
            "started on an earlier page (no vendor letterhead / invoice header of their own)\n"
            "}\n\n"
            f"YOU ARE READING PAGES {first_page}-{last_page} OF {total_str} TOTAL PAGES.\n"
            "- Report ONLY what these pages show. Do not infer, carry over, or invent values from "
            "pages you cannot see; omit those keys instead.\n"
            "- A 'page subtotal', 'carried forward', 'continued', 'balance brought forward' or "
            "running figure is NOT the grand total: omit `total`, `grand_total_page` and the tax "
            "keys when that is all these pages show.\n"
            "- Report `total` and the tax breakdown ONLY from the page that prints the document's "
            "final total. That page, and only that page, sets `grand_total_page`.\n"
            "- Set `continuation` honestly: it is how the merge decides which pages carry the "
            "vendor identity.\n"
            "- Omit `line_items`. Itemising every page overruns the per-batch output "
            "budget and doubles each batch's latency, and nothing downstream of the "
            "ledger reads itemisation. A batch that volunteers `line_items` anyway is "
            "still merged in page order.\n"
        )
    else:
        fields += "\n}\n"

    rules = (
        "IMPORTANT RULES:\n"
        "- If the document is rotated, read it in the correct orientation.\n"
        "- If the document is in a non-English language, still extract all fields.\n"
        "- If the document is a photo of a physical document (cheque, receipt), do your best to read it.\n"
        "- Never invent a value to fill a key. Omitting a key is always better than guessing it.\n"
        '- `currency_marker` is the one key where "" is a real answer: it means the amount is '
        "printed with no currency text beside it at all. Quote what is on the page; never "
        "write a marker the document does not print.\n"
        "- Money fields are plain decimals — a JSON number or a bare string like 1234.56. "
        "Never a currency symbol, never thousands separators.\n"
    )
    if batch is None:
        rules += (
            "- The 'total' field is REQUIRED. Always extract the primary amount even if formatting is unusual.\n"
            "- Omit `line_items` entirely — itemisation is not needed for this document.\n"
            "- If this is clearly NOT a financial document (e.g. an ID card, passport, personal photo), "
            'still return valid JSON with total set to "0" and vendor set to "NOT_A_FINANCIAL_DOCUMENT".\n'
        )

    if text_layer:
        rules += (
            f"- This document has {pages_total or 'several'} pages. Its FULL text layer is supplied "
            "below, and the image of page 1 is attached so you can read the letterhead/logo the text "
            "layer may not name. The text layer is authoritative for numbers; the image is for the "
            "vendor identity.\n"
            "- The final total is near the END of the text. Page subtotals and 'carried forward' "
            "figures along the way are NOT the total.\n"
        )

    return header + fields + "\n" + rules + "\nReturn ONLY the JSON object, no additional text."


def build_categorization_prompt(
    description: str,
    vendor: str,
    amount: str,
    currency: str,
    date: str,
) -> str:
    """Build a prompt for categorizing a transaction into one of the expense categories."""
    categories_list = "\n".join(f"- {cat}" for cat in EXPENSE_CATEGORIES)

    return (
        "You are a financial transaction categorization assistant.\n\n"
        "Categorize the following transaction into exactly one of these categories:\n"
        f"{categories_list}\n\n"
        "Transaction details:\n"
        f"- Description: {description}\n"
        f"- Vendor: {vendor}\n"
        f"- Amount: {amount}\n"
        f"- Currency: {currency}\n"
        f"- Date: {date}\n\n"
        'Return your answer as a JSON object: {"category": "<chosen category>"}'
    )


def _prepare_image_data(file_path: Path) -> tuple[list[bytes], str, str | None]:
    """Convert a document file to image byte arrays and/or text content.

    Returns (image_bytes_list, media_type, text_content).
    - PDFs are rendered page-by-page via PyMuPDF.
    - Standard images (jpg, png, webp) are read directly.
    - Other image formats (gif, bmp, tiff) are converted to PNG via Pillow.
    - Document formats (xlsx, csv, docx) are read as text; images list is empty
      and text_content contains the document's textual representation.
    """
    ext = file_path.suffix.lower()

    # ── PDF → page images ────────────────────────────────────────────────
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(str(file_path))
        images = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            images.append(pix.tobytes("png"))
            # Release the decoded RGB buffer before rendering the next page.
            # It is far larger than the PNG that is kept, and the same process
            # is serving every other request at the same time.
            pix = None
        doc.close()
        return images, "image/png", None

    # ── Native image formats (no conversion needed) ──────────────────────
    if ext in (".jpg", ".jpeg", ".jfif"):
        return [file_path.read_bytes()], "image/jpeg", None
    if ext == ".png":
        return [file_path.read_bytes()], "image/png", None
    if ext == ".webp":
        return [file_path.read_bytes()], "image/webp", None

    # ── Other image formats → convert to PNG via Pillow ──────────────────
    if ext in (".gif", ".bmp", ".tiff", ".tif"):
        import io

        from PIL import Image
        img = Image.open(file_path)
        # Handle animated GIFs — extract first frame only
        if hasattr(img, "n_frames") and img.n_frames > 1:
            img.seek(0)
        # Convert CMYK / palette / other modes to RGB
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return [buf.getvalue()], "image/png", None

    # ── XLSX / XLS → extract text content ────────────────────────────────
    if ext in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        lines = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            lines.append(f"=== Sheet: {sheet_name} ===")
            for row in ws.iter_rows(values_only=True):
                cell_strs = [str(c) if c is not None else "" for c in row]
                lines.append(" | ".join(cell_strs))
        wb.close()
        text = "\n".join(lines)
        return [], "text/plain", text

    # ── CSV → read as text ───────────────────────────────────────────────
    if ext == ".csv":
        text = file_path.read_text(encoding="utf-8", errors="replace")
        return [], "text/plain", text

    # ── DOCX → extract text via python-docx ─────────────────────────────
    if ext == ".docx":
        import docx  # python-docx; required for .docx receipts/invoices
        doc = docx.Document(str(file_path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                paragraphs.append(" | ".join(cells))
        text = "\n".join(paragraphs)
        return [], "text/plain", text

    # ── DOC (legacy binary) → python-docx cannot read this format ───────
    if ext == ".doc":
        raise ValueError(
            "Legacy .doc format is not supported. Please save as .docx or PDF."
        )

    # ── Fallback: treat as image ─────────────────────────────────────────
    return [file_path.read_bytes()], "image/png", None


# ---------------------------------------------------------------------------
# Large / complex receipts: page planning, batching, merge
# ---------------------------------------------------------------------------

@dataclass
class ReceiptBatch:
    """One slice of a long receipt: up to RECEIPT_BATCH_SIZE rendered pages.

    ``dpi`` is the *lowest* DPI any page in this slice was rendered at, and
    ``source_path`` is the PDF it came from. Together they are what lets a batch
    that came back empty be re-rendered sharper and asked again — see
    ``_retry_batch_at_higher_dpi``. Both are optional so a hand-built batch (the
    merge tests) still constructs with four arguments.
    """

    index: int          # 0-based batch number
    first_page: int     # 1-based, inclusive
    last_page: int      # 1-based, inclusive
    images: list[bytes]
    dpi: int = RECEIPT_BATCH_DPI
    source_path: Path | None = None

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def label(self) -> str:
        return f"pages {self.first_page}-{self.last_page}"


@dataclass
class ReceiptPlan:
    """How one document will be fed to the LLM.

    ``mode`` is one of:
      "single"  — one call with whatever ``_prepare_image_data`` produced
      "text"    — one call with the PDF's text layer plus the page-1 image
      "batched" — one call per ``ReceiptBatch``, merged afterwards
    """

    mode: str
    media_type: str
    images: list[bytes]
    text_content: str | None
    pages_total: int
    pages_used: int
    batches: list[ReceiptBatch]
    text_density: float | None = None

    @property
    def truncated(self) -> bool:
        return self.pages_used < self.pages_total


def _pdf_page_count(pdf_path: Path) -> int:
    """Page count of a PDF, without rendering anything. 0 if unreadable."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("could not read page count for %s: %s", pdf_path.name, exc)
        return 0


def _render_pdf_pages(pdf_path: Path, page_numbers: list[int], dpi: int) -> list[bytes]:
    """Render the given 0-based page numbers to PNG bytes at `dpi`."""
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    images: list[bytes] = []
    try:
        for number in page_numbers:
            if number < 0 or number >= doc.page_count:
                continue
            pix = doc.load_page(number).get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
            # Drop the decoded RGB buffer before the next page: it dwarfs the PNG
            # that is kept, and the same process is serving every other request
            # at the same time.
            pix = None
    finally:
        doc.close()
    return images


def _receipt_text_density(pdf_path: Path) -> float:
    """Fraction of pages carrying a real text layer (same gate as statements).

    Imported lazily: core.llm_statement_parser imports this module at import
    time, so a module-level import here would be circular.
    """
    from core.llm_statement_parser import _pdfplumber_text_density

    return _pdfplumber_text_density(pdf_path)


def _receipt_pdf_text(pdf_path: Path) -> str:
    """The PDF's full text layer with ``--- PAGE n OF m ---`` markers."""
    from core.llm_statement_parser import _extract_pdf_text

    return _extract_pdf_text(pdf_path)


def _build_receipt_plan(file_path: Path) -> ReceiptPlan:
    """Decide how this document gets sent to the model. CPU-bound; run in a thread."""
    ext = file_path.suffix.lower()

    if ext != ".pdf":
        images, media_type, text_content = _prepare_image_data(file_path)
        pages = len(images) or 1
        return ReceiptPlan(
            mode="single", media_type=media_type, images=images,
            text_content=text_content, pages_total=pages, pages_used=pages, batches=[],
        )

    page_count = _pdf_page_count(file_path)

    # Small receipt: the simple path, one call at 200 DPI.
    if page_count <= RECEIPT_SINGLE_CALL_MAX_PAGES:
        images, media_type, text_content = _prepare_image_data(file_path)
        pages = len(images) or max(page_count, 1)
        return ReceiptPlan(
            mode="single", media_type=media_type, images=images,
            text_content=text_content, pages_total=pages, pages_used=pages, batches=[],
        )

    density = _receipt_text_density(file_path)

    # Machine-generated long invoice: the text layer is complete and exact, so one
    # call reads every page for a fraction of the tokens the page images cost. The
    # page-1 image rides along because the vendor is often only in the letterhead
    # graphic.
    if density >= RECEIPT_TEXT_DENSITY_THRESHOLD:
        text = _receipt_pdf_text(file_path)
        header_image = _render_pdf_pages(file_path, [0], RECEIPT_FIRST_PAGES_DPI)
        logger.info(
            "%s: %d pages, text density %.2f → text-layer extraction + page-1 image",
            file_path.name, page_count, density,
        )
        return ReceiptPlan(
            mode="text", media_type="image/png", images=header_image,
            text_content=text, pages_total=page_count, pages_used=page_count,
            batches=[], text_density=density,
        )

    # Scanned long invoice: batch the images.
    pages_used = min(page_count, RECEIPT_MAX_VISION_PAGES)
    if pages_used < page_count:
        logger.warning(
            "%s has %d pages; rendering the first %d for extraction and flagging the rest",
            file_path.name, page_count, pages_used,
        )
    first_cut = min(RECEIPT_SINGLE_CALL_MAX_PAGES, pages_used)
    images = _render_pdf_pages(file_path, list(range(first_cut)), RECEIPT_FIRST_PAGES_DPI)
    page_dpis = [RECEIPT_FIRST_PAGES_DPI] * len(images)
    if pages_used > first_cut:
        overflow = _render_pdf_pages(
            file_path, list(range(first_cut, pages_used)), RECEIPT_OVERFLOW_DPI
        )
        images += overflow
        page_dpis += [RECEIPT_OVERFLOW_DPI] * len(overflow)

    batches: list[ReceiptBatch] = []
    for start in range(0, len(images), RECEIPT_BATCH_SIZE):
        chunk = images[start:start + RECEIPT_BATCH_SIZE]
        # The faintest page in the slice decides whether a re-render could help:
        # a batch is only ever re-rendered whole.
        chunk_dpi = min(page_dpis[start:start + len(chunk)] or [RECEIPT_OVERFLOW_DPI])
        batches.append(ReceiptBatch(
            index=len(batches),
            first_page=start + 1,
            last_page=start + len(chunk),
            images=chunk,
            dpi=chunk_dpi,
            source_path=file_path,
        ))
    logger.info(
        "%s: %d pages, text density %.2f → %d vision batches of %d",
        file_path.name, page_count, density, len(batches), RECEIPT_BATCH_SIZE,
    )
    return ReceiptPlan(
        mode="batched", media_type="image/png", images=images, text_content=None,
        pages_total=page_count, pages_used=pages_used, batches=batches,
        text_density=density,
    )


def _callback_takes_mode(callback) -> bool:
    """True when a progress callback declares a ``mode`` keyword.

    The callback contract is ``progress_cb(pages_done, pages_total)`` and plenty
    of callers pass a two-argument lambda. ``mode`` is therefore offered rather
    than assumed: a callback that does not declare it (or ``**kwargs``) is called
    with two arguments only.
    """
    if callback is None:
        return False
    try:
        params = inspect.signature(callback).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False
    if "mode" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _PageProgress:
    """Calls the caller's ``progress_cb(pages_done, pages_total)`` after each batch.

    The job queue that owns ``server/**`` drives a progress bar off this. It is
    deliberately forgiving: a callback that raises must never fail an extraction
    that already succeeded.

    ``mode`` says how the document is being read, because a page counter is a lie
    for a document that is one LLM call: ``"one_pass"`` means the whole thing
    goes up in a single call and only the elapsed clock can move until it
    answers; ``"paged"`` means genuine page batches, where a page counter is true.
    It is reported once before the first call (``announce()``) so the UI is right
    from the first second, and again with every advance.
    """

    def __init__(self, callback, pages_total: int, mode: str | None = None) -> None:
        self._cb = callback
        self._total = max(1, int(pages_total or 1))
        self._done = 0
        self._mode = mode
        self._cb_takes_mode = _callback_takes_mode(callback)

    async def announce(self) -> None:
        """Report the mode before any page has been read. Zero progress, real mode."""
        await self._emit()

    async def advance(self, pages: int) -> None:
        self._done = min(self._total, self._done + max(0, int(pages)))
        await self._emit()

    async def _emit(self) -> None:
        if self._cb is None:
            return
        try:
            if self._cb_takes_mode:
                outcome = self._cb(self._done, self._total, mode=self._mode)
            else:
                outcome = self._cb(self._done, self._total)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Future):
                await outcome
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("progress_cb raised (ignored): %s", exc)


# Fields whose value is an identity of the whole document, not of one page.
_RECEIPT_IDENTITY_FIELDS = (
    "vendor", "date", "payment_date", "currency", "currency_marker",
    "payment_method", "invoice_number", "purpose",
)
# Fields that only make sense alongside the grand total.
_RECEIPT_TOTAL_SIBLINGS = ("subtotal", "gst", "hst", "pst", "other_tax", "amount")
_NOT_FINANCIAL = "NOT_A_FINANCIAL_DOCUMENT"


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("", "null", "none", "n/a")
    return False


def _is_continuation_batch(result: dict) -> bool:
    """True when the model said these pages continue an earlier page's table."""
    raw = result.get("continuation")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1")
    return False


# Keys a batch answer can carry that are not content. The continuation flag is a
# claim ABOUT the page rather than anything read off it; a bare currency with no
# figure anywhere beside it settles nothing about whether these pages were legible
# — the prompt asks for `currency` only where the document states one, and a
# page that shows a currency code and no number is still a page nothing was read
# off. `currency_marker` rides with it for the same reason, and because a model
# that can only make out a dollar sign has still read nothing.
# `_`-prefixed keys are added by this module (provider, telemetry).
_BATCH_NON_CONTENT_KEYS = frozenset({"continuation", "currency", "currency_marker"})


def _batch_read_nothing(result: dict) -> bool:
    """True when a page batch came back with nothing at all on it.

    No vendor, no amount, no invoice number, no date — nothing the model could
    only know by reading the pixels. That means it was shown a page it could not
    resolve into anything, which on a scan is far more often contrast than
    content: see ``_retry_batch_at_higher_dpi``.

    ``continuation`` deliberately does not count as content. A page rendered in
    near-white grey can come back as exactly ``{"continuation": true}`` — the
    model could not read a word and still answered the one question that takes no
    reading. A page that genuinely is mid-table says more than that (a running
    subtotal, the invoice number in the header, the vendor in the running head),
    so requiring the marker to be ABSENT would make this retry almost
    unreachable.

    ``NOT_A_FINANCIAL_DOCUMENT`` counts as a vendor: that is a verdict the model
    reached by reading the page, not a failure to read it.
    """
    if not isinstance(result, dict):
        return False
    for key, value in result.items():
        if key.startswith("_") or key in _BATCH_NON_CONTENT_KEYS:
            continue
        if isinstance(value, bool):
            # "has_grand_total": true with no figure beside it is a claim, not
            # a figure. The figure itself, if there is one, is another key.
            continue
        if isinstance(value, (list, dict, tuple)):
            if value:
                return False
            continue
        if not _is_blank(value):
            return False
    return True


async def _retry_batch_at_higher_dpi(
    batch: ReceiptBatch,
    plan: ReceiptPlan,
    call,
    *,
    previous: dict | None = None,
    why: str = "read nothing",
) -> tuple[bool, dict | None]:
    """Re-render one batch at RECEIPT_RETRY_DPI and ask again. Once per batch.

    Two callers, one mechanism: a batch that read nothing at all
    (``_batch_read_nothing``) and a batch whose grand total was read at reduced
    resolution (``_verify_grand_total_at_full_dpi``). ``why`` is the phrase the
    log line uses to say which.

    Returns ``(asked_again, better_result)``. ``asked_again`` is False when a
    re-render was not possible or could not help — the batch already rendered at
    the retry DPI, the source PDF is not on hand, PyMuPDF refused — so the
    caller's retry count stays a count of calls actually spent. ``better_result``
    is the second answer only when it read something; otherwise the caller keeps
    the first answer and a genuinely blank page costs one extra call and nothing
    else. Re-rendering is CPU-bound (PyMuPDF rasterising pages), so it goes to a
    thread exactly like the first render did.

    An accepted second answer carries ``previous``'s telemetry forward — the
    first call happened and has to stay visible in ``extraction_log`` — and
    moves ``batch.dpi`` up to RECEIPT_RETRY_DPI, because this batch's answer now
    comes from sharper pixels. That is also what stops the next rule from
    spending a third call on the same pages.
    """
    if batch.source_path is None or batch.dpi >= RECEIPT_RETRY_DPI:
        return False, None
    page_numbers = list(range(batch.first_page - 1, batch.last_page))
    try:
        images = await asyncio.to_thread(
            _render_pdf_pages, batch.source_path, page_numbers, RECEIPT_RETRY_DPI
        )
    except Exception as exc:
        logger.warning(
            "could not re-render %s at %d DPI: %s",
            batch.label, RECEIPT_RETRY_DPI, exc,
        )
        return False, None
    if not images:
        return False, None

    logger.info(
        "%s %s at %d DPI — re-rendering at %d DPI and asking again",
        batch.label, why, batch.dpi, RECEIPT_RETRY_DPI,
    )
    prompt = build_extraction_prompt(
        batch=(batch.first_page, batch.last_page), pages_total=plan.pages_total
    )
    result = await call(
        images, plan.media_type, prompt, None, RECEIPT_BATCH_MAX_OUTPUT_TOKENS, False,
    )
    if _batch_read_nothing(result):
        logger.info("%s read nothing at %d DPI either — keeping the first answer",
                    batch.label, RECEIPT_RETRY_DPI)
        return True, None
    earlier = (previous or {}).get("_telemetry") or []
    if earlier:
        result["_telemetry"] = list(earlier) + list(result.get("_telemetry") or [])
    batch.dpi = RECEIPT_RETRY_DPI
    return True, result


def _claimed_grand_total_page(result: dict) -> int | None:
    """The page the model says carries the grand total, or None if it claims none.

    Accepts the boolean form too — models occasionally answer
    ``"has_grand_total": true`` instead of a page number — in which case the
    batch's own last page stands in (returned as 0 for "claimed, page unknown").
    """
    for key in ("grand_total_page", "grand_total_on_page", "total_page"):
        raw = result.get(key)
        if isinstance(raw, bool):
            continue
        if raw is None or _is_blank(raw):
            continue
        try:
            page = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if page > 0:
            return page
    for key in ("has_grand_total", "shows_grand_total"):
        raw = result.get(key)
        if raw is True or (isinstance(raw, str) and raw.strip().lower() in ("true", "yes")):
            return 0
    return None


def _batch_total_figure(result: dict) -> tuple[Decimal, object]:
    """``(amount, raw)`` of the grand total one batch answer carries.

    ``amount`` is 0 when the batch reported no figure at all — the same "total,
    else amount" order the merge and the single-call path use.
    """
    raw = result.get("total")
    if _is_blank(raw):
        raw = result.get("amount")
    return _safe_decimal(raw), raw


def _grand_total_candidates(
    pairs: list[tuple[ReceiptBatch, dict]],
) -> list[tuple[int, ReceiptBatch, dict, Decimal, object]]:
    """Every batch claiming to see the document's grand total, in merge order.

    ``(claimed_page, batch, result, amount, raw_amount)`` sorted by the page the
    claim names and then by batch order, so the LAST entry is the claim the merge
    accepts (a grand total is printed at the END of an invoice).

    Shared by ``_merge_receipt_batches`` and ``_verify_grand_total_at_full_dpi``
    on purpose: the batch that gets re-read sharper must be the batch whose
    figure is going to be used, and two copies of this ordering would drift.
    """
    candidates: list[tuple[int, ReceiptBatch, dict, Decimal, object]] = []
    for batch, result in pairs:
        claimed_page = _claimed_grand_total_page(result)
        if claimed_page is None:
            continue
        amount, raw_total = _batch_total_figure(result)
        if amount == 0:
            continue
        candidates.append((claimed_page or batch.last_page, batch, result, amount, raw_total))
    candidates.sort(key=lambda c: (c[0], c[1].index))
    return candidates


def _distinct_totals(
    candidates: list[tuple[int, ReceiptBatch, dict, Decimal, object]],
) -> list[Decimal]:
    """The genuinely different figures among grand-total claims.

    Two claims a cent or two apart are the same figure read twice, not a
    disagreement — rounding and OCR both land there.
    """
    distinct: list[Decimal] = []
    for _page, _batch, _result, amount, _raw in candidates:
        if not any(
            abs(amount - seen) <= RECEIPT_TOTAL_AGREEMENT_TOLERANCE for seen in distinct
        ):
            distinct.append(amount)
    return distinct


async def _verify_grand_total_at_full_dpi(
    pairs: list[tuple[ReceiptBatch, dict]], plan: ReceiptPlan, call
) -> tuple[int, int | None, list[str]]:
    """Re-read a grand total that was read at reduced resolution, before trusting it.

    Two rules, one pass, because they land on the same pages and share one budget:

    1. **A grand total read at RECEIPT_BATCH_DPI is re-read at RECEIPT_RETRY_DPI
       before it is trusted.** The sharper reading is the one that counts. If the
       two readings differ by more than a couple of cents, the merged result is
       flagged ``_confidence="low"`` with a reason naming both figures and both
       DPIs: the pixels changed the number, so a human looks at it even though
       the sharper reading is almost certainly the right one.
    2. **Two batches that disagree on the total are both re-read first.**
       Without this pass the disagreement is flagged and the later page's figure
       carried; a re-render can settle it outright. If the sharper readings
       agree, the document is accepted with no flag; if they still disagree, the
       merge's low-confidence flag stands with both figures in it.

    Together they spend at most RECEIPT_TOTAL_VERIFY_MAX_CALLS extra calls on one
    document. ``pairs`` is updated in place wherever a sharper reading replaced a
    batch's own first answer.

    Returns ``(extra_calls, total_verified_at_dpi, reasons)``.
    ``total_verified_at_dpi`` is the DPI the accepted figure was really read at —
    RECEIPT_RETRY_DPI whether it was re-read or rendered that sharp to begin
    with — and None when no batch claimed to see a grand total at all, because
    then there is nothing to verify and the merge already says so.
    """
    candidates = _grand_total_candidates(pairs)
    if not candidates:
        return 0, None, []

    reasons: list[str] = []
    disagreement = len(_distinct_totals(candidates)) > 1
    if disagreement:
        # Rule 2: every claimant a re-render could sharpen, not only the winner —
        # the disagreement is between them, so one sharper reading settles nothing.
        targets = [c for c in candidates if c[1].dpi < RECEIPT_RETRY_DPI]
        why = "disagrees with another batch on the grand total"
    else:
        # Rule 1: the claim the merge is about to accept.
        chosen = candidates[-1]
        targets = [chosen] if chosen[1].dpi < RECEIPT_RETRY_DPI else []
        why = "reported the grand total"

    calls = 0
    for _page, batch, result, amount, _raw in targets:
        if calls >= RECEIPT_TOTAL_VERIFY_MAX_CALLS:
            logger.warning(
                "the %d-call grand-total re-read budget ran out before %s",
                RECEIPT_TOTAL_VERIFY_MAX_CALLS, batch.label,
            )
            reasons.append(
                f"the grand-total re-read budget ({RECEIPT_TOTAL_VERIFY_MAX_CALLS} "
                f"calls) ran out before {batch.label} could be re-read at "
                f"{RECEIPT_RETRY_DPI} DPI"
            )
            break
        read_at = batch.dpi
        asked_again, better = await _retry_batch_at_higher_dpi(
            batch, plan, call, previous=result, why=why,
        )
        if asked_again:
            calls += 1
        if better is None:
            continue
        for position, (existing, _old) in enumerate(pairs):
            if existing is batch:
                pairs[position] = (batch, better)
                break
        sharper, _sharper_raw = _batch_total_figure(better)
        if (
            not disagreement
            and abs(sharper - amount) > RECEIPT_TOTAL_AGREEMENT_TOLERANCE
        ):
            reason = (
                f"the grand total on {batch.label} read {amount} at {read_at} DPI "
                f"and {sharper} at {RECEIPT_RETRY_DPI} DPI; the "
                f"{RECEIPT_RETRY_DPI} DPI reading was taken — needs a human"
            )
            logger.warning("%s", reason)
            reasons.append(reason)

    final = _grand_total_candidates(pairs)
    if not final:
        # The sharper reading no longer claims the grand total. The merge falls
        # back to the last figure any batch reported and flags it; calling
        # anything "verified" here would be a lie.
        return calls, None, reasons

    if disagreement and calls:
        if len(_distinct_totals(final)) > 1:
            reasons.append(
                f"re-reading at {RECEIPT_RETRY_DPI} DPI did not resolve the disagreement"
            )
        else:
            logger.info(
                "the %d DPI re-read resolved the grand-total disagreement on %s",
                RECEIPT_RETRY_DPI, final[-1][1].label,
            )

    verified = final[-1][1]
    if verified.dpi < RECEIPT_RETRY_DPI:
        reasons.append(
            f"the grand total on {verified.label} was read at {verified.dpi} DPI and "
            f"could not be re-read at {RECEIPT_RETRY_DPI} DPI"
        )
    return calls, verified.dpi, reasons


def _merge_receipt_batches(
    pairs: list[tuple[ReceiptBatch, dict]],
    *,
    pages_total: int,
    pages_used: int,
    notes: list[str] | None = None,
) -> dict:
    """Merge per-batch extractions of one long receipt into a single result.

    The rules, which are the whole point of the batched path:

    * **Identity** (vendor, date, invoice number, currency and the marker it is
      printed with, payment method, purpose) comes from the first batch that
      reports it — a blank marker is not a report — preferring batches the model
      did NOT mark as a continuation: a continuation page repeats a table, not a
      letterhead.
    * **total** may only come from a batch that says it can see the document's
      grand total. A page subtotal or "carried forward" figure is not a total,
      which is why the batch prompt asks which page carries the grand total.
    * **Two batches asserting different grand totals** is not something to
      resolve by picking one: the result is flagged ``_confidence="low"`` with a
      reason naming both, so a human decides. The later page's figure is carried
      so the document is still usable, but it is never presented as certain.
      ``_verify_grand_total_at_full_dpi`` runs before this and re-reads both
      claimants sharper, so what reaches here is a disagreement that persists at
      the retry DPI.
    * **Tax breakdown** follows the accepted total's batch — taxes are printed
      with the grand total, not on continuation pages.
    * **line_items** concatenate in page order.
    * ``pages_total`` / ``pages_used`` are always recorded, so a truncated or
      partly-failed read is visible instead of silent.

    ``notes`` are reasons found before the merge (the grand-total re-read pass)
    that belong in the same ``_confidence_reason``, so the user reads one
    explanation rather than two.
    """
    merged: dict = {}
    reasons: list[str] = list(notes or [])
    results = [result for _batch, result in pairs]

    # ── Identity: non-continuation batches first, page order within each group ──
    ordered = [p for p in pairs if not _is_continuation_batch(p[1])]
    ordered += [p for p in pairs if _is_continuation_batch(p[1])]

    for field_name in _RECEIPT_IDENTITY_FIELDS:
        for _batch, result in ordered:
            value = result.get(field_name)
            if _is_blank(value):
                continue
            if field_name == "vendor" and str(value).strip() == _NOT_FINANCIAL:
                continue
            merged[field_name] = value
            break

    if "vendor" not in merged and any(
        str(r.get("vendor", "")).strip() == _NOT_FINANCIAL for r in results
    ):
        # Every page that had an opinion said this is not a financial document.
        merged["vendor"] = _NOT_FINANCIAL

    # ── The grand total ───────────────────────────────────────────────────
    candidates = _grand_total_candidates(pairs)
    total_batch: dict | None = None

    if candidates:
        distinct = _distinct_totals(candidates)
        # The grand total is printed at the END of an invoice, so when two
        # claimants disagree the later page is the likelier one — but the
        # disagreement itself is what gets reported.
        chosen = candidates[-1]
        merged["total"] = chosen[4]
        total_batch = chosen[2]
        if len(distinct) > 1:
            detail = ", ".join(
                f"{amount} from {batch.label}"
                for _page, batch, _result, amount, _raw in candidates
            )
            reasons.append(
                f"page batches disagree on the grand total ({detail}); "
                f"carried {chosen[3]} from {chosen[1].label} — needs a human"
            )
    else:
        # Nobody claimed to see a grand total. Carry the last non-zero figure any
        # batch reported so the document is still usable, and say so.
        fallback = None
        for batch, result in pairs:
            raw = result.get("total")
            if _is_blank(raw):
                raw = result.get("amount")
            if _safe_decimal(raw) != 0:
                fallback = (batch, result, raw)
        if fallback is not None:
            merged["total"] = fallback[2]
            total_batch = fallback[1]
            reasons.append(
                "no page batch reported the document's grand total; carried "
                f"{fallback[2]} from {fallback[0].label}"
            )
        else:
            reasons.append("no page batch reported any amount")

    # ── Tax breakdown: with the total, else the first batch that has any ──
    for field_name in _RECEIPT_TOTAL_SIBLINGS:
        value = total_batch.get(field_name) if total_batch else None
        if _is_blank(value):
            for _batch, result in pairs:
                candidate = result.get(field_name)
                if not _is_blank(candidate):
                    value = candidate
                    break
        if not _is_blank(value):
            merged[field_name] = value

    # ── Line items concatenate, in page order ────────────────────────────
    line_items: list = []
    for _batch, result in pairs:
        items = result.get("line_items")
        if isinstance(items, list):
            line_items.extend(item for item in items if isinstance(item, dict))
    if line_items:
        merged["line_items"] = line_items

    # ── Provenance, telemetry, page accounting ───────────────────────────
    telemetry: list[ExtractionAttempt] = []
    for result in results:
        telemetry.extend(result.get("_telemetry") or [])
    if telemetry:
        merged["_telemetry"] = telemetry
    for result in results:
        if result.get("_provider"):
            merged["_provider"] = result["_provider"]
            merged["_model"] = result.get("_model")
            break

    merged["pages_total"] = pages_total
    merged["pages_used"] = pages_used
    merged["_receipt_mode"] = "batched"
    merged["_page_batches"] = len(pairs)
    if pages_used < pages_total:
        reasons.append(
            f"only the first {pages_used} of {pages_total} pages were read "
            f"(the vision limit is {RECEIPT_MAX_VISION_PAGES})"
        )
    if any(result.get("_confidence") == "low" for result in results):
        reasons.append("at least one page batch came back low confidence")

    is_valid, _warnings = validate_extraction(merged)
    if not is_valid:
        reasons.append("no amount could be extracted from any page")

    if reasons:
        merged["_confidence"] = "low"
        merged["_confidence_reason"] = "; ".join(reasons)
        logger.warning("receipt page-batch merge flagged low: %s", merged["_confidence_reason"])
    return merged


async def _extract_with_gemini(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    db=None,
    text_content: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    require_amount: bool = True,
) -> dict:
    """Extract document data through Gemini with structured JSON output.

    The model is whichever rung of ``gemini_config.model_candidates`` the caller
    has reached; the top rung is the default, because the rungs below it read
    handwritten notes, low-contrast scans, rotated images and crowded paper
    receipts less reliably.
    """
    from google import genai
    from google.genai import types

    from config.settings import gemini_config

    client = genai.Client(api_key=api_key or gemini_config.api_key)
    model = model or gemini_config.model

    # Build content parts: text prompt + images (or text document content, or —
    # for a long machine-generated invoice — both)
    if text_content:
        parts = [types.Part.from_text(
            text=f"{system_prompt}\n\nExtract data from this document ({file_path.name}).\n\nDocument content:\n{text_content}"
        )]
    else:
        parts = [types.Part.from_text(text=f"{system_prompt}\n\nExtract data from this document.")]
    for img_bytes in image_data:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=media_type))

    start = time.monotonic()
    # At most two attempts, and only for transient server-side faults. A quota
    # rejection raises immediately so extract_document can fail over to OpenAI
    # while the user's request is still alive.
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=types.Content(role="user", parts=parts),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                ),
                timeout=LLM_CALL_TIMEOUT_S,
            )
            break
        except Exception as e:
            if _is_quota_error(e):
                logger.warning(
                    "Gemini quota/rate rejection for %s (%s) — failing over to OpenAI immediately",
                    file_path.name, model,
                )
                raise
            if attempt == 0 and not isinstance(e, asyncio.TimeoutError):
                logger.warning(
                    "Gemini transient error for %s, one retry in %.0fs: %s",
                    file_path.name, _TRANSIENT_RETRY_BACKOFF_S, e,
                )
                await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_S)
            else:
                raise
    latency_ms = int((time.monotonic() - start) * 1000)

    # Cost tracking — log token usage if db is available
    _tokens_in: int | None = None
    _tokens_out: int | None = None
    _cost: float | None = None
    if hasattr(response, 'usage_metadata') and response.usage_metadata:
        _tokens_in = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
        _tokens_out = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
        _cost = (_tokens_in * _GEMINI_INPUT_COST_PER_TOKEN + _tokens_out * _GEMINI_OUTPUT_COST_PER_TOKEN)
        if db is not None:
            try:
                db.log_api_call("extraction", model, _tokens_in, _tokens_out, _cost)
            except Exception as e:
                logger.debug("Failed to log Gemini API cost: %s", e)

    raw_content = response.text
    result = parse_extraction_response(raw_content)
    is_valid, _warnings = validate_extraction(result, require_amount=require_amount)

    if not is_valid:
        # Log but don't fall back — Gemini returned valid JSON, just low confidence
        logger.warning("Gemini extraction low confidence for %s: %s", file_path.name, _warnings)
        result["_confidence"] = "low"

    result["_provider"] = "gemini"
    result["_model"] = model
    result["_telemetry"] = result.get("_telemetry", []) + [ExtractionAttempt(
        model=model,
        confidence=_string_to_numeric_confidence(result.get("_confidence")),
        latency_ms=latency_ms,
        tokens_in=_tokens_in,
        tokens_out=_tokens_out,
        cost_usd=round(_cost, 6) if _cost is not None else None,
        fallback_from=None,
        error=None,
    )]
    return result


async def _try_gemini_candidates(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    db=None,
    text_content: str | None = None,
    telemetry: list[ExtractionAttempt] | None = None,
    api_key: str | None = None,
    require_amount: bool = True,
) -> dict:
    """Try each configured Gemini model in turn; raise if all are exhausted.

    Two error classes advance to the next rung:

    * **Capacity** (429 / quota). The next model has its own daily bucket.
    * **Malformed output** (ValueError from parse/validate). JSON reliability
      is model-specific, so this is emphatically *not* a fault the next model
      shares. A model that returns invalid JSON on one document may still read
      that document more accurately than the next provider does, so conceding the
      provider on malformed output can trade a retryable parse failure for a
      wrong number. Staying on the same provider one rung longer is both cheaper
      and safer.

    Everything else — auth failures, a corrupt image, and especially timeouts
    — propagates immediately. A timeout means systemic slowness, and spending
    another LLM_CALL_TIMEOUT_S per remaining rung is what pushes the whole
    upload past the request timeout in front of it.
    """
    from config.settings import gemini_config

    candidates = gemini_config.model_candidates
    last_exc: Exception | None = None

    for i, model in enumerate(candidates):
        started = time.monotonic()
        try:
            return await _extract_with_gemini(
                file_path, image_data, media_type, system_prompt,
                db=db, text_content=text_content, model=model, api_key=api_key,
                require_amount=require_amount,
            )
        except Exception as exc:
            last_exc = exc
            if telemetry is not None:
                telemetry.append(ExtractionAttempt(
                    model=model,
                    confidence=0.0,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    fallback_from=None,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                ))
            is_capacity = _is_quota_error(exc)
            is_bad_output = isinstance(exc, ValueError) and not isinstance(exc, asyncio.TimeoutError)
            # Models are retired per project: a key issued in a new project can
            # get 404 "no longer available" for an older model. That is as
            # model-specific as it gets, so try the next rung rather than
            # concede the provider.
            _t = str(exc).lower()
            is_model_gone = "404" in _t or "not_found" in _t or "no longer available" in _t
            if not (is_capacity or is_bad_output or is_model_gone) or i == len(candidates) - 1:
                raise
            logger.warning(
                "Gemini model %s %s for %s; trying %s",
                model,
                "is out of quota" if is_capacity else "returned unusable output",
                file_path.name, candidates[i + 1],
            )

    raise last_exc if last_exc else RuntimeError("no Gemini candidates configured")


def _finish_reason(response) -> str | None:
    """The first choice's finish_reason, or None when the shape is unexpected.

    ``"length"`` means the model ran out of output budget mid-JSON: the answer is
    truncated, not wrong, and the fix is a bigger budget rather than a corrective
    prompt. Never raises — a mock or an odd provider payload just yields None.
    """
    try:
        reason = response.choices[0].finish_reason
    except Exception:
        return None
    return reason if isinstance(reason, str) else None


def _build_chat_messages(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    text_content: str | None,
) -> list[dict]:
    """Build chat-completions messages for a document (text or vision).

    Shared by every OpenAI-protocol provider — the local endpoint and hosted
    OpenAI see byte-identical prompts, which is what makes their extractions
    comparable.
    """
    import base64

    if text_content and not image_data:
        # Text-based document (xlsx, csv, docx) — send as text, no vision
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Extract data from this document ({file_path.name}).\n\nDocument content:\n{text_content}",
            },
        ]

    image_parts = []
    for img_bytes in image_data:
        b64_img = base64.b64encode(img_bytes).decode("utf-8")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64_img}"},
        })

    instruction = f"Extract data from this document: {file_path.name}"
    if text_content:
        # Long machine-generated invoice: the text layer carries every page, the
        # attached image is page 1 for the letterhead the text layer can't show.
        instruction = (
            f"Extract data from this document ({file_path.name}). The image is page 1 "
            "(letterhead / logo). The full text layer of every page follows.\n\n"
            f"Document text:\n{text_content}"
        )

    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                *image_parts,
            ],
        },
    ]


async def _extract_via_chat_completions(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    *,
    client,
    model: str,
    provider: str,
    db=None,
    text_content: str | None = None,
    timeout_s: float = LLM_CALL_TIMEOUT_S,
    create_kwargs: dict | None = None,
    input_cost_per_token: float = 0.0,
    output_cost_per_token: float = 0.0,
    record_zero_cost: bool = False,
    require_amount: bool = True,
) -> dict:
    """Run one document through any OpenAI-protocol endpoint.

    The whole call/parse/retry/telemetry body, parameterized by client, model
    and pricing, so the local provider and hosted OpenAI share it instead of
    drifting apart in two copies.
    """
    messages = _build_chat_messages(
        file_path, image_data, media_type, system_prompt, text_content
    )
    kwargs = dict(create_kwargs or {})

    start = time.monotonic()
    _tokens_in_total = 0
    _tokens_out_total = 0
    _cost_total = 0.0

    # A couple of short retries are worth it — but they stay inside the latency
    # budget. Sleeping 30-60s for a rate limit just guarantees the browser times
    # out first. A connection error is not retried here: it means the provider
    # is down, and the next provider in the chain is the better answer.
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
        except APIStatusError as e:
            if e.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                wait = _TRANSIENT_RETRY_BACKOFF_S * (attempt + 1)
                logger.warning(
                    "%s API error %d on attempt %d, retrying in %.0fs: %s",
                    provider, e.status_code, attempt + 1, wait, file_path.name,
                )
                await asyncio.sleep(wait)
            else:
                raise

    def _log_cost(resp):
        """Record token usage. Aggregates into the outer totals for telemetry."""
        nonlocal _tokens_in_total, _tokens_out_total, _cost_total
        if hasattr(resp, 'usage') and resp.usage:
            try:
                in_tokens = int(resp.usage.prompt_tokens or 0)
                out_tokens = int(resp.usage.completion_tokens or 0)
            except (TypeError, ValueError):
                # Non-numeric usage (e.g. MagicMock in tests) — skip.
                return
            cost = in_tokens * input_cost_per_token + out_tokens * output_cost_per_token
            _tokens_in_total += in_tokens
            _tokens_out_total += out_tokens
            _cost_total += cost
            if db is not None:
                try:
                    # Zero-cost providers still log, so usage shows up in the
                    # API-call ledger instead of looking like no work happened.
                    db.log_api_call("extraction", model, in_tokens, out_tokens, cost)
                except Exception as e:
                    logger.debug("Failed to log %s API cost: %s", provider, e)

    def _attach_telemetry(result_dict: dict) -> dict:
        latency_ms = int((time.monotonic() - start) * 1000)
        result_dict["_provider"] = provider
        result_dict["_model"] = model
        if _cost_total > 0:
            cost_usd = round(_cost_total, 6)
        else:
            cost_usd = 0.0 if record_zero_cost else None
        result_dict["_telemetry"] = result_dict.get("_telemetry", []) + [ExtractionAttempt(
            model=model,
            confidence=_string_to_numeric_confidence(result_dict.get("_confidence")),
            latency_ms=latency_ms,
            tokens_in=_tokens_in_total or None,
            tokens_out=_tokens_out_total or None,
            cost_usd=cost_usd,
            fallback_from=None,
            error=None,
        )]
        logger.info(
            "%s: provider=%s model=%s latency_ms=%d tokens_in=%s tokens_out=%s cost_usd=%s",
            file_path.name, provider, model, latency_ms,
            _tokens_in_total or 0, _tokens_out_total or 0, cost_usd,
        )
        return result_dict

    raw_content = response.choices[0].message.content
    _log_cost(response)

    # An output budget that was too small is a recoverable failure, not a wrong
    # answer: the JSON is simply cut off mid-field. Doubling the budget once and
    # re-asking is far more accurate than letting the corrective retry below
    # reason about truncated output.
    if _finish_reason(response) == "length" and kwargs.get("max_tokens"):
        budget = int(kwargs["max_tokens"])
        kwargs = {**kwargs, "max_tokens": budget * 2}
        logger.warning(
            "%s/%s hit the %d-token output budget on %s — one retry at %d",
            provider, model, budget, file_path.name, budget * 2,
        )
        response = await asyncio.wait_for(
            client.chat.completions.create(model=model, messages=messages, **kwargs),
            timeout=timeout_s,
        )
        raw_content = response.choices[0].message.content
        _log_cost(response)

    try:
        result = parse_extraction_response(raw_content)
        is_valid, _warnings = validate_extraction(result, require_amount=require_amount)
        if is_valid:
            return _attach_telemetry(result)
    except ValueError:
        pass

    # Retry once with corrective prompt
    retry_messages = messages + [
        {"role": "assistant", "content": raw_content},
        {"role": "user", "content": "The previous response was invalid. Please try again and return only valid JSON."},
    ]
    response2 = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=retry_messages,
            **kwargs,
        ),
        timeout=timeout_s,
    )
    raw_content2 = response2.choices[0].message.content
    _log_cost(response2)
    result = parse_extraction_response(raw_content2)

    is_valid, _warnings = validate_extraction(result, require_amount=require_amount)
    if not is_valid:
        result["_confidence"] = "low"

    return _attach_telemetry(result)


async def _extract_with_local(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    local_client=None,
    db=None,
    text_content: str | None = None,
    max_tokens: int | None = RECEIPT_MAX_OUTPUT_TOKENS,
    require_amount: bool = True,
) -> dict:
    """Extract document data using the locally hosted model (PRIMARY provider).

    Self-hosted, so the document never leaves the machine, and the provider tried
    first. Temperature 0 and a JSON response format because extraction is a
    transcription task, not a creative one; thinking is disabled because a
    <think> block adds latency and breaks strict JSON parsing.

    A dead endpoint (local model offline, vLLM not serving) is re-raised as
    `LocalLLMUnreachableError` so the API layer answers 503 with what happened
    instead of 500 "unexpected" — see core/llm_errors.py.
    """
    from config.settings import local_llm_config
    from core.llm_errors import as_local_unreachable, is_local_endpoint_down

    cfg = local_llm_config
    if local_client is None:
        local_client = _get_local_client()

    async with _get_local_semaphore():
        try:
            return await _extract_via_chat_completions(
                file_path, image_data, media_type, system_prompt,
                client=local_client,
                model=cfg.model,
                provider="local",
                db=db,
                text_content=text_content,
                timeout_s=cfg.timeout_s,
                create_kwargs={
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "extra_body": cfg.extra_body,
                    # Sized to the trimmed schema (see RECEIPT_MAX_OUTPUT_TOKENS);
                    # a truncated answer is caught by finish_reason and re-asked
                    # once with double the budget.
                    **({"max_tokens": max_tokens} if max_tokens else {}),
                },
                require_amount=require_amount,
                # Self-hosted endpoint: no per-token cost to track.
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
                record_zero_cost=True,
            )
        except Exception as exc:
            if is_local_endpoint_down(exc):
                logger.warning(
                    "local endpoint %s unreachable for %s: %s",
                    cfg.base_url, file_path.name, exc,
                )
                raise as_local_unreachable(exc) from exc
            raise


async def _extract_with_openai(
    file_path: Path,
    image_data: list[bytes],
    media_type: str,
    system_prompt: str,
    openai_client=None,
    db=None,
    text_content: str | None = None,
    require_amount: bool = True,
) -> dict:
    """Extract document data using OpenAI GPT-4.1 (optional last-resort fallback)."""
    if openai_client is None:
        openai_client = _get_default_client()

    return await _extract_via_chat_completions(
        file_path, image_data, media_type, system_prompt,
        client=openai_client,
        model=openai_config.model,
        provider="openai",
        db=db,
        text_content=text_content,
        require_amount=require_amount,
        timeout_s=LLM_CALL_TIMEOUT_S,
        input_cost_per_token=0.000002,   # GPT-4.1 pricing
        output_cost_per_token=0.000008,
    )


def _needs_second_opinion(result: dict) -> bool:
    """True when a provider returned a null vendor on an otherwise-valid doc.

    Providers sometimes return syntactically-valid JSON with a null `vendor` on
    image documents (notably JPGs where the vendor name is rendered as a logo).
    When the one field every financial document has is missing but date and
    total ARE populated, the next provider in the chain gets a shot at a
    field-complete second opinion. Engine-level rule — it applies to every
    provider and every document, not only to scanned images.
    """
    vendor_raw = result.get("vendor")
    vendor_str = str(vendor_raw).strip() if vendor_raw is not None else ""
    if vendor_str:
        return False  # includes NOT_A_FINANCIAL_DOCUMENT — a deliberate verdict
    total_raw = result.get("total")
    date_raw = result.get("date")
    has_total = total_raw not in (None, "", "0", "0.00") and _safe_decimal(total_raw) > 0
    has_date = date_raw not in (None, "")
    return has_total and has_date


def _adopt_text_layer_currency_marker(result: dict, text_content: str | None) -> None:
    """Take the currency marker off the document's own text layer, not the model.

    Machine-generated PDFs (and xlsx/csv/docx) reach the model as text, so the
    characters printed beside the amounts are on hand for free — and they are a
    better witness than a transcription of them. Used only when the model quoted
    nothing that names a currency, and only when every clear marker in the text
    agrees (``core.document_currency.marker_from_text``). A bare ``$`` is never
    adopted: it is not evidence of anything.

    Mutates ``result`` in place and never raises — a marker is an improvement to
    an extraction that already succeeded, never a reason to fail it.
    """
    if not text_content or not isinstance(result, dict):
        return
    try:
        if classify_currency_marker(result.get(_CURRENCY_MARKER_FIELD))[0]:
            return
        marker = marker_from_text(text_content)
        if marker:
            result[_CURRENCY_MARKER_FIELD] = marker
            result["_currency_marker_from"] = "text_layer"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("text-layer currency marker skipped: %s", exc)


async def _run_receipt_plan(plan: ReceiptPlan, call, progress: _PageProgress) -> dict:
    """Drive one provider across a receipt plan — one call, or one call per batch.

    ``call(images, media_type, prompt, text_content, max_tokens, require_amount)``
    is the provider's own extractor. Batched runs merge through
    ``_merge_receipt_batches``; every run records pages_total / pages_used and
    reports progress after each batch.

    A batched run additionally records ``dpi_retries`` (how many extra calls the
    two re-render rules spent) and, when some batch claimed to see the grand
    total, ``total_verified_at_dpi`` — the resolution the accepted figure was
    actually read at.
    """
    if plan.mode != "batched":
        prompt = build_extraction_prompt(
            pages_total=plan.pages_total if plan.mode == "text" else None,
            text_layer=plan.mode == "text",
        )
        result = await call(
            plan.images, plan.media_type, prompt, plan.text_content,
            RECEIPT_MAX_OUTPUT_TOKENS, True,
        )
        _adopt_text_layer_currency_marker(result, plan.text_content)
        await progress.advance(plan.pages_used)
        result["pages_total"] = plan.pages_total
        result["pages_used"] = plan.pages_used
        result["_receipt_mode"] = plan.mode
        return result

    if not plan.batches:
        # A PDF that reports pages but renders none is corrupt, not empty. Raising
        # lets the provider chain / the API say so instead of storing a document
        # with no vendor and no amount.
        raise ValueError("no page of this document could be rendered for extraction")

    pairs: list[tuple[ReceiptBatch, dict]] = []
    dpi_retries = 0
    for batch in plan.batches:
        prompt = build_extraction_prompt(
            batch=(batch.first_page, batch.last_page), pages_total=plan.pages_total
        )
        result = await call(
            batch.images, plan.media_type, prompt, None,
            RECEIPT_BATCH_MAX_OUTPUT_TOKENS, False,
        )
        # Nothing at all on these pages is a claim about the *scan*, not only
        # about the document: a faint page that survives 200 DPI can vanish at
        # 150. Re-render once, sharper, before that emptiness is merged in as
        # fact. Counted whether or not it helped — the count is the diagnostic.
        if _batch_read_nothing(result):
            asked_again, retried = await _retry_batch_at_higher_dpi(batch, plan, call)
            if asked_again:
                dpi_retries += 1
            if retried is not None:
                result = retried
        pairs.append((batch, result))
        await progress.advance(batch.page_count)

    # The grand total is the one figure on this document that reaches a CRA
    # filing, and 150 DPI is where a grey scan loses a digit. Re-read it sharper
    # before it is trusted, and re-read both sides of a disagreement before
    # reporting one. Bounded by RECEIPT_TOTAL_VERIFY_MAX_CALLS; every call it
    # spends is counted in dpi_retries, like the read-nothing retry above.
    verify_calls, verified_at_dpi, verify_reasons = await _verify_grand_total_at_full_dpi(
        pairs, plan, call
    )
    dpi_retries += verify_calls

    merged = _merge_receipt_batches(
        pairs, pages_total=plan.pages_total, pages_used=plan.pages_used,
        notes=verify_reasons,
    )
    merged["dpi_retries"] = dpi_retries
    if verified_at_dpi is not None:
        merged["total_verified_at_dpi"] = verified_at_dpi
    return merged


async def extract_document(
    file_path: Path,
    openai_client=None,
    db=None,
    gemini_api_key: str | None = None,
    local_client=None,
    progress_cb=None,
) -> dict:
    """Extract structured data from a document.

    Provider order is **local → Gemini → OpenAI**. The local endpoint leads
    because it keeps the document on the machine and needs no account anywhere;
    the two hosted providers are skipped entirely unless their key is non-empty,
    so a configuration with no hosted keys never sees a 401. If nothing is
    configured the caller gets a clear RuntimeError instead of a provider
    traceback.

    A provider that fails, or that returns a null vendor on an otherwise-valid
    financial document, advances to the next one. The last provider's answer is
    returned even if imperfect — a partial extraction the user can correct beats
    a failed upload.

    Page handling is decided by ``_build_receipt_plan``: up to four pages go in
    one vision call; a longer document is read from its text layer when it has
    one, and otherwise in batches of four pages that are merged by
    ``_merge_receipt_batches``. The result always carries ``pages_total`` and
    ``pages_used``.

    Concurrency is gated by _extraction_semaphore to prevent overwhelming
    LLM rate limits during bulk uploads.

    Args:
        file_path: Path to the document file.
        openai_client: Optional AsyncOpenAI client (for testing/override).
        db: Optional database instance for logging LLM costs.
        gemini_api_key: Optional per-call Gemini key, overriding the
            configured one for this call only.
        local_client: Optional AsyncOpenAI client for the local endpoint
            (for testing/override).
        progress_cb: Optional ``progress_cb(pages_done, pages_total)``, called
            after each page batch. Exceptions from it are logged and ignored.
    """
    sem = _get_extraction_semaphore()
    async with sem:
        from config.settings import gemini_config, local_llm_config
        from config.settings import openai_config as _oai_cfg

        # Measuring text density and rendering pages is CPU-bound and would
        # otherwise block the event loop, stalling every other in-flight request
        # (including other uploads) until it finished.
        plan = await asyncio.to_thread(_build_receipt_plan, file_path)
        # "single" and "text" are one LLM call over the whole document; only
        # "batched" reads it a few pages at a time. Saying which is what lets the
        # queue show an elapsed clock instead of a page counter that cannot move.
        progress = _PageProgress(
            progress_cb,
            plan.pages_total,
            mode="paged" if plan.mode == "batched" else "one_pass",
        )
        await progress.announce()

        telemetry: list[ExtractionAttempt] = []

        async def _run_local() -> dict:
            async def _call(images, media_type, prompt, text_content, max_tokens, require_amount):
                return await _extract_with_local(
                    file_path, images, media_type, prompt,
                    local_client=local_client, db=db, text_content=text_content,
                    max_tokens=max_tokens, require_amount=require_amount,
                )

            return await _run_receipt_plan(plan, _call, progress)

        async def _run_gemini() -> dict:
            async def _call(images, media_type, prompt, text_content, _max_tokens, require_amount):
                return await _try_gemini_candidates(
                    file_path, images, media_type, prompt,
                    db=db, text_content=text_content, telemetry=telemetry,
                    api_key=gemini_api_key, require_amount=require_amount,
                )

            return await _run_receipt_plan(plan, _call, progress)

        async def _run_openai() -> dict:
            async def _call(images, media_type, prompt, text_content, _max_tokens, require_amount):
                return await _extract_with_openai(
                    file_path, images, media_type, prompt, openai_client,
                    db=db, text_content=text_content, require_amount=require_amount,
                )

            return await _run_receipt_plan(plan, _call, progress)

        chain: list[tuple[str, object]] = []
        if local_llm_config.enabled:
            chain.append(("local", _run_local))
        if gemini_config.enabled:
            chain.append(("gemini", _run_gemini))
        # An explicitly injected client counts as configuration — that is how
        # the test suite and any caller with its own credentials opt in.
        if _oai_cfg.enabled or openai_client is not None:
            chain.append(("openai", _run_openai))

        if not chain:
            raise RuntimeError("no extraction provider configured")

        last_exc: Exception | None = None
        last_result: dict | None = None

        for i, (name, runner) in enumerate(chain):
            is_last = i == len(chain) - 1
            started = time.monotonic()
            try:
                result = await runner()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "%s extraction failed for %s (%s)%s",
                    name, file_path.name, exc,
                    "" if is_last else f"; trying {chain[i + 1][0]}",
                )
                continue

            last_result = result
            if is_last or not _needs_second_opinion(result):
                logger.info("Extracted %s via %s (%s)", file_path.name, name, result.get("_model"))
                break

            logger.info(
                "%s returned null vendor on %s despite valid date+total; "
                "asking %s for a field-complete second opinion",
                name, file_path.name, chain[i + 1][0],
            )
            # Preserve this attempt's telemetry so the escalation is observable.
            telemetry.append(ExtractionAttempt(
                model=result.get("_model") or name,
                confidence=_string_to_numeric_confidence(result.get("_confidence")),
                latency_ms=int((time.monotonic() - started) * 1000),
                fallback_from=None,
                error="null_vendor_on_financial_doc",
            ))

        if last_result is None:
            # Every configured provider failed. Surface the real error.
            raise last_exc if last_exc else RuntimeError("no extraction provider configured")

        # Merge earlier-provider telemetry with the successful attempt's, and
        # stamp fallback_from so a silent failover is visible in extraction_log.
        if telemetry:
            existing = last_result.get("_telemetry", [])
            for att in existing:
                if getattr(att, "fallback_from", None) is None:
                    att.fallback_from = telemetry[0].model
            last_result["_telemetry"] = telemetry + existing
        return last_result


async def record_extraction_attempts(
    db, user_id: str, receipt_id: int, telemetry: list[ExtractionAttempt],
    final_model: str | None = None, final_confidence: float | None = None,
) -> None:
    """Write one extraction_log row per attempt + update documents row.

    Best-effort — a telemetry failure must never fail a user's upload — but
    *loudly* best-effort: anything that stops a row landing (a rejected write, a
    missing column, an unavailable connection) logs at WARNING. At DEBUG the
    same failure is invisible, and a silent provider failover goes unnoticed
    with it.
    """
    if db is None or not telemetry:
        return
    try:
        conn_ctx = db._conn()
    except Exception:
        logger.warning("record_extraction_attempts: db._conn unavailable", exc_info=True)
        return
    try:
        with conn_ctx as conn:
            with conn.cursor() as cur:
                for att in telemetry:
                    try:
                        cur.execute(
                            """INSERT INTO extraction_log
                                   (receipt_id, user_id, model, confidence,
                                    latency_ms, tokens_in, tokens_out,
                                    cost_usd, fallback_from, error)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                receipt_id, user_id, att.model, att.confidence,
                                att.latency_ms, att.tokens_in, att.tokens_out,
                                att.cost_usd, att.fallback_from, att.error,
                            ),
                        )
                    except Exception as exc:
                        logger.warning("extraction_log insert failed for doc %s: %s", receipt_id, exc)
                        break
                if final_model is not None or final_confidence is not None:
                    try:
                        cur.execute(
                            """UPDATE documents
                                  SET extraction_model = COALESCE(%s, extraction_model),
                                      extraction_confidence_numeric = COALESCE(%s, extraction_confidence_numeric)
                                WHERE id = %s AND user_id = %s""",
                            (final_model, final_confidence, receipt_id, user_id),
                        )
                    except Exception as exc:
                        logger.warning("documents.extraction_model update failed for doc %s: %s", receipt_id, exc)
            try:
                conn.commit()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("record_extraction_attempts failed: %s", exc)

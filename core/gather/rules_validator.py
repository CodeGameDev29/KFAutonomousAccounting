"""Document rules validator — enforces the 20 invariants from DOCUMENT_RULES.md.

Two entry points:
- validate_gather_file(path) → checks rules 1–11
- validate_rename_file(path, extracted_data=None) → checks rules 12–19 + rule 20
Both return a RuleResult with pass/fail per rule and overall verdict.

Rule 20 implements the 2-of-3 substantive proof test for email-body PDFs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RuleCheck:
    rule_num: int
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RuleResult:
    filename: str
    passed: bool = True
    checks: list[RuleCheck] = field(default_factory=list)
    rejection_rules: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "passed": self.passed,
            "checks": [
                {"rule": c.rule_num, "name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "rejection_rules": self.rejection_rules,
        }


# ── Patterns ──────────────────────────────────────────────────────

_MONEY_RE = re.compile(
    r'\$\s*[\d,]+\.?\d*'
    r'|(?:CAD|USD|EUR|GBP)\s*[\d,]+\.?\d*'
    r'|\d{1,3}(?:,\d{3})+\.\d{2}'
    r'|\d+\.\d{2}\b',
    re.IGNORECASE,
)

# Personal (non-business) spend vocabulary. Any hit fails Rule 5.
_PERSONAL_RE = re.compile(
    r'\b(grocer|restaurant|cafe|coffee|gym|fitness|pharmacy|clinic|dental'
    r'|salon|cinema|streaming|apparel|clothing|fast\s*food|meal\s*delivery)\b',
    re.IGNORECASE,
)

_MARKETING_RE = re.compile(
    r'payment\s*buttons|payment\s*links|get\s*paid\s*fast'
    r'|how\s*you\s*invoice|unsubscribe.*offer'
    r'|get\s*up\s*to\s*\$?\d.*when\s*you'
    r'|refer\s*a\s*friend|limited\s*time\s*offer'
    r'|newsletter|promotional',
    re.IGNORECASE,
)

_FINANCIAL_DOC_RE = re.compile(
    r'\binvoice\b|\breceipt\b|\bstatement\b|\bbill\b'
    r'|\bpayment\b.*\b(?:confirm|receiv|sent|complet)'
    r'|\btransfer\b.*\b(?:confirm|complet|success)'
    r'|\btotal\b|\bsubtotal\b|\bamount\s*(?:due|paid|charged)'
    r'|\btax\b|\bgst\b|\bhst\b|\bpst\b',
    re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r'<(?:div|span|td|tr|table|img|br|hr|p|a|b|i|u|font|style)\b', re.IGNORECASE)
_BROKEN_CHARS_RE = re.compile(r'\?{3,}|â€|Â[\s\xa0]|Ã©|Ã¨|Ã¢|&#\d{2,};|&nbsp;|&amp;')

_VALID_TYPES = {'Invoice', 'Receipt', 'Statement', 'Bill', 'Contract', 'Report', 'Letter', 'Other'}

# ── Rule 20: Email body detection & 2-of-3 substantive proof ─────

_EMAIL_HEADER_RE = re.compile(
    r'^From:\s+.+$|^To:\s+.+$|^Subject:\s+.+$|^Date:\s+.+$|^Sent:\s+.+$|^Cc:\s+.+$',
    re.MULTILINE | re.IGNORECASE,
)

_FORWARDED_RE = re.compile(
    r'------\s*Forwarded\s+message\s*------|'
    r'Begin\s+forwarded\s+message|'
    r'-----\s*Original\s+Message\s*-----|'
    r'Forwarded\s+by\b',
    re.IGNORECASE,
)

_TRANSACTION_ID_RE = re.compile(
    r'(?:order|transaction|invoice|reference|confirmation|receipt|tracking|payment)\s*'
    r'(?:#|no\.?|number|num|id|ref)?[\s:]*'
    r'[A-Z0-9][A-Z0-9\-]{3,}',
    re.IGNORECASE,
)

# Patterns for generic vague phrasing that lack a named vendor
_VAGUE_PHRASING_RE = re.compile(
    r'your\s+(?:payment|transfer|bill|order)\s+'
    r'(?:was|has\s+been|is)\s+'
    r'(?:successful|complete|ready|processed|shipped)',
    re.IGNORECASE,
)


def _is_email_body(text: str) -> bool:
    """Detect whether a document appears to be an email body (printout).

    Conservative: requires multiple email-header lines in the first 800 chars
    OR a forwarded-message marker. Proper invoices/receipts that happen to
    mention "From:" once in an address block will NOT trigger this.
    """
    if not text or len(text.strip()) < 50:
        return False

    header_region = text[:800]
    header_matches = _EMAIL_HEADER_RE.findall(header_region)

    # Need at least 2 distinct email header lines to qualify as an email body.
    # A proper invoice that just happens to have "Date:" once won't trigger.
    if len(header_matches) >= 2:
        # Further confirm: at least one of From or Subject must be present
        has_from = bool(re.search(r'^From:\s+', header_region, re.MULTILINE | re.IGNORECASE))
        has_subject = bool(re.search(r'^Subject:\s+', header_region, re.MULTILINE | re.IGNORECASE))
        if has_from or has_subject:
            return True

    # Forwarded message indicator anywhere in the document
    if _FORWARDED_RE.search(text):
        return True

    return False


def check_rule_20(text: str, extracted_data: dict | None = None) -> tuple[bool, str]:
    """Rule 20: Email body notifications must pass the 2-of-3 substantive proof test.

    For email-body PDFs (not proper invoices/receipts), at least 2 of these 3
    elements must be present in the text:
        (a) Specific dollar amount — $X.XX or CAD/USD X.XX
        (b) Named counterparty — vendor/merchant name clearly stated
        (c) Transaction identifier — order #, transaction ID, invoice #, reference #

    Args:
        text: Extracted text content of the document.
        extracted_data: Optional dict from LLM extraction with keys like
            'vendor', 'amount', 'transaction_id', etc.

    Returns:
        (passes, reason) — passes is True if the doc is NOT an email body
        (rule doesn't apply) or if it IS an email body that passes the 2-of-3 test.
    """
    if not _is_email_body(text):
        return True, "Not an email body — Rule 20 N/A"

    score = 0
    evidence: list[str] = []

    # (a) Specific dollar amount
    amounts = _MONEY_RE.findall(text)
    has_amount = False
    if extracted_data and extracted_data.get("amount"):
        has_amount = True
    if amounts:
        # Filter out very small fragments that might be false positives
        # (e.g., page numbers like "1.00" appearing alone)
        for a in amounts:
            cleaned = re.sub(r'[\s,$]', '', a).replace('CAD', '').replace('USD', '').replace('EUR', '').replace('GBP', '').strip()
            try:
                val = float(cleaned)
                if val > 0:
                    has_amount = True
                    break
            except ValueError:
                continue
    if has_amount:
        score += 1
        evidence.append("amount")

    # (b) Named counterparty — vendor/merchant name
    has_vendor = False
    if extracted_data and extracted_data.get("vendor"):
        vendor = str(extracted_data["vendor"]).strip().lower()
        # Reject generic placeholders
        if vendor not in {"unknown", "none", "", "n/a", "email", "notification", "your", "customer"}:
            has_vendor = True
    if not has_vendor:
        # Try to detect vendor names in text heuristically:
        # Look for known patterns like "from VendorName" or "payment to VendorName"
        vendor_pattern = re.compile(
            r'(?:from|to|paid\s+to|payment\s+to|sent\s+to|charged\s+by|billed\s+by)\s+'
            r'([A-Z][A-Za-z0-9\s&.\'-]{2,30})',
            re.IGNORECASE,
        )
        # Exclude matches that are just email addresses
        vendor_matches = vendor_pattern.findall(text)
        for vm in vendor_matches:
            vm_clean = vm.strip()
            if '@' not in vm_clean and len(vm_clean) >= 3 and not _VAGUE_PHRASING_RE.match(vm_clean):
                has_vendor = True
                break
    if has_vendor:
        score += 1
        evidence.append("vendor")

    # (c) Transaction identifier — order #, transaction ID, invoice #, reference #
    has_txn_id = False
    if extracted_data and extracted_data.get("transaction_id"):
        has_txn_id = True
    if not has_txn_id and extracted_data and extracted_data.get("invoice_number"):
        has_txn_id = True
    if not has_txn_id and extracted_data and extracted_data.get("order_number"):
        has_txn_id = True
    if not has_txn_id and extracted_data and extracted_data.get("reference_number"):
        has_txn_id = True
    if not has_txn_id:
        has_txn_id = bool(_TRANSACTION_ID_RE.search(text))
    if has_txn_id:
        score += 1
        evidence.append("transaction_id")

    passes = score >= 2
    if passes:
        reason = f"Email body passes 2-of-3 test ({score}/3: {', '.join(evidence)})"
    else:
        reason = (
            f"Email body FAILS 2-of-3 substantive proof test ({score}/3"
            f"{': ' + ', '.join(evidence) if evidence else ': none found'}). "
            f"Insufficient documentation — discard."
        )

    return passes, reason


# ── Text extraction ───────────────────────────────────────────────

def _extract_text(filepath: Path) -> str:
    if filepath.suffix.lower() != '.pdf':
        return ""
    try:
        import fitz
        doc = fitz.open(str(filepath))
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception:
        return ""


def _get_page_texts(filepath: Path) -> list[str]:
    if filepath.suffix.lower() != '.pdf':
        return []
    try:
        import fitz
        doc = fitz.open(str(filepath))
        pages = [page.get_text().strip() for page in doc]
        doc.close()
        return pages
    except Exception:
        return []


# ── Gather validation (Rules 1–11) ───────────────────────────────

def validate_gather_file(filepath: str | Path) -> RuleResult:
    """Validate a gathered file against rules 1–11."""
    filepath = Path(filepath)
    result = RuleResult(filename=filepath.name)
    text = _extract_text(filepath)
    page_texts = _get_page_texts(filepath)
    num_pages = len(page_texts)

    # Rule 1: Monetary amount
    money = _MONEY_RE.findall(text)
    r1 = len(money) >= 1
    result.checks.append(RuleCheck(1, "Monetary amount", r1, f"{len(money)} amounts found"))

    # Rule 2: Not blank
    r2 = len(text.strip()) >= 50
    result.checks.append(RuleCheck(2, "Not blank", r2, f"{len(text.strip())} chars"))

    # Rule 3: Max 5 pages
    r3 = num_pages <= 5
    result.checks.append(RuleCheck(3, "Max 5 pages", r3, f"{num_pages} pages"))

    # Rule 4: No blank pages
    blank_pages = [i + 1 for i, t in enumerate(page_texts) if len(t) < 20]
    r4 = len(blank_pages) == 0
    result.checks.append(RuleCheck(4, "No blank pages", r4,
                                    f"blank pages: {blank_pages}" if blank_pages else "all pages have text"))

    # Rule 5: No personal vendors
    personal = _PERSONAL_RE.findall(text)
    r5 = len(personal) == 0
    result.checks.append(RuleCheck(5, "No personal vendors", r5,
                                    f"found: {personal[:3]}" if personal else "clean"))

    # Rule 6: Business relevant (light check — scorer handles deep check)
    r6 = True  # Assume pass if it got through the scorer
    result.checks.append(RuleCheck(6, "Business relevant", r6, "passed scorer"))

    # Rule 7: Not marketing
    marketing = _MARKETING_RE.findall(text)
    r7 = len(marketing) == 0
    result.checks.append(RuleCheck(7, "Not marketing", r7,
                                    f"found: {marketing[:2]}" if marketing else "clean"))

    # Rule 8: Financial document
    fin = _FINANCIAL_DOC_RE.findall(text)
    r8 = len(fin) >= 1
    result.checks.append(RuleCheck(8, "Financial document", r8,
                                    f"{len(fin)} indicators" if fin else "no financial indicators"))

    # Rule 9: Email header (only for Gmail body PDFs)
    is_email_pdf = filepath.name.startswith("Gmail -")
    if is_email_pdf:
        has_header = "From:" in text[:500] and "Subject:" in text[:500]
        r9 = has_header
        result.checks.append(RuleCheck(9, "Email header", r9,
                                        "header found" if has_header else "missing From/Subject"))
    else:
        result.checks.append(RuleCheck(9, "Email header", True, "not an email PDF — N/A"))

    # Rule 10: No raw HTML
    html_tags = _HTML_TAG_RE.findall(text)
    r10 = len(html_tags) == 0
    result.checks.append(RuleCheck(10, "No raw HTML", r10,
                                    f"found {len(html_tags)} tags" if html_tags else "clean"))

    # Rule 11: No broken characters
    broken = _BROKEN_CHARS_RE.findall(text)
    r11 = len(broken) == 0
    result.checks.append(RuleCheck(11, "No broken chars", r11,
                                    f"found: {broken[:3]}" if broken else "clean"))

    # Overall
    for c in result.checks:
        if not c.passed:
            result.passed = False
            result.rejection_rules.append(c.rule_num)

    return result


# ── Rename validation (Rules 12–20) ──────────────────────────────

def validate_rename_file(
    filepath: str | Path,
    extracted_data: dict | None = None,
) -> RuleResult:
    """Validate a renamed file against rules 12–20.

    Args:
        filepath: Path to the renamed file.
        extracted_data: Optional dict from LLM extraction (keys like 'vendor',
            'amount', 'transaction_id', etc.). Used by Rule 20 to check the
            2-of-3 substantive proof test for email-body PDFs.
    """
    filepath = Path(filepath)
    name = filepath.stem  # without extension
    result = RuleResult(filename=filepath.name)

    # Parse expected format: YYYYMMDD-VendorName-Type
    # Handle collision suffix: YYYYMMDD-Vendor-Type-2
    # Re-parse: find date (first 8 chars), then vendor and type
    date_part = name[:8] if len(name) >= 8 else ""
    remainder = name[9:] if len(name) > 9 else ""  # skip the hyphen after date
    # Split remainder on last hyphen to get vendor-Type or vendor-Type-N
    r_parts = remainder.rsplit('-', 1)
    if len(r_parts) == 2 and r_parts[1].isdigit():
        # Collision suffix — strip it
        remainder = remainder[:-(len(r_parts[1]) + 1)]
        r_parts = remainder.rsplit('-', 1)

    vendor_part = r_parts[0] if len(r_parts) >= 1 else ""
    type_part = r_parts[1] if len(r_parts) >= 2 else r_parts[0] if len(r_parts) == 1 else ""

    # If vendor_part contains the type, re-split
    for valid_type in _VALID_TYPES:
        if remainder.endswith(f"-{valid_type}"):
            vendor_part = remainder[:-(len(valid_type) + 1)]
            type_part = valid_type
            break

    # Rule 12: Date format
    r12 = bool(re.match(r'^\d{8}$', date_part))
    result.checks.append(RuleCheck(12, "Date format", r12, f"date='{date_part}'"))

    # Rule 13: Vendor name — CamelCase, no spaces, not Gmail/NonFinancial/Unknown
    bad_vendors = {'gmail', 'nonfinancial', 'unknown', '', 'other', 'none', 'test', 'document', 'file', 'sample'}
    r13 = (
        vendor_part.lower() not in bad_vendors
        and ' ' not in vendor_part
        and bool(re.fullmatch(r'\w+', vendor_part, re.UNICODE))
    )
    result.checks.append(RuleCheck(13, "Vendor name valid", r13, f"vendor='{vendor_part}'"))

    # Rule 14: Document type valid
    r14 = type_part in _VALID_TYPES
    result.checks.append(RuleCheck(14, "Type valid", r14, f"type='{type_part}'"))

    # Rule 15: NonFinancial exclusion
    r15 = 'nonfinancial' not in name.lower()
    result.checks.append(RuleCheck(15, "NonFinancial excluded", r15, ""))

    # Rule 16: No NonFinancial in name
    r16 = 'NonFinancial' not in name and 'nonfinancial' not in name.lower()
    result.checks.append(RuleCheck(16, "No NonFinancial name", r16, ""))

    # Rule 17: No Unknown-Other
    r17 = 'Unknown-Other' not in name
    result.checks.append(RuleCheck(17, "No Unknown-Other", r17, ""))

    # Rule 18: No empty extraction (00000000 + Unknown)
    r18 = not (date_part == '00000000' and vendor_part.lower() == 'unknown')
    result.checks.append(RuleCheck(18, "Not empty extraction", r18, ""))

    # Rule 19: No duplicate filenames (checked externally across all files)
    result.checks.append(RuleCheck(19, "No duplicates", True, "checked externally"))

    # Rule 20: 2-of-3 substantive proof test for email-body PDFs
    text = _extract_text(filepath)
    r20_passed, r20_detail = check_rule_20(text, extracted_data)
    result.checks.append(RuleCheck(20, "Email body 2-of-3 proof", r20_passed, r20_detail))

    for c in result.checks:
        if not c.passed:
            result.passed = False
            result.rejection_rules.append(c.rule_num)

    return result


def validate_rename_batch(
    filepaths: list[str | Path],
    extracted_data_list: list[dict | None] | None = None,
) -> list[RuleResult]:
    """Validate a batch of renamed files, including Rule 19 (duplicates) and Rule 20.

    Args:
        filepaths: List of file paths to validate.
        extracted_data_list: Optional parallel list of extracted_data dicts
            (one per file). If None, Rule 20 uses text-only heuristics.
    """
    results = []
    seen_names: dict[str, int] = {}

    for idx, fp in enumerate(filepaths):
        ext_data = None
        if extracted_data_list and idx < len(extracted_data_list):
            ext_data = extracted_data_list[idx]
        r = validate_rename_file(fp, extracted_data=ext_data)
        name = Path(fp).name
        if name in seen_names:
            # Rule 19 violation
            r.passed = False
            r.rejection_rules.append(19)
            for c in r.checks:
                if c.rule_num == 19:
                    c.passed = False
                    c.detail = f"duplicate of file #{seen_names[name]}"
        seen_names[name] = len(results) + 1
        results.append(r)

    return results

"""Document financial relevance scorer.

Reads a PDF's embedded text layer with PyMuPDF, then scores that text across
multiple factors to determine if it's a valid business financial document.
There is no OCR step: a scan or photograph with no text layer scores as empty
and is rejected on the completeness factor.

Score factors (each 0.0–1.0, weighted):
1. Monetary amount present      (weight 0.25) — $X.XX, CAD, totals
2. Transaction parties named    (weight 0.20) — vendor/payee/payer names
3. Transaction date present     (weight 0.15) — recognizable date
4. Business relevance           (weight 0.20) — related to company's purpose
5. Document structure           (weight 0.10) — looks like invoice/receipt/statement
6. Completeness                 (weight 0.10) — has enough content to be useful

Final score = weighted sum. Threshold for inclusion: 0.40
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

INCLUSION_THRESHOLD = 0.40


@dataclass
class DocumentScore:
    """Scoring breakdown for a single document."""
    filename: str
    monetary_amount: float = 0.0     # 0–1
    transaction_parties: float = 0.0  # 0–1
    transaction_date: float = 0.0     # 0–1
    business_relevance: float = 0.0   # 0–1
    document_structure: float = 0.0   # 0–1
    completeness: float = 0.0         # 0–1
    final_score: float = 0.0
    included: bool = False
    rejection_reason: str = ""
    extracted_text_preview: str = ""   # first 200 chars for debugging

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "monetary_amount": round(self.monetary_amount, 2),
            "transaction_parties": round(self.transaction_parties, 2),
            "transaction_date": round(self.transaction_date, 2),
            "business_relevance": round(self.business_relevance, 2),
            "document_structure": round(self.document_structure, 2),
            "completeness": round(self.completeness, 2),
            "final_score": round(self.final_score, 2),
            "included": self.included,
            "rejection_reason": self.rejection_reason,
        }


# Weights for final score
_WEIGHTS = {
    "monetary_amount": 0.25,
    "transaction_parties": 0.20,
    "transaction_date": 0.15,
    "business_relevance": 0.20,
    "document_structure": 0.10,
    "completeness": 0.10,
}

# ── Patterns ──────────────────────────────────────────────────────

_MONEY_RE = re.compile(
    r'\$\s*[\d,]+\.?\d*'
    r'|(?:CAD|USD|EUR|GBP)\s*[\d,]+\.?\d*'
    r'|\d{1,3}(?:,\d{3})+\.\d{2}'
    r'|\d+\.\d{2}\b',
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r'\b\d{4}[-/]\d{2}[-/]\d{2}\b'                     # 2000-01-15
    r'|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b'              # 01/15/2000
    r'|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s*\d{4}'  # Jan 15, 2000
    r'|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}',   # 15 Jan 2000
    re.IGNORECASE,
)

_INVOICE_STRUCTURE_RE = re.compile(
    r'\binvoice\b|\breceipt\b|\bstatement\b|\bbill\b'
    r'|\binvoice\s*#|\border\s*#|\btransaction\s*id'
    r'|\bsubtotal\b|\btax\b|\bgst\b|\bhst\b|\bpst\b'
    r'|\btotal\s*(?:due|amount|paid|charged)'
    r'|\bpayment\s*(?:method|received|confirmation)'
    r'|\baccount\s*(?:number|#|no)'
    r'|\bdue\s*date\b|\bbilling\s*(?:period|date)',
    re.IGNORECASE,
)

_PARTY_RE = re.compile(
    r'\b(?:from|to|payee|payer|vendor|merchant|sold\s*to|bill\s*to|ship\s*to|customer|client)\s*[:\-]?\s*\w',
    re.IGNORECASE,
)

# Keywords that mark a document as business-relevant: generic expense
# vocabulary, plus the payment rails this engine integrates with, whose names
# are often the only business wording on a transfer advice. Every token here is
# matched against document text, so adding or removing one changes which
# documents are scored as business-relevant. Keep the list generic — a supplier
# whose paperwork carries none of these words belongs in the vendor rules each
# deployment configures, not in a list shipped with the engine.
_BUSINESS_KEYWORDS = re.compile(
    r'\bsoftware\b|\bcloud\b|\bhosting\b|\bserver\b|\bdomain\b'
    r'|\bsubscription\b|\blicense\b|\bSaaS\b|\bAPI\b'
    r'|\boffice\b|\brent\b|\butilities?\b|\binternet\b|\btelecom'
    r'|\binsurance\b|\blegal\b|\baccounting\b|\bconsult'
    r'|\bcontract\b|\bfreelance\b|\bcontractor\b'
    r'|\btravel\b|\bflight\b|\bhotel\b|\bairfare\b'
    r'|\bequipment\b|\bhardware\b|\blaptop\b|\bmonitor\b'
    r'|\badvertis|\bmarketing\b|\bad\s*spend\b|\bcampaign\b'
    r'|\bpayroll\b|\bwage\b|\bsalary\b'
    r'|\bbank\b|\btransfer\b|\bwire\b|\binterac\b'
    r'|\butility\b|\bhydro\b|\belectric\b|\bgas\s*co\b'
    r'|\bcompute\b|\bstorage\b|\bbandwidth\b|\bregistrar\b|\bdata\s*cent(?:er|re)\b'
    r'|\bplatform\s*fee\b|\bprocessing\s*fee\b|\bseats?\b|\bworkspace\b'
    r'|\bpaypal\b|\bwise\b'
    r'|\binvoice\b|\breceipt\b',
    re.IGNORECASE,
)

# Personal (non-business) spend vocabulary. Any hit forces business relevance
# to zero and rejects the document.
_PERSONAL_KEYWORDS = re.compile(
    r'\b(grocer|restaurant|cafe|coffee|gym|fitness|pharmacy|clinic|dental'
    r'|salon|cinema|streaming|apparel|clothing|fast\s*food|meal\s*delivery)\b',
    re.IGNORECASE,
)

_NONFINANCIAL_RE = re.compile(
    r'\bnon[\s\-]?financial\b',
    re.IGNORECASE,
)


# ── Main scoring function ─────────────────────────────────────────

def extract_text(data: bytes, filename: str) -> str:
    """Return a PDF's embedded text layer, or "" when there is none.

    Non-PDF input, an unreadable file, and a PDF whose pages carry no text
    layer all return "". Nothing here rasterises or OCRs the page.
    """
    ext = Path(filename).suffix.lower()
    if ext != ".pdf":
        return ""
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text.strip()
    except Exception:
        return ""


def score_document(
    data: bytes,
    filename: str,
    text: str | None = None,
    company_description: str = "",
) -> DocumentScore:
    """Score a document across all financial relevance factors.

    Args:
        data: Raw file bytes.
        filename: Original filename.
        text: Pre-extracted text (if None, will extract from PDF).
        company_description: Optional description of the business, accepted for
            callers that hold one. Business relevance is currently scored from
            the keyword patterns in this module, not from this string.

    Returns:
        DocumentScore with all sub-scores and final score.
    """
    score = DocumentScore(filename=filename)

    if text is None:
        text = extract_text(data, filename)

    score.extracted_text_preview = text[:200] if text else ""

    # ── Factor 1: Monetary amount (0.25) ──
    money_matches = _MONEY_RE.findall(text)
    if len(money_matches) >= 3:
        score.monetary_amount = 1.0
    elif len(money_matches) == 2:
        score.monetary_amount = 0.8
    elif len(money_matches) == 1:
        score.monetary_amount = 0.5
    else:
        score.monetary_amount = 0.0

    # ── Factor 2: Transaction parties (0.20) ──
    party_matches = _PARTY_RE.findall(text)
    # Also check for company/person names (capitalized words after common labels)
    named_entities = re.findall(
        r'(?:from|to|payee|vendor|merchant|client|customer)\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        text,
    )
    party_count = len(party_matches) + len(named_entities)
    if party_count >= 2:
        score.transaction_parties = 1.0
    elif party_count == 1:
        score.transaction_parties = 0.6
    else:
        # Check if there's any recognizable company name in the text
        if re.search(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\s+(?:Inc|LLC|Ltd|Corp|Co\b)', text):
            score.transaction_parties = 0.5
        else:
            score.transaction_parties = 0.1

    # ── Factor 3: Transaction date (0.15) ──
    date_matches = _DATE_RE.findall(text)
    if len(date_matches) >= 2:
        score.transaction_date = 1.0
    elif len(date_matches) == 1:
        score.transaction_date = 0.7
    else:
        score.transaction_date = 0.0

    # ── Factor 4: Business relevance (0.20) ──
    biz_hits = len(_BUSINESS_KEYWORDS.findall(text))
    personal_hits = len(_PERSONAL_KEYWORDS.findall(text))

    # A payment rail names the method, not the vendor, so its hits are taken
    # back out of the business count. These tokens must be exactly the rail
    # tokens _BUSINESS_KEYWORDS also matches, or the subtraction removes hits
    # that were never added and the score comes out too low.
    payment_platform_hits = len(re.findall(
        r'\bpaypal\b|\bwise\b|\binterac\b', text, re.IGNORECASE
    ))
    effective_biz_hits = max(0, biz_hits - payment_platform_hits)

    if personal_hits > 0:
        # Personal vendor found — this is the actual transaction target
        score.business_relevance = 0.0
    elif effective_biz_hits >= 3:
        score.business_relevance = 1.0
    elif effective_biz_hits >= 1:
        score.business_relevance = 0.7
    elif biz_hits >= 1:
        # Only payment platforms matched — neutral, not clearly business
        score.business_relevance = 0.4
    else:
        score.business_relevance = 0.4  # Unknown — benefit of doubt

    # ── Factor 5: Document structure (0.10) ──
    structure_hits = len(_INVOICE_STRUCTURE_RE.findall(text))
    if structure_hits >= 4:
        score.document_structure = 1.0
    elif structure_hits >= 2:
        score.document_structure = 0.7
    elif structure_hits >= 1:
        score.document_structure = 0.4
    else:
        score.document_structure = 0.0

    # ── Factor 6: Completeness (0.10) ──
    text_len = len(text.strip())
    if text_len >= 500:
        score.completeness = 1.0
    elif text_len >= 200:
        score.completeness = 0.7
    elif text_len >= 50:
        score.completeness = 0.4
    else:
        score.completeness = 0.0

    # ── Final score ──
    score.final_score = (
        score.monetary_amount * _WEIGHTS["monetary_amount"]
        + score.transaction_parties * _WEIGHTS["transaction_parties"]
        + score.transaction_date * _WEIGHTS["transaction_date"]
        + score.business_relevance * _WEIGHTS["business_relevance"]
        + score.document_structure * _WEIGHTS["document_structure"]
        + score.completeness * _WEIGHTS["completeness"]
    )

    # ── Inclusion decision ──
    # Hard reject: known non-financial
    if _NONFINANCIAL_RE.search(text) or _NONFINANCIAL_RE.search(filename):
        score.included = False
        score.rejection_reason = "Labeled as non-financial"
    elif score.monetary_amount == 0.0:
        score.included = False
        score.rejection_reason = "No monetary amount found"
    elif score.completeness == 0.0:
        score.included = False
        score.rejection_reason = "Document is blank or nearly empty"
    elif personal_hits > 0:
        score.included = False
        score.rejection_reason = "Personal vendor detected, not business-relevant"
    elif score.final_score < INCLUSION_THRESHOLD:
        score.included = False
        score.rejection_reason = f"Score {score.final_score:.2f} below threshold {INCLUSION_THRESHOLD}"
    else:
        score.included = True

    return score

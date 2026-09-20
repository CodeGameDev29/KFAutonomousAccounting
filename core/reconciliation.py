"""Reconciliation engine — matches documents to bank transactions.

The matching strategy:
1. Amount match is the primary signal (exact match required in pass 1)
2. Vendor/description similarity helps distinguish between same-amount candidates
3. Date matching uses the document's PAYMENT DATE (when the bank charge would post),
   not the invoice/document date. If no payment date is available, falls back to
   document date with a decay score.
4. The amount shortlist (pass 1) scans every sign-compatible transaction in the
   account — nothing is filtered out by date before the amounts have been
   compared, and a pair whose amounts do not agree on any route is rejected
   there. The date window is applied one step later: pass 2 drops every
   candidate whose date is more than ``max_date_days`` from the document's best
   known date, before and after the date agent is asked. ``max_date_days``
   is a caller-supplied limit and defaults to 45 days throughout this module;
   :func:`compute_date_score` grades the surviving candidates on a decay curve
   rather than a cliff.

Only 1-to-1 matching is supported: each transaction matches at most one proof
of transaction. No split bundling, many-to-one, or one-to-many grouping.
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from config.settings import ReconciliationConfig

logger = logging.getLogger(__name__)

# ─── Reconciliation run log ───────────────────────────────────────────────
# Each reconciliation run writes one JSON file named ``recon_<run id>.json``
# into the directory below, where the run id is the run's local start time
# (``YYYYMMDD_HHMMSS``). The file holds the run's input transactions and
# documents, every agent shortlist, verdict and stated reasoning, every
# deterministic score, every claim resolution, and a closing summary — the same
# events the logger is given during the run, in order. The directory is
# ``RECON_LOG_DIR`` when that environment variable is set, and
# ``<project root>/logs/reconciliation`` otherwise; it is created on the first
# run. Nothing reads these files back: they exist so a run's decisions can be
# inspected without re-running it.

# ``or``, not a ``get`` default: .env.example ships ``RECON_LOG_DIR=`` empty, and
# ``Path("")`` is the current directory — which would drop a file holding the
# run's transactions into the checkout, outside everything .gitignore covers.
_RECON_LOG_DIR = Path(
    os.environ.get("RECON_LOG_DIR", "").strip()
    or Path(__file__).resolve().parent.parent / "logs" / "reconciliation"
)


class _ReconAuditLogger:
    """Thread-safe per-run event log that accumulates events and flushes to a JSON file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id: str = ""
        self._events: list[dict] = []
        self._enabled = True
        self._log_path: Path | None = None

    def start_run(self, num_txns: int, num_docs: int) -> str:
        """Begin a new reconciliation run. Returns the run ID."""
        with self._lock:
            ts = datetime.now()
            self._run_id = ts.strftime("%Y%m%d_%H%M%S")
            self._events = [{
                "event": "run_start",
                "run_id": self._run_id,
                "timestamp": ts.isoformat(),
                "num_transactions": num_txns,
                "num_documents": num_docs,
            }]
            _RECON_LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._log_path = _RECON_LOG_DIR / f"recon_{self._run_id}.json"
            return self._run_id

    def log(self, event: str, **kwargs) -> None:
        """Append an event to the current run's log."""
        if not self._enabled:
            return
        entry = {
            "event": event,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        with self._lock:
            self._events.append(entry)

    def flush(self) -> Path | None:
        """Write all accumulated events to disk and return the log file path."""
        with self._lock:
            if not self._events or not self._log_path:
                return None
            self._events.append({
                "event": "run_end",
                "timestamp": datetime.now().isoformat(),
                "total_events": len(self._events),
            })
            try:
                # Custom serializer for Decimal/date types
                def _default(obj):
                    if isinstance(obj, Decimal):
                        return float(obj)
                    if isinstance(obj, (date, datetime)):
                        return obj.isoformat()
                    return str(obj)

                self._log_path.write_text(
                    _json.dumps(self._events, indent=2, default=_default),
                    encoding="utf-8",
                )
                logger.info("Reconciliation audit log written to %s", self._log_path)
                return self._log_path
            except Exception:
                logger.exception("Failed to write reconciliation audit log")
                return None

    @property
    def run_id(self) -> str:
        return self._run_id


# Singleton audit logger instance
_audit = _ReconAuditLogger()

from config.settings import local_llm_config as _local_llm_cfg
from config.settings import reconciliation_config as _recon_cfg
from core.adjudication_memory import (
    AGENT_AMOUNT,
    AGENT_DATE,
    AGENT_FINAL,
    AdjudicationMemory,
    inputs_hash,
)
from core.adjudication_memory import (
    DISABLED as _NO_MEMORY,
)
from core.amount_match import (
    FX_CAD_USD_MAX,
    FX_CAD_USD_MIN,
    PENNY_TOLERANCE,
    WIRE_FEE_TOLERANCE,
    compare_amounts,
    compare_amounts_for_document,
)
from core.amount_match import MISMATCH as _AMOUNT_MISMATCH
from core.document_currency import (
    ASSUMED as _CURRENCY_ASSUMED,
)
from core.document_currency import (
    resolve_document_currency,
)
from core.llm_sync import llm_call_stats_snapshot, reset_llm_call_stats
from models.document import Document
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import Transaction, account_str

# Re-exported under the private names used inside this module. The values come
# from core.amount_match so the band is defined in exactly one place.
_FX_CAD_USD_MIN = FX_CAD_USD_MIN
_FX_CAD_USD_MAX = FX_CAD_USD_MAX

_WIRE_FEE_TOLERANCE = WIRE_FEE_TOLERANCE

# Does pass 1 ask the model at all? Off by default: the deterministic amount
# pass already enumerates every route the engine can accept an amount on, the
# model's shortlist is merged in as a union and so can only add candidates the
# later agents then have to reject, and the call is the largest single
# contributor to the matching phase's elapsed time. See
# `ReconciliationConfig.amount_agent_llm`. Read once here as a module global so
# the flag is one name in the engine and a test can flip it without rebuilding
# the frozen config.
AMOUNT_AGENT_LLM: bool = _recon_cfg.amount_agent_llm

# ─── How big a reply a matcher agent may write ────────────────────────────
#
# Every agent prompt puts its decision fields FIRST and `reasoning` last, and
# asks for that reasoning as one sentence of at most 25 words, so a well-formed
# reply is short by construction and the decision is already written by the time
# a truncation could bite.
#
# 384 tokens sits well above such a reply while still capping a runaway: a model
# that starts rambling hits `finish_reason=length` early instead of generating
# to the end of its own context. `core.llm_sync` retries a truncated reply once
# with double the budget, so the cost of setting this too low is a second call,
# never a lost verdict.
RECON_AGENT_MAX_TOKENS = 384

# ─── What may be stored as a settled match ────────────────────────────────
#
# The auto-approve threshold only chooses between AUTO_APPROVED and
# PENDING_REVIEW for the *match record*. On its own it lets a weak pair — a
# vendor sub-score of 0.0 against an amount that is merely in the neighbourhood
# — mark the *transaction* MATCHED at any confidence at all, which both tells
# the user the row is reconciled and removes it from every later candidate pool,
# so the receipt that would have matched it outright can never be found.
#
# Two constants close that:

# Below this, a pair is a suggestion, not a reconciliation. It is FLAGGED for
# review — the match record is stored as PENDING_REVIEW, and the transaction
# and receipt both stay unmatched and in the candidate pool. 0.60 is the
# review_threshold the engine documents (PassConfig.review_threshold,
# RECONCILIATION_REVIEW_THRESHOLD): at 0.50 weight on amount, a pair can clear
# 0.60 only with a real amount agreement plus one corroborating signal.
MATCH_CONFIDENCE_FLOOR: float = _recon_cfg.review_threshold

# A vendor sub-score this low means the receipt's vendor name shares *nothing*
# with the bank description — rapidfuzz returns exactly 0.0 for "amazon.ca" vs
# "orchid telecom". It is only evidence of a wrong pair when the description
# actually names someone (see _description_names_other_vendor); an opaque
# description ("POS PURCHASE 0000") legitimately scores 0.
#
# It is NOT the gate on the vendor-contradiction rule, because two unrelated
# names rarely score a clean 0.0: fuzz.ratio("wholesale club #0000", "telecom")
# is 0.22, not 0.0, since the two names share an l, an s, an o and a c. That
# incidental letter overlap alone is enough to lift the score over this floor
# and skip the check entirely. A description that names a different merchant is
# a contradiction at ANY vendor sub-score.
VENDOR_SCORE_NO_SIGNAL: float = 0.05

# An amount sub-score at or below this means the engine found no amount
# agreement on any route it has: not the same figure, not within the wire-fee
# window, not a converted amount on the receipt, not a USD amount inside the
# bank description, and not a rate inside the CAD/USD band.
AMOUNT_SCORE_NO_EVIDENCE: float = 0.05

# How much better a new candidate must be before it may take a transaction
# away from an existing sub-floor match. 0.15 is one full sub-score weight
# (vendor 0.20, date 0.30): the challenger has to win on a whole signal, not
# on scoring noise. Displacement is allowed only against a match that is
# itself below MATCH_CONFIDENCE_FLOOR — a settled match is never displaced
# without a human.
DISPLACEMENT_MARGIN: float = 0.15

# Bank-description noise: tokens that name no vendor. Stripped before asking
# whether a description names somebody other than the receipt's vendor.
_BANK_NOISE_TOKENS = frozenset({
    "pos", "purchase", "payment", "paymnt", "pmt", "debit", "credit", "card",
    "visa", "mastercard", "amex", "interac", "etransfer", "transfer", "sent",
    "recv", "received", "deposit", "withdrawal", "withdraw", "atm", "abm",
    "cash", "cheque", "check", "chq", "fee", "fees", "service", "charge",
    "charges", "internet", "online", "banking", "bill", "billpay", "invoice",
    "monthly", "annual", "recurring", "subscription", "misc", "miscellaneous",
    "merchandise", "store", "retail", "inc", "ltd", "llc", "corp", "co",
    "company", "canada", "cad", "usd",
    # City names: a card descriptor prints the branch or terminal city after the
    # merchant name, so the city is a location tag and never the party paid.
    # One or two of the larger centres per region, so a descriptor from any
    # province loses its city the same way.
    "halifax", "montreal", "ottawa", "toronto", "winnipeg", "regina",
    "calgary", "edmonton", "vancouver",
    "www",
    "com", "net", "org", "http", "https", "ref", "reference", "trace",
    "confirmation", "authorization", "auth", "trsf", "trf", "dep", "wire",
    # Acquirer / rail tags that ride along with a real merchant name and name
    # nobody themselves: "EXMPL MKTP CA" names a retailer, not a merchant called Mktp.
    "mktp", "pymt", "outgoing", "incoming", "preauth", "preauthorized",
})

# Payment rails. These DO name a party, but the party is the pipe the money went
# through, not who was paid: a Wise line says Wise whoever the contractor was.
# Stripped along with the noise before asking who a description names, so
# "WISE TRANSFER" is an opaque description (nobody named) while
# "PAYPAL *ORCHID TELECOM" still names ORCHID TELECOM.
_PAYMENT_RAIL_TOKENS = frozenset({
    "wise", "wisemsp", "transferwise", "paypal", "paypalmsp", "interac",
    "payoneer", "remitly", "moneygram",
})

# ─── The amount envelope: what the amount agent is even shown ─────────────
#
# Handing the amount agent every sign-compatible unmatched transaction in the
# account makes the prompt grow with the size of the ledger, and most of those
# rows are not candidates under any rule the engine has or that the prompt
# itself describes.
#
# So the model is shown only transactions whose magnitude is inside an envelope
# around the document's total. The envelope is deliberately *wider than every
# route the engine can accept an amount on*, so narrowing it cannot drop a pair
# the matcher would have taken:
#
#   * same currency, within $2 or 15%          → ratio 0.85–1.15
#   * wire fee deducted from an incoming wire  → _WIRE_FEE_TOLERANCE absolute
#   * cross-currency through the CAD/USD band  → 1/FX_CAD_USD_MAX up to
#     FX_CAD_USD_MAX, either direction
#   * a converted amount printed on the receipt (_extract_alt_amount) —
#     covered by running the envelope over the alt amount as well
#   * a USD amount inside a credit-card description — same band as FX
#
# 0.5× to 2.0× with $30 of absolute slack contains all of them with room to
# spare, and is also strictly wider than the generosity the prompt asks the
# model for ("within ~15% for cross-currency, within $1 for same-currency"), so
# every id either pass can shortlist is inside the envelope.
AMOUNT_ENVELOPE_LOW_RATIO = Decimal("0.5")
AMOUNT_ENVELOPE_HIGH_RATIO = Decimal("2.0")
AMOUNT_ENVELOPE_ABS_SLACK = Decimal("30.00")

# ─── When the agents cannot change the answer ─────────────────────────────
#
# A document with exactly one surviving candidate, whose amount is the same
# figure in the same currency, whose date is within a few days, and whose
# vendor is plainly named on the bank line with nothing contradicting it, is
# not a judgement call. The three agents would settle it anyway: the date agent
# short-circuits on a single candidate, the final agent short-circuits on a
# single candidate and scores it deterministically, and the amount agent can
# only add to the shortlist. Settling it here skips all three calls and records
# `adjudication="deterministic"` on the match so the Review panel and the audit
# log say plainly that no model was asked.
#
# Every other pair still goes to the agents — multi-candidate, cross-currency,
# near-miss amounts, distant dates, unrecognisable vendors, and anything the
# merchant-contradiction rule touches.

# "The same figure." Two cents absorbs a rounding difference between a receipt
# total and a bank line; it is not a tolerance in any real sense.
UNAMBIGUOUS_AMOUNT_TOLERANCE = Decimal("0.02")

# Same-week. compute_date_score already treats 1–3 days as 0.9 — the band for
# "the charge posted a day or two after the purchase", which is the normal
# domain rather than a discrepancy.
UNAMBIGUOUS_MAX_DATE_GAP_DAYS = 3

# The vendor must be *named*, not merely plausible. compute_vendor_score returns
# exactly 0.80 for its strong signal — a word of the receipt's vendor appearing
# in the bank description — and that is the ceiling of that path. The only score
# above 0.80 comes from the fuzzy fallback on two near-identical strings, which
# a descriptor carrying a rail tag or a branch city ("ORCHID TELECOM MOBILITY"
# against "Orchid Telecom") does not produce. So 0.80, not a higher number, is
# the value that means "the bank line names this vendor"; a threshold above it
# would exclude the case the rule exists to describe. The rule still requires an
# exact amount, a ≤3-day gap and a clean merchant-contradiction check before it
# settles anything.
UNAMBIGUOUS_MIN_VENDOR_SCORE = 0.80

# A description token has to be at least this long before its failure to match
# the receipt's vendor means anything. Three letters are as often an airport
# code, a branch tag or a truncation as a name.
_MERCHANT_TOKEN_MIN_LEN = 4

# rapidfuzz ratio below which two names are simply not the same name. 50 is
# deliberately generous — "amzn"/"amazon" scores 60 and stays one party — so the
# rule only fires on names with nothing in common.
_VENDOR_TOKEN_MATCH_RATIO = 50.0


@dataclass
class PassConfig:
    """Configuration for a single reconciliation pass."""
    name: str
    amount_tolerance: Decimal = PENNY_TOLERANCE
    fx_matching: bool = False
    review_threshold: float = 0.60
    # auto_approve_threshold: Two-layer design:
    #   - config/settings.py defines the server-side storage threshold (default 0.90)
    #     used by the REST API layer to decide whether to auto-approve stored matches.
    #   - Individual PassConfig instances (e.g. PASS_RELAXED) can override this value
    #     for specific passes. PASS_RELAXED uses 0.70 because relaxed-pass matches
    #     need lower confidence to auto-approve (they are inherently less certain).
    #   - The default_factory reads from settings so PASS_STRICT inherits the
    #     server-side value automatically.
    auto_approve_threshold: float = field(
        default_factory=lambda: _recon_cfg.auto_approve_threshold
    )
    amount_weight: float = 0.5
    date_weight: float = 0.3
    vendor_weight: float = 0.2


PASS_STRICT = PassConfig(name="strict")
PASS_RELAXED = PassConfig(
    name="relaxed",
    amount_tolerance=Decimal("1.00"),
    fx_matching=True,
    review_threshold=0.40,
    auto_approve_threshold=0.70,
    amount_weight=0.6,
    date_weight=0.2,
    vendor_weight=0.2,
)
DEFAULT_PASSES = [PASS_STRICT, PASS_RELAXED]


def score_manual_match(
    txn_amount: Decimal,
    txn_currency: str,
    txn_date: date,
    txn_description: str,
    doc_total: Decimal | None,
    doc_currency: str | None,
    doc_date: date | None,
    doc_vendor: str | None,
    max_date_days: int = 45,
    *,
    doc_fields: Mapping[str, object] | None = None,
    doc_filename: str = "",
) -> tuple[float, float, float, float]:
    """Deterministic scoring for manual match (no LLM).

    Returns (confidence, amount_score, date_score, vendor_score).
    Uses PASS_STRICT weights: amount 50%, date 30%, vendor 20%.

    The amount half asks :func:`core.amount_match.compare_amounts` — the same
    rule the auto matcher and the explanation builder use, so a manual pick and
    an automatic one never disagree about a pair. Comparing magnitudes with the
    currencies ignored would score CAD 3,000 against USD 3,000 as a perfect 1.0
    on a pair the auto matcher rejects outright.

    ``doc_fields`` is the document row (currency_source, subtotal, the tax
    columns) when the caller has it, so the receipt's currency is *resolved*
    rather than taken at face value: an app-store screenshot with a bare "$"
    and a 12% Canadian tax line is CAD, whatever the extractor guessed. Without it
    only the stored code is available.

    The vendor half is :func:`compute_vendor_score` — the engine's own vendor
    scorer. A bare ``token_sort_ratio`` scores
    "EXAMPLE SVC *Subscription CITY ST" against "Example Services LLC" at 0.46
    where the auto matcher scores the same pair 0.80: one pair, two answers.
    """
    doc_code, doc_source = resolve_document_currency(
        doc_fields if doc_fields is not None else {"currency": doc_currency}
    )
    comparison = compare_amounts(
        txn_amount, txn_currency, doc_total, doc_code,
        doc_currency_source=doc_source,
    )

    if comparison.same_currency and comparison.verdict == _AMOUNT_MISMATCH:
        # Same currency, amounts disagree: keep a graded score so the manual
        # picker can still rank near-misses ahead of wild ones.
        txn_abs, doc_abs = abs(txn_amount), abs(doc_total or Decimal(0))
        max_val = max(txn_abs, doc_abs)
        amount_score = (
            max(0.0, 1.0 - float(abs(txn_abs - doc_abs) / max_val)) if max_val else 0.0
        )
    else:
        amount_score = comparison.score

    # Date score
    if doc_date is None:
        date_score = 0.5  # Neutral for unknown date
    else:
        day_diff = abs((txn_date - doc_date).days)
        if day_diff == 0:
            date_score = 1.0
        elif day_diff <= max_date_days:
            date_score = max(0.0, 1.0 - (day_diff / max_date_days))
        else:
            date_score = 0.0

    # Vendor score — the engine's scorer, so the manual picker and the auto
    # matcher can never disagree about the same two strings.
    vendor_score = compute_vendor_score(
        txn_description or "", doc_vendor or "", doc_filename or "",
    )

    # Weighted composite
    confidence = 0.5 * amount_score + 0.3 * date_score + 0.2 * vendor_score

    return (round(confidence, 4), round(amount_score, 4),
            round(date_score, 4), round(vendor_score, 4))


@dataclass
class UnmatchedReason:
    """Explains why a transaction or document couldn't be matched."""
    item_type: str  # "transaction" or "document"
    item_id: int
    reason: str  # "no_matching_amount", "below_threshold", "no_candidates"
    details: str
    best_candidate_score: float | None = None


@dataclass
class MultiPassResult:
    """Result of multi-pass reconciliation."""
    matches: list[ReconciliationMatch] = field(default_factory=list)
    pass_labels: dict[int, str] = field(default_factory=dict)  # match index -> pass name
    unmatched_transactions: list[UnmatchedReason] = field(default_factory=list)
    unmatched_documents: list[UnmatchedReason] = field(default_factory=list)
    non_reconcilable_txn_ids: set[int] = field(default_factory=set)
    pass_counts: dict[str, int] = field(default_factory=dict)  # pass name -> match count
    # How the LLM agents actually fared this run: total calls, how many fell
    # back to deterministic scoring, and under which label. Carried out of the
    # engine so the API can report it instead of leaving it in a log file
    # nobody reads.
    llm_stats: dict = field(default_factory=dict)
    # How many agent questions were answered from a stored verdict instead of
    # the model: {enabled, known, hits, misses, new}.
    memory_stats: dict = field(default_factory=dict)

    @property
    def reconcilable_count(self) -> int:
        return len(self.matches) + len(self.unmatched_transactions)

    @property
    def match_count(self) -> int:
        return len(self.matches)

    @property
    def match_rate(self) -> float:
        if self.reconcilable_count == 0:
            return 0.0
        return self.match_count / self.reconcilable_count * 100


def compute_amount_score(transaction_amount: Decimal, document_total: Decimal) -> float:
    """Score amount match with tolerance for FX rounding.

    Returns:
        1.0 for exact match
        0.9 for a difference no larger than ``PENNY_TOLERANCE`` (penny rounding)
        0.0 for no match
    """
    diff = abs(abs(transaction_amount) - abs(document_total))
    if diff == 0:
        return 1.0
    if diff <= PENNY_TOLERANCE:
        return 0.9
    return 0.0


def compute_amount_score_with_tolerance(
    transaction_amount: Decimal,
    document_total: Decimal,
    tolerance: Decimal = PENNY_TOLERANCE,
) -> float:
    """Score amount match with configurable tolerance."""
    diff = abs(abs(transaction_amount) - abs(document_total))
    if diff == 0:
        return 1.0
    if diff <= PENNY_TOLERANCE:
        return 0.9
    if diff <= tolerance:
        return 0.8
    # A bank that deducts its handling fee from an incoming wire credits less
    # than the invoice by that fee. Only a caller that already asked for a
    # dollars-wide tolerance gets this route.
    if diff <= _WIRE_FEE_TOLERANCE and tolerance >= Decimal("2.00"):
        return 0.65
    return 0.0


def compute_amount_score_fx(
    txn_amount: Decimal,
    doc_total: Decimal,
    txn_currency: str,
    doc_currency: str,
) -> float:
    """Score amount match across currencies using the canonical amount rule.

    Thin wrapper over :func:`core.amount_match.compare_amounts` — same
    currency compares magnitudes, different currencies convert through the
    CAD/USD band. One rule, one place; this function keeps its name and its
    numbers so callers and their tests are unaffected.
    """
    return compare_amounts(
        txn_amount, txn_currency, doc_total, doc_currency,
    ).score


def _extract_usd_amount(description: str) -> Decimal | None:
    """Extract the original USD amount from CC transaction descriptions.

    Credit-card descriptions include FX info: "USD 12.34@1.3500 EXAMPLE VENDOR CITY"
    Returns the USD amount (12.34) or None.
    """
    m = re.search(r"USD\s+([\d.]+)@[\d.]+", description)
    if m:
        try:
            return Decimal(m.group(1))
        except Exception:
            pass
    return None


_OWN_COMPANY_ENV = "OWN_COMPANY_PATTERNS"
_OWN_COMPANY_FALLBACK = "your company,your company inc"
_own_company_warned = False


def _own_company_patterns() -> list[str]:
    """The names this ledger's own outgoing invoices are issued under.

    Read from ``OWN_COMPANY_PATTERNS`` (comma-separated, case-insensitive
    substring match against a document's vendor field). It is the only signal
    that tells an invoice this ledger *issued* from a bill it *received*, and
    therefore the only thing that lets a document be classified as income
    rather than expense: see :func:`_is_income_document` and
    :func:`check_sign_compatibility`.

    Left unset, or left at the placeholder ``.env.example`` ships, the patterns
    match no vendor name any document actually carries: every document then
    classifies as an expense, and an invoice cannot match the deposit that paid
    it. That is a silent wrong answer rather than an error, so the first call in
    a process warns and names the variable. The classification itself is
    unchanged — the placeholder is still what gets matched — so what the
    variable holds decides behaviour, not the warning.
    """
    global _own_company_warned
    raw = (os.getenv(_OWN_COMPANY_ENV) or "").strip()
    unconfigured = not raw or raw.lower() == _OWN_COMPANY_FALLBACK
    if unconfigured and not _own_company_warned:
        _own_company_warned = True
        logger.warning(
            "%s is unset or still holds its placeholder value. A document is "
            "recognised as income by matching its vendor against the names your "
            "own invoices are issued under; until %s names those, every document "
            "is classified as an expense and invoices you issued will not match "
            "the deposits that paid them. Set it to a comma-separated list of "
            "your own company names.",
            _OWN_COMPANY_ENV, _OWN_COMPANY_ENV,
        )
    return [p.strip().lower() for p in (raw or _OWN_COMPANY_FALLBACK).split(",") if p.strip()]


# One warning per process, emitted when the engine is imported rather than on
# the first document, so a misconfiguration shows up in the log at startup and
# not in the middle of a run.
_own_company_patterns()


def _is_income_document(doc: "Document") -> bool:
    """Determine if a document represents income (invoice sent by the company).

    Income documents (outgoing invoices) should only match CREDIT/income
    transactions. Expense documents (receipts, bills) should only match
    DEBIT/expense transactions.

    Heuristics:
    - Filename contains "Invoice" + vendor is the user's own company → income
    - Filename contains "Invoice" alone → likely income (invoices are sent)
    - Has an invoice_number → likely income
    - Otherwise → expense (receipt/bill)
    """
    filename = (doc.original_filename or "").lower()
    vendor = (doc.vendor or "").lower()

    own_company_patterns = _own_company_patterns()

    has_invoice_keyword = "invoice" in filename
    is_own_company = any(p in vendor for p in own_company_patterns)

    # Strong signal: invoice from own company = outgoing invoice = income
    if has_invoice_keyword and is_own_company:
        return True

    # Invoice with invoice_number from own company
    if doc.invoice_number and is_own_company:
        return True

    # Default: not income (treat as expense document)
    return False


def _is_transaction_expense(txn: "Transaction") -> bool:
    """Determine if a transaction represents an expense (money going out).

    Primary signal: ``transaction_type`` field on the Transaction model
    (``DEBIT`` = purchase/outflow = expense; ``CREDIT`` = payment/deposit =
    not an expense). This is the canonical form produced by the LLM
    statement parser — ``core/llm_statement_parser.py`` stores bank
    debits as ``transaction_type="DEBIT"`` with a POSITIVE ``amount``,
    which the pure-sign heuristic below would misclassify.

    Fallback (no ``transaction_type`` on the row): use the amount sign —
    credit-card positive = expense, bank negative = expense. Rows built
    directly rather than through the parser carry no ``transaction_type``,
    and this keeps them classifiable.
    """
    tt = (getattr(txn, "transaction_type", None) or "").upper()
    if tt == "DEBIT":
        return True
    if tt == "CREDIT":
        return False

    # Fallback — no transaction_type recorded on this row.
    # account_str() accepts both forms the account field takes: an AccountType
    # enum instance, and the plain string the statement parser writes.
    account = account_str(txn.account).upper()
    is_cc = account in ("CREDITCARD", "CREDIT_CARD")
    if is_cc:
        return txn.amount > 0  # CC: positive = purchase = expense
    return txn.amount < 0  # Bank: negative = outflow = expense


def check_sign_compatibility(txn: "Transaction", doc: "Document") -> bool:
    """Check if the transaction sign is compatible with the document type.

    Returns True if the match is sign-compatible, False if it should be rejected.
    - Expense transactions should match expense documents (receipts, bills)
    - Income transactions should match income documents (invoices sent)
    """
    is_expense = _is_transaction_expense(txn)
    is_income_doc = _is_income_document(doc)

    if is_expense and is_income_doc:
        # Expense transaction matched to income invoice — REJECT
        return False
    if not is_expense and not is_income_doc:
        # Income transaction matched to expense receipt — REJECT
        return False

    return True


def compute_date_score(
    transaction_date: date,
    best_date: date | None,
) -> float:
    """Score based on how close the transaction date is to the document's best known date.

    Uses a smooth decay: same day = 1.0, decays over distance.
    No hard cutoff — but distant dates get very low scores.
    """
    if best_date is None:
        return 0.1  # small baseline when no date info available

    diff = abs((transaction_date - best_date).days)
    if diff == 0:
        return 1.0
    if diff <= 3:
        return 0.9
    if diff <= 7:
        return 0.7
    if diff <= 14:
        return 0.5
    if diff <= 21:
        return 0.4
    if diff <= 45:
        return 0.2
    return 0.02  # very small but not zero — allows amount+vendor to still work


_PAYMENT_INTERMEDIARY_PATTERNS = [
    r"WISEMSP", r"WISE\s*MSP", r"WISE\s*PAYMENTS",
    r"PAYPALMSP", r"PAYPAL\s*MSP",
    r"INTERACe-Transfer(?:Sent|Fulfill)", r"INTERAC.*TRANSFER.*SENT",
    r"OUTGOING\s*WIRE", r"OUTGOINGWIRE",
]


def compute_vendor_score(
    transaction_description: str,
    document_vendor: str,
    document_filename: str = "",
) -> float:
    """Fuzzy vendor name similarity using rapidfuzz, case-insensitive.

    Also checks for common keyword matches (telecom, utility and transfer-service
    keywords, etc.)
    which may not show up well in pure string similarity.

    Payment intermediaries (Wise, PayPal, INTERAC) get a differentiated
    score (0.30–0.55) based on vendor name uniqueness from the document
    filename, enabling the greedy matcher to disambiguate multiple
    contractor payments on the same date.
    """
    if not transaction_description or not document_vendor:
        return 0.0

    desc_lower = transaction_description.lower()
    vendor_lower = document_vendor.lower()

    # Direct keyword match — vendor name appears in transaction description
    vendor_words = re.findall(r'[a-z]{3,}', vendor_lower)
    for word in vendor_words:
        if word in desc_lower:
            return 0.8  # strong signal

    # Payment intermediary — bank description shows Wise/PayPal/INTERAC
    # instead of the actual vendor.  The receipt will show the contractor
    # name (e.g., "Contractor A", "Contractor B"), not the platform name — this mismatch
    # is expected and should NOT penalize matching.  Return a score derived
    # from the vendor name + filename to give the greedy matcher a
    # unique fingerprint per document, breaking ties when multiple
    # intermediary transactions on the same date have identical amounts.
    for pattern in _PAYMENT_INTERMEDIARY_PATTERNS:
        if re.search(pattern, transaction_description, re.IGNORECASE):
            # Verify vendor name appears in the document filename —
            # this confirms the document is properly attributed.
            fname_lower = (document_filename or "").lower()
            vendor_confirmed = False
            for word in vendor_words:
                if len(word) >= 4 and word in fname_lower:
                    vendor_confirmed = True
                    break
            if vendor_confirmed:
                # Use a deterministic hash of the vendor name to create a
                # unique but reproducible offset in [0.00, 0.20].  This
                # ensures different vendors get different scores, enabling
                # the optimizer to find a globally better assignment.
                h = sum(ord(c) for c in vendor_lower) % 100
                return 0.50 + h / 500.0  # range: 0.50–0.70
            # Unconfirmed vendor for intermediary: the receipt's vendor name
            # naturally differs from the bank description (platform vs payee).
            # Use a moderate neutral score with a small hash offset so the
            # greedy matcher can still disambiguate different contractors.
            h = sum(ord(c) for c in vendor_lower) % 100
            return 0.45 + h / 500.0  # range: 0.45–0.65

    # Fuzzy ratio as fallback
    ratio = fuzz.ratio(desc_lower, vendor_lower)
    return ratio / 100.0


def _extract_payment_date(doc: Document) -> date | None:
    """Try to extract the expected payment/charge date from the document.

    Checks:
    1. The extraction raw JSON for a 'payment_date' field
    2. The 'payment_method' field for date hints (e.g., "Bank account on February 02")
    3. Falls back to None if no payment date clues found
    """
    import json

    # Check extraction_raw_json for payment_date field
    if doc.extraction_raw_json:
        try:
            raw = doc.extraction_raw_json
            if isinstance(raw, str):
                # Handle both JSON strings and Python repr strings.
                # Gemini extraction stores as repr (single quotes) but
                # json.loads requires double quotes — try JSON first,
                # fall back to eval for repr format.
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    data = json.loads(raw.replace("'", '"'))

                payment_date_str = data.get("payment_date") if isinstance(data, dict) else None
                if payment_date_str and payment_date_str != "null":
                    try:
                        return date.fromisoformat(payment_date_str)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    # Check payment_method for date hints
    if doc.payment_method:
        pm = doc.payment_method
        # Look for patterns like "February 02", "Feb 2", "YYYY-MM-DD"
        # Month name + day
        m = re.search(
            r'(?:on|by|date:?)\s*(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?',
            pm, re.IGNORECASE
        )
        if m:
            month_str, day_str, year_str = m.groups()
            year = int(year_str) if year_str else (doc.document_date.year if doc.document_date else datetime.now().year)
            try:
                for fmt in ("%B", "%b"):
                    try:
                        month = datetime.strptime(month_str, fmt).month
                        return date(year, month, int(day_str))
                    except ValueError:
                        continue
            except Exception:
                pass

        # ISO date pattern
        m = re.search(r'(\d{4}-\d{2}-\d{2})', pm)
        if m:
            try:
                return date.fromisoformat(m.group(1))
            except ValueError:
                pass

    return None


_MONTH_ABBR_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_date_hint_from_filename(filename: str) -> date | None:
    """Extract a month/year hint from the document filename.

    Recognises patterns like ``Dec2026``, ``Nov2026``, ``2026-12``.
    Returns the 15th of that month as a representative date.
    """
    if not filename:
        return None
    # Pattern 1: MonYYYY (e.g. Dec2026, Nov2026)
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{4})", filename, re.IGNORECASE)
    if m:
        month_num = _MONTH_ABBR_MAP.get(m.group(1).lower()[:3])
        year = int(m.group(2))
        if month_num and 2020 <= year <= 2030:
            return date(year, month_num, 15)
    # Pattern 2: YYYY Mon (e.g. "2026 Dec")
    m = re.search(r"(\d{4})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", filename, re.IGNORECASE)
    if m:
        year = int(m.group(1))
        month_num = _MONTH_ABBR_MAP.get(m.group(2).lower()[:3])
        if month_num and 2020 <= year <= 2030:
            return date(year, month_num, 15)
    # Pattern 3: DDMonYYYY (e.g. Dec252026)
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{1,2}(\d{4})", filename, re.IGNORECASE)
    if m:
        month_num = _MONTH_ABBR_MAP.get(m.group(1).lower()[:3])
        year = int(m.group(2))
        if month_num and 2020 <= year <= 2030:
            return date(year, month_num, 15)
    return None


def _get_best_date(doc: Document) -> date | None:
    """Get the best date to use for matching.

    Priority: payment_date > document_date > filename month hint.

    When the extracted document_date conflicts with the month the filename
    names (the extractor reads November off the page while the filename says
    ``Dec2026``), prefer the filename hint — a filename is assigned by a person
    and is a more reliable month signal than an extraction from the page.
    """
    payment_date = _extract_payment_date(doc)
    best = payment_date or doc.document_date

    # Cross-check against filename month hint
    fname_hint = _extract_date_hint_from_filename(doc.original_filename)
    if best and fname_hint:
        # If the extracted date's month differs from the filename month, and
        # the filename month is later, prefer the filename hint. This corrects
        # an extraction that picked up an issue or period date printed on the
        # document rather than the month the document belongs to.
        if best.month != fname_hint.month or best.year != fname_hint.year:
            # Only override if the filename month is within 2 months
            month_diff = (fname_hint.year - best.year) * 12 + fname_hint.month - best.month
            if 0 < month_diff <= 2:
                return fname_hint
    if best:
        return best
    return fname_hint


def _extract_alt_amount(doc: Document) -> Decimal | None:
    """Extract the alternative/converted amount from extraction raw JSON.

    For wire transfers, Gemini extracts both the sent amount (total, in USD)
    and the converted amount (amount, in CAD). The 'amount' field often contains
    the exact CAD value that appears on the bank statement.
    """
    import json

    if not doc.extraction_raw_json:
        return None

    try:
        raw = doc.extraction_raw_json
        if isinstance(raw, str):
            if raw.startswith("{") or raw.startswith("["):
                data = json.loads(raw)
            else:
                data = json.loads(raw.replace("'", '"'))

            if isinstance(data, dict):
                alt = data.get("amount")
                if alt and alt != "null":
                    cleaned = str(alt).strip().replace("$", "").replace(",", "").replace(" ", "")
                    if cleaned:
                        val = Decimal(cleaned)
                        # Only use if it's different from total (avoids duplicates)
                        if doc.total is not None and val != abs(doc.total) and val > 0:
                            return val
    except Exception:
        pass
    return None


def compute_confidence(
    amount_score: float,
    date_score: float,
    vendor_score: float,
    amount_weight: float = 0.5,
    date_weight: float = 0.3,
    vendor_weight: float = 0.2,
) -> float:
    """Weighted sum of individual scores."""
    return (
        amount_weight * amount_score
        + date_weight * date_score
        + vendor_weight * vendor_score
    )


def _description_tokens(text: str) -> set[str]:
    """Vendor-ish words in a bank description — noise and digits removed."""
    words = re.findall(r"[a-z]{3,}", (text or "").lower())
    return {w for w in words if w not in _BANK_NOISE_TOKENS}


def _merchant_tokens(text: str) -> list[tuple[str, str]]:
    """``(original, lowercase)`` for every word in a description that names a party.

    Bank noise and payment rails are gone; order is the order they were printed
    in, because that is how the name gets read back to the user ("WHOLESALE
    CLUB", not "CLUB WHOLESALE").
    """
    out: list[tuple[str, str]] = []
    for raw in re.findall(r"[A-Za-z]{3,}", text or ""):
        lower = raw.lower()
        if lower in _BANK_NOISE_TOKENS or lower in _PAYMENT_RAIL_TOKENS:
            continue
        out.append((raw, lower))
    return out


@lru_cache(maxsize=1)
def _known_vendor_patterns() -> tuple[re.Pattern, ...]:
    """Every vendor name a shipped or user-supplied rule already names, compiled.

    Two sources, both of which exist for other reasons and are therefore
    maintained: the categorization agent's canonical alias table
    (``core/categorization_agent/vendor_clustering.CANONICAL_ALIASES``) and the
    user's own ``config/vendor_rules.yaml``. A description token that matches one
    of these is a merchant name by definition, because a rule already names it.

    Loaded lazily and cached: reconciliation must not pay a YAML parse per
    candidate pair, and importing the categorization agent at module scope would
    be a cycle.
    """
    patterns: list[re.Pattern] = []
    try:
        from core.categorization_agent.vendor_clustering import CANONICAL_ALIASES

        patterns.extend(pattern for pattern, _key in CANONICAL_ALIASES)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not load canonical vendor aliases: %s", exc)

    try:
        import yaml

        from config.settings import PROJECT_ROOT

        raw = (PROJECT_ROOT / "config" / "vendor_rules.yaml").read_text(encoding="utf-8")
        for rule in (yaml.safe_load(raw) or {}).get("rules") or []:
            expression = (rule or {}).get("vendor_pattern")
            if not expression:
                continue
            try:
                patterns.append(re.compile(expression, re.IGNORECASE))
            except re.error as exc:
                logger.warning("skipping unusable vendor_pattern %r: %s", expression, exc)
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not load config/vendor_rules.yaml: %s", exc)

    return tuple(patterns)


def _known_vendor_in(text: str) -> str | None:
    """The vendor name a configured rule already names inside ``text``, or None."""
    if not text:
        return None
    for pattern in _known_vendor_patterns():
        found = pattern.search(text)
        if found:
            return found.group(0).strip() or text
    return None


def _vendor_cluster_key(text: str) -> str:
    """The categorization agent's stable vendor key for a name, or "".

    ``EXMPL MKTP CA`` and ``Example.ca`` both key to ``example``, which is how the
    contradiction rule tells an abbreviation apart from a different merchant.
    """
    if not text:
        return ""
    try:
        from core.categorization_agent.vendor_clustering import normalize_description

        return normalize_description(text) or ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not normalize vendor name %r: %s", text, exc)
        return ""


@lru_cache(maxsize=4096)
def _merchant_contradiction(
    txn_description: str,
    doc_vendor: str,
    doc_filename: str = "",
) -> str | None:
    """The merchant this description names, when it is NOT the receipt's vendor.

    Returns the name as printed on the bank line ("WHOLESALE CLUB"), or None
    when there is no contradiction to report. This is the merchant-contradiction question, and
    it is asked at every vendor sub-score:

    1. Strip bank noise and payment rails. Nothing left ("POS PURCHASE 0000",
       "MONTHLY FEE", "WISE TRANSFER", "INTERAC E-TRANSFER") means the
       description names nobody — no contradiction is possible, and the
       confidence floor judges the pair on its own.
    2. A receipt with no vendor and no vendor in its filename has nothing to
       contradict.
    3. The two are the same party if they share a token, if one name contains
       the other, or if they key to the same vendor cluster ("EXMPL MKTP CA" and
       "Example.ca" are both ``example``) — or if a single known-vendor pattern
       matches both.
    4. Otherwise the description names someone else if it names a vendor a
       configured rule already knows, or if any word in it of four letters or
       more is simply not the receipt's vendor by fuzzy ratio. "WHOLESALE CLUB"
       against an ORCHID TELECOM receipt is both.

    Cached on its three strings: a reconciliation run asks this once per
    candidate pair, and the same description recurs across many of them.
    """
    merchant = _merchant_tokens(txn_description)
    if not merchant:
        return None

    vendor_tokens = _description_tokens(doc_vendor)
    file_tokens = _description_tokens(doc_filename)
    doc_tokens = vendor_tokens | file_tokens
    if not doc_tokens:
        return None

    desc_tokens = {lower for _raw, lower in merchant}

    # ── Same party? ───────────────────────────────────────────────────────
    if desc_tokens & doc_tokens:
        return None
    for d in desc_tokens:
        for v in doc_tokens:
            if len(d) >= 4 and len(v) >= 4 and (d in v or v in d):
                return None

    label = " ".join(raw for raw, _lower in merchant[:3])
    vendor_label = (doc_vendor or "").strip() or "unknown"

    desc_key = _vendor_cluster_key(label)
    vendor_key = _vendor_cluster_key(doc_vendor)
    if desc_key and vendor_key and desc_key == vendor_key:
        return None
    for pattern in _known_vendor_patterns():
        if pattern.search(label) and pattern.search(doc_vendor or ""):
            return None

    # A payment intermediary's own description (WISEMSP, PAYPAL MSP, an outgoing
    # wire) names the platform, not the payee — the mismatch is expected.
    for pattern in _PAYMENT_INTERMEDIARY_PATTERNS:
        if re.search(pattern, txn_description or "", re.IGNORECASE):
            return None

    # ── Does it name somebody? ────────────────────────────────────────────
    known = _known_vendor_in(label)
    if known:
        # Report the merchant, not the whole bank line: "ORCHID TELECOM", not
        # "ORCHID TELECOM COMM BPY".
        return f"{known}, receipt is from {vendor_label}"

    vendor_compare = " ".join(sorted(vendor_tokens)) or (doc_vendor or "")
    for raw, lower in merchant:
        if len(lower) < _MERCHANT_TOKEN_MIN_LEN:
            continue
        best = max(
            (fuzz.ratio(lower, other) for other in doc_tokens),
            default=0.0,
        )
        best = max(best, fuzz.partial_ratio(lower, vendor_compare))
        if best < _VENDOR_TOKEN_MATCH_RATIO:
            return f"{label}, receipt is from {vendor_label}"

    return None


def _description_names_other_vendor(
    txn_description: str,
    doc_vendor: str | None,
    doc_filename: str = "",
) -> bool:
    """True when the bank description plainly names someone else.

    The boolean form of :func:`_merchant_contradiction`, kept because that is
    the question most callers ask.
    """
    return _merchant_contradiction(
        txn_description or "", doc_vendor or "", doc_filename or ""
    ) is not None


@dataclass(frozen=True)
class MatchClassification:
    """Whether a scored pair may be stored as a settled (MATCHED) match."""

    flagged: bool
    reasons: tuple[str, ...] = ()

    @property
    def settled(self) -> bool:
        return not self.flagged

    def reason_text(self) -> str:
        return "; ".join(self.reasons)


def classify_match(
    confidence: float,
    vendor_score: float,
    txn_description: str,
    doc_vendor: str | None,
    doc_filename: str = "",
    confidence_floor: float = MATCH_CONFIDENCE_FLOOR,
    amount_score: float | None = None,
) -> MatchClassification:
    """Decide whether a pair is a reconciliation or only a suggestion.

    Returns ``flagged=True`` when the pair must land in review instead of
    being written as MATCHED:

    1. confidence below ``confidence_floor`` — not enough evidence, whatever
       the mix of signals;
    2. the bank description plainly names a different merchant — the amount and
       date can agree by coincidence, and a receipt from another vendor is the
       textbook wrong pair. Asked at EVERY vendor sub-score: gating it on
       ``vendor_score <= VENDOR_SCORE_NO_SIGNAL`` would let "WHOLESALE CLUB
       #0000" against a telecom invoice settle, because the two names happen to
       share four letters and rapidfuzz therefore returns 0.22 rather than 0.0; or
    3. no amount evidence at all. Amount is the primary signal (weight 0.50),
       and ``confidence`` can come from the LLM final agent while the
       deterministic scores say the amounts never reconciled — which is how a
       CAD 3,000 row against a USD 3,000 receipt could be written down as a
       majority-confidence match even after the FX band rejected it. Date and
       vendor alone are a suggestion, not a reconciliation.

    Flagged pairs stay in the candidate pool, so a better candidate found
    later can displace them (see DISPLACEMENT_MARGIN).
    """
    reasons: list[str] = []
    if confidence < confidence_floor:
        reasons.append(
            f"confidence {confidence:.0%} is below the {confidence_floor:.0%} floor"
        )
    if amount_score is not None and amount_score <= AMOUNT_SCORE_NO_EVIDENCE:
        reasons.append(
            "no amount agreement — the amounts do not reconcile at any rate the "
            "engine can justify"
        )
    contradiction = _merchant_contradiction(
        txn_description or "", doc_vendor or "", doc_filename or ""
    )
    if contradiction:
        reasons.append(f"description names {contradiction}")
    return MatchClassification(flagged=bool(reasons), reasons=tuple(reasons))


def _fix_intermediary_crossassign(
    results: list[ReconciliationMatch],
    transactions: list[Transaction],
    documents: list[Document],
    candidate_map: dict[tuple[int, int], tuple[float, float, float, float]],
    txn_ids: list[int],
    doc_ids: list[int],
    review_threshold: float,
    auto_approve_threshold: float,
) -> list[ReconciliationMatch]:
    """Fix cross-assignments among same-date intermediary payments.

    When multiple Wise/PayPal/INTERAC payments occur on the same date,
    the greedy matcher may assign invoices to the wrong transaction
    because scores are tied.  This function identifies such groups and
    reassigns by rank-ordering: largest document total ↔ largest
    transaction amount, preserving the count and improving accuracy.
    """
    txn_map = {t.id: t for t in transactions}
    doc_map = {d.id: d for d in documents}

    # Group matched results by (date cluster, intermediary-flag).
    # Use a 2-day window so Dec 9 and Dec 10 payments form one group
    # (Wise/INTERAC batches often span adjacent business days).
    from collections import defaultdict
    intermediary_indices: list[int] = []
    for idx, m in enumerate(results):
        txn = txn_map.get(m.transaction_id)
        if txn is None:
            continue
        is_intermediary = any(
            re.search(p, txn.description or "", re.IGNORECASE)
            for p in _PAYMENT_INTERMEDIARY_PATTERNS
        )
        if is_intermediary:
            intermediary_indices.append(idx)

    # Cluster intermediary matches by date proximity (±2 days)
    groups: dict[date, list[int]] = defaultdict(list)
    for idx in intermediary_indices:
        txn = txn_map[results[idx].transaction_id]
        txn_date = txn.date_posted
        # Find existing cluster within 2 days
        merged = False
        for cluster_date in list(groups.keys()):
            if abs((txn_date - cluster_date).days) <= 2:
                groups[cluster_date].append(idx)
                merged = True
                break
        if not merged:
            groups[txn_date].append(idx)

    # Also check for cross-date misassignments: if an intermediary match
    # has txn date >30 days from the doc date, check if there's an unmatched
    # transaction closer to the doc date with a valid FX score. This handles
    # cases where the greedy matcher assigns a doc to a far-away
    # transaction when a closer one exists.
    result_txn_ids = {r.transaction_id for r in results}
    unmatched_txn_set = {t.id for t in transactions if t.id not in result_txn_ids}
    for idx, m in enumerate(results):
        txn = txn_map.get(m.transaction_id)
        doc = doc_map.get(m.document_id)
        if txn is None or doc is None:
            continue
        is_intermediary = any(
            re.search(p, txn.description or "", re.IGNORECASE)
            for p in _PAYMENT_INTERMEDIARY_PATTERNS
        )
        if not is_intermediary:
            continue
        best_date = _get_best_date(doc)
        if best_date is None:
            continue
        day_diff = abs((txn.date_posted - best_date).days)
        if day_diff <= 30:
            continue  # close enough
        # This match is >30 days apart — look for a better unmatched transaction
        for uid in list(unmatched_txn_set):
            utxn = txn_map.get(uid)
            if utxn is None:
                continue
            pair_key = (uid, m.document_id)
            if pair_key not in candidate_map:
                continue
            u_day_diff = abs((utxn.date_posted - best_date).days)
            if u_day_diff < day_diff:
                # Found a closer unmatched transaction — swap
                conf, a_score, d_score, v_score = candidate_map[pair_key]
                status = (
                    MatchStatus.AUTO_APPROVED
                    if conf >= auto_approve_threshold
                    else MatchStatus.PENDING_REVIEW
                )
                # Free up the old transaction
                unmatched_txn_set.add(m.transaction_id)
                unmatched_txn_set.discard(uid)
                results[idx] = ReconciliationMatch(
                    transaction_id=uid,
                    document_id=m.document_id,
                    confidence_score=conf,
                    amount_score=a_score,
                    date_score=d_score,
                    vendor_score=v_score,
                    match_type=MatchType.ONE_TO_ONE,
                    status=status,
                )
                break

    # For each group of ≥2 intermediary matches on the same date, re-assign
    # by rank order.  Also pull in unmatched same-date intermediary txns to
    # widen the pool — a matched doc might belong to an unmatched txn.
    for cluster_date_key, indices in groups.items():
        if len(indices) < 2:
            continue

        match_date = cluster_date_key

        # Collect the transaction and document IDs in this group
        group_txn_ids = [results[i].transaction_id for i in indices]
        group_doc_ids = [results[i].document_id for i in indices]

        # Include unmatched nearby-date intermediary txns in the ranking pool
        extra_txn_ids = []
        for uid in list(unmatched_txn_set):
            utxn = txn_map.get(uid)
            if utxn is None or abs((utxn.date_posted - match_date).days) > 2:
                continue
            is_int = any(
                re.search(p, utxn.description or "", re.IGNORECASE)
                for p in _PAYMENT_INTERMEDIARY_PATTERNS
            )
            if is_int:
                # Check this txn has at least one valid candidate pair
                # with one of the group docs
                has_candidate = any(
                    (uid, did) in candidate_map for did in group_doc_ids
                )
                if has_candidate:
                    extra_txn_ids.append(uid)

        all_txn_ids = group_txn_ids + extra_txn_ids

        # Sort transactions by absolute amount (descending)
        group_txns = sorted(
            [(tid, abs(txn_map[tid].amount)) for tid in all_txn_ids],
            key=lambda x: x[1], reverse=True,
        )
        # Sort documents by absolute total (descending)
        group_docs = sorted(
            [(did, abs(doc_map[did].total or 0)) for did in group_doc_ids],
            key=lambda x: x[1], reverse=True,
        )

        # Rank-order: pair txns and docs by descending amount
        # Only pair up to min(len(txns), len(docs)) — extra txns stay unmatched
        new_pairs = list(zip(
            [t[0] for t in group_txns[:len(group_docs)]],
            [d[0] for d in group_docs],
        ))

        # Verify all new pairs are valid candidates
        all_valid = all(
            (tid, did) in candidate_map
            for tid, did in new_pairs
        )
        if not all_valid:
            continue  # Can't reassign — some pairs aren't valid candidates

        # Check if this is actually different from current assignment
        current_pairs = set((results[i].transaction_id, results[i].document_id) for i in indices)
        new_pairs_set = set(new_pairs)
        if current_pairs == new_pairs_set:
            continue

        # Reassign existing matches
        for idx_pos, result_idx in enumerate(indices):
            new_tid, new_did = new_pairs[idx_pos]
            pair_key = (new_tid, new_did)
            conf, a_score, d_score, v_score = candidate_map[pair_key]
            status = (
                MatchStatus.AUTO_APPROVED
                if conf >= auto_approve_threshold
                else MatchStatus.PENDING_REVIEW
            )
            # Track txn swaps for unmatched_txn_set
            old_tid = results[result_idx].transaction_id
            if new_tid != old_tid:
                if new_tid in unmatched_txn_set:
                    unmatched_txn_set.discard(new_tid)
                if old_tid not in {r.transaction_id for r in results if r is not results[result_idx]}:
                    unmatched_txn_set.add(old_tid)
            results[result_idx] = ReconciliationMatch(
                transaction_id=new_tid,
                document_id=new_did,
                confidence_score=conf,
                amount_score=a_score,
                date_score=d_score,
                vendor_score=v_score,
                match_type=MatchType.ONE_TO_ONE,
                status=status,
            )

    return results


def _format_txn_for_llm(txn: Transaction) -> str:
    """Format a transaction as a concise string for LLM agent prompts."""
    return (
        f"ID:{txn.id} | {txn.date_posted} | {account_str(txn.account)} | "
        f"{'−' if txn.amount < 0 else ''}{txn.currency} {abs(txn.amount):.2f} | "
        f"{txn.description[:80]}"
    )


def _format_doc_for_llm(doc: Document) -> str:
    """Format a document (proof of transaction) as a concise string for LLM agent prompts.

    The currency is the resolved one, labelled with where it came from, so the
    model is not told "USD" about a receipt that only ever printed "$".
    """
    best_date = _get_best_date(doc)
    code, source = resolve_document_currency(doc)
    if code is None:
        currency = "currency not stated"
    elif source == "inferred":
        currency = f"{code} (inferred from its tax line)"
    else:
        currency = code
    return (
        f"Vendor: {doc.vendor or 'Unknown'} | "
        f"Date: {best_date or doc.document_date or 'Unknown'} | "
        f"Total: {currency} {abs(doc.total) if doc.total else '?'} | "
        f"File: {doc.original_filename or 'unknown'}"
    )


def _call_llm_for_reconciliation(prompt: str, label: str = "reconciliation_agent") -> dict:
    """Call the shared engine LLM ladder for one reconciliation agent.

    ``label`` names the agent so the log line and the run's fallback tally say
    which one fell back, rather than attributing every failure to the shared
    caller.
    """
    from core.match_validator import _call_llm
    return _call_llm(prompt, label=label, max_tokens=RECON_AGENT_MAX_TOKENS)


def _ask_agent(
    memory: "AdjudicationMemory",
    agent: str,
    doc: Document,
    txns: list[Transaction],
    prompt: str,
    label: str,
    extra: dict | None = None,
) -> tuple[dict, bool, str]:
    """One agent's reply, from memory when the question is unchanged.

    Returns ``(verdict, from_memory, digest)``. On a miss the model is called
    and the reply is returned unstored — the caller stores it once it has
    validated the shape, so a malformed reply is never remembered as an answer.
    """
    digest = inputs_hash(agent, doc, txns, _local_llm_cfg.model, extra=extra)
    remembered = memory.get(agent, doc.id, digest)
    if remembered is not None:
        _audit.log(
            "agent_verdict_remembered",
            doc_id=doc.id,
            agent=agent,
            inputs_hash=digest,
            verdict=remembered,
        )
        return remembered, True, digest
    return _call_llm_for_reconciliation(prompt, label=label), False, digest


# ─── Pass 1: Monetary Amount Agent ────────────────────────────────────────

_AMOUNT_AGENT_PROMPT = """You are a financial reconciliation agent. Your ONLY job is to shortlist bank transaction candidates that could match this proof of transaction based on MONETARY AMOUNT.

PROOF OF TRANSACTION:
{doc_info}

CANDIDATE BANK TRANSACTIONS (all unmatched):
{txn_list}

INSTRUCTIONS:
- Compare the proof of transaction's total amount against each transaction's amount
- An exact or near-exact amount match is the strongest signal
- Consider FX conversion: if proof is in USD and transaction in CAD, the CAD amount is the USD amount times the CAD/USD rate. The allowances this engine applies are stated at the end of these instructions. Similarly for other currency pairs
- Consider that a bank may deduct a handling fee from an incoming wire, reducing the deposited amount
- Consider that credit card transactions may show "USD XX.XX@rate" format where the USD amount matches the proof
- Consider the nature of the vendor/transaction — subscription services charge exact recurring amounts, wire transfers may have FX variance
- Be generous in shortlisting — include any transaction where the amount COULD reasonably match (within ~15% for cross-currency, within $1 for same-currency)
- Do NOT consider date or vendor name in this step — ONLY amount
- The shortlist is the answer. "reasoning" is a note: ONE sentence, AT MOST 25 WORDS. Do not list the amounts again, do not explain each candidate, do not write a paragraph.

Return ONLY a JSON object with this exact format — shortlist first, reasoning last:
{{"shortlist": [<list of transaction IDs as integers>], "reasoning": "<one sentence, 25 words maximum>"}}

If no transactions have a plausible amount match, return:
{{"shortlist": [], "reasoning": "<one sentence, 25 words maximum>"}}"""

# The numeric allowances the amount prompt quotes, appended to it at the call
# site. They are the engine's own constants rather than a second set of numbers
# written into the prompt text, so the model is never told a narrower rule than
# the deterministic pass applies. Kept out of _AMOUNT_AGENT_PROMPT because that
# template's placeholders are fixed by its callers.
_AMOUNT_AGENT_ALLOWANCES = """

ALLOWANCES THIS ENGINE APPLIES:
- Cross-currency CAD/USD: the implied rate must fall between {fx_min} and {fx_max}. An implied rate outside that band is not a conversion and is not a match
- Incoming wire: the bank may deduct a handling fee of up to ${wire_fee}, so the credited amount can be smaller than the proof's total by at most that much"""


def _amount_envelope(doc: Document) -> list[tuple[Decimal, Decimal]]:
    """The magnitude bands a transaction must fall in to be worth asking about.

    One band per amount the document offers — its total, and the converted
    amount printed on it when there is one — each widened by
    ``AMOUNT_ENVELOPE_LOW_RATIO``/``AMOUNT_ENVELOPE_HIGH_RATIO`` and
    ``AMOUNT_ENVELOPE_ABS_SLACK``. See the constants for why those numbers
    contain every route the engine can accept an amount on.

    Returns an empty list when the document has no amount at all, which the
    caller reads as "no narrowing possible".
    """
    amounts: list[Decimal] = []
    if doc.total is not None:
        amounts.append(abs(doc.total))
    alt = _extract_alt_amount(doc)
    if alt is not None:
        amounts.append(abs(alt))
    bands: list[tuple[Decimal, Decimal]] = []
    for amount in amounts:
        if amount <= 0:
            continue
        low = amount * AMOUNT_ENVELOPE_LOW_RATIO - AMOUNT_ENVELOPE_ABS_SLACK
        high = amount * AMOUNT_ENVELOPE_HIGH_RATIO + AMOUNT_ENVELOPE_ABS_SLACK
        bands.append((max(Decimal("0"), low), high))
    return bands


def _within_amount_envelope(
    txn: Transaction,
    bands: list[tuple[Decimal, Decimal]],
) -> bool:
    """True when the transaction's magnitude falls in any of the bands."""
    if not bands:
        return True
    magnitude = abs(txn.amount)
    return any(low <= magnitude <= high for low, high in bands)


def _agent_monetary_match(
    doc: Document,
    transactions: list[Transaction],
    compatible_txns: list[Transaction] | None = None,
    det_shortlist: list[int] | None = None,
    memory: "AdjudicationMemory" = _NO_MEMORY,
) -> list[int]:
    """Pass 1: shortlist transactions by monetary amount match.

    The deterministic shortlist is the answer. When
    ``RECONCILIATION_AMOUNT_AGENT_LLM`` is on, the model is asked as well and
    its shortlist is merged in — the union can only ever *add* candidates, so
    proven matches (exact amount, CC USD extraction, valid FX rate) can never
    be dropped by LLM omission. The flag is **off by default**: the union makes
    the model's contribution purely additive, so anything it adds is a candidate
    the later agents have to reject, and the call is the largest single
    contributor to the matching phase's elapsed time.

    ``compatible_txns`` and ``det_shortlist`` may be supplied by a caller that
    already computed them (``_process_single_doc`` does, to decide whether the
    pair needs an agent at all). Recomputing the shortlist would re-emit every
    ``deterministic_amount_match`` audit event, so the caller passes it down
    rather than paying for it twice.
    """
    if not transactions or doc.total is None:
        _audit.log("amount_agent_skip", doc_id=doc.id, reason="no transactions or no total")
        return []

    # Pre-filter: remove sign-incompatible transactions
    if compatible_txns is None:
        compatible_txns = [t for t in transactions if check_sign_compatibility(t, doc)]
    if not compatible_txns:
        _audit.log("amount_agent_skip", doc_id=doc.id, reason="no sign-compatible transactions")
        return []

    # Always compute deterministic shortlist as a safety net
    if det_shortlist is None:
        det_shortlist = _deterministic_amount_shortlist(doc, compatible_txns)

    if not AMOUNT_AGENT_LLM:
        # The deterministic shortlist alone. Nothing below this line can
        # remove an id from it — the model's contribution was a union — so the
        # only thing skipped is the call itself.
        _audit.log(
            "amount_agent_llm_off",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            doc_total=doc.total,
            doc_currency=doc.currency,
            deterministic_shortlist=det_shortlist,
            num_compatible_txns=len(compatible_txns),
            reason="RECONCILIATION_AMOUNT_AGENT_LLM=0",
        )
        return det_shortlist

    # Narrow what the model is *shown* to the amount envelope. The
    # deterministic shortlist above is still computed over every compatible
    # transaction, and the envelope is wider than every route that shortlist
    # uses, so nothing the engine could accept is hidden from the model — the
    # prompt just stops carrying rows whose magnitude rules them out on
    # sight.
    bands = _amount_envelope(doc)
    shown_txns = [t for t in compatible_txns if _within_amount_envelope(t, bands)]
    # Belt and braces: anything the deterministic pass shortlisted is shown,
    # whatever the envelope says, so the two can never disagree.
    shown_ids = {t.id for t in shown_txns}
    for txn in compatible_txns:
        if txn.id in det_shortlist and txn.id not in shown_ids:
            shown_txns.append(txn)
            shown_ids.add(txn.id)

    _audit.log(
        "amount_agent_deterministic",
        doc_id=doc.id,
        doc_vendor=doc.vendor,
        doc_total=doc.total,
        doc_currency=doc.currency,
        deterministic_shortlist=det_shortlist,
        num_compatible_txns=len(compatible_txns),
        num_shown_to_agent=len(shown_txns),
    )

    if not shown_txns:
        # Nothing in the account is even the right order of magnitude. The
        # model has nothing to add to an empty deterministic shortlist.
        _audit.log(
            "amount_agent_envelope_empty",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            doc_total=doc.total,
            doc_currency=doc.currency,
            num_compatible_txns=len(compatible_txns),
        )
        return det_shortlist

    doc_info = _format_doc_for_llm(doc)
    txn_lines = "\n".join(_format_txn_for_llm(t) for t in shown_txns)

    prompt = _AMOUNT_AGENT_PROMPT.format(
        doc_info=doc_info, txn_list=txn_lines,
    ) + _AMOUNT_AGENT_ALLOWANCES.format(
        fx_min=f"{_FX_CAD_USD_MIN:.2f}",
        fx_max=f"{_FX_CAD_USD_MAX:.2f}",
        wire_fee=f"{_WIRE_FEE_TOLERANCE:.2f}",
    )

    try:
        result, from_memory, digest = _ask_agent(
            memory, AGENT_AMOUNT, doc, shown_txns, prompt, "amount_agent",
        )
        shortlist = result.get("shortlist", [])
        reasoning = result.get("reasoning", "")
        # Validate IDs exist in the set the model was actually shown — an id it
        # invented, or one from an earlier prompt, is not a candidate.
        llm_shortlist = [int(tid) for tid in shortlist if int(tid) in shown_ids]

        # Merge: union of LLM + deterministic shortlists
        merged = list(dict.fromkeys(llm_shortlist + det_shortlist))

        if not from_memory:
            memory.put(
                AGENT_AMOUNT, doc.id, digest, result, _local_llm_cfg.model,
                reasoning=reasoning,
            )

        _audit.log(
            "amount_agent_result",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            llm_shortlist=llm_shortlist,
            deterministic_shortlist=det_shortlist,
            merged_shortlist=merged,
            llm_reasoning=reasoning,
            added_by_deterministic=[tid for tid in det_shortlist if tid not in llm_shortlist],
            from_memory=from_memory,
        )

        return merged
    except Exception as e:
        logger.warning("Amount agent failed for doc %s: %s — using deterministic fallback", doc.id, e)
        _audit.log(
            "amount_agent_llm_error",
            doc_id=doc.id,
            error=str(e),
            fallback_shortlist=det_shortlist,
        )
        return det_shortlist


def _deterministic_amount_shortlist(
    doc: Document,
    transactions: list[Transaction],
) -> list[int]:
    """Deterministic fallback for amount shortlisting when LLM is unavailable.

    The document's currency is *resolved* first (core/document_currency.py):
    a receipt that never stated one is read in each candidate's own currency
    rather than failing a bare ``==`` on the stored code. Gating every route on
    that equality is how a receipt with a blank currency column reaches pass 1
    with an empty shortlist however plainly its total matches.
    """
    if doc.total is None:
        return []
    doc_abs = abs(doc.total)
    doc_currency, _doc_currency_source = resolve_document_currency(doc)
    doc_currency = doc_currency or ""
    results = []

    # Pre-compute alt amount
    alt_amount = _extract_alt_amount(doc)

    for txn in transactions:
        txn_abs = abs(txn.amount)
        txn_currency = (txn.currency or "").upper()
        # An unresolved document currency is read as this candidate's own.
        same_currency = not doc_currency or txn_currency == doc_currency
        match_reason = None

        # Same currency: within $2 or 15%
        if same_currency:
            if abs(txn_abs - doc_abs) <= Decimal("2.00"):
                match_reason = f"same_currency_close (diff=${abs(txn_abs - doc_abs):.2f})"
                results.append(txn.id)
            elif doc_abs > 0 and abs(txn_abs - doc_abs) / doc_abs <= Decimal("0.15"):
                match_reason = f"same_currency_15pct (diff={abs(txn_abs - doc_abs) / doc_abs:.1%})"
                results.append(txn.id)

        # Alt amount match
        if not match_reason and alt_amount and abs(txn_abs - alt_amount) <= Decimal("2.00"):
            match_reason = f"alt_amount_match (alt={alt_amount}, diff=${abs(txn_abs - alt_amount):.2f})"
            results.append(txn.id)

        # CC FX: extract USD from description.
        # Accepts both shapes the account field takes: an AccountType enum
        # instance, and a plain string, plus the separate account_type column.
        _is_credit_card = (
            (hasattr(txn, 'account') and txn.account and (
                (hasattr(txn.account, 'value') and txn.account.value == "CreditCard") or
                (isinstance(txn.account, str) and txn.account == "CreditCard")
            )) or
            (getattr(txn, 'account_type', None) == "CREDIT_CARD")
        )
        if not match_reason and _is_credit_card:
            usd = _extract_usd_amount(txn.description)
            if usd and abs(usd - doc_abs) <= Decimal("1.00"):
                match_reason = f"cc_usd_extraction (usd_in_desc={usd}, diff=${abs(usd - doc_abs):.2f})"
                results.append(txn.id)

        # Cross-currency FX check
        if not match_reason and not same_currency and doc_abs > 0 and txn_abs > 0:
            if txn_currency == "CAD" and doc_currency == "USD":
                rate = txn_abs / doc_abs
                if _FX_CAD_USD_MIN <= rate <= _FX_CAD_USD_MAX:
                    match_reason = f"fx_cad_usd (rate={rate:.4f}, range={_FX_CAD_USD_MIN}-{_FX_CAD_USD_MAX})"
                    results.append(txn.id)
            elif txn_currency == "USD" and doc_currency == "CAD":
                rate = doc_abs / txn_abs
                if _FX_CAD_USD_MIN <= rate <= _FX_CAD_USD_MAX:
                    match_reason = f"fx_usd_cad (rate={rate:.4f})"
                    results.append(txn.id)

        # Same currency intermediary FX fallback
        if not match_reason and same_currency and txn_currency == "CAD" and doc_abs > 0:
            is_intermediary = any(
                re.search(p, txn.description or "", re.IGNORECASE)
                for p in _PAYMENT_INTERMEDIARY_PATTERNS
            )
            if is_intermediary:
                rate = txn_abs / doc_abs
                if _FX_CAD_USD_MIN <= rate <= _FX_CAD_USD_MAX:
                    match_reason = f"intermediary_fx (rate={rate:.4f})"
                    results.append(txn.id)

        if match_reason:
            _audit.log(
                "deterministic_amount_match",
                doc_id=doc.id,
                txn_id=txn.id,
                doc_total=doc.total,
                doc_currency=doc_currency,
                txn_amount=txn.amount,
                txn_currency=txn_currency,
                txn_description=txn.description[:60],
                match_reason=match_reason,
            )

    return results


# ─── Pass 2: Date Validation Agent ────────────────────────────────────────

_DATE_AGENT_PROMPT = """You are a financial reconciliation agent. Your ONLY job is to narrow down a shortlist of bank transaction candidates based on DATE plausibility.

PROOF OF TRANSACTION:
{doc_info}

SHORTLISTED TRANSACTIONS (from amount matching):
{txn_list}

HARD RULE — MAXIMUM DATE VARIANCE: {max_date_days} days
Any transaction whose date is more than {max_date_days} days from the proof of transaction's date MUST be removed from the shortlist. This is a user-configured hard limit — do NOT override it regardless of amount match quality.

INSTRUCTIONS:
- The proof of transaction's date is when the purchase/service occurred or the invoice was issued
- The bank transaction date is when the charge appeared on the bank statement
- Normal delays: credit card charges 1-5 days, wire transfers 1-7 business days, subscription charges same day or ±3 days, contractor invoices net-15 to net-30 payment terms
- FIRST: remove ALL transactions that exceed the {max_date_days}-day hard limit
- THEN: among remaining candidates, prefer those with closer dates
- If the proof has no date, keep all candidates (there is nothing to filter by)
- Be strict — remove transactions that are unreasonably far from the proof's date
- The shortlist is the answer. "reasoning" is a note: ONE sentence, AT MOST 25 WORDS. Do not walk through each candidate's gap, do not write a paragraph.

Return ONLY a JSON object with this exact format — shortlist first, reasoning last:
{{"shortlist": [<list of transaction IDs as integers>], "reasoning": "<one sentence, 25 words maximum>"}}

If ALL candidates exceed the {max_date_days}-day limit, return an empty shortlist:
{{"shortlist": [], "reasoning": "All candidates exceed the {max_date_days}-day maximum date variance"}}"""


def _agent_date_match(
    doc: Document,
    shortlisted_txn_ids: list[int],
    transactions: list[Transaction],
    max_date_days: int = 45,
    memory: "AdjudicationMemory" = _NO_MEMORY,
) -> list[int]:
    """Pass 2: LLM agent narrows shortlist by date plausibility.

    Args:
        max_date_days: Hard maximum number of days between doc date and txn date.
            Candidates exceeding this are removed before and after LLM evaluation.
    """
    if not shortlisted_txn_ids:
        return []

    txn_map = {t.id: t for t in transactions}
    shortlisted_txns = [txn_map[tid] for tid in shortlisted_txn_ids if tid in txn_map]
    if not shortlisted_txns:
        return []

    # ── Hard cutoff: remove candidates exceeding max_date_days ──
    best_date = _get_best_date(doc)
    if best_date is not None:
        pre_filter = []
        removed_by_hard_limit = []
        for txn in shortlisted_txns:
            gap = abs((txn.date_posted - best_date).days)
            if gap <= max_date_days:
                pre_filter.append(txn)
            else:
                removed_by_hard_limit.append((txn.id, gap))

        if removed_by_hard_limit:
            _audit.log(
                "date_agent_hard_cutoff",
                doc_id=doc.id,
                doc_vendor=doc.vendor,
                doc_best_date=str(best_date),
                max_date_days=max_date_days,
                removed=[{"txn_id": tid, "gap_days": gap} for tid, gap in removed_by_hard_limit],
            )

        if not pre_filter:
            # All candidates exceed the hard limit — return empty
            _audit.log(
                "date_agent_all_exceeded",
                doc_id=doc.id,
                doc_vendor=doc.vendor,
                max_date_days=max_date_days,
                all_gaps=[{"txn_id": tid, "gap_days": gap} for tid, gap in removed_by_hard_limit],
            )
            return []

        shortlisted_txns = pre_filter
        shortlisted_txn_ids = [t.id for t in shortlisted_txns]

    # If only 1 candidate, skip LLM call — nothing to filter
    if len(shortlisted_txns) == 1:
        _audit.log(
            "date_agent_single_candidate",
            doc_id=doc.id,
            txn_id=shortlisted_txns[0].id,
        )
        return [shortlisted_txns[0].id]

    doc_info = _format_doc_for_llm(doc)
    txn_lines = "\n".join(_format_txn_for_llm(t) for t in shortlisted_txns)

    prompt = _DATE_AGENT_PROMPT.format(
        doc_info=doc_info, txn_list=txn_lines, max_date_days=max_date_days,
    )

    try:
        result, from_memory, digest = _ask_agent(
            memory, AGENT_DATE, doc, shortlisted_txns, prompt, "date_agent",
            extra={"max_date_days": max_date_days},
        )
        shortlist = result.get("shortlist", [])
        reasoning = result.get("reasoning", "")
        valid_ids = {t.id for t in shortlisted_txns}
        filtered = [int(tid) for tid in shortlist if int(tid) in valid_ids]
        removed = [tid for tid in shortlisted_txn_ids if tid not in filtered]

        if not from_memory:
            memory.put(
                AGENT_DATE, doc.id, digest, result, _local_llm_cfg.model,
                reasoning=reasoning,
            )

        _audit.log(
            "date_agent_result",
            from_memory=from_memory,
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            doc_best_date=str(best_date),
            max_date_days=max_date_days,
            input_shortlist=shortlisted_txn_ids,
            output_shortlist=filtered if filtered else shortlisted_txn_ids,
            removed_by_date=removed if filtered else [],
            llm_reasoning=reasoning,
        )

        # Never return empty — at minimum keep the (already hard-filtered) list
        return filtered if filtered else shortlisted_txn_ids
    except Exception as e:
        logger.warning("Date agent failed for doc %s: %s — using deterministic fallback", doc.id, e)
        det_result = _deterministic_date_filter(doc, shortlisted_txns, max_date_days)
        _audit.log(
            "date_agent_llm_error",
            doc_id=doc.id,
            error=str(e),
            fallback_result=det_result,
        )
        return det_result


def _deterministic_date_filter(
    doc: Document,
    shortlisted_txns: list[Transaction],
    max_date_days: int = 45,
) -> list[int]:
    """Deterministic fallback for date filtering when LLM is unavailable."""
    best_date = _get_best_date(doc)
    if best_date is None:
        return [t.id for t in shortlisted_txns]  # Can't filter without date

    # Keep transactions within max_date_days, sorted by proximity
    scored = []
    for txn in shortlisted_txns:
        gap = abs((txn.date_posted - best_date).days)
        if gap <= max_date_days:
            scored.append((gap, txn.id))

    if not scored:
        # All exceed the hard limit — return empty (respect user config)
        return []

    scored.sort()
    return [tid for _, tid in scored]


# ─── Pass 3: Vendor & Final Match Agent ───────────────────────────────────

_FINAL_AGENT_PROMPT = """You are a financial reconciliation agent. Your job is to pick the SINGLE BEST matching bank transaction for this proof of transaction.

PROOF OF TRANSACTION:
{doc_info}

FINAL CANDIDATE TRANSACTIONS:
{txn_list}

INSTRUCTIONS:
- You must pick exactly ONE transaction from the list above that best matches this proof
- Primary signal: vendor name similarity (does the bank description mention the same vendor/company as the proof?)
- Secondary signals: amount closeness, date proximity, transaction nature
- For payment intermediaries (Wise, PayPal, INTERAC, wire transfers): the bank description will show the platform name, NOT the vendor. In this case, rely more on amount and date
- Consider the full picture: an $81.45 charge on the credit card should match a receipt for $81.45 from the same vendor, even if dates differ by a few days
- If you cannot confidently pick one, still pick the BEST one and say so in the confidence label
- The id and the confidence label are the answer. "reasoning" is a note: ONE sentence, AT MOST 25 WORDS. Do not compare every candidate in it, do not write a paragraph.

Return ONLY a JSON object with this exact format — decision first, reasoning last:
{{"matched_transaction_id": <single integer ID>, "confidence": "<high|medium|low>", "reasoning": "<one sentence, 25 words maximum>"}}

If you truly cannot match any (extremely unlikely given earlier filtering), return:
{{"matched_transaction_id": null, "confidence": "none", "reasoning": "<one sentence, 25 words maximum>"}}"""


def _agent_final_match(
    doc: Document,
    shortlisted_txn_ids: list[int],
    transactions: list[Transaction],
    memory: "AdjudicationMemory" = _NO_MEMORY,
) -> tuple[int | None, str, float]:
    """Pass 3: LLM agent picks the single best match and confidence level.

    Returns (matched_txn_id, confidence_label, confidence_score).
    """
    if not shortlisted_txn_ids:
        return None, "none", 0.0

    txn_map = {t.id: t for t in transactions}
    shortlisted_txns = [txn_map[tid] for tid in shortlisted_txn_ids if tid in txn_map]
    if not shortlisted_txns:
        return None, "none", 0.0

    # If only 1 candidate, skip LLM — it's the match
    if len(shortlisted_txns) == 1:
        txn = shortlisted_txns[0]
        # Compute deterministic confidence for the single candidate
        a_score = _compute_deterministic_amount_score(doc, txn)
        d_score = compute_date_score(txn.date_posted, _get_best_date(doc))
        v_score = compute_vendor_score(txn.description, doc.vendor or "", doc.original_filename or "")
        conf = compute_confidence(a_score, d_score, v_score)
        label = "high" if conf >= 0.75 else "medium" if conf >= 0.50 else "low"
        _audit.log(
            "final_agent_single_candidate",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            matched_txn_id=txn.id,
            amount_score=a_score,
            date_score=d_score,
            vendor_score=v_score,
            confidence=conf,
            label=label,
        )
        return txn.id, label, conf

    doc_info = _format_doc_for_llm(doc)
    txn_lines = "\n".join(_format_txn_for_llm(t) for t in shortlisted_txns)

    prompt = _FINAL_AGENT_PROMPT.format(doc_info=doc_info, txn_list=txn_lines)

    try:
        result, from_memory, digest = _ask_agent(
            memory, AGENT_FINAL, doc, shortlisted_txns, prompt, "final_agent",
        )
        matched_id = result.get("matched_transaction_id")
        confidence_label = result.get("confidence", "medium")
        reasoning = result.get("reasoning", "")

        if matched_id is not None:
            matched_id = int(matched_id)
            valid_ids = {t.id for t in shortlisted_txns}
            if matched_id not in valid_ids:
                _audit.log(
                    "final_agent_invalid_id",
                    doc_id=doc.id,
                    returned_id=matched_id,
                    valid_ids=list(valid_ids),
                )
                matched_id = None

        if matched_id is None:
            if not from_memory:
                memory.put(
                    AGENT_FINAL, doc.id, digest, result, _local_llm_cfg.model,
                    reasoning=reasoning,
                )
            _audit.log(
                "final_agent_no_match",
                doc_id=doc.id,
                doc_vendor=doc.vendor,
                candidates=shortlisted_txn_ids,
                llm_reasoning=reasoning,
                from_memory=from_memory,
            )
            return None, "none", 0.0

        # Convert label to score
        conf_map = {"high": 0.90, "medium": 0.70, "low": 0.50}
        conf_score = conf_map.get(confidence_label, 0.70)

        if not from_memory:
            memory.put(
                AGENT_FINAL, doc.id, digest, result, _local_llm_cfg.model,
                transaction_id=matched_id,
                confidence=conf_score,
                reasoning=reasoning,
            )

        _audit.log(
            "final_agent_match",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            matched_txn_id=matched_id,
            confidence_label=confidence_label,
            confidence_score=conf_score,
            candidates=shortlisted_txn_ids,
            llm_reasoning=reasoning,
            from_memory=from_memory,
        )

        return matched_id, confidence_label, conf_score

    except Exception as e:
        logger.warning("Final agent failed for doc %s: %s — using deterministic fallback", doc.id, e)
        det_result = _deterministic_final_match(doc, shortlisted_txns)
        _audit.log(
            "final_agent_llm_error",
            doc_id=doc.id,
            error=str(e),
            deterministic_result={"txn_id": det_result[0], "label": det_result[1], "score": det_result[2]},
        )
        return det_result


def _compute_deterministic_amount_score(doc: Document, txn: Transaction) -> float:
    """Compute a deterministic amount score for a single txn-doc pair.

    This is the ``amount_score`` a stored match carries, and the number the
    Review panel shows as "Amount (50%)". Currency is read before magnitude:
    comparing magnitudes first would score CAD 3,000 against USD 3,000 as
    Amount 100. Same currency keeps the graded window below (including the
    wire-fee allowance banks deduct from incoming wires); different currencies
    have to clear the FX band instead.

    "Different currencies" means the document *stated* or *implied* a different
    one (core/document_currency.py). A receipt that named no currency at all is
    read in the transaction's: scoring it zero for failing an FX band it was
    never in would fail a same-currency charge against its own receipt.
    """
    if doc.total is None:
        return 0.0
    txn_cur = (txn.currency or "").upper()
    doc_cur, doc_cur_source = resolve_document_currency(doc)
    same_currency = doc_cur is None or txn_cur == doc_cur
    if same_currency:
        a_score = compute_amount_score_with_tolerance(txn.amount, doc.total, Decimal("2.00"))
    else:
        a_score = compare_amounts(
            txn.amount, txn_cur, doc.total, doc_cur,
            doc_currency_source=doc_cur_source,
        ).score
    if a_score > 0:
        return a_score
    # Try alt amount
    alt = _extract_alt_amount(doc)
    if alt:
        a_score = compute_amount_score_with_tolerance(txn.amount, alt, Decimal("2.00"))
        if a_score > 0:
            return min(a_score, 0.95)
    # Try CC FX.
    # Accepts both shapes the account field takes: an AccountType enum
    # instance, and a plain string, plus the separate account_type column.
    _is_credit_card = (
        (hasattr(txn, 'account') and txn.account and (
            (hasattr(txn.account, 'value') and txn.account.value == "CreditCard") or
            (isinstance(txn.account, str) and txn.account == "CreditCard")
        )) or
        (getattr(txn, 'account_type', None) == "CREDIT_CARD")
    )
    if _is_credit_card:
        usd = _extract_usd_amount(txn.description)
        if usd and doc.total:
            a_score = compute_amount_score_with_tolerance(usd, doc.total, Decimal("1.00"))
            if a_score > 0:
                return a_score
    if not same_currency:
        # Cross-currency with no route that works: the implied rate is outside
        # the band, there is no converted amount on the receipt and no USD
        # amount in the bank description. That is not weak evidence, it is no
        # evidence — score it 0 so the pair can never reach the confidence
        # floor on date and vendor alone.
        return 0.0
    return 0.3  # Base score for a pair that arrived through agent shortlisting


def _deterministic_final_match(
    doc: Document,
    shortlisted_txns: list[Transaction],
) -> tuple[int | None, str, float]:
    """Deterministic fallback: pick best by weighted score."""
    best_id = None
    best_conf = 0.0

    for txn in shortlisted_txns:
        a_score = _compute_deterministic_amount_score(doc, txn)
        d_score = compute_date_score(txn.date_posted, _get_best_date(doc))
        v_score = compute_vendor_score(txn.description, doc.vendor or "", doc.original_filename or "")
        conf = compute_confidence(a_score, d_score, v_score)
        if conf > best_conf:
            best_conf = conf
            best_id = txn.id

    label = "high" if best_conf >= 0.75 else "medium" if best_conf >= 0.50 else "low"
    return best_id, label, best_conf


def _unambiguous_candidate(
    doc: Document,
    det_shortlist: list[int],
    txn_map: dict[int, Transaction],
    max_date_days: int,
) -> Transaction | None:
    """The one transaction this document obviously belongs to, or None.

    "Obviously" is the conjunction of five things, every one of them required
    (see the UNAMBIGUOUS_* constants for the numbers and the reasoning):

    1. the deterministic amount pass found **exactly one** candidate, and it
       survives the same ``max_date_days`` hard cutoff pass 2 would apply;
    2. the two amounts are in the **same currency**, and the document either
       stated that currency or a Canadian tax line implied it — cross-currency
       always goes to the agents whatever the implied rate looks like, and so
       does a receipt that never named a currency at all. An assumed currency
       is good enough to *score* a pair (see
       :func:`core.amount_match.compare_amounts`) and deliberately not good
       enough to settle one without asking;
    3. the amounts are the **same figure** to within two cents;
    4. the dates are within ``UNAMBIGUOUS_MAX_DATE_GAP_DAYS``; and
    5. the bank description **names the receipt's vendor**
       (``compute_vendor_score`` at or above its strong-signal value) and
       ``_merchant_contradiction`` finds nobody else named on the line.

    Anything else — two candidates, a near-miss amount, a wire fee, a converted
    amount, an opaque description, a description naming another merchant — is
    not this function's business and goes to the three agents as before.
    """
    if doc.total is None or len(det_shortlist) != 1:
        return None
    txn = txn_map.get(det_shortlist[0])
    if txn is None:
        return None

    # (2) Same currency only. A blank currency on either side is not a match of
    # currencies; it is a gap, and a gap is exactly what the agents are for.
    txn_currency = (txn.currency or "").upper()
    doc_currency, doc_currency_source = resolve_document_currency(doc)
    if doc_currency_source == _CURRENCY_ASSUMED:
        return None
    if not txn_currency or not doc_currency or txn_currency != doc_currency:
        return None

    # (3) The same figure.
    if abs(abs(txn.amount) - abs(doc.total)) > UNAMBIGUOUS_AMOUNT_TOLERANCE:
        return None

    # (1b) and (4) The date has to exist and be close.
    best_date = _get_best_date(doc)
    if best_date is None:
        return None
    gap = abs((txn.date_posted - best_date).days)
    if gap > max_date_days or gap > UNAMBIGUOUS_MAX_DATE_GAP_DAYS:
        return None

    # (5) The bank line names this vendor, and names nobody else.
    vendor_score = compute_vendor_score(
        txn.description or "", doc.vendor or "", doc.original_filename or "",
    )
    if vendor_score < UNAMBIGUOUS_MIN_VENDOR_SCORE:
        return None
    if _merchant_contradiction(
        txn.description or "", doc.vendor or "", doc.original_filename or "",
    ):
        return None

    return txn


# ─── Main 3-Agent Pipeline ───────────────────────────────────────────────

# The date gap of a document that has no usable date at all. Large enough to
# sort behind every real gap, fixed so the ordering stays total.
_NO_DATE_GAP_DAYS = 1_000_000


def _date_gap_days(doc: Document, txn: Transaction) -> int:
    """Whole days between the document's best date and the transaction's."""
    best_date = _get_best_date(doc)
    if best_date is None:
        return _NO_DATE_GAP_DAYS
    return abs((txn.date_posted - best_date).days)


@dataclass(frozen=True)
class _Proposal:
    """One document's scored claim on one transaction, before anyone wins it."""

    doc_id: int
    txn_id: int
    confidence: float
    flagged: bool
    amount_score: float
    date_gap: int
    match: ReconciliationMatch

    @property
    def order_key(self) -> tuple[int, float, float, int, int]:
        """Best first under an ascending sort, and a **total** order.

        Settled before flagged, then confidence, then amount score, then the
        smaller date gap, then the lower document id. Nothing in it depends on
        when the proposal arrived, and the last component is unique, so two
        proposals can never tie.
        """
        return (
            0 if not self.flagged else 1,
            -self.confidence,
            -self.amount_score,
            self.date_gap,
            self.doc_id,
        )


class _TxnClaims:
    """Which document ends up holding which transaction.

    A transaction can only be matched once, so documents compete for it.
    Deciding that competition by *arrival* — first claim holds the transaction,
    a challenger must beat it by DISPLACEMENT_MARGIN — makes the outcome depend
    on which worker finished first: two close sub-floor (flagged) candidates for
    the same row swap places from run to run, and the Review queue shows a
    different suggestion each time.

    Nothing is decided on arrival. Workers only **propose**; the run settles
    every transaction once, after all the scoring is done, by
    :attr:`_Proposal.order_key`:

        settled before flagged, confidence desc, amount score desc,
        date gap asc, document id asc

    Settled-before-flagged keeps the property that mattered: a pair below the
    confidence floor, or one the merchant-contradiction rule caught, never takes
    a transaction away from a clean one, whatever its confidence. Below that the
    key is pure score, and its last component is unique, so the holder is a
    function of the proposals alone — not of the order they were made in.

    DISPLACEMENT_MARGIN is not part of this, because inside one run there is no
    incumbent to displace: every competitor is ranked at the same moment. It
    still guards *stored* matches from earlier runs, in
    ``server/api/reconciliation._store_matches``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[int, list[_Proposal]] = {}
        self._winners: dict[int, _Proposal] = {}

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def propose(self, proposal: _Proposal) -> None:
        """Record one document's claim. Always accepted; nobody wins yet."""
        with self._lock:
            self._proposals.setdefault(proposal.txn_id, []).append(proposal)

    def resolve(self) -> tuple[list[_Proposal], list[_Proposal]]:
        """Settle every proposed transaction. Returns (winners, losers).

        Drains the proposals, so a later round starts empty and an earlier
        round's winner is already held.
        """
        with self._lock:
            proposals = self._proposals
            self._proposals = {}
        winners: list[_Proposal] = []
        losers: list[_Proposal] = []
        for txn_id in sorted(proposals):
            ranked = sorted(proposals[txn_id], key=lambda p: p.order_key)
            winners.append(ranked[0])
            losers.extend(ranked[1:])
        with self._lock:
            for winner in winners:
                self._winners[winner.txn_id] = winner
        return winners, losers

    def held_txn_ids(self) -> set[int]:
        """Transactions already settled by a resolved round."""
        with self._lock:
            return set(self._winners)

    def held_doc_ids(self) -> set[int]:
        """Documents that already hold a transaction."""
        with self._lock:
            return {p.doc_id for p in self._winners.values()}

    def matches(self) -> list[ReconciliationMatch]:
        """The run's matches, best first — a deterministic order.

        ``_store_matches`` sorts these by confidence with a stable sort, so the
        order they arrive in decides ties there too. Handing them over already
        ranked by the same total order keeps that decision deterministic as
        well.
        """
        with self._lock:
            held = list(self._winners.values())
        return [p.match for p in sorted(held, key=lambda p: p.order_key)]


def _finalise_match(
    *,
    doc: Document,
    matched_id: int,
    matched_txn: Transaction,
    conf_score: float,
    confidence_label: str,
    adjudication: str,
    amount_shortlist: list[int],
    date_shortlist: list[int],
    claims: "_TxnClaims",
    match_count: list[int] | None,
    auto_approve_threshold: float,
    report: "Callable[..., None]",
    vendor_label: str,
) -> ReconciliationMatch | None:
    """Score, classify, propose and record one document↔transaction pair.

    The tail of the pipeline, shared by both routes into it: the three agents,
    and the deterministic settle for an unambiguous pair. Everything that
    decides what is *stored* lives here — the deterministic sub-scores, the
    settled-or-flagged classification, the proposal and the audit record — so
    the two routes cannot drift apart on any of it. ``adjudication`` is the only thing that
    differs between them, and it is carried onto the match and into the audit
    log rather than changing any decision.

    The proposal is not a claim: who holds a contested transaction is decided
    once, after every worker has finished, by :meth:`_TxnClaims.resolve`.
    """
    a_score = _compute_deterministic_amount_score(doc, matched_txn)
    d_score = compute_date_score(matched_txn.date_posted, _get_best_date(doc))
    v_score = compute_vendor_score(
        matched_txn.description, doc.vendor or "", doc.original_filename or ""
    )

    status = (
        MatchStatus.AUTO_APPROVED
        if conf_score >= auto_approve_threshold
        else MatchStatus.PENDING_REVIEW
    )

    # Is this pair a reconciliation, or only a suggestion?
    classification = classify_match(
        conf_score, v_score, matched_txn.description or "",
        doc.vendor, doc.original_filename or "",
        amount_score=a_score,
    )
    if classification.flagged:
        # Never store a flagged pair as settled — review decides.
        status = MatchStatus.PENDING_REVIEW

    match = ReconciliationMatch(
        transaction_id=matched_id,
        document_id=doc.id,
        confidence_score=conf_score,
        amount_score=a_score,
        date_score=d_score,
        vendor_score=v_score,
        match_type=MatchType.ONE_TO_ONE,
        status=status,
        adjudication=adjudication,
    )

    # ── Propose the transaction (the run decides the holder later) ──
    claims.propose(_Proposal(
        doc_id=doc.id,
        txn_id=matched_id,
        confidence=conf_score,
        flagged=classification.flagged,
        amount_score=a_score,
        date_gap=_date_gap_days(doc, matched_txn),
        match=match,
    ))

    if classification.flagged:
        _audit.log(
            "match_flagged_for_review",
            doc_id=doc.id,
            txn_id=matched_id,
            confidence=conf_score,
            vendor_score=v_score,
            reasons=list(classification.reasons),
        )
        report(
            f"{vendor_label} ${doc.total} ↔ transaction {matched_id} — FLAGGED for review: "
            f"{classification.reason_text()}",
        )

    if match_count:
        # Live progress only: a proposal that loses its transaction at
        # resolution is subtracted again there.
        with claims.lock:
            match_count[0] += 1

    txn_desc = matched_txn.description[:30] if matched_txn.description else "?"

    _audit.log(
        "doc_pipeline_matched",
        doc_id=doc.id,
        doc_vendor=doc.vendor,
        doc_total=doc.total,
        doc_currency=doc.currency,
        matched_txn_id=matched_id,
        txn_amount=matched_txn.amount,
        txn_currency=matched_txn.currency,
        txn_date=str(matched_txn.date_posted),
        txn_description=matched_txn.description[:80],
        amount_score=a_score,
        date_score=d_score,
        vendor_score=v_score,
        confidence=conf_score,
        confidence_label=confidence_label,
        status=status.value,
        amount_shortlist=amount_shortlist,
        date_shortlist=date_shortlist,
        flagged_for_review=classification.flagged,
        flag_reasons=list(classification.reasons),
        adjudication=adjudication,
    )

    outcome = "FLAGGED" if classification.flagged else "MATCHED"
    picked_from = (
        "settled deterministically — no agent was asked"
        if adjudication == "deterministic"
        else f"Final: picked from {len(date_shortlist)} candidates"
    )
    report(
        f"{vendor_label} ${doc.total} ↔ transaction {matched_id} ({txn_desc}) — "
        f"{outcome} {confidence_label} ({conf_score:.0%}) [{picked_from}]",
        is_final=True,
    )

    return match


def _process_single_doc(
    doc: Document,
    doc_i: int,
    total_docs: int,
    transactions: list[Transaction],
    txn_map: dict[int, Transaction],
    claims: _TxnClaims,
    auto_approve_threshold: float,
    progress_callback: "Callable[[dict], None] | None" = None,
    match_count: list[int] | None = None,
    max_date_days: int = 45,
    rejected_pairs: frozenset[tuple[int, int]] = frozenset(),
    memory: "AdjudicationMemory" = _NO_MEMORY,
    held_txn_ids: frozenset[int] = frozenset(),
) -> ReconciliationMatch | None:
    """Run the full 3-agent pipeline for a single document.

    This function is designed to run concurrently in a thread pool. It never
    decides who *holds* a transaction: it proposes one pair into ``claims``
    (a :class:`_TxnClaims`) and the run settles every contested transaction
    afterwards, by score. ``held_txn_ids`` is what an earlier settled round
    already gave away — fixed for the whole round, so every worker in a round
    sees exactly the same pool however the threads interleave.

    ``rejected_pairs`` holds the (transaction_id, document_id) pairs a human has
    already rejected. They are removed from this document's candidate pool
    *before* pass 1 — not filtered out after scoring — so a rejected pair can
    neither be proposed again nor claim the transaction and keep a better
    candidate away from it.
    """
    vendor_label = doc.vendor or "unknown"

    def _report(msg: str, is_final: bool = False) -> None:
        """Report progress. Worker signals is_final on its last message but does no counting."""
        if progress_callback is not None:
            try:
                progress_callback({
                    "total_docs": total_docs,
                    "matches_so_far": match_count[0] if match_count else 0,
                    "log": msg,
                    "is_final": is_final,
                })
            except Exception:
                logger.debug("Progress callback failed for doc %d: %s", doc_i, msg)

    # The pool for this round: every transaction an earlier round did not
    # already settle, minus the pairs this user has rejected. It does not
    # change while the round runs, so it cannot depend on which worker got
    # there first.
    available_txns = [
        t for t in transactions
        if t.id not in held_txn_ids and (t.id, doc.id) not in rejected_pairs
    ]
    _rejected_here = [
        t.id for t in transactions if (t.id, doc.id) in rejected_pairs
    ]
    if _rejected_here:
        _audit.log(
            "candidates_excluded_user_rejected",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            excluded_txn_ids=_rejected_here,
        )
    if not available_txns:
        return None

    _audit.log(
        "doc_pipeline_start",
        doc_id=doc.id,
        doc_vendor=doc.vendor,
        doc_total=doc.total,
        doc_currency=doc.currency,
        doc_date=str(doc.document_date),
        doc_filename=doc.original_filename,
        doc_best_date=str(_get_best_date(doc)),
        available_txn_count=len(available_txns),
    )

    # ── Does this document need an agent at all? ──────────────────────────
    #
    # The deterministic amount pass runs first either way — the amount agent
    # merges its result in as a safety net, so it is never skipped — and when
    # it leaves exactly one obvious candidate the three agent calls cannot
    # change the answer. See `_unambiguous_candidate` for what "obvious" means.
    compatible_txns = [t for t in available_txns if check_sign_compatibility(t, doc)]
    det_shortlist: list[int] = []
    if compatible_txns and doc.total is not None:
        det_shortlist = _deterministic_amount_shortlist(doc, compatible_txns)

    adjudication = "agents"
    settled_txn = _unambiguous_candidate(doc, det_shortlist, txn_map, max_date_days)
    if settled_txn is not None:
        adjudication = "deterministic"
        matched_id = settled_txn.id
        amount_shortlist = [matched_id]
        date_shortlist = [matched_id]
        _a = _compute_deterministic_amount_score(doc, settled_txn)
        _d = compute_date_score(settled_txn.date_posted, _get_best_date(doc))
        _v = compute_vendor_score(
            settled_txn.description, doc.vendor or "", doc.original_filename or "",
        )
        conf_score = compute_confidence(_a, _d, _v)
        confidence_label = (
            "high" if conf_score >= 0.75 else "medium" if conf_score >= 0.50 else "low"
        )
        _audit.log(
            "deterministic_adjudication",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            matched_txn_id=matched_id,
            amount_score=_a,
            date_score=_d,
            vendor_score=_v,
            confidence=conf_score,
            label=confidence_label,
            date_gap_days=abs(
                (settled_txn.date_posted - _get_best_date(doc)).days
            ),
            adjudication="deterministic",
            reason=(
                "single deterministic candidate, same currency, amounts equal "
                f"to within ${UNAMBIGUOUS_AMOUNT_TOLERANCE}, date gap within "
                f"{UNAMBIGUOUS_MAX_DATE_GAP_DAYS} days, bank line names the vendor "
                "and names nobody else — no agent could change this"
            ),
        )
        _report(
            f"{vendor_label} ${doc.total} — settled without the agents "
            f"(one exact candidate, vendor named on the bank line)"
        )
        return _finalise_match(
            doc=doc,
            matched_id=matched_id,
            matched_txn=settled_txn,
            conf_score=conf_score,
            confidence_label=confidence_label,
            adjudication=adjudication,
            amount_shortlist=amount_shortlist,
            date_shortlist=date_shortlist,
            claims=claims,
            match_count=match_count,
            auto_approve_threshold=auto_approve_threshold,
            report=_report,
            vendor_label=vendor_label,
        )

    # Pass 1: Amount Agent — shortlist by monetary match
    amount_shortlist = _agent_monetary_match(
        doc, available_txns, compatible_txns, det_shortlist, memory=memory,
    )
    if not amount_shortlist:
        logger.debug("Doc %s (%s): no amount matches found", doc.id, doc.vendor)
        _audit.log(
            "doc_pipeline_no_amount_match",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            doc_total=doc.total,
            doc_currency=doc.currency,
        )
        _report(f"{vendor_label} ${doc.total} — no amount match, skipped", is_final=True)
        return None

    logger.info(
        "Doc %s (%s): Pass 1 (Amount) → %d candidates: %s",
        doc.id, doc.vendor, len(amount_shortlist), amount_shortlist,
    )
    _report(f"{vendor_label} ${doc.total} — Amount: {len(amount_shortlist)} candidates")

    # Pass 2: Date Agent — narrow by date plausibility
    try:
        date_shortlist = _agent_date_match(
            doc, amount_shortlist, transactions, max_date_days, memory=memory,
        )
    except Exception as e:
        logger.exception("Date agent crashed for doc %s (%s)", doc.id, doc.vendor)
        _audit.log("date_agent_crash", doc_id=doc.id, doc_vendor=doc.vendor, error=str(e))
        _report(f"{vendor_label} ${doc.total} — Date agent error: {str(e)[:80]}", is_final=True)
        return None

    if not date_shortlist:
        _audit.log(
            "doc_pipeline_date_filtered_all",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            max_date_days=max_date_days,
            amount_shortlist=amount_shortlist,
        )
        # Show which candidates were rejected and by how much
        best_date = _get_best_date(doc)
        gap_details = []
        for tid in amount_shortlist:
            txn = txn_map.get(tid)
            if txn and best_date:
                gap = abs((txn.date_posted - best_date).days)
                gap_details.append(f"transaction {tid} {gap}d")
        gap_str = ", ".join(gap_details) if gap_details else "no details"
        _report(
            f"{vendor_label} ${doc.total} — "
            f"all {len(amount_shortlist)} candidates exceed {max_date_days}-day date limit [{gap_str}]",
            is_final=True,
        )
        return None

    removed_count = len(amount_shortlist) - len(date_shortlist)
    date_kept_set = set(date_shortlist)

    # Build per-candidate detail string
    best_date = _get_best_date(doc)
    detail_parts = []
    for tid in amount_shortlist:
        txn = txn_map.get(tid)
        if txn and best_date:
            gap = abs((txn.date_posted - best_date).days)
            status = "kept" if tid in date_kept_set else "removed"
            detail_parts.append(f"transaction {tid} {gap}d {status}")
        elif txn:
            status = "kept" if tid in date_kept_set else "removed"
            detail_parts.append(f"transaction {tid} {status}")

    detail_str = "; ".join(detail_parts)
    _report(
        f"{vendor_label} ${doc.total} — "
        f"Date: {len(date_shortlist)} survived, {removed_count} removed (max {max_date_days}d) [{detail_str}]"
    )

    logger.info(
        "Doc %s (%s): Pass 2 (Date) → %d candidates: %s",
        doc.id, doc.vendor, len(date_shortlist), date_shortlist,
    )

    # Pass 3: Final Agent — pick single best match
    try:
        matched_id, confidence_label, conf_score = _agent_final_match(
            doc, date_shortlist, transactions, memory=memory,
        )
    except Exception as e:
        logger.exception("Final agent crashed for doc %s (%s)", doc.id, doc.vendor)
        _audit.log("final_agent_crash", doc_id=doc.id, doc_vendor=doc.vendor, error=str(e))
        _report(f"{vendor_label} ${doc.total} — Final agent error: {str(e)[:80]}", is_final=True)
        return None

    if matched_id is None:
        logger.info("Doc %s (%s): Pass 3 (Final) → no match", doc.id, doc.vendor)
        _audit.log(
            "doc_pipeline_no_final_match",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            amount_shortlist=amount_shortlist,
            date_shortlist=date_shortlist,
        )
        candidate_ids_str = ", ".join(str(tid) for tid in date_shortlist)
        _report(
            f"{vendor_label} ${doc.total} — "
            f"Final: no match from {len(date_shortlist)} candidates [{candidate_ids_str}]",
            is_final=True,
        )
        return None

    logger.info(
        "Doc %s (%s): Pass 3 (Final) → matched txn %d (confidence: %s, %.2f)",
        doc.id, doc.vendor, matched_id, confidence_label, conf_score,
    )

    matched_txn = txn_map.get(matched_id)
    if matched_txn is None:
        return None

    return _finalise_match(
        doc=doc,
        matched_id=matched_id,
        matched_txn=matched_txn,
        conf_score=conf_score,
        confidence_label=confidence_label,
        adjudication=adjudication,
        amount_shortlist=amount_shortlist,
        date_shortlist=date_shortlist,
        claims=claims,
        match_count=match_count,
        auto_approve_threshold=auto_approve_threshold,
        report=_report,
        vendor_label=vendor_label,
    )


# Maximum number of documents processed concurrently.
#
# One document's three passes are a pipeline — the date agent needs the amount
# agent's shortlist — so a worker has at most one LLM call in flight and this
# number is also the number of concurrent requests the matcher puts on the LLM
# endpoint. It is the matcher's own concurrency budget,
# `LocalLLMConfig.engine_max_concurrent` — deliberately NOT
# `local_llm_config.max_concurrent`, which governs the document-extraction
# queue's much heavier vision calls and is set far lower; holding the matcher to
# that limit leaves the endpoint idle.
_MAX_CONCURRENT_DOCS = _local_llm_cfg.engine_max_concurrent

# How many times a document that lost a contested transaction is re-run against
# what is left. One round means the loser simply ends unmatched; three covers
# the case this exists for — several equal receipts against several equal bank
# rows — while bounding the work, since the rounds are a fixed ceiling rather
# than a loop to convergence. Each extra round re-runs only the documents that
# actually lost.
_MAX_CLAIM_ROUNDS = 3


def find_matches(
    transactions: list[Transaction],
    documents: list[Document],
    config: ReconciliationConfig | None = None,
    pass_config: PassConfig | None = None,
    progress_callback: "Callable[[dict], None] | None" = None,
    max_date_days: int = 45,
    rejected_pairs: "set[tuple[int, int]] | None" = None,
    memory: "AdjudicationMemory | None" = None,
) -> list[ReconciliationMatch]:
    """Match transactions to documents using concurrent 3-agent pipeline.

    Processes up to ``_MAX_CONCURRENT_DOCS`` documents simultaneously. Each
    document gets its own 3-agent pipeline (Amount → Date → Vendor) running in a thread pool, and
    ends it by *proposing* one transaction. Nothing is claimed on arrival:
    once the pool has drained, :meth:`_TxnClaims.resolve` settles every
    contested transaction by score alone (see :attr:`_Proposal.order_key`), so
    the holders are a function of the scores and not of which worker finished
    first.

    A document that loses its transaction gets another look. Its whole
    pipeline is re-run against the pool the settled round left behind, up to
    ``_MAX_CLAIM_ROUNDS`` times, so two identical receipts against two identical
    bank rows still end up matched one each.

    ``rejected_pairs`` — (transaction_id, document_id) pairs a human rejected,
    read once per run by the caller — is applied at candidate generation inside
    each worker, so a rejected pair is never proposed and never blocks.
    """
    if not transactions or not documents:
        return []

    if config is None:
        config = ReconciliationConfig()
    if pass_config is None:
        pass_config = PASS_STRICT

    auto_approve_threshold = pass_config.auto_approve_threshold
    txn_map = {t.id: t for t in transactions}
    total_docs = len(documents)
    rejected = frozenset(rejected_pairs or ())

    # Shared proposal registry — one transaction, one document, best pair wins
    claims = _TxnClaims()
    # Mutable counter so workers can report live match count
    match_count: list[int] = [0]

    # Filter to processable documents
    eligible_docs = [
        (i, doc) for i, doc in enumerate(documents)
        if doc.total is not None and doc.total != 0
    ]

    def _log(msg: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({
                "total_docs": total_docs,
                "matches_so_far": match_count[0],
                "log": msg,
            })
        except Exception:
            logger.debug("Progress callback failed: %s", msg)

    pending = eligible_docs
    for round_no in range(1, _MAX_CLAIM_ROUNDS + 1):
        if not pending:
            break
        held = frozenset(claims.held_txn_ids())
        if len(held) >= len(transactions):
            break
        if round_no > 1:
            _audit.log(
                "claim_round_start",
                round=round_no,
                documents=[doc.id for _i, doc in pending],
                held_txn_ids=sorted(held),
            )
            _log(
                f"Second look: {len(pending)} proof(s) lost a contested transaction "
                f"— re-running them against the {len(transactions) - len(held)} "
                f"transactions still free (round {round_no})"
            )

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_DOCS) as pool:
            futures = {
                pool.submit(
                    _process_single_doc,
                    doc, doc_i, total_docs,
                    transactions, txn_map,
                    claims,
                    auto_approve_threshold,
                    progress_callback, match_count,
                    max_date_days,
                    rejected,
                    memory or _NO_MEMORY,
                    held,
                ): doc_i
                for doc_i, doc in pending
            }

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    doc_i = futures[future]
                    logger.exception("Worker failed for doc index %d", doc_i)
                    _audit.log(
                        "worker_exception",
                        doc_index=doc_i,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    _log(f"Worker error: {type(exc).__name__}: {str(exc)[:80]}")

        # ── Settle the round: best proposal per transaction, by score ──
        winners, losers = claims.resolve()
        for winner in winners:
            _audit.log(
                "claim_resolved",
                txn_id=winner.txn_id,
                doc_id=winner.doc_id,
                confidence=winner.confidence,
                flagged=winner.flagged,
                amount_score=winner.amount_score,
                date_gap_days=winner.date_gap,
                round=round_no,
            )
        held_docs = claims.held_doc_ids()
        retry: list[tuple[int, Document]] = []
        for loser in losers:
            winner = next(w for w in winners if w.txn_id == loser.txn_id)
            _audit.log(
                "claim_lost",
                txn_id=loser.txn_id,
                doc_id=loser.doc_id,
                confidence=loser.confidence,
                flagged=loser.flagged,
                amount_score=loser.amount_score,
                date_gap_days=loser.date_gap,
                winning_doc_id=winner.doc_id,
                winning_confidence=winner.confidence,
                winning_flagged=winner.flagged,
                round=round_no,
            )
            _log(
                f"Proof {loser.doc_id} ({loser.confidence:.0%}) lost transaction "
                f"{loser.txn_id} to proof {winner.doc_id} "
                f"({winner.confidence:.0%}) — the stronger pair holds it"
            )
            if match_count[0] > 0:
                match_count[0] -= 1
            if loser.doc_id not in held_docs:
                found = next(
                    ((i, d) for i, d in pending if d.id == loser.doc_id), None,
                )
                if found is not None:
                    retry.append(found)
        pending = retry

    if pending:
        # Out of rounds with documents still contesting. They end unmatched,
        # and the run log says which ones and why.
        _audit.log(
            "claim_rounds_exhausted",
            rounds=_MAX_CLAIM_ROUNDS,
            documents=[doc.id for _i, doc in pending],
        )

    # The registry is the source of truth: a proposal that lost its transaction
    # is not in it, so its document ends the run unmatched.
    return claims.matches()


def _diagnose_unmatched_txn(
    txn: Transaction,
    all_docs: list[Document],
) -> UnmatchedReason:
    """Determine why a transaction couldn't be matched to any document."""
    if not all_docs:
        return UnmatchedReason(
            item_type="transaction", item_id=txn.id,
            reason="no_candidates",
            details=f"No documents available. {txn.date_posted} ${txn.amount} {txn.currency} — {txn.description[:50]}",
        )

    best_score = 0.0
    best_doc = None
    best_reason = "no_matching_amount"

    for doc in all_docs:
        if doc.total is None:
            continue
        if not check_sign_compatibility(txn, doc):
            continue
        # Currency-aware from the start: comparing CAD 3,000 against USD 3,000
        # as magnitudes would report a strong "best candidate" for a pair the
        # matcher rejects outright.
        a_score = compare_amounts_for_document(
            txn.amount, txn.currency, doc.total, doc,
            tolerance=Decimal("2.00"),
        ).score

        if a_score > 0:
            d_score = compute_date_score(txn.date_posted, _get_best_date(doc))
            v_score = compute_vendor_score(txn.description, doc.vendor or "")
            conf = compute_confidence(a_score, d_score, v_score)
            if conf > best_score:
                best_score = conf
                best_doc = doc
                best_reason = "below_threshold"

    if best_doc is None:
        return UnmatchedReason(
            item_type="transaction", item_id=txn.id,
            reason="no_matching_amount",
            details=f"No document with matching amount. {txn.date_posted} ${txn.amount} {txn.currency} — {txn.description[:50]}",
        )

    return UnmatchedReason(
        item_type="transaction", item_id=txn.id,
        reason=best_reason,
        details=(
            f"Best candidate: {best_doc.vendor or '?'} ${best_doc.total} {best_doc.currency} "
            f"on {best_doc.document_date} (score: {best_score:.0%}). "
            f"Txn: {txn.date_posted} ${txn.amount} {txn.currency} — {txn.description[:40]}"
        ),
        best_candidate_score=best_score,
    )


def _diagnose_unmatched_doc(
    doc: Document,
    all_txns: list[Transaction],
) -> UnmatchedReason:
    """Determine why a document couldn't be matched to any transaction."""
    if not all_txns:
        return UnmatchedReason(
            item_type="document", item_id=doc.id,
            reason="no_candidates",
            details=f"No transactions available. {doc.vendor or '?'} ${doc.total} {doc.currency} on {doc.document_date}",
        )

    best_score = 0.0
    best_txn = None

    for txn in all_txns:
        if doc.total is None:
            continue
        if not check_sign_compatibility(txn, doc):
            continue
        # Same one rule as the matcher and the manual picker.
        a_score = compare_amounts_for_document(
            txn.amount, txn.currency, doc.total, doc,
            tolerance=Decimal("2.00"),
        ).score

        if a_score > 0:
            d_score = compute_date_score(txn.date_posted, _get_best_date(doc))
            v_score = compute_vendor_score(txn.description, doc.vendor or "")
            conf = compute_confidence(a_score, d_score, v_score)
            if conf > best_score:
                best_score = conf
                best_txn = txn

    if best_txn is None:
        return UnmatchedReason(
            item_type="document", item_id=doc.id,
            reason="no_matching_amount",
            details=f"No transaction with matching amount. {doc.vendor or '?'} ${doc.total} {doc.currency} on {doc.document_date}",
        )

    return UnmatchedReason(
        item_type="document", item_id=doc.id,
        reason="below_threshold",
        details=(
            f"Best candidate: {best_txn.date_posted} ${best_txn.amount} {best_txn.currency} "
            f"— {best_txn.description[:40]} (score: {best_score:.0%}). "
            f"Doc: {doc.vendor or '?'} ${doc.total} {doc.currency}"
        ),
        best_candidate_score=best_score,
    )


def find_matches_multi_pass(
    transactions: list[Transaction],
    documents: list[Document],
    config: ReconciliationConfig | None = None,
    passes: list[PassConfig] | None = None,
    progress_callback: "Callable[[dict], None] | None" = None,
    max_date_days: int = 45,
    rejected_pairs: "set[tuple[int, int]] | None" = None,
    memory: "AdjudicationMemory | None" = None,
) -> MultiPassResult:
    """Run 3-agent LLM reconciliation pipeline.

    Uses a single pass with 3 sequential LLM agents per document:
      Agent 1 (Amount): Shortlist by monetary match
      Agent 2 (Date): Narrow by date plausibility
      Agent 3 (Final): Pick single best match

    Writes one JSON run log per run into the directory named by
    ``RECON_LOG_DIR`` (see the top of this module), holding the run's inputs,
    every agent shortlist and verdict, every deterministic score and a closing
    summary.

    Args:
        progress_callback: Optional callback invoked with progress info.
        rejected_pairs: (transaction_id, document_id) pairs a human has
            rejected. Read once per run by the caller
            (``DatabasePg.get_user_rejected_pairs``) and excluded at candidate
            generation, so a rejected pair is never proposed again.
        memory: Verdicts this user's earlier runs already got from the model,
            loaded once by the caller. An agent whose question is byte-for-byte
            unchanged reuses the stored reply instead of calling the model; the
            reply is then scored and classified exactly as a fresh one would be.
            Omit it and nothing is remembered or reused.
    """
    result = MultiPassResult()

    if not transactions or not documents:
        return result

    # ── Start audit log for this run ──
    _audit.start_run(len(transactions), len(documents))
    _audit.log(
        "run_config",
        max_date_days=max_date_days,
        user_rejected_pairs=sorted(rejected_pairs or ()),
    )
    # Fresh LLM tally for this run.
    reset_llm_call_stats()

    # Record the run's inputs so its decisions can be read back from the log
    # alone, without the caller's data.
    _audit.log(
        "input_transactions",
        transactions=[
            {
                "id": t.id,
                "account": account_str(t.account),
                "date": str(t.date_posted),
                "amount": t.amount,
                "currency": t.currency,
                "description": t.description[:100],
                "source_file": t.source_file,
            }
            for t in transactions
        ],
    )
    # Log all input documents
    _audit.log(
        "input_documents",
        documents=[
            {
                "id": d.id,
                "vendor": d.vendor,
                "total": d.total,
                "currency": d.currency,
                "document_date": str(d.document_date),
                "best_date": str(_get_best_date(d)),
                "filename": d.original_filename,
            }
            for d in documents
        ],
    )

    if progress_callback is not None:
        progress_callback({
            "pass_index": 0,
            "total_passes": 1,
            "pass_name": "3-agent",
            "matches_so_far": 0,
        })

    # Run the 3-agent pipeline (single pass — agents handle all matching)
    matches = find_matches(transactions, documents, config,
                           progress_callback=progress_callback,
                           max_date_days=max_date_days,
                           rejected_pairs=rejected_pairs,
                           memory=memory)

    for match in matches:
        idx = len(result.matches)
        result.matches.append(match)
        result.pass_labels[idx] = "3-agent"

    result.pass_counts["3-agent"] = len(matches)

    # Diagnose unmatched items
    matched_txn_ids = {m.transaction_id for m in matches}
    matched_doc_ids = {m.document_id for m in matches}
    all_remaining_docs = [d for d in documents if d.id not in matched_doc_ids]
    all_remaining_txns = [t for t in transactions if t.id not in matched_txn_ids]

    for txn in all_remaining_txns:
        reason = _diagnose_unmatched_txn(txn, all_remaining_docs)
        result.unmatched_transactions.append(reason)
        _audit.log(
            "unmatched_transaction",
            txn_id=txn.id,
            txn_amount=txn.amount,
            txn_currency=txn.currency,
            txn_date=str(txn.date_posted),
            txn_description=txn.description[:80],
            reason=reason.reason,
            details=reason.details,
            best_candidate_score=reason.best_candidate_score,
        )
    for doc in all_remaining_docs:
        reason = _diagnose_unmatched_doc(doc, all_remaining_txns)
        result.unmatched_documents.append(reason)
        _audit.log(
            "unmatched_document",
            doc_id=doc.id,
            doc_vendor=doc.vendor,
            doc_total=doc.total,
            doc_currency=doc.currency,
            doc_date=str(doc.document_date),
            doc_filename=doc.original_filename,
            reason=reason.reason,
            details=reason.details,
            best_candidate_score=reason.best_candidate_score,
        )

    # ── How the LLM agents fared ──
    result.llm_stats = llm_call_stats_snapshot()
    fallbacks = result.llm_stats.get("fallbacks", 0)
    if fallbacks:
        logger.warning(
            "Reconciliation run used the deterministic fallback for %d of %d LLM "
            "agent calls: %s",
            fallbacks, result.llm_stats.get("calls", 0),
            result.llm_stats.get("by_label", {}),
        )
        if progress_callback is not None:
            try:
                progress_callback({"log": (
                    f"WARNING: {fallbacks} of {result.llm_stats.get('calls', 0)} AI agent "
                    f"calls failed and fell back to deterministic scoring "
                    f"({result.llm_stats.get('by_label', {})})"
                )})
            except Exception:
                logger.debug("Progress callback failed for LLM fallback summary")

    # Log final summary
    _audit.log(
        "run_summary",
        total_matches=len(matches),
        unmatched_transactions=len(result.unmatched_transactions),
        unmatched_documents=len(result.unmatched_documents),
        match_rate=f"{result.match_rate:.1f}%",
        llm_stats=result.llm_stats,
    )

    # ── Flush audit log to disk ──
    log_path = _audit.flush()
    if log_path and progress_callback:
        progress_callback({"log": f"Audit log saved: {log_path}"})

    return result

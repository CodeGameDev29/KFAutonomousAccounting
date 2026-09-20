"""Match explainer — produces human-readable explanations for reconciliation matches.

Given the numeric scores and raw field values from the reconciliation engine,
this module generates plain-English summaries that explain *why* two items were
matched and highlights anything that deserves human review.

The public entry point is ``explain_match()``.  It is designed to **never raise
an exception** — partial or malformed inputs always produce a best-effort
``MatchExplanation``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from core.amount_match import (
    ASSUMED_SAME_CURRENCY,
    EXACT,
    FX_BAND,
    FX_CAD_USD_MAX,
    FX_CAD_USD_MIN,
    NO_FX_BAND,
    ROUNDING,
    UNKNOWN,
    compare_amounts,
)
from core.document_currency import ASSUMED, INFERRED

# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

@dataclass
class MatchExplanation:
    """Human-readable explanation of a single reconciliation match."""

    summary: str
    amount_detail: str
    date_detail: str
    vendor_detail: str
    confidence_label: str
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------

def _to_decimal(value: object) -> Decimal:
    """Coerce *value* to ``Decimal``, returning ``Decimal(0)`` on failure."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _to_float(value: object) -> float:
    """Coerce *value* to ``float``, returning ``0.0`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO-format date string, returning ``None`` on failure."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _fmt_amount(amount: Decimal, currency: str | None = None) -> str:
    """Format a Decimal as a currency string like ``$123.45`` or ``$123.45 USD``."""
    try:
        formatted = f"${abs(amount):,.2f}"
    except Exception:
        formatted = f"${amount}"
    if currency:
        formatted = f"{formatted} {currency}"
    return formatted


def _safe_trim(text: str, max_len: int) -> str:
    """Shorten *text* without ever leaving a sentence mid-thought.

    A hard slice at a character count produces explanations like
    ``"... (12 days apart..., 'Orchid Telecom' found in bank description"``
    — a cut inside a parenthetical that leaves the bracket open. This trims on
    a word boundary and then, if the result has an unclosed ``(`` or an odd
    number of quotes, backs up to before that character rather than emitting a
    broken sentence.
    """
    text = (text or "").strip()
    if len(text) <= max_len:
        return text

    cut = text[:max_len]
    space = cut.rfind(" ")
    if space > max_len // 2:
        cut = cut[:space]

    # Never end inside a parenthetical or a quoted name.
    if cut.count("(") > cut.count(")"):
        cut = cut[:cut.rfind("(")]
    if cut.count("'") % 2:
        cut = cut[:cut.rfind("'")]

    cut = cut.rstrip(" ,;:-")
    return f"{cut}…" if cut else text[:max_len].rstrip()


# ---------------------------------------------------------------------------
# Amount detail
# ---------------------------------------------------------------------------

def _currency_caveat(comparison) -> str:
    """The parenthetical that says how the receipt's currency was arrived at.

    Empty for a receipt that stated its own — which is most of them — so the
    sentence a user already knows does not change.

    The wording is the manual picker's sentence
    (:meth:`core.amount_match.AmountComparison.currency_note`) in brackets, so
    the same pair explains itself the same way whether a user matched it by
    hand or the auto matcher did. It belongs in **both** the detail line and
    the one-line summary: the summary is the only part of this object the
    Review panel renders (``server/api/reconciliation.py`` returns
    ``explanation.summary`` and drops the rest), so a caveat that lives only in
    ``amount_detail`` is a caveat nobody ever reads — a receipt whose currency
    was inferred from its tax line would read on /review as a flat
    "matches exactly".
    """
    try:
        if comparison.doc_total is None:
            return ""
        if comparison.doc_currency_source == ASSUMED:
            read_as = comparison.txn_currency or "CAD"
            return f" (receipt states no currency; read as {read_as})"
        if comparison.doc_currency_source == INFERRED and comparison.same_currency:
            return (
                f" (receipt currency inferred as {comparison.doc_currency} "
                f"from its Canadian tax line)"
            )
    except Exception:
        pass
    return ""


def _build_amount_detail(
    txn_amount: Decimal,
    txn_currency: str,
    doc_total: Decimal,
    doc_currency: str,
    amount_score: float,
    doc_currency_source: str | None = None,
) -> str:
    """Return a human-readable explanation of the amount comparison.

    The same/cross-currency decision — and whether the word "exactly" is
    allowed to appear — comes from :func:`core.amount_match.compare_amounts`,
    the one rule the matcher and the manual picker also use, so this panel
    never calls CAD 3,000 against USD 3,000 an exact match.
    """
    try:
        comparison = compare_amounts(
            txn_amount, txn_currency, doc_total, doc_currency,
            doc_currency_source=doc_currency_source,
        )
        diff = comparison.diff if comparison.diff is not None else Decimal("0")
        caveat = _currency_caveat(comparison)

        if comparison.same_currency:
            if comparison.verdict == ASSUMED_SAME_CURRENCY:
                # Same figure, in a currency the receipt never named.
                if comparison.magnitude_verdict == EXACT:
                    return f"{_fmt_amount(txn_amount)} matches exactly{caveat}"
                return (
                    f"{_fmt_amount(txn_amount)} vs {_fmt_amount(doc_total)} "
                    f"({_fmt_amount(diff)} difference){caveat}"
                )
            if comparison.verdict == EXACT:
                return f"{_fmt_amount(txn_amount)} matches exactly{caveat}"
            if comparison.verdict == ROUNDING:
                return (
                    f"{_fmt_amount(txn_amount)} vs {_fmt_amount(doc_total)} "
                    f"({_fmt_amount(diff)} rounding difference){caveat}"
                )
            if amount_score <= 0:
                return (
                    f"Amount mismatch: {_fmt_amount(txn_amount)} vs "
                    f"{_fmt_amount(doc_total)}{caveat}"
                )
            # Scored above zero on a route of its own (alternate amount on the
            # receipt, USD amount inside the bank description) — show the numbers.
            return (
                f"{_fmt_amount(txn_amount)} vs {_fmt_amount(doc_total)} "
                f"({_fmt_amount(diff)} difference){caveat}"
            )

        # ── Different currencies: converted, never "exact" ──
        if comparison.verdict == FX_BAND and comparison.implied_rate is not None:
            return (
                f"{_fmt_amount(txn_amount, txn_currency)} vs "
                f"{_fmt_amount(doc_total, doc_currency)} "
                f"(FX conversion, implied rate {comparison.implied_rate:.4f})"
            )
        if comparison.verdict == NO_FX_BAND:
            return (
                f"{_fmt_amount(txn_amount, txn_currency)} vs "
                f"{_fmt_amount(doc_total, doc_currency)} "
                f"(cross-currency, no {txn_currency}/{doc_currency} rate to convert with)"
            )
        rate = (
            f", implied rate {comparison.implied_rate:.4f}"
            if comparison.implied_rate is not None else ""
        )
        return (
            f"{_fmt_amount(txn_amount, txn_currency)} vs "
            f"{_fmt_amount(doc_total, doc_currency)} "
            f"(no plausible FX conversion{rate} — outside the "
            f"{FX_CAD_USD_MIN:.2f}–{FX_CAD_USD_MAX:.2f} CAD/USD band)"
        )
    except Exception:
        return "Amount comparison unavailable"


def _amount_summary_fragment(
    txn_amount: Decimal,
    txn_currency: str,
    doc_total: Decimal,
    doc_currency: str,
    amount_score: float,
    doc_currency_source: str | None = None,
) -> str:
    """Compact mid-sentence form of the amount comparison, for the summary.

    Carries the same currency caveat the detail line carries — this fragment is
    the only one of the two a user ever sees (the API returns the summary and
    nothing else), and "$41.25 matches exactly" about a currency the receipt
    never printed is a claim the receipt never made.
    """
    try:
        comparison = compare_amounts(
            txn_amount, txn_currency, doc_total, doc_currency,
            doc_currency_source=doc_currency_source,
        )
        if comparison.verdict == UNKNOWN:
            return "no amount on the receipt"
        caveat = _currency_caveat(comparison)
        if comparison.same_currency:
            if comparison.verdict == ASSUMED_SAME_CURRENCY:
                if comparison.magnitude_verdict == EXACT:
                    return f"{_fmt_amount(txn_amount)} matches exactly{caveat}"
                return (
                    f"{_fmt_amount(txn_amount)} vs "
                    f"{_fmt_amount(doc_total)}{caveat}"
                )
            if comparison.verdict == EXACT:
                return f"{_fmt_amount(txn_amount)} matches exactly{caveat}"
            diff = comparison.diff if comparison.diff is not None else Decimal("0")
            if comparison.verdict == ROUNDING:
                return f"{_fmt_amount(txn_amount)} within {_fmt_amount(diff)}{caveat}"
            if amount_score > 0:
                return (
                    f"{_fmt_amount(txn_amount)} vs "
                    f"{_fmt_amount(doc_total)}{caveat}"
                )
            return (
                f"amounts differ ({_fmt_amount(txn_amount)} vs "
                f"{_fmt_amount(doc_total)}){caveat}"
            )
        if comparison.verdict == FX_BAND and comparison.implied_rate is not None:
            return (
                f"{_fmt_amount(txn_amount, txn_currency)} converts from "
                f"{_fmt_amount(doc_total, doc_currency)} at "
                f"{comparison.implied_rate:.4f}"
            )
        return (
            f"amounts do not reconcile across currencies "
            f"({_fmt_amount(txn_amount, txn_currency)} vs "
            f"{_fmt_amount(doc_total, doc_currency)})"
        )
    except Exception:
        return "amount comparison unavailable"


# ---------------------------------------------------------------------------
# Date detail
# ---------------------------------------------------------------------------

def _build_date_detail(
    txn_date: str | None,
    doc_date: str | None,
    date_score: float,
) -> str:
    """Return a human-readable explanation of the date comparison."""
    try:
        if doc_date is None:
            return "No date on receipt"

        parsed_txn = _parse_date(txn_date)
        parsed_doc = _parse_date(doc_date)

        if parsed_txn is None:
            return "Transaction date unavailable"
        if parsed_doc is None:
            return "No date on receipt"

        diff_days = abs((parsed_txn - parsed_doc).days)

        if diff_days == 0:
            detail = "Same day"
        elif diff_days <= 3:
            day_word = "day" if diff_days == 1 else "days"
            detail = f"{diff_days} {day_word} apart"
        elif diff_days <= 14:
            detail = f"{diff_days} days apart (normal processing delay)"
        else:
            detail = f"{diff_days} days apart (unusual delay)"

        # Em dash, not another set of brackets: the phrases above already
        # carry their own parenthetical.
        return f"Transaction {txn_date} vs receipt {doc_date} — {detail}"
    except Exception:
        return "Date comparison unavailable"


def _date_summary_fragment(txn_date: str | None, doc_date: str | None) -> str:
    """Compact mid-sentence form of the date comparison, for the summary.

    Just the relationship ("same day", "12 days apart (normal processing
    delay)"). The two dates themselves are already on screen next to this
    sentence, and repeating them pushes the summary past its length limit,
    where it gets cut mid-parenthesis.
    """
    try:
        parsed_txn = _parse_date(txn_date)
        parsed_doc = _parse_date(doc_date)
        if parsed_doc is None:
            return "no date on the receipt"
        if parsed_txn is None:
            return "transaction date unavailable"

        diff_days = abs((parsed_txn - parsed_doc).days)
        if diff_days == 0:
            return "same day"
        if diff_days == 1:
            return "1 day apart"
        if diff_days <= 3:
            return f"{diff_days} days apart"
        if diff_days <= 14:
            return f"{diff_days} days apart (normal processing delay)"
        return f"{diff_days} days apart (unusual delay)"
    except Exception:
        return "date comparison unavailable"


# ---------------------------------------------------------------------------
# Vendor detail
# ---------------------------------------------------------------------------

def _build_vendor_detail(
    doc_vendor: str | None,
    txn_description: str | None,
    vendor_score: float,
) -> str:
    """Return a human-readable explanation of the vendor comparison."""
    try:
        vendor_display = doc_vendor or "unknown"
        desc_display = txn_description or "unknown"

        if vendor_score >= 0.8:
            return f"'{vendor_display}' found in bank description"
        if vendor_score >= 0.35:
            return "Possible match via payment intermediary (Wise/PayPal)"
        return f"Vendor '{vendor_display}' not found in '{desc_display}'"
    except Exception:
        return "Vendor comparison unavailable"


def _vendor_summary_fragment(
    doc_vendor: str | None,
    txn_description: str | None,
    vendor_score: float,
) -> str:
    """Compact mid-sentence form of the vendor comparison, for the summary."""
    try:
        vendor_display = doc_vendor or "unknown"
        if vendor_score >= 0.8:
            return f"'{vendor_display}' found in the bank description"
        if vendor_score >= 0.35:
            return "vendor matched via a payment intermediary"
        desc_display = _safe_trim(txn_description or "unknown", 30)
        return f"vendor '{vendor_display}' not found in '{desc_display}'"
    except Exception:
        return "vendor comparison unavailable"


# ---------------------------------------------------------------------------
# Summary + confidence
# ---------------------------------------------------------------------------

def _confidence_label(confidence_score: float) -> str:
    if confidence_score >= 0.85:
        return "HIGH"
    if confidence_score >= 0.60:
        return "MEDIUM"
    return "LOW"


# Longest a single clause may be inside the summary sentence. Only the vendor
# clause can exceed it (it quotes a bank description of arbitrary length), and
# _safe_trim shortens that without breaking the sentence.
_SUMMARY_CLAUSE_MAX_LEN = 80

# The amount clause gets a longer leash. The limit above exists for text a
# *document* supplies — a bank description of any length — while the amount
# clause is built here out of two figures and, when the receipt never printed
# its currency, the caveat saying so. At 80 characters that caveat is what
# falls off (``_safe_trim`` backs out of an unclosed bracket, dropping the whole
# parenthetical), and it is the one part of the sentence a user cannot
# reconstruct from the numbers beside it.
_SUMMARY_AMOUNT_CLAUSE_MAX_LEN = 120


def _build_summary(
    confidence_label: str,
    amount_fragment: str,
    date_fragment: str,
    vendor_fragment: str,
) -> str:
    """Build the one-line summary sentence.

    Composed from purpose-built short clauses, not from slices of the longer
    detail strings — slicing produces broken text in the UI, such as
    ``"(12 days apart..., 'X' found in bank description"``. Every clause here
    is complete, and the sentence ends with a full stop.
    """
    try:
        amount = _safe_trim(amount_fragment, _SUMMARY_AMOUNT_CLAUSE_MAX_LEN)
        when = _safe_trim(date_fragment, _SUMMARY_CLAUSE_MAX_LEN)
        vendor = _safe_trim(vendor_fragment, _SUMMARY_CLAUSE_MAX_LEN)

        if confidence_label == "HIGH":
            return f"Strong match: {amount}, {when}, {vendor}."
        if confidence_label == "MEDIUM":
            return f"Probable match: {amount}, {when}. Review vendor."
        return "Weak match: review all fields carefully."
    except Exception:
        return "Match explanation unavailable"


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def _build_flags(
    txn_currency: str,
    doc_currency: str,
    match_type: str,
    date_score: float,
    vendor_score: float,
    doc_currency_source: str | None = None,
) -> list[str]:
    """Collect warning flags for the match."""
    flags: list[str] = []
    try:
        doc_cur = (doc_currency or "").upper()
        if doc_currency_source == ASSUMED or not doc_cur:
            # No currency was ever read off this receipt, so nothing was
            # converted. Say what is actually true instead of "FX applied".
            flags.append("Receipt currency not stated")
        elif (txn_currency or "").upper() != doc_cur:
            flags.append("FX conversion applied")
        elif doc_currency_source == INFERRED:
            flags.append("Receipt currency inferred from its tax line")
    except Exception:
        pass

    try:
        if (match_type or "").upper() != "ONE_TO_ONE":
            flags.append(f"Split transaction ({match_type})")
    except Exception:
        pass

    try:
        if date_score < 0.5:
            flags.append("Unusual date gap")
    except Exception:
        pass

    try:
        if vendor_score < 0.35:
            flags.append("Vendor unconfirmed")
    except Exception:
        pass

    return flags


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def explain_match(
    txn_amount: Decimal,
    txn_currency: str,
    txn_date: str,                # ISO date string
    txn_description: str,
    doc_total: Decimal,
    doc_currency: str,
    doc_date: str | None,         # ISO date string or None
    doc_vendor: str | None,
    confidence_score: float,
    amount_score: float,
    date_score: float,
    vendor_score: float,
    match_type: str,              # ONE_TO_ONE, MANY_TO_ONE, ONE_TO_MANY
    doc_currency_source: str | None = None,   # stated | inferred | assumed
) -> MatchExplanation:
    """Produce a human-readable explanation for a reconciliation match.

    This function **never raises**.  Any internal error is caught and a
    best-effort ``MatchExplanation`` with safe fallback text is returned.
    """
    # Normalise inputs defensively so downstream helpers get sane types.
    try:
        txn_amount = _to_decimal(txn_amount)
        doc_total = _to_decimal(doc_total)
        confidence_score = _to_float(confidence_score)
        amount_score = _to_float(amount_score)
        date_score = _to_float(date_score)
        vendor_score = _to_float(vendor_score)
        txn_currency = str(txn_currency or "")
        doc_currency = str(doc_currency or "")
        txn_date = str(txn_date or "")
        txn_description = str(txn_description or "")
        match_type = str(match_type or "ONE_TO_ONE")
    except Exception:
        pass

    # Build each section independently so one failure doesn't block others.
    try:
        amount_detail = _build_amount_detail(
            txn_amount, txn_currency, doc_total, doc_currency, amount_score,
            doc_currency_source,
        )
    except Exception:
        amount_detail = "Amount comparison unavailable"

    try:
        date_detail = _build_date_detail(txn_date, doc_date, date_score)
    except Exception:
        date_detail = "Date comparison unavailable"

    try:
        vendor_detail = _build_vendor_detail(doc_vendor, txn_description, vendor_score)
    except Exception:
        vendor_detail = "Vendor comparison unavailable"

    try:
        label = _confidence_label(confidence_score)
    except Exception:
        label = "LOW"

    try:
        summary = _build_summary(
            label,
            _amount_summary_fragment(
                txn_amount, txn_currency, doc_total, doc_currency, amount_score,
                doc_currency_source,
            ),
            _date_summary_fragment(txn_date, doc_date),
            _vendor_summary_fragment(doc_vendor, txn_description, vendor_score),
        )
    except Exception:
        summary = "Match explanation unavailable"

    try:
        flags = _build_flags(
            txn_currency, doc_currency, match_type, date_score, vendor_score,
            doc_currency_source,
        )
    except Exception:
        flags = []

    return MatchExplanation(
        summary=summary,
        amount_detail=amount_detail,
        date_detail=date_detail,
        vendor_detail=vendor_detail,
        confidence_label=label,
        flags=flags,
    )

"""LLM-based post-match validation for reconciliation.

After the reconciliation engine produces candidate matches, this module
validates suspicious matches along two dimensions:

1. **Date gaps** (>7 days): An LLM evaluates whether the gap between the
   bank transaction date and the document date is reasonable given the
   transaction context.

2. **Amount discrepancies** (larger than the penny tolerance, or
   cross-currency): An LLM evaluates whether the amount difference is
   explainable by FX conversion, fees, tips, rounding, partial payments, etc.
   This is especially important for CAD/USD cross-currency matches where the
   reconciliation engine uses a flat FX band match.

If either check determines the match is unreasonable, it is rejected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from core.amount_match import (
    FX_CAD_USD_MAX,
    FX_CAD_USD_MIN,
    PENNY_TOLERANCE,
    WIRE_FEE_TOLERANCE,
)

logger = logging.getLogger(__name__)

# Thresholds for triggering LLM validation
DATE_GAP_VALIDATION_THRESHOLD = 7  # days
# Same-currency: anything above penny rounding is worth a second look. The
# threshold is the engine's one penny tolerance, not a second copy of it.
AMOUNT_DISCREPANCY_THRESHOLD = PENNY_TOLERANCE
# Cross-currency matches always get amount validation

# This validator must never be stricter than the matcher that produced the
# match: rejecting a pair the FX band already accepted would undo a correct
# match on a rounding difference in the implied rate. So the fallback's band is
# the canonical band widened on the low side by this slack.
_VALIDATOR_RATE_SLACK = Decimal("0.05")
_VALIDATOR_FX_MIN = FX_CAD_USD_MIN - _VALIDATOR_RATE_SLACK
_VALIDATOR_FX_MAX = FX_CAD_USD_MAX

# ─── Shared LLM call infrastructure ───────────────────────────────────

# Token budget for every engine-side JSON call.
#
# A budget in the low hundreds truncates a reply mid-object, because a model
# writes its "reasoning" paragraph before it closes the JSON and the decision
# fields are then unreadable. A truncated reply downgrades an amount agent, a
# date agent, the match validator or the self-consistency check to its
# deterministic fallback, so the budget has to clear a reply that carries real
# reasoning with room to spare. core.llm_sync retries a truncated reply with
# double this, so the cost of setting it too low is a second call, never a lost
# verdict.
ENGINE_JSON_MAX_TOKENS = 1024


def _call_llm(
    prompt: str,
    label: str = "match_validator",
    max_tokens: int = ENGINE_JSON_MAX_TOKENS,
) -> dict:
    """Call the LLM ladder — local first, then Gemini/OpenAI if keyed.

    Also the call the categorization agent and the 3-pass reconciliation agents
    route through, so this one function decides the provider for every
    synchronous LLM call in the engine. ``label`` names the caller in the log
    and in the run's fallback tally, so a failure is attributed to the agent
    that made it rather than to this module.

    ``max_tokens`` defaults to the engine-wide ceiling above. The three matcher
    agents pass their own, smaller budget (``RECON_AGENT_MAX_TOKENS``) because
    their prompts cap the reasoning at one sentence; everything else — the
    validators here, the categorization agent's batch replies — takes the
    ceiling.
    """
    from core.llm_sync import call_json

    return call_json(prompt, max_tokens=max_tokens, label=label)


# ─── Date gap validation ──────────────────────────────────────────────

DATE_VALIDATION_PROMPT = """You are a financial reconciliation agent. Determine if the date gap between a bank transaction and its proof-of-transaction document is acceptable.

BANK TRANSACTION:
- Date: {txn_date}
- Amount: {txn_amount} {txn_currency}
- Description: {txn_description}
- Account: {txn_account}

PROOF OF TRANSACTION:
- Vendor: {doc_vendor}
- Date: {doc_date}
- Total: {doc_total} {doc_currency}
- Filename: {doc_filename}

DATE GAP: {gap_days} days (transaction is {direction} the document)

Consider:
1. Subscription, cloud, hosting and software services can have 30-60 day gaps between charge and invoice
2. International payments (Wise, PayPal) typically clear within 1-5 business days
3. Credit card charges usually post within 1-3 days
4. Refunds can take 5-14 days
5. Physical store purchases post same day or next day
6. Pre-authorized recurring payments may differ by up to 5 days
7. Invoice-based payments may have 30-90 day terms

Respond ONLY with valid JSON:
{{"max_acceptable_days": <number>, "reasoning": "<brief explanation>", "verdict": "ACCEPT" or "REJECT"}}"""


def validate_date_gaps(
    matches_with_context: list[dict],
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> list[dict]:
    """Validate date gaps for a batch of matches using LLM.

    Args:
        on_progress: Optional callback(completed, total, result_dict) called after each item.
    """
    if not matches_with_context:
        return []

    total = len(matches_with_context)
    results = []
    for i, ctx in enumerate(matches_with_context):
        try:
            result = _validate_date_single(ctx)
            results.append(result)
        except Exception as e:
            logger.warning(f"Date validation failed for match {ctx['match_index']}: {e}")
            result = _date_heuristic_fallback(ctx)
            results.append(result)

        if on_progress:
            on_progress(i + 1, total, result)

    return results


def _validate_date_single(ctx: dict) -> dict:
    """Call the LLM to validate a single match's date gap."""
    direction = "after" if ctx["txn_date"] > ctx["doc_date"] else "before"

    prompt = DATE_VALIDATION_PROMPT.format(
        txn_date=ctx["txn_date"].isoformat(),
        txn_amount=ctx["txn_amount"],
        txn_currency=ctx["txn_currency"],
        txn_description=ctx["txn_description"],
        txn_account=ctx["txn_account"],
        doc_vendor=ctx["doc_vendor"],
        doc_date=ctx["doc_date"].isoformat(),
        doc_total=ctx["doc_total"],
        doc_currency=ctx["doc_currency"],
        doc_filename=ctx["doc_filename"],
        gap_days=ctx["gap_days"],
        direction=direction,
    )

    data = _call_llm(prompt, label="match_validator.date_gap")
    max_days = int(data.get("max_acceptable_days", 14))
    verdict = "ACCEPT" if ctx["gap_days"] <= max_days else "REJECT"
    if data.get("verdict") == "REJECT":
        verdict = "REJECT"

    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_days": max_days,
        "reasoning": data.get("reasoning", ""),
    }


def _date_heuristic_fallback(ctx: dict) -> dict:
    """Rule-based fallback when LLM is unavailable."""
    desc_lower = ctx["txn_description"].lower()
    vendor_lower = (ctx.get("doc_vendor") or "").lower()

    # Generic words a recurring-service descriptor or vendor name carries. A
    # specific vendor whose descriptor carries none of them belongs in the
    # operator's own vendor rules, not in a list shipped with the engine.
    subscription_keywords = ["subscription", "subscript", "recurring", "monthly",
                             "annual", "renewal", "plan", "licen", "saas",
                             "software", "cloud", "hosting", "domain"]
    if any(kw in desc_lower or kw in vendor_lower for kw in subscription_keywords):
        max_days = 45
    elif any(kw in desc_lower for kw in ["wise", "paypal", "wire", "interac", "transfer"]):
        max_days = 10
    elif ctx["txn_account"] == "CreditCard":
        max_days = 7
    else:
        max_days = 14

    verdict = "ACCEPT" if ctx["gap_days"] <= max_days else "REJECT"
    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_days": max_days,
        "reasoning": f"Heuristic: max {max_days} days for this transaction type",
    }


# ─── Amount discrepancy validation ────────────────────────────────────

AMOUNT_VALIDATION_PROMPT = """You are a financial reconciliation agent. Determine if the amount discrepancy between a bank transaction and its proof-of-transaction document is acceptable.

BANK TRANSACTION:
- Date: {txn_date}
- Amount: {txn_amount} {txn_currency}
- Description: {txn_description}
- Account: {txn_account}

PROOF OF TRANSACTION:
- Vendor: {doc_vendor}
- Date: {doc_date}
- Total: {doc_total} {doc_currency}
- Filename: {doc_filename}

AMOUNT DISCREPANCY:
- Transaction amount: {txn_amount} {txn_currency}
- Document total: {doc_total} {doc_currency}
- Absolute difference: {abs_diff}
- Currencies match: {same_currency}
{fx_info}

Consider these reasons an amount discrepancy might be acceptable:
1. Currency exchange (verify the implied CAD/USD rate falls inside the {fx_min}-{fx_max} band this engine accepts)
2. Bank or wire transfer fees (up to ${wire_fee} on an international transfer)
3. Credit card foreign transaction fees (typically 2.5%)
4. Tips or gratuities on restaurant/service charges
5. Tax differences (GST/HST/PST applied differently on receipt vs bank charge)
6. Partial payments or installments
7. Rounding differences from FX conversion (usually <$1)
8. PayPal/Wise intermediary fees deducted from the transferred amount

For cross-currency matches:
- Calculate the implied exchange rate and verify it is plausible for the transaction date
- CAD/USD must fall between {fx_min} and {fx_max}; outside that band the two amounts are not a conversion of each other
- A 2-5% deviation from market rate is normal due to bank markup

Respond ONLY with valid JSON:
{{"max_acceptable_discrepancy": <number in dollars>, "reasoning": "<brief explanation>", "verdict": "ACCEPT" or "REJECT"}}"""


def validate_amount_discrepancies(
    matches_with_context: list[dict],
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> list[dict]:
    """Validate amount discrepancies for a batch of matches using LLM.

    Args:
        on_progress: Optional callback(completed, total, result_dict) called after each item.
    """
    if not matches_with_context:
        return []

    total = len(matches_with_context)
    results = []
    for i, ctx in enumerate(matches_with_context):
        try:
            result = _validate_amount_single(ctx)
            results.append(result)
        except Exception as e:
            logger.warning(f"Amount validation failed for match {ctx['match_index']}: {e}")
            result = _amount_heuristic_fallback(ctx)
            results.append(result)

        if on_progress:
            on_progress(i + 1, total, result)

    return results


def _ctx_same_currency(ctx: dict) -> bool:
    """Are these two amounts in the same currency, as far as anyone can tell?

    The document's currency reaches this module already resolved (see
    :func:`core.document_currency.resolve_document_currency`): a receipt that
    never named a currency arrives as ``None`` with source ``assumed``, and is
    read in the transaction's currency rather than being called cross-currency
    on the strength of a blank column.
    """
    from core.document_currency import ASSUMED

    doc_cur = str(ctx.get("doc_currency") or "").upper()
    if not doc_cur or ctx.get("doc_currency_source") == ASSUMED:
        return True
    return str(ctx.get("txn_currency") or "").upper() == doc_cur


def _validate_amount_single(ctx: dict) -> dict:
    """Call the LLM to validate a single match's amount discrepancy."""
    txn_amt = Decimal(str(ctx["txn_amount"]))
    doc_amt = Decimal(str(ctx["doc_total"]))
    abs_diff = abs(abs(txn_amt) - abs(doc_amt))

    same_currency = _ctx_same_currency(ctx)

    # Build FX info string
    fx_info = ""
    if not same_currency:
        txn_abs = abs(txn_amt)
        doc_abs = abs(doc_amt)
        if doc_abs > 0 and txn_abs > 0:
            if ctx["txn_currency"].upper() == "CAD" and ctx["doc_currency"].upper() == "USD":
                implied_rate = txn_abs / doc_abs
                fx_info = f"- Implied CAD/USD rate: {implied_rate:.4f} (transaction CAD / document USD)"
            elif ctx["txn_currency"].upper() == "USD" and ctx["doc_currency"].upper() == "CAD":
                implied_rate = doc_abs / txn_abs
                fx_info = f"- Implied CAD/USD rate: {implied_rate:.4f} (document CAD / transaction USD)"
            else:
                fx_info = f"- Cross-currency: {ctx['txn_currency']} vs {ctx['doc_currency']}"

    prompt = AMOUNT_VALIDATION_PROMPT.format(
        txn_date=ctx["txn_date"].isoformat() if isinstance(ctx.get("txn_date"), date) else str(ctx.get("txn_date", "")),
        txn_amount=ctx["txn_amount"],
        txn_currency=ctx["txn_currency"],
        txn_description=ctx["txn_description"],
        txn_account=ctx["txn_account"],
        doc_vendor=ctx["doc_vendor"],
        doc_date=ctx["doc_date"].isoformat() if isinstance(ctx.get("doc_date"), date) else str(ctx.get("doc_date", "")),
        doc_total=ctx["doc_total"],
        # Never show the model a currency the receipt did not print.
        doc_currency=(
            ctx["doc_currency"]
            if str(ctx.get("doc_currency") or "").strip()
            else f"not stated (read as {ctx['txn_currency']})"
        ),
        doc_filename=ctx["doc_filename"],
        abs_diff=f"${abs_diff:.2f}",
        same_currency="Yes" if same_currency else "No",
        fx_info=fx_info,
        # The band and the fee allowance the prompt quotes are the engine's own
        # constants, so the model is never told a different rule from the one
        # the matcher applied.
        fx_min=f"{FX_CAD_USD_MIN:.2f}",
        fx_max=f"{FX_CAD_USD_MAX:.2f}",
        wire_fee=f"{WIRE_FEE_TOLERANCE:.2f}",
    )

    data = _call_llm(prompt, label="match_validator.amount")
    max_disc = float(data.get("max_acceptable_discrepancy", 1.0))
    verdict = data.get("verdict", "ACCEPT")

    # For same-currency matches, override if raw discrepancy exceeds LLM's max.
    # For cross-currency matches, trust the LLM's verdict — raw abs_diff is
    # meaningless across currencies (e.g. $300 USD vs $430 CAD = $130 diff
    # which is the FX conversion, not an error).
    if same_currency and float(abs_diff) > max_disc and verdict != "REJECT":
        verdict = "REJECT"

    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_discrepancy": max_disc,
        "actual_discrepancy": float(abs_diff),
        "reasoning": data.get("reasoning", ""),
    }


def _amount_heuristic_fallback(ctx: dict) -> dict:
    """Rule-based fallback when LLM is unavailable."""
    txn_amt = Decimal(str(ctx["txn_amount"]))
    doc_amt = Decimal(str(ctx["doc_total"]))
    abs_diff = abs(abs(txn_amt) - abs(doc_amt))

    same_currency = _ctx_same_currency(ctx)
    desc_lower = ctx["txn_description"].lower()

    if not same_currency:
        # Cross-currency: check if implied rate is within CAD/USD band
        txn_abs = abs(txn_amt)
        doc_abs = abs(doc_amt)
        if txn_abs > 0 and doc_abs > 0:
            if ctx["txn_currency"].upper() == "CAD" and ctx["doc_currency"].upper() == "USD":
                implied_rate = txn_abs / doc_abs
            elif ctx["txn_currency"].upper() == "USD" and ctx["doc_currency"].upper() == "CAD":
                implied_rate = doc_abs / txn_abs
            else:
                implied_rate = Decimal("0")

            if _VALIDATOR_FX_MIN <= implied_rate <= _VALIDATOR_FX_MAX:
                return {
                    "match_index": ctx["match_index"],
                    "verdict": "ACCEPT",
                    "max_acceptable_discrepancy": float(abs_diff) + 5.0,
                    "actual_discrepancy": float(abs_diff),
                    "reasoning": f"Heuristic: cross-currency match, implied rate {implied_rate:.4f} is within CAD/USD band",
                }
            else:
                return {
                    "match_index": ctx["match_index"],
                    "verdict": "REJECT",
                    "max_acceptable_discrepancy": 0,
                    "actual_discrepancy": float(abs_diff),
                    "reasoning": (
                        f"Heuristic: implied rate {implied_rate:.4f} outside CAD/USD band "
                        f"({_VALIDATOR_FX_MIN:.2f}-{_VALIDATOR_FX_MAX:.2f})"
                    ),
                }

    # Same currency discrepancy
    # A transfer rail may take its handling fee out of the amount moved.
    if any(kw in desc_lower for kw in ["wise", "wire", "transfer", "interac"]):
        max_disc = float(WIRE_FEE_TOLERANCE)
    # Tips/restaurants: allow 20% for gratuity
    elif any(kw in desc_lower for kw in ["restaurant", "cafe", "bar", "food"]):
        max_disc = float(abs(txn_amt)) * 0.25
    # Tax differences
    elif float(abs_diff) <= float(abs(txn_amt)) * 0.15:
        max_disc = float(abs(txn_amt)) * 0.15
    # Default: allow up to $2
    else:
        max_disc = 2.0

    verdict = "ACCEPT" if float(abs_diff) <= max_disc else "REJECT"
    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_discrepancy": max_disc,
        "actual_discrepancy": float(abs_diff),
        "reasoning": f"Heuristic: max ${max_disc:.2f} discrepancy for this transaction type",
    }

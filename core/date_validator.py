"""LLM-based date gap validation for reconciliation matches.

After the reconciliation engine produces matches, this module validates
matches with suspicious date gaps (>7 days between transaction and document).
An LLM evaluates whether the date gap is reasonable given the transaction
context (vendor, amount, description, payment method, etc.) and returns
the maximum acceptable date window.

If the gap exceeds what the LLM deems acceptable, the match is rejected.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Only validate matches with date gaps exceeding this threshold (days)
DATE_GAP_VALIDATION_THRESHOLD = 7

VALIDATION_PROMPT = """You are a financial reconciliation agent. Your task is to determine if a date gap between a bank transaction and a proof-of-transaction document is acceptable.

BANK TRANSACTION:
- Date: {txn_date}
- Amount: {txn_amount} {txn_currency}
- Description: {txn_description}
- Account: {txn_account}

PROOF OF TRANSACTION (receipt/invoice):
- Vendor: {doc_vendor}
- Date: {doc_date}
- Total: {doc_total} {doc_currency}
- Filename: {doc_filename}

DATE GAP: {gap_days} days (transaction is {direction} the document)

Based on the nature of this transaction, determine the MAXIMUM acceptable date gap in days. Consider:
1. Subscription, cloud, hosting and software services can have 30-60 day gaps between charge and invoice
2. International payments (Wise, PayPal) typically clear within 1-5 business days
3. Credit card charges usually post within 1-3 days
4. Refunds can take 5-14 days
5. Physical store purchases post same day or next day
6. Online purchases typically 1-3 days
7. Pre-authorized recurring payments may show a billing date different from charge date by up to 5 days
8. Invoice-based payments may have 30-90 day terms

Respond ONLY with valid JSON (no markdown, no explanation):
{{"max_acceptable_days": <number>, "reasoning": "<brief explanation>", "verdict": "ACCEPT" or "REJECT"}}
"""


def validate_date_gaps(
    matches_with_context: list[dict],
) -> list[dict]:
    """Validate date gaps for a batch of matches using LLM.

    Args:
        matches_with_context: list of dicts with keys:
            - match_index: int (index into the matches list)
            - txn_date: date
            - txn_amount: str
            - txn_currency: str
            - txn_description: str
            - txn_account: str
            - doc_vendor: str
            - doc_date: date
            - doc_total: str
            - doc_currency: str
            - doc_filename: str
            - gap_days: int

    Returns:
        list of dicts with keys: match_index, verdict ("ACCEPT"/"REJECT"),
        max_acceptable_days, reasoning
    """
    if not matches_with_context:
        return []

    results = []
    for ctx in matches_with_context:
        try:
            result = _validate_single_match(ctx)
            results.append(result)
        except Exception as e:
            logger.warning(f"Date validation failed for match {ctx['match_index']}: {e}")
            # On LLM failure, default to a conservative heuristic
            results.append(_heuristic_fallback(ctx))

    return results


def build_validation_prompt(ctx: dict) -> str:
    """Fill VALIDATION_PROMPT for one match context.

    Separate from the LLM call so a test can assert the template formats. A
    placeholder the caller does not supply raises KeyError on every call, the
    caller catches it, and every verdict comes from the heuristic instead — a
    failure that is silent unless the formatting is checked on its own. Same
    parameterisation as ``core/match_validator.py`` — one ``{direction}``
    placeholder.
    """
    txn_after = ctx["txn_date"] > ctx["doc_date"]

    return VALIDATION_PROMPT.format(
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
        direction="after" if txn_after else "before",
    )


def _validate_single_match(ctx: dict) -> dict:
    """Call the LLM ladder to validate a single match's date gap.

    Provider order is local -> Gemini -> OpenAI, skipping any hosted provider
    with no key (see core/llm_sync.py). If every provider is unavailable the
    deterministic heuristic below answers instead — a reconciliation run must
    never fail because an LLM is down.
    """
    from core.llm_sync import call_json

    try:
        prompt = build_validation_prompt(ctx)
    except (KeyError, IndexError) as exc:
        # A template that cannot be filled is a bug in this file, not a
        # provider outage — logged at ERROR so it cannot sit behind the
        # heuristic unnoticed. tests/test_prompt_templates.py is the guard
        # that stops it reaching here at all.
        logger.error(
            "date_validator: VALIDATION_PROMPT failed to format (%s) — using heuristic", exc
        )
        return _heuristic_fallback(ctx)

    try:
        data = call_json(prompt, max_tokens=200, label="date_validator")
    except Exception as exc:
        logger.warning("Date validation LLM unavailable (%s) — using heuristic", exc)
        return _heuristic_fallback(ctx)

    try:
        max_days = int(data.get("max_acceptable_days", 14))
        model_verdict = data.get("verdict")
        reasoning = data.get("reasoning", "")
    except (AttributeError, TypeError, ValueError) as exc:
        # Model answered, but not with the shape the thresholds below need.
        logger.warning("Date validation returned unusable JSON (%s) — using heuristic", exc)
        return _heuristic_fallback(ctx)

    verdict = "ACCEPT" if ctx["gap_days"] <= max_days else "REJECT"
    # Override with the model's verdict if it explicitly says REJECT
    if model_verdict == "REJECT":
        verdict = "REJECT"

    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_days": max_days,
        "reasoning": reasoning,
    }


def _heuristic_fallback(ctx: dict) -> dict:
    """Rule-based fallback when LLM is unavailable."""
    desc_lower = ctx["txn_description"].lower()
    vendor_lower = (ctx.get("doc_vendor") or "").lower()

    # Subscriptions / cloud services: allow up to 45 days, because a plan is
    # billed on its own cycle and the bank charge can post well after the
    # invoice the engine is matching it to. The tokens are generic words a
    # recurring-service descriptor or vendor name carries; a specific vendor
    # whose descriptor carries none of them belongs in the vendor rules each
    # deployment configures, not in a list shipped with the engine.
    subscription_keywords = ["subscription", "subscript", "recurring", "monthly",
                             "annual", "renewal", "plan", "licen", "saas",
                             "software", "cloud", "hosting", "domain"]
    if any(kw in desc_lower or kw in vendor_lower for kw in subscription_keywords):
        max_days = 45
    # International wire / intermediary: allow up to 10 days
    elif any(kw in desc_lower for kw in ["wise", "paypal", "wire", "interac", "transfer"]):
        max_days = 10
    # Credit card typical: allow up to 7 days
    elif ctx["txn_account"] == "CreditCard":
        max_days = 7
    # Default: allow up to 14 days
    else:
        max_days = 14

    verdict = "ACCEPT" if ctx["gap_days"] <= max_days else "REJECT"
    return {
        "match_index": ctx["match_index"],
        "verdict": verdict,
        "max_acceptable_days": max_days,
        "reasoning": f"Heuristic: max {max_days} days for this transaction type",
    }

"""Phase 2 — vendor normalization and clustering.

Takes a list of ``TxnRow`` (anything that survived the Phase 1 rule
pre-pass) and groups them by a stable ``vendor_key``. Each cluster
carries enough context for Phase 4 to decide a category for the whole
group in one LLM call.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date
from typing import Any

from config.data_files import vendor_aliases

from .context import TxnRow, VendorCluster
from .llm_shapes import unwrap_item_list
from .prompts import VENDOR_NORMALIZATION_FALLBACK

logger = logging.getLogger(__name__)


# Canonical aliases: normalized regex → stable key. The table lives in
# ``config/vendor_aliases.yaml`` — edit that file, not this line, to add or
# change a vendor.
CANONICAL_ALIASES: list[tuple[re.Pattern, str]] = vendor_aliases()


# Tokens that never contribute meaning — strip before canonicalization.
# The place names are there because a card descriptor prints the branch or
# terminal city, and sometimes the province, after the merchant name: they are a
# location tag and never the party paid. One or two of the larger centres per
# region are listed, plus the province and territory codes, so a descriptor from
# any province loses its location the same way. ``on`` is deliberately absent
# from the province codes: as a standalone word it is the English preposition
# and would be stripped out of merchant names that contain it.
_NOISE_TOKEN_RE = re.compile(
    r"\b(ca|usa|us|inc|llc|corp|ltd|limited|co|mktp|transaction|purchase|payment|debit|credit"
    r"|ab|bc|mb|nb|nl|ns|nt|nu|pe|qc|sk|yt"
    r"|halifax|montreal|ottawa|toronto|winnipeg|regina|calgary|edmonton|vancouver)\b",
    re.IGNORECASE,
)
_REF_NUM_RE = re.compile(r"\b\d{6,}\b")
_STAR_CODE_RE = re.compile(r"\*[A-Z0-9]+")
_HASH_CODE_RE = re.compile(r"#\d+")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{2,4}\b")
_TRAILING_SINGLE_RE = re.compile(r"\s+[A-Z]\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")

# Payment-processor action prefixes (PayPal and card processors). These describe the
# *mechanism* of the transaction, not the vendor. PayPal statement rows look
# like "Express Checkout Payment Example Retailer" — without stripping the
# prefix, every PayPal merchant collapses into a single "express_checkout"
# cluster and the LLM assigns them all to "Bank & Payment Processing Fees".
#
# Order matters: longer/more specific patterns come first so they match before
# shorter ones (e.g. "PreApproved Payment Bill User" before "Payment").
#
# Descriptions that are ENTIRELY a processor action (no merchant tail) are
# handled below — they get a stable key like "paypal_fx" so they cluster into
# their own vendor rather than being over-stripped to an empty string.
_PROCESSOR_ACTION_PREFIX_RE = re.compile(
    r"^(?:"
    r"PreApproved\s+Payment\s+Bill\s+User"
    r"|Express\s+Checkout\s+Payment"
    r"|Website\s+Payment"
    r"|Mobile\s+Payment"
    r"|Subscription\s+Payment"
    r"|Recurring\s+Payment"
    r"|Shopping\s+Cart\s+Payment"
    r"|Virtual\s+Terminal\s+Payment"
    r"|Mass\s+Pay\s+Payment"
    r"|Payment\s+Refund"
    r"|Refund\s+from"
    r"|Pre-?Authorized\s+Payment\s+to"
    r"|Payment\s+to"
    r"|Payment\s+from"
    r"|Purchase\s+from"
    r")\s+",
    re.IGNORECASE,
)

# Processor-action descriptions that have NO merchant tail — they describe a
# PayPal/Wise internal action (funding, FX spread, withdrawal) and should be
# grouped under a single stable key per action type. Without this, each
# currency-conversion row gets normalized to "general_currency" which is a
# reasonable but accidental grouping; with this, the clustering is intentional.
_PROCESSOR_STANDALONE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^General\s+Currency\s+Conversion\b", re.I), "paypal_fx"),
    (re.compile(r"^Bank\s+Deposit\s+to\s+PP\s+Account\b", re.I), "paypal_funding"),
    (re.compile(r"^User\s+Initiated\s+Withdrawal\b", re.I), "paypal_withdrawal"),
    (re.compile(r"^Payment\s+Hold(?:ing)?\b", re.I), "paypal_hold"),
    (re.compile(r"^Reversal\s+of\s+General\s+Account\s+Hold\b", re.I), "paypal_hold"),
    (re.compile(r"^Account\s+Hold\s+for\s+Open\s+Authorization\b", re.I), "paypal_hold"),
]


def strip_processor_prefix(description: str) -> str:
    """Return ``description`` with any payment-processor action prefix removed.

    Shared by vendor-key normalization AND sample-description cleaning so
    the LLM never sees misleading prefixes like ``"Express Checkout Payment
    Example Retailer"`` — those confuse the vendor-decision phase into
    calling the cluster "Bank & Payment Processing Fees" even when the
    vendor_key is correctly ``"example_retailer"``.

    Leaves descriptions that have no prefix alone, and leaves standalone
    processor actions (``"General Currency Conversion"``) alone too, since
    stripping would leave an empty string.
    """
    if not description:
        return description
    for pattern, _ in _PROCESSOR_STANDALONE_PATTERNS:
        if pattern.search(description):
            return description
    return _PROCESSOR_ACTION_PREFIX_RE.sub("", description, count=1).strip() or description


def normalize_description(description: str) -> str:
    """Produce a stable vendor key from a raw transaction description.

    Applies regex scrubbing first, then matches against the canonical
    alias table. Returns empty string if nothing meaningful remains —
    callers should route those to the LLM fallback.
    """
    if not description:
        return ""

    text = description.strip()

    # Processor-standalone actions (PayPal FX, funding, withdrawal) — these
    # descriptions have no merchant tail and must cluster by action type.
    for pattern, key in _PROCESSOR_STANDALONE_PATTERNS:
        if pattern.search(text):
            return key

    # Strip payment-processor action prefix so the merchant name becomes the
    # leading tokens. Run BEFORE noise stripping because the prefix itself
    # contains noise words ("Payment", "Bill") that would otherwise leave
    # orphan tokens like "Express Checkout Example Retailer".
    text = _PROCESSOR_ACTION_PREFIX_RE.sub("", text, count=1).strip()

    text = _REF_NUM_RE.sub(" ", text)
    text = _STAR_CODE_RE.sub(" ", text)
    text = _HASH_CODE_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    text = _TRAILING_SINGLE_RE.sub(" ", text)
    text = _NOISE_TOKEN_RE.sub(" ", text)

    # First 2-3 meaningful tokens.
    tokens = [t for t in re.split(r"\s+", text) if t]
    candidate = " ".join(tokens[:3]).lower()

    # Canonical alias table.
    for pattern, key in CANONICAL_ALIASES:
        if pattern.search(candidate):
            return key

    # Fallback: slugify the first 1-2 tokens.
    slug = _NON_ALNUM_RE.sub("_", candidate).strip("_")
    slug = _MULTI_UNDERSCORE_RE.sub("_", slug)
    parts = [p for p in slug.split("_") if p]
    if not parts:
        return ""
    return "_".join(parts[:2])


def _summarize_proof_line_items(line_items_json: Any) -> str:
    """Return a short plain-text summary of line items for the LLM prompt.

    Accepts a JSON string, an already-parsed list, or ``None``. Returns at
    most ~200 chars so per-cluster serialisation stays compact.
    """
    if not line_items_json:
        return ""
    items = line_items_json
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            return items[:200]
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items[:5]:
        if isinstance(item, dict):
            desc = (item.get("description") or item.get("name") or "").strip()
            qty = item.get("quantity")
            amt = item.get("amount") or item.get("total") or item.get("price")
            piece = desc
            if qty and desc:
                piece = f"{qty}x {desc}"
            if amt:
                piece = f"{piece} ({amt})" if piece else str(amt)
            if piece:
                parts.append(piece)
        elif isinstance(item, str):
            parts.append(item)
    return "; ".join(parts)[:200]


def _proof_example(r: TxnRow) -> dict | None:
    """Condense a txn's proof dict into a compact example for the cluster."""
    if not r.proof:
        return None
    vendor = (r.proof.get("vendor") or "").strip()
    return {
        "txn_id": r.id,
        "txn_description": r.description,
        "source": r.proof.get("source"),
        "link_depth": r.proof.get("link_depth", 0),
        "proof_vendor": vendor or None,
        "proof_total": r.proof.get("total"),
        "proof_currency": r.proof.get("currency"),
        "proof_invoice_number": r.proof.get("invoice_number"),
        "proof_line_items": _summarize_proof_line_items(r.proof.get("line_items_json")),
    }


def _group_by_vendor_key(
    txns: list[TxnRow],
    key_by_id: dict[int, str],
) -> list[VendorCluster]:
    """Materialise VendorCluster objects from a txn_id→vendor_key map."""
    buckets: dict[str, list[TxnRow]] = defaultdict(list)
    for t in txns:
        key = key_by_id.get(t.id)
        if not key:
            continue
        buckets[key].append(t)

    clusters: list[VendorCluster] = []
    for key, rows in buckets.items():
        totals: dict[str, float] = defaultdict(float)
        dates: list[date] = []
        samples: list[str] = []
        sample_ids: list[int] = []
        ids: list[int] = []
        notes: list[str] = []
        proof_examples: list[dict] = []
        proof_count = 0
        for r in rows:
            totals[r.currency] = totals.get(r.currency, 0.0) + abs(r.amount)
            dates.append(r.date_posted)
            if len(samples) < 5:
                # Strip processor action prefixes so the LLM sees the
                # merchant name (e.g. "Example Retailer" instead of
                # "Express Checkout Payment Example Retailer").
                samples.append(strip_processor_prefix(r.description))
                sample_ids.append(r.id)
            # Collect distinct user notes (authoritative categorization signal).
            note = (getattr(r, "note", None) or "").strip()
            if note and note not in notes and len(notes) < 5:
                notes.append(note)
            ids.append(r.id)
            if r.proof:
                proof_count += 1
                if len(proof_examples) < 5:
                    example = _proof_example(r)
                    if example:
                        proof_examples.append(example)
        clusters.append(
            VendorCluster(
                vendor_key=key,
                count=len(rows),
                total_by_currency=dict(totals),
                date_range=(min(dates), max(dates)),
                sample_descriptions=samples,
                sample_txn_ids=sample_ids,
                transaction_ids=ids,
                sample_notes=notes,
                proof_examples=proof_examples,
                proof_coverage=round(proof_count / max(len(rows), 1), 3),
            )
        )
    clusters.sort(key=lambda c: c.primary_amount_cad_approx, reverse=True)
    return clusters


def cluster_transactions(
    txns: list[TxnRow],
    llm_call: callable | None = None,
) -> tuple[list[VendorCluster], dict[int, str]]:
    """Normalize, group, and return clusters plus txn_id→vendor_key map.

    ``llm_call`` is the shared ``_call_llm`` helper. If provided, it's
    used as a fallback for descriptions that regex alone can't normalize.
    When not provided (unit tests), unnormalizable descriptions get a
    deterministic slug fallback — no LLM round trip.
    """
    if not txns:
        return [], {}

    key_by_id: dict[int, str] = {}
    unresolved: list[TxnRow] = []
    for t in txns:
        # Matched-receipt vendor beats everything else. When a transaction
        # has a direct or linked-chain proof (a reconciled receipt), the
        # proof vendor IS the merchant — the description is noise
        # ("General Currency Conversion", "Bank Deposit to PP Account",
        # "Express Checkout Payment X"). Re-keying by proof vendor splits
        # the paypal_fx / paypal_funding buckets into per-merchant clusters
        # so Phase 4 can categorize each one correctly instead of lumping
        # them all under "Bank & Payment Processing Fees".
        proof_vendor = (t.proof or {}).get("vendor") if t.proof else None
        if proof_vendor:
            pk = normalize_description(proof_vendor)
            if pk:
                key_by_id[t.id] = pk
                continue

        # No approved proof: the DESCRIPTION decides the key, on every run.
        #
        # The stored ``vendor_key`` is deliberately NOT trusted here. It is
        # written by a previous run, and a previous run may have derived it from
        # a proof that has since been rejected: the row would then keep the
        # vendor key of an unrelated supplier — and the category that key
        # implies — long after the weak match that produced it was rejected.
        # Recomputing from the description makes a stale identity impossible to
        # inherit: the key is a pure function of what the statement line says
        # plus what is proven today.
        k = normalize_description(t.description)
        if k:
            key_by_id[t.id] = k
            continue

        # The description carries no vendor at all ("USD 41.25"). A stored key on
        # such a row can only have come from the LLM normalization fallback on an
        # earlier run — never from a proof, because a proof would have re-keyed it
        # above — so reusing it is safe and saves the round trip.
        if t.vendor_key:
            key_by_id[t.id] = t.vendor_key
            continue
        unresolved.append(t)

    if unresolved and llm_call is not None:
        try:
            descriptions = [t.description for t in unresolved]
            prompt = VENDOR_NORMALIZATION_FALLBACK.format(
                descriptions_json=json.dumps(descriptions, ensure_ascii=False)
            )
            result = llm_call(prompt)
            for item in unwrap_item_list(result, "vendor_key"):
                try:
                    idx = int(item.get("index"))
                    key = str(item.get("vendor_key", "")).strip().lower()
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(unresolved) and key:
                    key_by_id[unresolved[idx].id] = key
        except Exception as exc:
            logger.warning("LLM vendor normalization fallback failed: %s", exc)

    # Any still-unresolved rows get a crude fallback so they still cluster.
    for t in unresolved:
        if t.id in key_by_id:
            continue
        slug = _NON_ALNUM_RE.sub("_", t.description.lower()).strip("_")
        key_by_id[t.id] = (slug.split("_", 1)[0] or "unknown")[:32]

    clusters = _group_by_vendor_key(txns, key_by_id)
    return clusters, key_by_id

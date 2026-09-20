"""Cross-statement transaction linking: auto-detect transfer pairs across bank statements.

Runs as a pre-processing step before document reconciliation. Identifies credit card
payments, internal transfers, FX conversions, and Wise funding transfers by matching
transactions across different accounts using amount, date, keyword, and account pair signals.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from core.amount_match import FX_CAD_USD_MAX, FX_CAD_USD_MIN
from models.transaction import Transaction, account_str

logger = logging.getLogger(__name__)

# ── Scoring weights ────────────────────────────────────────────────────
WEIGHT_AMOUNT = 0.45
WEIGHT_DATE = 0.25
WEIGHT_KEYWORD = 0.20
WEIGHT_ACCOUNT_PAIR = 0.10

AUTO_APPROVE_THRESHOLD = 0.92
REVIEW_THRESHOLD = 0.55

# ── Certainty: a pair that is pinned by everything except the calendar ──
#
# The date score exists to discriminate between *competing* candidates, not to
# express doubt about a pair that is already pinned from every other direction.
# When the two legs are the same sum to the cent in the same currency, point in
# opposite directions, sit in the account pair the link type is defined for, and
# each side's description independently names the movement (a SOURCE keyword on
# one, the matching TARGET keyword on the other), there is no second reading of
# those rows — a credit-card payment that clears three business days after it
# leaves the chequing account is the same movement of money, not a weaker one.
#
# So such a pair is floored at the auto-approve line. Everything about the rule
# is deliberately narrow:
#   * exact amount only — a pair whose amounts differ (any FX conversion, any
#     cent of variance) never reaches the floor and stays in review;
#   * both sides must carry compatible keywords — a one-sided keyword hit does
#     not qualify;
#   * the account pair must be the one the link type is defined for; and
#   * the pair must still fall inside DATE_WINDOWS for its type.
CERTAIN_LINK_TYPES: frozenset[str] = frozenset({
    "CREDIT_CARD_PAYMENT", "INTERNAL_TRANSFER", "FX_CONVERSION", "WISE_TRANSFER",
})

# ── And the mirror of it: a mismatch is never certain ──────────────────
#
# Two legs of one movement of money between two accounts in the SAME currency
# are the same number. When they are not — a cent, thirty cents, a dollar —
# something is unaccounted for (a fee taken on the way, a wrong pairing, a
# mis-parsed figure), and the other signals must not be allowed to carry the
# pair over the auto-approve line on their own — a near-perfect date, keyword
# and account-pair score can otherwise outweigh a gap of a few cents. Such a
# pair is still proposed, as a review, which is what it is. Cross-currency
# pairs are untouched — their two numbers differ by construction and the FX
# rate is what reconciles them.
AMOUNT_MISMATCH_CEILING = 0.85

# Link types where ONE transaction may legitimately anchor MANY counterparts.
# A refund is the case that forces it: a single bulk charge is returned item by
# item, so the charge faces N credits. Every other type here is a single
# movement of one sum of money between two accounts and stays strictly 1:1 —
# a credit-card payment that "paid" two different statements would be a
# reconciliation error, not a fan-out.
FAN_OUT_LINK_TYPES: frozenset[str] = frozenset({"REFUND"})

# A fan-out group's legs may not add up to more than the anchor they offset,
# with a cent of slack for rounding. Five $100 credits cannot all be refunds of
# one $300 charge; letting them link anyway would invent evidence.
FAN_OUT_TOLERANCE = Decimal("0.01")

# ── Valid account pairs for each link type ─────────────────────────────
VALID_ACCOUNT_PAIRS: dict[str, set[tuple[str, str]]] = {
    "CREDIT_CARD_PAYMENT": {("CAD", "CreditCard"), ("USD", "CreditCard")},
    "INTERNAL_TRANSFER": {("CAD", "USD"), ("USD", "CAD")},
    "FX_CONVERSION": {("CAD", "USD"), ("USD", "CAD")},
    "WISE_TRANSFER": {("CAD", "Wise"), ("USD", "Wise")},
    "REFUND": {
        ("CreditCard", "CAD"), ("CreditCard", "USD"),
        ("Wise", "CAD"), ("Wise", "USD"),
    },
    "OTHER": set(),  # no restriction
}

# Date windows per link type (max days)
DATE_WINDOWS: dict[str, int] = {
    "CREDIT_CARD_PAYMENT": 7,
    "INTERNAL_TRANSFER": 4,
    "FX_CONVERSION": 3,
    "WISE_TRANSFER": 10,
    "REFUND": 21,
    "OTHER": 14,
}


# ── Data Classes ───────────────────────────────────────────────────────

@dataclass
class TransactionLinkCandidate:
    source_transaction_id: int
    target_transaction_id: int
    link_type: str
    confidence_score: float
    amount_score: float
    date_score: float
    keyword_score: float
    account_pair_score: float
    explanation: str
    exchange_rate: str | None = None
    amount_variance: str | None = None
    # Legs of one fan-out group share a chain_id; position orders them.
    # Declared fields rather than monkey-patched attributes so the value
    # actually survives to the persistence layer.
    chain_id: str | None = None
    chain_position: int = 0


@dataclass
class Chain:
    chain_id: str
    links: list[TransactionLinkCandidate]
    total_confidence: float


@dataclass
class TransactionLinkerResult:
    links: list[TransactionLinkCandidate] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)
    unlinked_transfers: list[int] = field(default_factory=list)
    auto_approved: list[TransactionLinkCandidate] = field(default_factory=list)
    pending_review: list[TransactionLinkCandidate] = field(default_factory=list)


@dataclass
class KeywordEntry:
    pattern: re.Pattern
    transfer_type: str
    side: str  # SOURCE or TARGET
    account_types: list[str]
    priority: int


# ── Keyword loading ────────────────────────────────────────────────────

def _load_keywords(config_path: Path | None = None) -> list[KeywordEntry]:
    """Load transfer keyword patterns from YAML config."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config" / "transfer_keywords.yaml"
    if not config_path.exists():
        logger.warning("Transfer keywords config not found: %s", config_path)
        return []
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    entries = []
    for item in data.get("entries", []):
        try:
            entries.append(KeywordEntry(
                pattern=re.compile(item["pattern"], re.IGNORECASE),
                transfer_type=item["transfer_type"],
                side=item["side"],
                account_types=item["account_types"],
                priority=item.get("priority", 50),
            ))
        except (re.error, KeyError) as exc:
            logger.warning("Skipping invalid keyword entry: %s — %s", item, exc)
    return entries


def _match_keywords(
    description: str, account: str, keywords: list[KeywordEntry]
) -> list[KeywordEntry]:
    """Return all keyword entries matching a transaction's description and account."""
    matches = []
    for kw in keywords:
        if account in kw.account_types and kw.pattern.search(description):
            matches.append(kw)
    return matches


# ── FX rate extraction ─────────────────────────────────────────────────

def _extract_fx_rate(description: str) -> float | None:
    """Extract embedded FX rate from BMO transaction descriptions."""
    # Bank format: "AT1.2345" or "@1.2345"
    m = re.search(r"AT(\d+\.\d{2,6})", description)
    if m:
        rate = float(m.group(1))
        if 0.5 < rate < 3.0:
            return rate
    m = re.search(r"@(\d+\.\d{2,10})", description)
    if m:
        rate = float(m.group(1))
        if 0.5 < rate < 3.0:
            return rate
    return None


# ── Scoring functions ──────────────────────────────────────────────────

def _score_amount(
    src: Transaction, tgt: Transaction, fx_rate: float | None = None
) -> tuple[float, str | None, str | None]:
    """Score amount match. Returns (score, exchange_rate_str, amount_variance_str)."""
    src_amt = abs(src.amount)
    tgt_amt = abs(tgt.amount)
    exchange_rate_str = None
    amount_variance_str = None

    # Same currency
    if src.currency == tgt.currency:
        diff = abs(src_amt - tgt_amt)
        if diff == 0:
            return 1.0, None, None
        elif diff <= Decimal("0.01"):
            return 0.98, None, None
        elif diff <= Decimal("0.50"):
            amount_variance_str = f"{diff} {src.currency} difference"
            return 0.85, None, amount_variance_str
        elif diff <= Decimal("2.00"):
            amount_variance_str = f"{diff} {src.currency} difference"
            return 0.60, None, amount_variance_str
        return 0.0, None, None

    # Cross-currency
    if fx_rate is not None and fx_rate > 0:
        exchange_rate_str = str(fx_rate)
        # Check if amounts are consistent with the rate
        if src.currency == "CAD" and tgt.currency == "USD":
            expected_usd = src_amt / Decimal(str(fx_rate))
            pct_diff = abs(expected_usd - tgt_amt) / tgt_amt if tgt_amt else Decimal("1")
        elif src.currency == "USD" and tgt.currency == "CAD":
            expected_cad = src_amt * Decimal(str(fx_rate))
            pct_diff = abs(expected_cad - tgt_amt) / tgt_amt if tgt_amt else Decimal("1")
        else:
            pct_diff = Decimal("1")

        if pct_diff <= Decimal("0.005"):
            return 0.95, exchange_rate_str, None
        elif pct_diff <= Decimal("0.02"):
            amount_variance_str = f"{float(pct_diff)*100:.1f}% FX variance"
            return 0.80, exchange_rate_str, amount_variance_str

    # Cross-currency with no rate printed on either description: the implied
    # ratio has to land inside the engine's CAD/USD band (core/amount_match.py),
    # the same band the receipt matcher accepts a conversion on.
    if {src.currency, tgt.currency} == {"CAD", "USD"}:
        if src_amt > 0 and tgt_amt > 0:
            ratio = float(max(src_amt, tgt_amt) / min(src_amt, tgt_amt))
            if float(FX_CAD_USD_MIN) <= ratio <= float(FX_CAD_USD_MAX):
                return 0.78, None, f"Implied FX rate ~{ratio:.4f}"

    return 0.0, None, None


def _score_date(src: Transaction, tgt: Transaction, link_type: str) -> float:
    """Score date proximity based on link type."""
    days = abs((src.date_posted - tgt.date_posted).days)

    scores: dict[str, list[tuple[int, float]]] = {
        "CREDIT_CARD_PAYMENT": [
            (0, 1.0), (1, 0.95), (2, 0.85), (3, 0.70), (5, 0.40), (7, 0.15),
        ],
        "FX_CONVERSION": [
            (0, 1.0), (1, 0.90), (2, 0.60), (3, 0.30),
        ],
        "WISE_TRANSFER": [
            (0, 1.0), (1, 0.95), (2, 0.90), (3, 0.80), (5, 0.60), (7, 0.40), (10, 0.15),
        ],
        "INTERNAL_TRANSFER": [
            (0, 1.0), (1, 0.90), (2, 0.70), (3, 0.40),
        ],
        "REFUND": [
            (0, 1.0), (3, 0.90), (7, 0.75), (14, 0.50), (21, 0.25),
        ],
    }

    tiers = scores.get(link_type, scores["INTERNAL_TRANSFER"])
    for max_days, score in tiers:
        if days <= max_days:
            return score
    return 0.0


def _score_keywords(
    src: Transaction,
    tgt: Transaction,
    keywords: list[KeywordEntry],
) -> tuple[float, str]:
    """Score keyword matches. Returns (score, inferred_link_type)."""
    src_matches = _match_keywords(src.description, account_str(src.account), keywords)
    tgt_matches = _match_keywords(tgt.description, account_str(tgt.account), keywords)

    if not src_matches and not tgt_matches:
        return 0.15, "OTHER"

    # Find compatible pairs (SOURCE+TARGET with same transfer_type)
    best_type = "OTHER"
    best_priority = 0
    paired = False

    for sm in src_matches:
        for tm in tgt_matches:
            if sm.transfer_type == tm.transfer_type and sm.side != tm.side:
                paired = True
                combined_priority = sm.priority + tm.priority
                if combined_priority > best_priority:
                    best_priority = combined_priority
                    best_type = sm.transfer_type

    if paired:
        return 1.0, best_type

    # One side matches
    all_matches = src_matches + tgt_matches
    if all_matches:
        best = max(all_matches, key=lambda m: m.priority)
        return 0.60, best.transfer_type

    return 0.15, "OTHER"


def _score_account_pair(
    src: Transaction, tgt: Transaction, link_type: str
) -> float:
    """Score whether the account pair is valid for the link type."""
    pair = (account_str(src.account), account_str(tgt.account))
    reverse_pair = (account_str(tgt.account), account_str(src.account))

    valid = VALID_ACCOUNT_PAIRS.get(link_type, set())
    if pair in valid or reverse_pair in valid:
        return 1.0

    # Check if valid for a different link type
    for lt, pairs in VALID_ACCOUNT_PAIRS.items():
        if lt != link_type and (pair in pairs or reverse_pair in pairs):
            return 0.50

    return 0.0


# ── Main pipeline ──────────────────────────────────────────────────────

def _is_transfer_candidate(txn: Transaction, keywords: list[KeywordEntry]) -> bool:
    """Check if a transaction is a potential transfer based on keywords or category."""
    # Category-based signal — normalized match so it catches the fixed canonical
    # names ("Credit card payment", "Internal transfer") and any other spelling
    # the alias table maps onto them.
    from core.transfer_exclusion import is_transfer_category

    if is_transfer_category(txn.category):
        return True

    # Check keyword matches
    if _match_keywords(txn.description, account_str(txn.account), keywords):
        return True

    return False


def _pair_key(a: int, b: int) -> tuple[int, int]:
    """Direction-free identity of a pair, so a rejection sticks either way round."""
    return (a, b) if a <= b else (b, a)


def _generate_candidate_pairs(
    candidates: list[Transaction],
    rejected_pairs: set[tuple[int, int]] | None = None,
) -> list[tuple[Transaction, Transaction]]:
    """Generate all valid cross-account, sign-compatible pairs.

    ``rejected_pairs`` holds pairs the user has already rejected (either
    direction); they are dropped here so a rejected link is never re-proposed —
    the same gate the receipt matcher applies at candidate generation.
    """
    # Normalized on the way in, so a caller that kept a pair in the order the
    # link row stored it is still honoured.
    rejected = {_pair_key(a, b) for a, b in (rejected_pairs or set())}
    pairs = []
    for i, src in enumerate(candidates):
        for tgt in candidates[i + 1:]:
            # Must be different accounts
            if src.account == tgt.account:
                continue
            # Sign compatibility: one debit, one credit
            if src.transaction_type == tgt.transaction_type:
                continue
            if _pair_key(src.id, tgt.id) in rejected:
                continue
            pairs.append((src, tgt))
    return pairs


def run_transaction_linking(
    transactions: list[Transaction],
    existing_links: list[dict] | None = None,
    keyword_config_path: Path | None = None,
    rejected_pairs: set[tuple[int, int]] | None = None,
) -> TransactionLinkerResult:
    """Run the cross-statement transaction linking pipeline.

    Args:
        transactions: Every transaction the linker may consider. Callers hand
            over the whole linkable pool (UNMATCHED *and* IGNORED rows with no
            active link) — a row that was stamped IGNORED because it needs no
            receipt is exactly the kind of row that is one leg of a transfer.
        existing_links: Already-linked transaction IDs to skip.
        keyword_config_path: Optional path to transfer_keywords.yaml.
        rejected_pairs: Pairs the user has already rejected (either direction);
            never re-proposed.

    Returns:
        TransactionLinkerResult with detected links and chains.

    The result depends only on the input, never on the order the statements
    happened to be reconciled in: ties in the greedy assignment are broken by
    input order (``sort`` is stable), so a caller that hands over a stably
    ordered pool gets the same links on every run.
    """
    result = TransactionLinkerResult()

    if not transactions:
        return result

    # Build the set of transactions that may not take another link.
    #
    # Role matters now that groups exist: a transaction that already anchors a
    # fan-out group (it is the SOURCE of a refund link) is still eligible for
    # further legs — that is the whole point of one-to-many. A transaction that
    # is already someone's TARGET is spoken for and stays excluded.
    already_linked: set[int] = set()
    open_anchors: set[int] = set()
    if existing_links:
        for link in existing_links:
            source_id = link.get("source_transaction_id", -1)
            already_linked.add(link.get("target_transaction_id", -1))
            if link.get("link_type") in FAN_OUT_LINK_TYPES:
                open_anchors.add(source_id)
            else:
                already_linked.add(source_id)
        # A transaction that is somebody's target can never also be an anchor.
        open_anchors -= already_linked

    # Also skip transactions already LINKED or MATCHED — except open anchors,
    # which are LINKED precisely because they already carry a leg.
    skip_statuses = {"LINKED", "MATCHED"}

    # 1. Load keywords
    keywords = _load_keywords(keyword_config_path)

    # 2. Filter to transfer candidates
    candidates = [
        t for t in transactions
        if t.id is not None
        and t.id not in already_linked
        and (t.status.value not in skip_statuses or t.id in open_anchors)
        and _is_transfer_candidate(t, keywords)
    ]

    if not candidates:
        logger.info("No transfer candidates found among %d transactions", len(transactions))
        return result

    logger.info("Found %d transfer candidates from %d transactions", len(candidates), len(transactions))

    # 3. Generate candidate pairs
    pairs = _generate_candidate_pairs(candidates, rejected_pairs)
    logger.info("Generated %d candidate pairs", len(pairs))

    # 4. Score each pair
    scored: list[TransactionLinkCandidate] = []
    for src, tgt in pairs:
        # Determine link type from keywords first
        kw_score, inferred_type = _score_keywords(src, tgt, keywords)

        # Try to extract FX rate
        fx_rate = _extract_fx_rate(src.description) or _extract_fx_rate(tgt.description)

        # Override type if FX rate found and currencies differ
        if fx_rate and src.currency != tgt.currency and inferred_type not in ("WISE_TRANSFER",):
            inferred_type = "FX_CONVERSION"

        # Score components
        amt_score, exchange_rate_str, variance_str = _score_amount(src, tgt, fx_rate)
        date_score = _score_date(src, tgt, inferred_type)
        acct_score = _score_account_pair(src, tgt, inferred_type)

        # Composite score
        composite = (
            WEIGHT_AMOUNT * amt_score
            + WEIGHT_DATE * date_score
            + WEIGHT_KEYWORD * kw_score
            + WEIGHT_ACCOUNT_PAIR * acct_score
        )

        # Skip if below review threshold
        if composite < REVIEW_THRESHOLD:
            continue

        # Check date window
        days = abs((src.date_posted - tgt.date_posted).days)
        max_days = DATE_WINDOWS.get(inferred_type, 14)
        if days > max_days:
            continue

        # A pair pinned by amount, keywords and account pair is certain; only
        # the calendar separates it from a same-day pair. See CERTAIN_LINK_TYPES.
        if (
            inferred_type in CERTAIN_LINK_TYPES
            and amt_score == 1.0
            and kw_score == 1.0
            and acct_score == 1.0
        ):
            composite = max(composite, AUTO_APPROVE_THRESHOLD)
        elif src.currency == tgt.currency and amt_score < 1.0:
            # Same currency, different number: review, never auto-approve.
            composite = min(composite, AMOUNT_MISMATCH_CEILING)

        # Ensure source is the debit side (money leaving)
        if src.transaction_type == "CREDIT" and tgt.transaction_type == "DEBIT":
            src, tgt = tgt, src

        explanation = _build_explanation(src, tgt, inferred_type, amt_score, date_score, kw_score)

        scored.append(TransactionLinkCandidate(
            source_transaction_id=src.id,
            target_transaction_id=tgt.id,
            link_type=inferred_type,
            confidence_score=round(composite, 4),
            amount_score=round(amt_score, 4),
            date_score=round(date_score, 4),
            keyword_score=round(kw_score, 4),
            account_pair_score=round(acct_score, 4),
            explanation=explanation,
            exchange_rate=exchange_rate_str if fx_rate else None,
            amount_variance=variance_str,
        ))

    # 5. Greedy assignment, highest score first.
    #
    #    Every transaction takes at most one link, with one exception: an
    #    anchor of a FAN_OUT_LINK_TYPES group stays open for further legs of
    #    the SAME group. Without that exception a bulk charge refunded in
    #    three instalments keeps whichever credit scored highest and the other
    #    two are dropped on the floor.
    scored.sort(key=lambda c: c.confidence_score, reverse=True)
    txn_by_id = {t.id: t for t in transactions if t.id is not None}
    used: set[int] = set()                    # can take no further links
    anchors: dict[int, str] = {}              # anchor id -> its group's link_type
    anchor_chain: dict[int, str] = {}         # anchor id -> shared chain_id
    anchor_claimed: dict[int, Decimal] = {}   # anchor id -> legs booked so far

    for candidate in scored:
        src = candidate.source_transaction_id
        tgt = candidate.target_transaction_id
        # A target is always consumed, so it can never also anchor a group.
        if tgt in used or tgt in anchors:
            continue
        if src in used:
            continue
        if src in anchors and anchors[src] != candidate.link_type:
            continue

        fans_out = candidate.link_type in FAN_OUT_LINK_TYPES
        if fans_out:
            anchor_txn = txn_by_id.get(src)
            leg_txn = txn_by_id.get(tgt)
            if anchor_txn is None or leg_txn is None:
                continue
            # Refund legs may not add up to more than the charge they offset —
            # but only when the sums are actually comparable. REFUND's valid
            # account pairs are cross-currency by definition (CreditCard<->CAD,
            # Wise<->USD), and a USD 220 leg is not "more than" a CAD 300
            # anchor. Comparing raw magnitudes across currencies would reject
            # perfectly good refunds; without an FX rate the honest move is to
            # let the link stand and leave the arithmetic to the reviewer.
            if leg_txn.currency == anchor_txn.currency:
                claimed = anchor_claimed.get(src, Decimal("0")) + abs(leg_txn.amount)
                if claimed > abs(anchor_txn.amount) + FAN_OUT_TOLERANCE:
                    continue
                anchor_claimed[src] = claimed
            anchors[src] = candidate.link_type
            anchor_chain.setdefault(src, str(uuid.uuid4()))
            candidate.chain_id = anchor_chain[src]
            candidate.chain_position = sum(
                1 for link in result.links
                if link.chain_id == anchor_chain[src]
            )
        else:
            used.add(src)

        result.links.append(candidate)
        used.add(tgt)

    # 6. Build chains: detect A→B + B→C patterns, assign shared chain_id
    _build_chains(result)

    # 7. Classify
    for link in result.links:
        if link.confidence_score >= AUTO_APPROVE_THRESHOLD:
            result.auto_approved.append(link)
        else:
            result.pending_review.append(link)

    # Track unlinked transfer candidates. Fan-out anchors never enter `used`
    # (they stay open for more legs), so add them back before subtracting.
    linked_ids = used | set(anchors)
    for c in candidates:
        if c.id not in linked_ids:
            result.unlinked_transfers.append(c.id)

    logger.info(
        "Transaction linking complete: %d links (%d auto-approved, %d pending review), "
        "%d chains, %d unlinked transfers",
        len(result.links), len(result.auto_approved), len(result.pending_review),
        len(result.chains), len(result.unlinked_transfers),
    )

    return result


def _build_explanation(
    src: Transaction, tgt: Transaction, link_type: str,
    amt_score: float, date_score: float, kw_score: float,
) -> str:
    """Build a human-readable explanation for a link."""
    type_labels = {
        "CREDIT_CARD_PAYMENT": "Credit card payment",
        "INTERNAL_TRANSFER": "Internal bank transfer",
        "FX_CONVERSION": "Foreign exchange conversion",
        "WISE_TRANSFER": "Wise account transfer",
        "REFUND": "Refund/credit",
        "OTHER": "Cross-statement transfer",
    }
    label = type_labels.get(link_type, link_type)
    days = abs((src.date_posted - tgt.date_posted).days)
    return (
        f"{label}: {account_str(src.account)} ${abs(src.amount)} {src.currency} "
        f"-> {account_str(tgt.account)} ${abs(tgt.amount)} {tgt.currency}"
        f"{f' ({days}d apart)' if days > 0 else ''}"
    )


def _build_chains(result: TransactionLinkerResult) -> None:
    """Detect A→B + B→C patterns and assign shared chain_ids."""
    if not result.links:
        return

    # Build adjacency: txn_id -> list of links
    adjacency: dict[int, list[TransactionLinkCandidate]] = {}
    for link in result.links:
        adjacency.setdefault(link.source_transaction_id, []).append(link)
        adjacency.setdefault(link.target_transaction_id, []).append(link)

    # Find connected components via BFS
    visited: set[int] = set()
    for link in result.links:
        start = link.source_transaction_id
        if start in visited:
            continue

        # BFS to find all connected transactions
        component_links: list[TransactionLinkCandidate] = []
        queue = [start]
        component_txns: set[int] = set()
        while queue:
            txn_id = queue.pop(0)
            if txn_id in component_txns:
                continue
            component_txns.add(txn_id)
            visited.add(txn_id)
            for adj_link in adjacency.get(txn_id, []):
                if adj_link not in component_links:
                    component_links.append(adj_link)
                other = (adj_link.target_transaction_id
                         if adj_link.source_transaction_id == txn_id
                         else adj_link.source_transaction_id)
                if other not in component_txns:
                    queue.append(other)

        if len(component_links) >= 2:
            # This is a chain (multiple links in the same connected component).
            # A fan-out group already minted its own chain_id during greedy
            # assignment — reuse it rather than renumbering a group that is
            # already coherent.
            existing = next(
                (link.chain_id for link in component_links if link.chain_id), None
            )
            chain_id = existing or str(uuid.uuid4())
            for i, chain_link in enumerate(component_links):
                chain_link.chain_id = chain_id
                if existing is None:
                    chain_link.chain_position = i

            total_conf = sum(cl.confidence_score for cl in component_links) / len(component_links)
            result.chains.append(Chain(
                chain_id=chain_id,
                links=component_links,
                total_confidence=round(total_conf, 4),
            ))

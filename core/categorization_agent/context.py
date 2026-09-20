"""Dataclasses passed between categorization-agent phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

CategoryKind = Literal["income", "expense", "transfer", "tax"]
Confidence = Literal["high", "medium", "low"]
Scope = Literal["all", "uncategorized"]


@dataclass
class BusinessProfile:
    company_name: str | None
    industry: str | None
    business_summary: str | None
    province: str | None

    def as_prompt_lines(self) -> str:
        return (
            f"Name: {self.company_name or 'Unknown'}\n"
            f"Industry: {self.industry or 'Unknown'}\n"
            f"Province: {self.province or 'Unknown'}\n"
            f"Summary: {self.business_summary or 'Not provided'}"
        )


@dataclass
class UserCategory:
    id: int | None
    name: str
    definition: str | None
    umbrella_for: str | None
    example_vendors: list[str] = field(default_factory=list)
    kind: CategoryKind | None = None
    is_system: bool = False


@dataclass
class VendorCacheEntry:
    vendor_key: str
    category_name: str | None
    confidence: Confidence | None
    reasoning: str | None
    sample_desc: str | None


@dataclass
class TxnRow:
    id: int
    date_posted: date
    amount: float
    currency: str
    account: str
    description: str
    category: str | None
    category_confirmed_by_user: bool
    vendor_key: str | None = None
    # Free-text note the USER left on this transaction. A high-priority,
    # authoritative signal about what the transaction is — the categorization
    # phases must read and comprehend it (see prompts), especially when the
    # bank description is ambiguous.
    note: str | None = None
    # Matched-document proof: vendor, total, line items, invoice_number, etc.
    # Populated for txns that have a direct reconciliation match OR that
    # are reachable through active transaction_links from a matched sibling.
    # ``proof["source"]`` is ``"direct"`` or ``"linked"``; ``link_depth`` is
    # 0 for direct, >=1 for proof inherited via the link chain.
    proof: dict | None = None


@dataclass
class ConfirmedExample:
    txn_id: int
    description: str
    amount: float
    currency: str
    category: str


@dataclass
class CategorizationContext:
    user_id: str
    scope: Scope
    business_profile: BusinessProfile
    prior_taxonomy: list[UserCategory]
    prior_vendor_cache: dict[str, VendorCacheEntry]
    confirmed_examples: list[ConfirmedExample]
    transactions: list[TxnRow]


@dataclass
class VendorCluster:
    vendor_key: str
    count: int
    total_by_currency: dict[str, float]
    date_range: tuple[date, date]
    sample_descriptions: list[str]
    sample_txn_ids: list[int]
    transaction_ids: list[int]
    # Distinct user notes across this cluster's transactions (authoritative
    # categorization signal — see prompts). Empty when no note was left.
    sample_notes: list[str] = field(default_factory=list)
    # Aggregated proof evidence for this cluster. ``proof_examples`` is up
    # to 5 compact dicts {vendor, total, currency, line_items_excerpt}
    # describing matched receipts. ``proof_coverage`` is the fraction of
    # txns in this cluster that have any proof (direct or linked).
    proof_examples: list[dict] = field(default_factory=list)
    proof_coverage: float = 0.0

    @property
    def primary_amount_cad_approx(self) -> float:
        """Approximate CAD-equivalent total, used only to rank clusters.

        The factors below are static ordering weights, not an FX conversion,
        and no ledger figure is derived from this property.
        """
        return (
            self.total_by_currency.get("CAD", 0.0)
            + self.total_by_currency.get("USD", 0.0) * 1.35
            + self.total_by_currency.get("EUR", 0.0) * 1.45
            + self.total_by_currency.get("GBP", 0.0) * 1.70
        )


@dataclass
class VendorDecision:
    vendor_key: str
    category_name: str
    confidence: Confidence
    reasoning: str
    needs_refinement: bool = False


@dataclass
class CategorizationRunResult:
    status: Literal["complete", "error"]
    total: int
    rule_locked: int
    vendors_categorized: int
    txns_refined: int
    categories_final: int
    llm_calls: int
    taxonomy_diff: dict
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

    def as_summary(self) -> dict:
        return {
            "status": self.status,
            "total": self.total,
            "rule_locked": self.rule_locked,
            "vendors_categorized": self.vendors_categorized,
            "txns_refined": self.txns_refined,
            "categories_final": self.categories_final,
            "llm_calls": self.llm_calls,
            "taxonomy_diff": self.taxonomy_diff,
            "error": self.error,
        }

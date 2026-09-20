"""Business-aware, multi-phase categorization agent.

Single entry point for both reconciliation's auto-categorize step and the
Recategorize button. Runs seven phases (context load, rule pre-pass,
vendor clustering, taxonomy synthesis, vendor decisions, refinement,
consistency check, persistence) over the user's full transaction history
with rolling cross-phase state for global coherence.
"""

from __future__ import annotations

from .agent import run_categorization_agent
from .context import (
    BusinessProfile,
    CategorizationContext,
    CategorizationRunResult,
    ConfirmedExample,
    TxnRow,
    UserCategory,
    VendorCacheEntry,
    VendorCluster,
    VendorDecision,
)
from .progress import (
    NullProgressReporter,
    ProgressReporter,
    RecategorizeProgressReporter,
    ReconciliationProgressReporter,
)

__all__ = [
    "run_categorization_agent",
    "BusinessProfile",
    "CategorizationContext",
    "CategorizationRunResult",
    "ConfirmedExample",
    "TxnRow",
    "UserCategory",
    "VendorCacheEntry",
    "VendorCluster",
    "VendorDecision",
    "ProgressReporter",
    "NullProgressReporter",
    "RecategorizeProgressReporter",
    "ReconciliationProgressReporter",
]

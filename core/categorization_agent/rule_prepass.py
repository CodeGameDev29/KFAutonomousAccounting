"""Phase 1 — deterministic regex pre-pass.

Matches every transaction description against a list of generic patterns
and locks any hits so downstream LLM phases never see them. Typical locks
are bank fees, internal transfers, and CRA payments — the kinds of things
a rule can decide with no context.

The reconciliation module imports ``_auto_categorize_transactions`` from here.
"""

from __future__ import annotations

import re
from typing import Iterable

from config.data_files import category_rules
from config.ised_categories import UNCATEGORIZED

# Generic category patterns that work for any Canadian small business, emitting
# ONLY canonical ISED/GIFI names (config/ised_categories.py). Each entry is a
# 3-tuple ``(canonical_name, [regex...], non_reconcilable)``.
#
# ``non_reconcilable=True`` => internal/no-receipt flow: the row is excluded
# from the reconciliation match loop (server/api/reconciliation.py) AND locked
# before the LLM phases. It is keyed on the matched PATTERN (not the emitted
# name) because coarse canonical names are shared across reconcilable and
# non-reconcilable flows (client income vs payroll direct-deposit both map to
# "Sales of goods and services").
#
# Business-specific patterns (e.g., client names, transfer reference numbers)
# should be configured per-user via vendor_rules.yaml or a future per-user
# category_rules DB table — NOT hardcoded here. File order is preserved
# (first-match precedence at ``match_category``).
#
# The table itself lives in ``config/category_rules.yaml`` — edit that file to
# change, add or remove a rule; no Python change is needed.
CATEGORY_PATTERNS: list[tuple[str, list[str], bool]] = category_rules()


def match_category(description: str) -> str | None:
    """Return the first rule-matched canonical category for ``description`` or None."""
    for category, patterns, _non_recon in CATEGORY_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, description, re.IGNORECASE):
                return category
    return None


def is_non_reconcilable(description: str) -> bool:
    """True if ``description`` matches a rule pattern flagged non_reconcilable.

    Description-based (not name-based) because canonical names are shared across
    reconcilable and non-reconcilable flows (client income vs payroll deposit).
    """
    for _category, patterns, non_recon in CATEGORY_PATTERNS:
        if not non_recon:
            continue
        for pattern in patterns:
            if re.search(pattern, description, re.IGNORECASE):
                return True
    return False


# Exported for callers that need the SET OF CANONICAL NAMES any
# non_reconcilable rule can emit. Do NOT use for the reconciliation gate —
# use is_non_reconcilable(description) instead.
NON_RECONCILABLE_CATEGORIES: frozenset[str] = frozenset(
    name for name, _p, non_recon in CATEGORY_PATTERNS if non_recon
)


def _auto_categorize_transactions(txns: Iterable) -> dict[int, str]:
    """Map transactions to rule categories, in the shape reconciliation.py calls.

    Given an iterable of ``Transaction`` model instances (or anything with
    ``id`` and ``description`` attributes), return a ``{id: category}``
    mapping. Descriptions that do not match any rule fall back to
    ``"Uncategorized"`` (display label; the transactions.category write path
    stores NULL, per config/ised_categories.py).
    """
    result: dict[int, str] = {}
    for txn in txns:
        category = match_category(txn.description) or UNCATEGORIZED
        result[txn.id] = category
    return result

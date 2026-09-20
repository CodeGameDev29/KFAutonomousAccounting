"""Phase 3 — fixed taxonomy provider (Canada ISED + CRA GIFI).

Returns the closed, government-sourced taxonomy from
``config/ised_categories.py`` unchanged — no LLM call, no invented category
names. The public signature and return shape are the ones ``agent.py``,
Phases 4-6 and persistence expect.

The taxonomy is a CLOSED list; there is never an add/rename/remove diff.
"""

from __future__ import annotations

import logging

from config.ised_categories import taxonomy_upsert_rows

from .context import CategorizationContext, VendorCluster

logger = logging.getLogger(__name__)


def synthesize_taxonomy(
    context: CategorizationContext,
    clusters: list[VendorCluster],
    llm_call: callable,  # accepted for call-site compatibility; UNUSED (no LLM call).
) -> tuple[list[dict], dict]:
    """Return the FIXED taxonomy (``upsert_user_categories`` shape) and an empty diff.

    The taxonomy is the closed ISED/GIFI list; there is never an add/rename/remove
    diff, so ``{}`` keeps ``agent.py``'s added/renamed progress logging a no-op.
    ``context`` / ``clusters`` / ``llm_call`` are intentionally not consulted:
    the fixed list is complete, and user-confirmed rows keep their stored value
    regardless (they are hard-locked and never re-touched by persistence).
    """
    fixed = taxonomy_upsert_rows()
    logger.info("Phase 3: using fixed ISED/GIFI taxonomy (%d categories).", len(fixed))
    return fixed, {}

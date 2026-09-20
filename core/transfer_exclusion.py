"""Canonical money rules for income/expense aggregations.

Two independent gates, and they are NOT interchangeable:

* :func:`countable_sql` / :func:`is_countable` — drops rows whose money is
  phantom (``EXCLUDED``: rejected at import as a mis-parse/duplicate). These
  rows must not appear as ledger rows either.
* :func:`transfer_exclusion_sql` / :func:`is_transfer_category` — drops
  internal transfers from income/expense *totals* while keeping them visible
  as ledger rows.

Every aggregation that reports income/expense/net must apply BOTH.

──────────────────────────────────────────────────────────────────────────────
Internal-transfer exclusion
──────────────────────────────────────────────────────────────────────────────

Money moving between the user's own accounts (USD<->CAD conversions, credit
card payments, Wise top-ups) must not be counted as income on the receiving
side and expense on the sending side — that double-counts gain/loss in the
binder, reports, and analytics.

ONLY unambiguous own-account movements are excluded. Wage/payroll categories
count (real deductible expenses — the rule pre-pass tags every cheque
"Wage Payment"), and owner's draws count (real money leaving the business;
missing invoices are a receipt problem, not grounds to drop the cashflow from
the books).

Two independent signals mark a transaction as an internal transfer:

1. Category-based — the transaction's ``category`` string is transfer-like.
   Categories are free-form user-editable text, so matching is done on a
   normalized form (lowercase, alphanumerics only) against a family of known
   transfer names, plus any user category whose taxonomy ``kind`` is
   ``'transfer'`` (see ``user_categories.kind``).
2. Link-based — the transaction participates in an active (non-rejected)
   ``transaction_links`` row whose ``link_type`` is a transfer type. REFUND
   links are NOT excluded: a purchase and its refund are both real cashflows
   that legitimately offset in the books.

Every aggregation that reports income/expense/net must apply BOTH signals.
SQL sites use :func:`transfer_exclusion_sql`; Python object sites use
:func:`is_transfer_category` plus a precomputed set of transfer-linked
transaction ids (``DatabasePg.get_transfer_linked_txn_ids``).
"""

from __future__ import annotations

import re

# transaction_links.link_type values that represent money moving between the
# user's own accounts. REFUND and OTHER are intentionally not listed.
TRANSFER_LINK_TYPES: tuple[str, ...] = (
    "CREDIT_CARD_PAYMENT",
    "INTERNAL_TRANSFER",
    "FX_CONVERSION",
    "WISE_TRANSFER",
)

# Normalized (lowercase, alphanumeric-only) transfer-like category names.
# Covers the rule pre-pass categories, the seeded taxonomy, and the common
# user-authored variants such as "Inter-Account Transfers".
TRANSFER_CATEGORY_KEYS: frozenset[str] = frozenset(
    {
        "creditcardpayment",
        "creditcardpayments",
        "internaltransfer",
        "internaltransfers",
        "interaccounttransfer",
        "interaccounttransfers",
        "usdtocad",
        "cadtousd",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Countability — the ONLY status that gates money.
#
# A transaction's `status` is a statement about PAPERWORK, not about dollars,
# with exactly one exception. Do not add to this set without reading the note.
#
#   EXCLUDED  — the row isn't real: a statement-parse artifact, a duplicate, or
#               a line that isn't the user's. Rejected during import review
#               (server/api/statements.py). Its dollars are phantom and must
#               never reach a total, a chart, the ledger, or a tax figure.
#
# IGNORED is deliberately NOT here, and this is the whole point of the split.
# IGNORED means "self-evident — no receipt and no note needed" (a bank fee,
# interest, an internal transfer). That is a claim about *documentation*. The
# money is entirely there: an ignored $14.95 bank fee is a $14.95 deductible
# expense and must appear in expense totals, category breakdowns, the record
# binder and the QBO/Xero exports exactly like any other row. Adding IGNORED
# here would silently delete money from the user's books.
NON_COUNTABLE_STATUSES: tuple[str, ...] = ("EXCLUDED",)


def is_countable(status: str | None) -> bool:
    """False only for rows whose money is phantom (see NON_COUNTABLE_STATUSES)."""
    return str(getattr(status, "value", status) or "") not in NON_COUNTABLE_STATUSES


def countable_sql(alias: str = "t") -> tuple[str, list]:
    """Return (predicate_sql, params) that is TRUE for rows whose money counts.

    Embed as ``AND {predicate}`` in any WHERE clause that feeds an income /
    expense / net / category / tax aggregation, for a transactions table
    aliased as ``alias``. Uses %s placeholders (psycopg2).
    """
    return f"({alias}.status <> ALL(%s))", [list(NON_COUNTABLE_STATUSES)]


def normalize_category(name: str | None) -> str:
    """Lowercase and strip everything but alphanumerics for fuzzy matching."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def is_transfer_category(name: str | None) -> bool:
    """True if the category string names an internal-transfer bucket."""
    return normalize_category(name) in TRANSFER_CATEGORY_KEYS


def transfer_exclusion_sql(alias: str = "t") -> tuple[str, list]:
    """Return (predicate_sql, params) that is TRUE for internal transfers.

    Meant to be embedded as ``AND NOT (<predicate>)`` in a WHERE clause or as
    ``CASE WHEN NOT (<predicate>) ...`` in an aggregate, for a transactions
    table aliased as ``alias``. Uses %s placeholders (psycopg2).
    """
    a = alias
    predicate = f"""(
        regexp_replace(LOWER(COALESCE({a}.category, '')), '[^a-z0-9]', '', 'g') = ANY(%s)
        OR EXISTS (
            SELECT 1 FROM user_categories uc
            WHERE uc.user_id = {a}.user_id
              AND uc.kind = 'transfer'
              AND uc.name = {a}.category
        )
        OR EXISTS (
            SELECT 1 FROM transaction_links tlx
            WHERE tlx.user_id = {a}.user_id
              AND tlx.status != 'USER_REJECTED'
              AND tlx.link_type = ANY(%s)
              AND (tlx.source_transaction_id = {a}.id
                   OR tlx.target_transaction_id = {a}.id)
        )
    )"""
    return predicate, [list(TRANSFER_CATEGORY_KEYS), list(TRANSFER_LINK_TYPES)]

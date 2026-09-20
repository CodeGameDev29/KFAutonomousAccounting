"""Counterpart index for report payloads — the one place 1:N linkage is shaped.

The owner's month report (server/api/reports.py) and the public shared report
(server/api/shared.py) both inline a transaction's linked counterparts, and the
two views must not disagree — which is why the shaping lives here once rather
than in each of them.

One transaction maps to a LIST of counterparts, ordered by link id, so a charge
refunded across several credits keeps every leg and renders the same way every
time.
"""

from __future__ import annotations

from models.transaction import account_str


def build_counterpart_index(
    all_links: list[dict],
    txn_lookup: dict[int, object],
) -> dict[int, list[tuple[int, str]]]:
    """Map each transaction id to every counterpart also present in this batch.

    Both directions are indexed, so a hub charge lists all of its refund legs
    and each leg lists the charge back. Links whose other side falls outside
    ``txn_lookup`` (a counterpart in a different month) are skipped — the
    report can only render rows it holds.
    """
    index: dict[int, list[tuple[int, str]]] = {}
    for link in sorted(all_links, key=lambda entry: entry.get("id") or 0):
        src = link.get("source_transaction_id")
        tgt = link.get("target_transaction_id")
        link_type = link.get("link_type", "OTHER")
        if src in txn_lookup and tgt in txn_lookup:
            index.setdefault(src, []).append((tgt, link_type))
            index.setdefault(tgt, []).append((src, link_type))
    return index


def linked_summaries(
    txn_id: int,
    index: dict[int, list[tuple[int, str]]],
    txn_lookup: dict[int, object],
) -> list[dict]:
    """Render every counterpart of ``txn_id`` as a report-payload dict."""
    summaries: list[dict] = []
    for other_id, link_type in index.get(txn_id, []):
        other = txn_lookup.get(other_id)
        if other is None:
            continue
        summaries.append({
            "id": other.id,
            "account": account_str(other.account),
            "date": other.date_posted.isoformat(),
            "description": other.description,
            "amount": str(other.amount),
            "currency": other.currency,
            "type": other.transaction_type,
            "link_type": link_type,
        })
    return summaries

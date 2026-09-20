"""Compare app-parsed transactions against XLSX ground truth.

Matches rows by (account, date, amount) and reports discrepancies
in transaction counts, categories, and missing/extra transactions.

Usage:
    from scripts.qa.compare import compare_transactions
    from models.transaction import Transaction
    from scripts.qa.xlsx_parser import GroundTruthRow

    report = compare_transactions(parsed_txns, ground_truth_rows)
    report.print_summary()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from models.transaction import Transaction
from scripts.qa.xlsx_parser import GroundTruthRow

logger = logging.getLogger(__name__)


@dataclass
class MatchedPair:
    """A matched transaction-ground truth pair."""
    txn: Transaction
    gt: GroundTruthRow
    category_match: bool
    description_match: bool


@dataclass
class ComparisonReport:
    """Results of comparing parsed transactions against ground truth."""
    month_label: str
    matched: list[MatchedPair] = field(default_factory=list)
    missing_from_app: list[GroundTruthRow] = field(default_factory=list)  # in the XLSX but not parsed
    extra_in_app: list[Transaction] = field(default_factory=list)  # parsed but not in the XLSX
    category_mismatches: list[MatchedPair] = field(default_factory=list)

    @property
    def total_gt(self) -> int:
        return len(self.matched) + len(self.missing_from_app)

    @property
    def total_parsed(self) -> int:
        return len(self.matched) + len(self.extra_in_app)

    @property
    def match_rate(self) -> float:
        if self.total_gt == 0:
            return 1.0
        return len(self.matched) / self.total_gt

    @property
    def category_match_rate(self) -> float:
        if not self.matched:
            return 1.0
        return sum(1 for m in self.matched if m.category_match) / len(self.matched)

    def print_summary(self) -> None:
        print(f"\n{'='*60}")
        print(f"Comparison Report: {self.month_label}")
        print(f"{'='*60}")
        print(f"Ground truth rows:  {self.total_gt}")
        print(f"App parsed rows:    {self.total_parsed}")
        print(f"Matched:            {len(self.matched)}")
        print(f"Missing from app:   {len(self.missing_from_app)}")
        print(f"Extra in app:       {len(self.extra_in_app)}")
        print(f"Match rate:         {self.match_rate:.1%}")

        if self.category_mismatches:
            print(f"\nCategory mismatches ({len(self.category_mismatches)}):")
            for m in self.category_mismatches:
                print(f"  {m.gt.date_posted} {m.gt.amount:>10} "
                      f"XLSX={m.gt.category:<25} APP=???  "
                      f"{m.gt.description[:40]}")

        if self.missing_from_app:
            print(f"\nMissing from app ({len(self.missing_from_app)}):")
            for gt in self.missing_from_app:
                print(f"  [{gt.account}] {gt.date_posted} {gt.amount:>10} "
                      f"{gt.description[:50]}")

        if self.extra_in_app:
            print(f"\nExtra in app ({len(self.extra_in_app)}):")
            for txn in self.extra_in_app:
                print(f"  [{txn.account.value}] {txn.date_posted} {txn.amount:>10} "
                      f"{txn.description[:50]}")


def _normalize_amount(amount: Decimal) -> Decimal:
    """Normalize amount for comparison — use absolute value rounded to 2 decimals."""
    return abs(amount).quantize(Decimal("0.01"))


def compare_transactions(
    parsed_txns: list[Transaction],
    ground_truth: list[GroundTruthRow],
    month_label: str = "",
    date_tolerance_days: int = 3,
    categories: dict[int, str] | None = None,
) -> ComparisonReport:
    """Compare parsed transactions against ground truth.

    Matching strategy:
    1. Exact match: same account, same date, same amount
    2. Fuzzy date: same account, date within tolerance, same amount
    3. Remaining unmatched → missing/extra

    Args:
        parsed_txns: Transactions parsed from bank PDFs
        ground_truth: Rows parsed from XLSX
        month_label: Label for the report
        date_tolerance_days: Max days difference for fuzzy date matching
        categories: Optional txn.id → category mapping for category comparison
    """
    report = ComparisonReport(month_label=month_label)
    if categories is None:
        categories = {}

    # Group by account
    gt_by_account: dict[str, list[GroundTruthRow]] = {}
    for gt in ground_truth:
        gt_by_account.setdefault(gt.account, []).append(gt)

    txn_by_account: dict[str, list[Transaction]] = {}
    for txn in parsed_txns:
        acct = txn.account.value  # AccountType enum → string
        txn_by_account.setdefault(acct, []).append(txn)

    all_accounts = set(gt_by_account.keys()) | set(txn_by_account.keys())

    # Per-account matching (Pass 1 + Pass 2)
    unmatched_gt: list[GroundTruthRow] = []
    unmatched_txn: list[Transaction] = []

    for account in sorted(all_accounts):
        gt_rows = list(gt_by_account.get(account, []))
        txn_rows = list(txn_by_account.get(account, []))

        # Track which items have been matched
        gt_matched = [False] * len(gt_rows)
        txn_matched = [False] * len(txn_rows)

        # Pass 1: exact match (same date, same amount)
        for gi, gt in enumerate(gt_rows):
            if gt_matched[gi]:
                continue
            gt_amt = _normalize_amount(gt.amount)
            for ti, txn in enumerate(txn_rows):
                if txn_matched[ti]:
                    continue
                txn_amt = _normalize_amount(txn.amount)
                if txn.date_posted == gt.date_posted and txn_amt == gt_amt:
                    _record_match(report, txn, gt, categories)
                    gt_matched[gi] = True
                    txn_matched[ti] = True
                    break

        # Pass 2: fuzzy date match (within tolerance, same amount)
        for gi, gt in enumerate(gt_rows):
            if gt_matched[gi]:
                continue
            gt_amt = _normalize_amount(gt.amount)
            best_ti = None
            best_delta = timedelta(days=date_tolerance_days + 1)
            for ti, txn in enumerate(txn_rows):
                if txn_matched[ti]:
                    continue
                txn_amt = _normalize_amount(txn.amount)
                if txn_amt != gt_amt:
                    continue
                delta = abs(txn.date_posted - gt.date_posted)
                if delta <= timedelta(days=date_tolerance_days) and delta < best_delta:
                    best_delta = delta
                    best_ti = ti
            if best_ti is not None:
                _record_match(report, txn_rows[best_ti], gt, categories)
                gt_matched[gi] = True
                txn_matched[best_ti] = True

        # Collect per-account unmatched for cross-account pass
        for gi, gt in enumerate(gt_rows):
            if not gt_matched[gi]:
                unmatched_gt.append(gt)
        for ti, txn in enumerate(txn_rows):
            if not txn_matched[ti]:
                unmatched_txn.append(txn)

    # Pass 3: cross-account matching for remaining unmatched items.
    # A hand-kept workbook may record one account's rows in another account's
    # section, so a row left over after the per-account passes is matched on
    # (date, amount) alone before it is called missing.
    gt_matched_x = [False] * len(unmatched_gt)
    txn_matched_x = [False] * len(unmatched_txn)

    for gi, gt in enumerate(unmatched_gt):
        if gt_matched_x[gi]:
            continue
        gt_amt = _normalize_amount(gt.amount)
        for ti, txn in enumerate(unmatched_txn):
            if txn_matched_x[ti]:
                continue
            txn_amt = _normalize_amount(txn.amount)
            if txn.date_posted == gt.date_posted and txn_amt == gt_amt:
                _record_match(report, txn, gt, categories)
                gt_matched_x[gi] = True
                txn_matched_x[ti] = True
                break

    # Pass 4: cross-account fuzzy date
    for gi, gt in enumerate(unmatched_gt):
        if gt_matched_x[gi]:
            continue
        gt_amt = _normalize_amount(gt.amount)
        best_ti = None
        best_delta = timedelta(days=date_tolerance_days + 1)
        for ti, txn in enumerate(unmatched_txn):
            if txn_matched_x[ti]:
                continue
            txn_amt = _normalize_amount(txn.amount)
            if txn_amt != gt_amt:
                continue
            delta = abs(txn.date_posted - gt.date_posted)
            if delta <= timedelta(days=date_tolerance_days) and delta < best_delta:
                best_delta = delta
                best_ti = ti
        if best_ti is not None:
            _record_match(report, unmatched_txn[best_ti], gt, categories)
            gt_matched_x[gi] = True
            txn_matched_x[best_ti] = True

    # Final: collect truly unmatched
    for gi, gt in enumerate(unmatched_gt):
        if not gt_matched_x[gi]:
            report.missing_from_app.append(gt)

    for ti, txn in enumerate(unmatched_txn):
        if not txn_matched_x[ti]:
            report.extra_in_app.append(txn)

    return report


def _record_match(
    report: ComparisonReport,
    txn: Transaction,
    gt: GroundTruthRow,
    categories: dict[int, str],
) -> None:
    """Record a matched pair and check category.

    A category comparison needs both sides: an unset category on either the app
    row or the workbook row is "nothing to compare", never a mismatch.
    """
    app_category = categories.get(txn.id, "")
    cat_match = (
        app_category.strip().lower() == gt.category.strip().lower()
        if (app_category.strip() and gt.category.strip())
        else True
    )
    pair = MatchedPair(
        txn=txn,
        gt=gt,
        category_match=cat_match,
        description_match=True,  # not checking description similarity yet
    )
    report.matched.append(pair)
    if not cat_match:
        report.category_mismatches.append(pair)

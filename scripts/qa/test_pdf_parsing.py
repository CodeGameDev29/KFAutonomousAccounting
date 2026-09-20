"""Validate PDF bank statement parsing against XLSX ground truth.

For each month, parses every bank statement PDF the manifest found — one per
account the filenames name, which is whatever ``config/accounts.yaml`` is set
up for — using the same pdf_to_csv() + parse_csv() pipeline the app uses, then
compares the extracted transactions against the XLSX ground truth.

It reads your own files. See ``scripts/qa/manifest.py`` for where to put them;
nothing ships with this repository.

Usage:
    # Test a single month
    python -m scripts.qa.test_pdf_parsing --month 2026-01

    # Test all months with XLSX ground truth
    python -m scripts.qa.test_pdf_parsing

    # Test every month of one year
    python -m scripts.qa.test_pdf_parsing --year 2026
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.bank_parser import parse_csv
from core.pdf_statement_parser import pdf_to_csv
from models.transaction import AccountType, Transaction
from scripts.qa.compare import ComparisonReport, compare_transactions
from scripts.qa.manifest import MonthData, build_manifest
from scripts.qa.xlsx_parser import parse_xlsx

logger = logging.getLogger(__name__)


def account_override(account: str) -> AccountType | None:
    """The parser's account enum for an account key, when it has one.

    An account key is free-form text, while the built-in CSV parsers take the
    enum. A key the enum does not carry leaves the parser to work the account
    out from the CSV itself.
    """
    try:
        return AccountType(account)
    except ValueError:
        return None


def parse_bank_pdfs(month: MonthData) -> list[Transaction]:
    """Parse every statement PDF this month has, using the app's pipeline."""
    all_txns: list[Transaction] = []

    if not month.statements:
        logger.warning("%s: no statement PDFs", month.label)

    for account, pdf_path in sorted(month.statements.items()):
        if not pdf_path.is_file():
            logger.warning("%s: %s statement is gone: %s", month.label, account, pdf_path)
            continue

        logger.info("Parsing %s PDF: %s", account, pdf_path.name)
        result = pdf_to_csv(pdf_path)
        if result is None:
            logger.error("%s: pdf_to_csv returned None for %s", month.label, account)
            continue

        csv_path, detected_type = result
        logger.info("  -> CSV: %s, detected type: %s", csv_path.name, detected_type)

        # The filename already named the account, so use that rather than the
        # type auto-detected from the CSV header.
        try:
            txns = parse_csv(csv_path, account_override=account_override(account))
            logger.info("  -> Parsed %d transactions", len(txns))
            all_txns.extend(txns)
        except Exception as e:
            logger.error("  -> parse_csv failed: %s", e)

        # Clean up temp CSV
        try:
            csv_path.unlink(missing_ok=True)
        except Exception:
            pass

    return all_txns


def validate_month(month: MonthData) -> ComparisonReport | None:
    """Run full PDF parsing validation for a single month."""
    if not month.xlsx:
        logger.info("%s: No XLSX ground truth, skipping", month.label)
        return None

    if not month.has_bank_pdfs():
        logger.warning("%s: No bank PDFs found", month.label)
        return None

    # Parse bank PDFs
    txns = parse_bank_pdfs(month)
    logger.info("%s: Parsed %d total transactions from PDFs", month.label, len(txns))

    # Parse XLSX ground truth
    gt_rows = parse_xlsx(month.xlsx)
    logger.info("%s: %d ground truth rows from XLSX", month.label, len(gt_rows))

    # Compare
    report = compare_transactions(txns, gt_rows, month_label=month.label)
    return report


def validate_year_aggregate(manifest, year: int) -> ComparisonReport | None:
    """Parse ALL PDFs for a year and compare against ALL XLSX ground truth combined.

    This eliminates cross-month boundary noise — transactions that appear on
    one month's PDF but belong to the adjacent month's XLSX period.
    """
    keys = [k for k in sorted(manifest.keys())
            if k.startswith(str(year)) and manifest[k].xlsx]

    if not keys:
        return None

    all_txns: list[Transaction] = []
    all_gt: list = []

    for key in keys:
        month = manifest[key]
        if month.has_bank_pdfs():
            txns = parse_bank_pdfs(month)
            all_txns.extend(txns)
        if month.xlsx:
            from scripts.qa.xlsx_parser import parse_xlsx as px
            gt = px(month.xlsx)
            all_gt.extend(gt)

    report = compare_transactions(
        all_txns, all_gt,
        month_label=f"Year {year} Aggregate",
        date_tolerance_days=3,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description="Validate PDF parsing against XLSX ground truth")
    parser.add_argument("--month", type=str, help="Single month to test, e.g. 2026-01")
    parser.add_argument("--year", type=int, help="Test all months in a year, e.g. 2026")
    parser.add_argument("--aggregate", action="store_true",
                        help="Aggregate all months in the year before comparing (eliminates cross-month noise)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    manifest = build_manifest()

    # Year-aggregate mode
    if args.aggregate:
        years = [args.year] if args.year else sorted(
            {int(k[:4]) for k in manifest}
        )
        has_errors = False
        for year in years:
            report = validate_year_aggregate(manifest, year)
            if report:
                report.print_summary()
                if report.missing_from_app or report.extra_in_app:
                    has_errors = True
        sys.exit(1 if has_errors else 0)

    # Select months to test
    if args.month:
        keys = [args.month]
    elif args.year:
        keys = [k for k in sorted(manifest.keys()) if k.startswith(str(args.year))]
    else:
        # All months with XLSX ground truth
        keys = [k for k in sorted(manifest.keys()) if manifest[k].xlsx]

    if not keys:
        print("No months to test.")
        sys.exit(1)

    print(f"Testing {len(keys)} month(s): {', '.join(keys)}\n")

    all_reports: list[ComparisonReport] = []
    for key in keys:
        month = manifest[key]
        report = validate_month(month)
        if report:
            report.print_summary()
            all_reports.append(report)

    # Overall summary
    if len(all_reports) > 1:
        total_gt = sum(r.total_gt for r in all_reports)
        total_matched = sum(len(r.matched) for r in all_reports)
        total_missing = sum(len(r.missing_from_app) for r in all_reports)
        total_extra = sum(len(r.extra_in_app) for r in all_reports)

        print(f"\n{'='*60}")
        print("OVERALL SUMMARY")
        print(f"{'='*60}")
        print(f"Months tested:      {len(all_reports)}")
        print(f"Total GT rows:      {total_gt}")
        print(f"Total matched:      {total_matched}")
        print(f"Total missing:      {total_missing}")
        print(f"Total extra:        {total_extra}")
        print(f"Overall match rate: {total_matched/total_gt:.1%}" if total_gt else "N/A")

    # Exit with error if any month has mismatches
    has_errors = any(r.missing_from_app or r.extra_in_app for r in all_reports)
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()

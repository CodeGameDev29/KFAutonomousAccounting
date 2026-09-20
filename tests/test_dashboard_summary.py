"""The match rate the dashboard prints, and what it counts.

DatabasePg.get_dashboard_summary computes match_rate from transaction status
counts. Two things make or break that number: IGNORED rows must stay out of the
denominator (a row nobody has to reconcile cannot drag the rate down), and an
account with no transactions at all must return 0.0 rather than raising
ZeroDivisionError on its first page load.

The PostgreSQL connection pool and cursor are mocked to return controlled
row data, so these tests run without a database.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

from db.database_pg import DatabasePg

# ── Helpers ──────────────────────────────────────────────────────────────

def _make_db_with_status_counts(
    status_rows: list[tuple[str, int]],
    month_income_expense_rows: list[tuple[str, Decimal]] | None = None,
    doc_count: int = 0,
) -> DatabasePg:
    """Create a DatabasePg with a mocked connection that returns controlled data.

    Args:
        status_rows: List of (status, count) tuples, e.g. [("MATCHED", 6), ("UNMATCHED", 2)]
        month_income_expense_rows: List of (type, amount) tuples for current month
        doc_count: Number of documents to return
    """
    mock_pool = MagicMock()

    db = DatabasePg(pool=mock_pool, user_id="test-uuid-dashboard")

    # Build a mock cursor that returns different results for sequential queries
    mock_cursor = MagicMock()
    call_count = 0

    def fetchall_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First fetchall: status counts (GROUP BY status)
            return status_rows
        elif call_count == 2:
            # Second fetchall: current month income/expenses
            return month_income_expense_rows or []
        return []

    def fetchone_side_effect():
        # fetchone is called for document count (SELECT COUNT(*))
        return (doc_count,)

    mock_cursor.fetchall = MagicMock(side_effect=fetchall_side_effect)
    mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
    mock_cursor.execute = MagicMock()

    mock_conn = MagicMock()
    mock_cursor_ctx = MagicMock()
    mock_cursor_ctx.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor_ctx.__exit__ = MagicMock(return_value=False)

    mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)

    @contextmanager
    def fake_conn():
        yield mock_conn

    db._conn = fake_conn

    return db


# ── Tests ────────────────────────────────────────────────────────────────

class TestGetDashboardSummary:
    """The match rate the dashboard renders, over controlled status counts."""

    def test_match_rate_ignores_ignored(self):
        """match_rate = matched/(matched+unmatched)*100, IGNORED excluded.

        6 MATCHED + 2 UNMATCHED + 3 IGNORED -> match_rate = 6/8 * 100 = 75.0
        """
        status_rows = [
            ("MATCHED", 6),
            ("UNMATCHED", 2),
            ("IGNORED", 3),
        ]
        db = _make_db_with_status_counts(status_rows, doc_count=5)

        summary = db.get_dashboard_summary()

        assert summary["matched_count"] == 6
        assert summary["unmatched_count"] == 2
        assert summary["transaction_count"] == 11  # 6+2+3
        assert summary["match_rate"] == 75.0
        assert summary["document_count"] == 5

    def test_zero_transactions_no_division_error(self):
        """0 transactions -> match_rate = 0.0, no ZeroDivisionError."""
        status_rows = []  # No transactions at all
        db = _make_db_with_status_counts(status_rows, doc_count=0)

        summary = db.get_dashboard_summary()

        assert summary["transaction_count"] == 0
        assert summary["matched_count"] == 0
        assert summary["unmatched_count"] == 0
        assert summary["match_rate"] == 0.0
        assert summary["document_count"] == 0

    def test_all_matched(self):
        """All transactions matched -> match_rate = 100.0."""
        status_rows = [("MATCHED", 10)]
        db = _make_db_with_status_counts(status_rows, doc_count=10)

        summary = db.get_dashboard_summary()

        assert summary["match_rate"] == 100.0
        assert summary["matched_count"] == 10
        assert summary["unmatched_count"] == 0

    def test_all_unmatched(self):
        """All transactions unmatched -> match_rate = 0.0."""
        status_rows = [("UNMATCHED", 5)]
        db = _make_db_with_status_counts(status_rows, doc_count=3)

        summary = db.get_dashboard_summary()

        assert summary["match_rate"] == 0.0
        assert summary["unmatched_count"] == 5

    def test_only_ignored_transactions(self):
        """Only IGNORED transactions -> reconcilable = 0 -> match_rate = 0.0."""
        status_rows = [("IGNORED", 7)]
        db = _make_db_with_status_counts(status_rows, doc_count=0)

        summary = db.get_dashboard_summary()

        assert summary["transaction_count"] == 7
        assert summary["match_rate"] == 0.0

    def test_income_and_expenses_included(self):
        """Current month income and expenses are returned as strings."""
        status_rows = [("MATCHED", 3), ("UNMATCHED", 1)]
        income_expense_rows = [
            ("CREDIT", Decimal("5000.00")),
            ("DEBIT", Decimal("1200.50")),
        ]
        db = _make_db_with_status_counts(
            status_rows,
            month_income_expense_rows=income_expense_rows,
            doc_count=2,
        )

        summary = db.get_dashboard_summary()

        assert summary["current_month_income"] == "5000.00"
        assert summary["current_month_expenses"] == "1200.50"

    def test_match_rate_rounding(self):
        """Match rate is rounded to 1 decimal place."""
        # 1 matched / 3 reconcilable = 33.333... -> rounds to 33.3
        status_rows = [("MATCHED", 1), ("UNMATCHED", 2)]
        db = _make_db_with_status_counts(status_rows, doc_count=1)

        summary = db.get_dashboard_summary()

        assert summary["match_rate"] == 33.3

"""Tests for Wise per-month statement sync (sync_wise_by_month and helpers)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.wise_api import (
    _last_day_of_month,
    _month_range,
    _stable_hash,
    sync_wise_by_month,
)
from models.transaction import AccountType, Transaction

# ── _stable_hash() tests ────────────────────────────────────────────────


class TestStableHash:
    def test_deterministic(self):
        assert _stable_hash("TRANSFER-123") == _stable_hash("TRANSFER-123")

    def test_different_inputs(self):
        assert _stable_hash("A") != _stable_hash("B")

    def test_returns_int(self):
        assert isinstance(_stable_hash("X"), int)

    def test_positive(self):
        assert _stable_hash("anything") >= 0

    def test_hash_fits_in_pg_bigint(self):
        """All hash outputs must fit in PostgreSQL BIGINT (0 to 2^63 - 1)."""
        PG_BIGINT_MAX = (1 << 63) - 1
        for i in range(1000):
            h = _stable_hash(f"TRANSFER-{i}")
            assert 0 <= h <= PG_BIGINT_MAX, (
                f"_stable_hash('TRANSFER-{i}') = {h} exceeds BIGINT max {PG_BIGINT_MAX}"
            )

    def test_the_corpus_spans_the_pg_integer_overflow_range(self):
        """A narrower 8-hex digest of these same keys does exceed PG INTEGER.

        That is what makes the corpus above meaningful: if no key in it could
        overflow, test_hash_fits_in_pg_bigint would be vacuous.
        """
        import hashlib
        PG_INT_MAX = (1 << 31) - 1
        overflows = [
            int(hashlib.md5(f"TRANSFER-{i}".encode()).hexdigest()[:8], 16)
            for i in range(1000)
            if int(hashlib.md5(f"TRANSFER-{i}".encode()).hexdigest()[:8], 16) > PG_INT_MAX
        ]
        assert len(overflows) > 0, (
            "Expected at least one TRANSFER-N value to overflow PG INTEGER under "
            "an 8-hex digest, but none did. Corpus may need expanding."
        )


# ── _month_range() tests ────────────────────────────────────────────────


class TestMonthRange:
    def test_single_month(self):
        assert _month_range((2026, 1), (2026, 1)) == [(2026, 1)]

    def test_span_year(self):
        result = _month_range((2025, 11), (2026, 2))
        assert result == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]

    def test_full_year(self):
        result = _month_range((2026, 1), (2026, 12))
        assert len(result) == 12
        assert result[0] == (2026, 1)
        assert result[-1] == (2026, 12)

    def test_empty_when_reversed(self):
        result = _month_range((2026, 3), (2026, 1))
        assert result == []


# ── _last_day_of_month() tests ──────────────────────────────────────────


class TestLastDayOfMonth:
    def test_january(self):
        assert _last_day_of_month(2026, 1) == date(2026, 1, 31)

    def test_february_non_leap(self):
        assert _last_day_of_month(2025, 2) == date(2025, 2, 28)

    def test_february_leap(self):
        assert _last_day_of_month(2024, 2) == date(2024, 2, 29)

    def test_april(self):
        assert _last_day_of_month(2026, 4) == date(2026, 4, 30)


# ── sync_wise_by_month() tests ──────────────────────────────────────────


def _make_txn(amount: float = -10.0, currency: str = "USD", txn_date: date | None = None) -> Transaction:
    """Helper to create a Transaction for testing."""
    return Transaction(
        account=AccountType.WISE,
        transaction_type="DEBIT",
        date_posted=txn_date or date(2026, 1, 15),
        amount=Decimal(str(amount)),
        currency=currency,
        description="Test transfer",
        source_file="wise_api",
        source_row=12345,
    )


def _mock_accounts(currencies: list[str], creation: str = "2026-01-01T00:00:00Z") -> list[dict]:
    """Build fake balances response."""
    return [
        {"id": i + 1, "currency": c, "creationTime": creation}
        for i, c in enumerate(currencies)
    ]


class TestSyncWiseByMonth:
    """Tests for the main sync_wise_by_month orchestration function."""

    @patch("core.wise_api.time.sleep")  # skip actual sleeps
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_first_time_sync(self, mock_accounts, mock_fetch, mock_sleep):
        """First sync with no existing data inserts transactions."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Should fetch Jan, Feb, Mar = 3 months
        assert mock_fetch.call_count == 3
        # Each month inserts 1 txn
        assert db.insert_transaction.call_count == 3
        # Results should contain per-month keys
        assert "wise_USD_202601" in results
        assert "wise_USD_202602" in results
        assert "wise_USD_202603" in results
        assert results["wise_USD_202601"]["new"] == 1

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_skip_past_months(self, mock_accounts, mock_fetch, mock_sleep):
        """Past months already synced are skipped."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        # Jan and Feb already synced
        db.get_source_files_by_prefix.return_value = [
            "wise_USD_202601", "wise_USD_202602",
        ]

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Only Mar (current month) should be fetched
        assert mock_fetch.call_count == 1
        assert "wise_USD_202603" in results

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_replace_current_month(self, mock_accounts, mock_fetch, mock_sleep):
        """Current month is deleted and re-fetched if already synced."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-03-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = ["wise_USD_202603"]

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Should delete current month before re-fetching
        db.delete_transactions_by_source_file.assert_called_once_with("wise_USD_202603")
        assert mock_fetch.call_count == 1

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_empty_months_produce_zero_result(self, mock_accounts, mock_fetch, mock_sleep):
        """Months with 0 transactions produce a result entry with new=0, total=0."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = []  # No transactions

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Empty months produce result entries with new=0, total=0
        assert "wise_USD_202601" in results
        assert "wise_USD_202602" in results
        assert results["wise_USD_202601"] == {"new": 0, "total": 0}
        assert results["wise_USD_202602"] == {"new": 0, "total": 0}
        assert db.insert_transaction.call_count == 0

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_multi_currency(self, mock_accounts, mock_fetch, mock_sleep):
        """Multiple currencies create separate source_file entries."""
        mock_accounts.return_value = _mock_accounts(
            ["USD", "CAD"], creation="2026-03-01T00:00:00Z"
        )
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        assert "wise_USD_202603" in results
        assert "wise_CAD_202603" in results
        assert mock_fetch.call_count == 2

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_source_file_naming(self, mock_accounts, mock_fetch, mock_sleep):
        """source_file follows the wise_{CURRENCY}_{YYYYMM} format."""
        mock_accounts.return_value = _mock_accounts(["EUR"], creation="2025-11-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 1, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        for key in results:
            import re
            assert re.match(r"^wise_[A-Z]{3}_\d{6}$", key), f"Bad format: {key}"

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_source_file_set_on_transaction(self, mock_accounts, mock_fetch, mock_sleep):
        """Each inserted transaction's source_file is overridden to the per-month value."""
        txn = _make_txn()
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-03-01T00:00:00Z")
        mock_fetch.return_value = [txn]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            sync_wise_by_month("token", 1, db, "user-123")

        inserted_txn = db.insert_transaction.call_args[0][0]
        assert inserted_txn.source_file == "wise_USD_202603"

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_partial_failure(self, mock_accounts, mock_fetch, mock_sleep):
        """One month failing doesn't block other months."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")

        def side_effect(token, profile_id, currency, since, until, account_id=None):
            if since.month == 2:
                raise RuntimeError("API error")
            return [_make_txn()]

        mock_fetch.side_effect = side_effect

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Jan and Mar should succeed, Feb should have error
        assert results["wise_USD_202601"]["new"] == 1
        assert "error" in results["wise_USD_202602"]
        assert results["wise_USD_202603"]["new"] == 1

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_progress_callback(self, mock_accounts, mock_fetch, mock_sleep):
        """Progress callback is called for each fetched month."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []
        callback = MagicMock()

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            sync_wise_by_month("token", 1, db, "user-123", progress_callback=callback)

        # Callback is called for every _log call (account info, per-month progress, results, etc.)
        # At minimum there must be calls for both months
        assert callback.call_count >= 2
        # Verify that messages for both Jan and Feb were emitted
        all_messages = [call[0][0] for call in callback.call_args_list]
        assert any("Jan" in msg for msg in all_messages), "Expected a log message mentioning Jan"
        assert any("Feb" in msg for msg in all_messages), "Expected a log message mentioning Feb"
        # Verify the per-month sync message format
        sync_msgs = [msg for msg in all_messages if "Syncing Wise USD" in msg]
        assert len(sync_msgs) == 2  # One per month
        assert any("Jan" in msg for msg in sync_msgs)
        assert any("Feb" in msg for msg in sync_msgs)

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_no_accounts_returns_empty(self, mock_accounts, mock_fetch, mock_sleep):
        """No balance accounts returns empty results."""
        mock_accounts.return_value = []
        db = MagicMock()
        results = sync_wise_by_month("token", 1, db, "user-123")
        assert results == {}
        mock_fetch.assert_not_called()

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_missing_creation_time_falls_back(self, mock_accounts, mock_fetch, mock_sleep):
        """Missing creationTime falls back to 12 months ago."""
        mock_accounts.return_value = [{"id": 1, "currency": "USD"}]  # No creationTime
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Should still produce results (fallback to ~12 months of data)
        assert len(results) > 0

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_all_inserts_fail_produces_error_result(self, mock_accounts, mock_fetch, mock_sleep):
        """When every insert fails, result dict includes an 'error' key."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-03-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn(), _make_txn(), _make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []
        db.insert_transaction.side_effect = Exception("integer out of range")

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        assert "wise_USD_202603" in results
        result = results["wise_USD_202603"]
        assert result["new"] == 0
        assert result["total"] == 3
        assert "error" in result, (
            f"Expected 'error' key when all inserts fail, got: {result}"
        )
        assert result["error"]  # non-empty string

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_partial_inserts_no_error_key(self, mock_accounts, mock_fetch, mock_sleep):
        """Partial insert failure (some succeed) does not produce an error key."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-03-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn(), _make_txn(), _make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []
        # First call succeeds, second and third fail
        db.insert_transaction.side_effect = [None, Exception("dup"), Exception("dup")]

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month("token", 1, db, "user-123")

        result = results["wise_USD_202603"]
        assert result["new"] == 1
        assert result["total"] == 3
        assert "error" not in result, (
            f"Partial failure should not produce error key, got: {result}"
        )


# ── since_date / until_date parameter tests ────────────────────────────


class TestSyncWiseByMonthDateParams:
    """The since_date / until_date parameters that bound a sync.

    Without them a sync always walks from the balance account's creation month
    to today, which re-fetches months the caller did not ask for. The bounds
    narrow the walk — but never past the account's creation month, because
    there is nothing to fetch before the account existed.
    """

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_since_date_overrides_creation_time(self, mock_accounts, mock_fetch, mock_sleep):
        """since_date restricts sync to start from that month, overriding creationTime.

        Account creationTime is Jan 2026, but since_date=Mar 2026 means only Mar is fetched.
        """
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month(
                "token", 1, db, "user-123",
                since_date=date(2026, 3, 1),
            )

        # Only March should be fetched (since_date=Mar 2026, today=Mar 2026)
        assert mock_fetch.call_count == 1
        assert "wise_USD_202603" in results
        assert "wise_USD_202601" not in results
        assert "wise_USD_202602" not in results

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_until_date_restricts_range(self, mock_accounts, mock_fetch, mock_sleep):
        """until_date stops syncing before today's month.

        Account starts Jan 2026; until_date=Feb 28 means only Jan and Feb are fetched,
        not March even though today is Mar 15.
        """
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month(
                "token", 1, db, "user-123",
                until_date=date(2026, 2, 28),
            )

        # Only Jan and Feb should be fetched
        assert mock_fetch.call_count == 2
        assert "wise_USD_202601" in results
        assert "wise_USD_202602" in results
        assert "wise_USD_202603" not in results

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_since_date_and_until_date_narrow_window(self, mock_accounts, mock_fetch, mock_sleep):
        """Both since_date and until_date together restrict to a single month."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month(
                "token", 1, db, "user-123",
                since_date=date(2026, 2, 1),
                until_date=date(2026, 2, 28),
            )

        # Only Feb should be fetched
        assert mock_fetch.call_count == 1
        assert "wise_USD_202602" in results
        assert "wise_USD_202601" not in results
        assert "wise_USD_202603" not in results

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_since_date_cannot_go_before_account_creation(self, mock_accounts, mock_fetch, mock_sleep):
        """since_date earlier than account creationTime is clipped to account creation month.

        since_date=Jun 2025 but account created Jan 2026 — effective start is Jan 2026.
        """
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            results = sync_wise_by_month(
                "token", 1, db, "user-123",
                since_date=date(2025, 6, 1),  # before account creation
            )

        # Effective start should be Jan 2026 (account creation), not Jun 2025
        # So Jan, Feb, Mar 2026 should be fetched (3 months), not 10 months back to Jun 2025
        assert mock_fetch.call_count == 3
        assert "wise_USD_202601" in results
        assert "wise_USD_202602" in results
        assert "wise_USD_202603" in results

    @patch("core.wise_api.time.sleep")
    @patch("core.wise_api.fetch_wise_transactions")
    @patch("core.wise_api.get_borderless_accounts")
    def test_no_date_params_uses_creation_time(self, mock_accounts, mock_fetch, mock_sleep):
        """With neither bound, the walk starts at the account's creationTime."""
        mock_accounts.return_value = _mock_accounts(["USD"], creation="2026-01-01T00:00:00Z")
        mock_fetch.return_value = [_make_txn()]

        db = MagicMock()
        db.get_source_files_by_prefix.return_value = []

        with patch("core.wise_api.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            # No since_date or until_date — the unbounded walk
            results = sync_wise_by_month("token", 1, db, "user-123")

        # Jan, Feb, Mar (creation month to today month) = 3 fetches
        assert mock_fetch.call_count == 3
        assert "wise_USD_202601" in results
        assert "wise_USD_202602" in results
        assert "wise_USD_202603" in results
        assert results["wise_USD_202601"]["new"] == 1


# ── fetch_wise_transactions source_row test ─────────────────────────────


class TestFetchWiseTransactionsSourceRow:
    """Verify that fetch_wise_transactions uses _stable_hash for referenceNumber."""

    @patch("core.wise_api._get_client")
    @patch("core.wise_api.get_borderless_accounts")
    def test_reference_number_used_as_source_row(self, mock_balances, mock_client):
        """referenceNumber should be hashed into source_row."""
        mock_balances.return_value = [{"id": 1, "currency": "USD"}]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "transactions": [
                {
                    "amount": {"value": -10.0, "currency": "USD"},
                    "date": "2026-01-15T00:00:00Z",
                    "referenceNumber": "TRANSFER-999",
                    "details": {"description": "Test"},
                    "type": "DEBIT",
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        client_instance = MagicMock()
        client_instance.get.return_value = mock_response
        mock_client.return_value = client_instance

        from core.wise_api import fetch_wise_transactions

        txns = fetch_wise_transactions("token", 1, currency="USD")
        assert len(txns) == 1
        assert txns[0].source_row == _stable_hash("TRANSFER-999")

    @patch("core.wise_api._get_client")
    @patch("core.wise_api.get_borderless_accounts")
    def test_missing_reference_falls_back_to_index(self, mock_balances, mock_client):
        """Missing referenceNumber should fall back to enumeration index."""
        mock_balances.return_value = [{"id": 1, "currency": "USD"}]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "transactions": [
                {
                    "amount": {"value": -5.0, "currency": "USD"},
                    "date": "2026-01-15T00:00:00Z",
                    "referenceNumber": "",
                    "details": {"description": "No ref"},
                    "type": "DEBIT",
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        client_instance = MagicMock()
        client_instance.get.return_value = mock_response
        mock_client.return_value = client_instance

        from core.wise_api import fetch_wise_transactions

        txns = fetch_wise_transactions("token", 1, currency="USD")
        assert len(txns) == 1
        assert txns[0].source_row == 1  # Enumeration index fallback



# ── details.type == FEE ─────────────────────────────────────────────────
#
# Wise bills its transfer fee as a separate statement row. The only clue in the
# text is the wording "Wise Charges for: TRANSFER-…", which is Wise's to change:
# once it changes, the row stops reading as a bank charge, goes to the receipt
# matcher, and is categorized from whatever receipt it half-matched — a bank fee
# filed as travel or vehicle expense, and a receipt spent proving it. The
# payload's ``details.type`` says "FEE" outright, so the parser keys on that and
# only falls back to the wording.


def _wise_row(detail_type: str | None, description: str, ref: str = "TRANSFER-1"):
    row = {
        "amount": {"value": -6.25, "currency": "USD"},
        "date": "2026-08-03T00:00:00Z",
        "referenceNumber": ref,
        "details": {"description": description},
        "type": "DEBIT",
    }
    if detail_type is not None:
        row["details"]["type"] = detail_type
    return row


def _fetch(rows):
    from core.wise_api import fetch_wise_transactions

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"transactions": rows}
    response.raise_for_status = MagicMock()
    client_instance = MagicMock()
    client_instance.get.return_value = response

    with patch("core.wise_api.get_borderless_accounts",
               return_value=[{"id": 1, "currency": "USD"}]), \
            patch("core.wise_api._get_client", return_value=client_instance):
        return fetch_wise_transactions("token", 1, currency="USD")


class TestWiseFeeRows:
    def test_todays_wording_is_stored_unchanged(self):
        """"Wise Charges for: …" already reads as a bank charge, so it is left
        exactly as Wise wrote it — the TRANSFER reference is worth keeping and
        rewriting it would make every historical row look different."""
        txns = _fetch([_wise_row("FEE", "Wise Charges for: TRANSFER-00000000")])
        assert txns[0].description == "Wise Charges for: TRANSFER-00000000"

    def test_a_new_wording_is_labelled_so_the_rule_still_catches_it(self):
        txns = _fetch([_wise_row("FEE", "Charge applied to transfer 00000000")])
        assert txns[0].description == "Wise fee: Charge applied to transfer 00000000"

    def test_a_labelled_fee_row_is_a_bank_charge_and_needs_no_receipt(self):
        from core.categorization_agent.rule_prepass import (
            is_non_reconcilable,
            match_category,
        )

        for wording in (
            "Wise Charges for: TRANSFER-00000000",
            "Charge applied to transfer 00000000",
            "Service charge",
        ):
            desc = _fetch([_wise_row("FEE", wording)])[0].description
            assert match_category(desc) == "Interest and bank charges", desc
            assert is_non_reconcilable(desc) is True, desc

    def test_non_fee_rows_are_untouched(self):
        """The transfer the fee belongs to is a sub-contract payment, and the
        incoming client payment is revenue. Neither may be relabelled."""
        rows = [
            _wise_row("TRANSFER", "Sent money to Contractor A (fee: 6.25 USD)", "T-1"),
            _wise_row("DEPOSIT", "Received money from Northwind Media LLC", "T-2"),
            _wise_row("CONVERSION", "Converted 1000 USD to CAD", "T-3"),
        ]
        descriptions = [t.description for t in _fetch(rows)]
        assert descriptions == [
            "Sent money to Contractor A (fee: 6.25 USD)",
            "Received money from Northwind Media LLC",
            "Converted 1000 USD to CAD",
        ]

    def test_a_row_with_no_details_type_is_untouched(self):
        """Older statement payloads have no details.type at all."""
        txns = _fetch([_wise_row(None, "Sent money to Contractor B (fee: 6.25 USD)")])
        assert txns[0].description == "Sent money to Contractor B (fee: 6.25 USD)"

    def test_fee_detection_is_case_insensitive(self):
        txns = _fetch([_wise_row("fee", "Charge applied to transfer 1")])
        assert txns[0].description.startswith("Wise fee: ")

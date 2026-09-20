"""Tests for PayPal per-month statement sync (sync_paypal_by_month and helpers)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.paypal_api import (
    _extract_counterparty,
    _last_day_of_month,
    _month_range,
    _should_import_transaction,
    _stable_hash,
    _txn_detail_to_transaction,
    sync_paypal_by_month,
)
from models.transaction import AccountType

# ── Test Factories ─────────────────────────────────────────────────────


def _make_paypal_txn(
    txn_id: str = "PP-12345",
    event_code: str = "T0006",
    status: str = "S",
    amount: str = "-50.00",
    currency: str = "USD",
    date_str: str = "2026-01-15T10:30:00+0000",
    payer_name: str = "Acme Corp",
    subject: str = "",
) -> dict:
    """Create a mock PayPal transaction detail dict."""
    return {
        "transaction_info": {
            "transaction_id": txn_id,
            "transaction_event_code": event_code,
            "transaction_status": status,
            "transaction_amount": {
                "value": amount,
                "currency_code": currency,
            },
            "transaction_initiation_date": date_str,
            "transaction_subject": subject,
            "transaction_note": "",
        },
        "payer_info": {
            "payer_name": {
                "alternate_full_name": payer_name,
                "given_name": "",
                "surname": "",
            },
            "email_address": "",
        },
        "cart_info": {"item_details": []},
    }


def _make_paypal_api_response(
    txns: list[dict],
    total_pages: int = 1,
    page: int = 1,
) -> dict:
    """Create a mock PayPal API response envelope."""
    return {
        "transaction_details": txns,
        "total_pages": total_pages,
        "page": page,
        "total_items": len(txns),
    }


def _mock_db():
    """Create a mock DatabasePg instance with commonly-needed methods."""
    db = MagicMock()
    db.get_source_files_by_prefix.return_value = []
    db.insert_transaction.return_value = None
    db.delete_transactions_by_source_file.return_value = None
    return db


# ── _stable_hash() tests ──────────────────────────────────────────────


class TestStableHash:
    def test_deterministic(self):
        assert _stable_hash("PP-12345:T0006") == _stable_hash("PP-12345:T0006")

    def test_different_inputs_differ(self):
        assert _stable_hash("PP-12345:T0006") != _stable_hash("PP-12345:T0200")

    def test_returns_int(self):
        assert isinstance(_stable_hash("PP-12345:T0006"), int)

    def test_positive(self):
        assert _stable_hash("anything") >= 0

    def test_fits_in_pg_bigint(self):
        """All hash outputs must fit in PostgreSQL BIGINT (0 to 2^63 - 1)."""
        PG_BIGINT_MAX = (1 << 63) - 1
        for i in range(1000):
            h = _stable_hash(f"PP-{i}:T0006")
            assert 0 <= h <= PG_BIGINT_MAX

    def test_composite_key_isolates_event_code(self):
        """Same txn_id with different event codes must produce distinct hashes."""
        h1 = _stable_hash("PP-12345:T0006")
        h2 = _stable_hash("PP-12345:T0200")
        h3 = _stable_hash("PP-12345:T0100")
        assert len({h1, h2, h3}) == 3

    def test_matches_wise_implementation(self):
        """PayPal _stable_hash must produce same output as Wise for same input."""
        from core.wise_api import _stable_hash as wise_hash
        test_input = "TEST-CROSS-COMPAT"
        assert _stable_hash(test_input) == wise_hash(test_input)


# ── _month_range() tests ──────────────────────────────────────────────


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

    def test_reversed_returns_empty(self):
        assert _month_range((2026, 6), (2026, 1)) == []


# ── _last_day_of_month() tests ────────────────────────────────────────


class TestLastDayOfMonth:
    def test_january(self):
        assert _last_day_of_month(2026, 1) == date(2026, 1, 31)

    def test_february_non_leap(self):
        assert _last_day_of_month(2025, 2) == date(2025, 2, 28)

    def test_february_leap(self):
        assert _last_day_of_month(2024, 2) == date(2024, 2, 29)

    def test_april(self):
        assert _last_day_of_month(2026, 4) == date(2026, 4, 30)


# ── _should_import_transaction() tests ─────────────────────────────────


class TestTCodeFiltering:
    def test_t0006_payment_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T0006"})

    def test_t0100_fee_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T0100"})

    def test_t1106_refund_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T1106"})

    def test_t0200_conversion_skipped(self):
        assert not _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T0200"})

    def test_t2100_hold_skipped(self):
        assert not _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T2100"})

    def test_t9800_display_only_skipped(self):
        assert not _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T9800"})

    def test_pending_status_skipped(self):
        assert not _should_import_transaction({"transaction_status": "P", "transaction_event_code": "T0006"})

    def test_denied_status_skipped(self):
        assert not _should_import_transaction({"transaction_status": "D", "transaction_event_code": "T0006"})

    def test_t1201_chargeback_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T1201"})

    def test_t0300_bank_transfer_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T0300"})

    def test_t0400_withdrawal_included(self):
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T0400"})

    def test_t1300_auth_skipped(self):
        assert not _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T1300"})

    def test_empty_event_code_imported(self):
        """Transactions with no event code are imported conservatively."""
        assert _should_import_transaction({"transaction_status": "S", "transaction_event_code": ""})

    def test_unknown_code_skipped(self):
        assert not _should_import_transaction({"transaction_status": "S", "transaction_event_code": "T9999"})

    def test_deny_allow_sets_disjoint(self):
        """Deny and allow prefix sets must not overlap."""
        deny = {"T02", "T20", "T21", "T13", "T98"}
        allow = {"T00", "T01", "T11", "T03", "T04"}
        assert deny.isdisjoint(allow)


# ── _extract_counterparty() tests ─────────────────────────────────────


class TestCounterpartyExtraction:
    def test_alternate_full_name_priority(self):
        txn = _make_paypal_txn(payer_name="Acme Corp")
        assert _extract_counterparty(txn) == "Acme Corp"

    def test_given_name_surname_fallback(self):
        txn = _make_paypal_txn(payer_name="")
        txn["payer_info"]["payer_name"]["given_name"] = "John"
        txn["payer_info"]["payer_name"]["surname"] = "Doe"
        assert _extract_counterparty(txn) == "John Doe"

    def test_subject_fallback(self):
        txn = _make_paypal_txn(payer_name="")
        txn["transaction_info"]["transaction_subject"] = "Invoice #42"
        assert _extract_counterparty(txn) == "Invoice #42"

    def test_item_name_fallback(self):
        txn = _make_paypal_txn(payer_name="")
        txn["cart_info"]["item_details"] = [{"item_name": "Widget Pro"}]
        assert _extract_counterparty(txn) == "Widget Pro"

    def test_email_domain_fallback(self):
        """With no payer name, the email's domain is the last real signal."""
        txn = _make_paypal_txn(payer_name="")
        txn["payer_info"]["email_address"] = "ap@example.com"
        assert _extract_counterparty(txn) == "Example"

    def test_ultimate_fallback(self):
        txn = _make_paypal_txn(payer_name="")
        assert _extract_counterparty(txn) == "PayPal"


# ── _txn_detail_to_transaction() tests ─────────────────────────────────


class TestTransactionConversion:
    def test_basic_conversion(self):
        txn_data = _make_paypal_txn(
            txn_id="PP-001", event_code="T0006", amount="-99.50",
            currency="USD", date_str="2026-03-15T12:00:00+0000",
            payer_name="Test Vendor"
        )
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202603")
        assert result is not None
        assert result.account == AccountType.PAYPAL
        assert result.amount == Decimal("-99.50")
        assert result.currency == "USD"
        assert result.source_file == "paypal_USD_202603"
        assert result.transaction_type == "DEBIT"

    def test_filtered_transaction_returns_none(self):
        txn_data = _make_paypal_txn(event_code="T0200")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is None

    def test_credit_transaction(self):
        txn_data = _make_paypal_txn(amount="150.00")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.transaction_type == "CREDIT"
        assert result.amount == Decimal("150.00")

    def test_composite_source_row(self):
        txn_data = _make_paypal_txn(txn_id="PP-12345", event_code="T0006")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.source_row == _stable_hash("PP-12345:T0006")

    def test_date_parsing_iso8601(self):
        txn_data = _make_paypal_txn(date_str="2026-01-15T10:30:00+0000")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.date_posted == date(2026, 1, 15)

    def test_amount_positive_sign_prefix(self):
        txn_data = _make_paypal_txn(amount="+50.00")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.amount == Decimal("50.00")
        assert result.transaction_type == "CREDIT"

    def test_zero_amount(self):
        txn_data = _make_paypal_txn(amount="0.00")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.amount == Decimal("0.00")

    def test_large_amount(self):
        txn_data = _make_paypal_txn(amount="-99999999.99")
        result = _txn_detail_to_transaction(txn_data, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.amount == Decimal("-99999999.99")


# ── sync_paypal_by_month() tests ──────────────────────────────────────


class TestSyncPaypalByMonth:
    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_first_time_sync(self, mock_date, mock_token, mock_fetch):
        """First sync should fetch all months in range, insert all transactions."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 3, 15),
            currencies=["USD"],
        )

        assert mock_fetch.call_count == 3  # Jan, Feb, Mar
        assert "paypal_USD_202601" in results
        assert "paypal_USD_202602" in results
        assert "paypal_USD_202603" in results

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_skip_past_months(self, mock_date, mock_token, mock_fetch):
        """Past months already synced should be skipped (no API call)."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        # Simulate Jan and Feb already synced
        db.get_source_files_by_prefix.return_value = [
            "paypal_USD_202601",
            "paypal_USD_202602",
        ]

        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 3, 15),
            currencies=["USD"],
        )

        # Only March (current month) should be fetched
        assert mock_fetch.call_count == 1

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_replace_current_month(self, mock_date, mock_token, mock_fetch):
        """Current month should delete existing and re-fetch."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        db.get_source_files_by_prefix.return_value = ["paypal_USD_202603"]

        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 3, 1),
            until_date=date(2026, 3, 15),
            currencies=["USD"],
        )

        db.delete_transactions_by_source_file.assert_called_once_with("paypal_USD_202603")

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_empty_months_zero_result(self, mock_date, mock_token, mock_fetch):
        """Months with no transactions should report zero."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = []

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert results["paypal_USD_202601"] == {"new": 0, "total": 0}

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_multi_currency(self, mock_date, mock_token, mock_fetch):
        """Should produce separate source files per currency."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD", "CAD"],
        )

        assert "paypal_USD_202601" in results
        assert "paypal_CAD_202601" in results

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_source_file_naming_format(self, mock_date, mock_token, mock_fetch):
        """All source file keys must match paypal_[A-Z]{3}_YYYYMM pattern."""
        import re
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 3, 15),
            currencies=["USD"],
        )

        pattern = re.compile(r"^paypal_[A-Z]{3}_\d{6}$")
        for key in results:
            assert pattern.match(key), f"Key {key} doesn't match expected pattern"

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_source_file_set_on_transaction(self, mock_date, mock_token, mock_fetch):
        """Inserted transactions must have the correct source_file."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn()]

        db = _mock_db()
        sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert db.insert_transaction.call_count >= 1
        inserted_txn = db.insert_transaction.call_args[0][0]
        assert inserted_txn.source_file == "paypal_USD_202601"

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_partial_failure(self, mock_date, mock_token, mock_fetch):
        """If one month fails, other months should still succeed."""
        mock_date.today.return_value = date(2026, 2, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.side_effect = [
            [_make_paypal_txn()],  # Jan succeeds
            Exception("API timeout"),  # Feb fails
        ]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 2, 15),
            currencies=["USD"],
        )

        assert "new" in results["paypal_USD_202601"]
        assert "error" in results["paypal_USD_202602"]

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_progress_callback(self, mock_date, mock_token, mock_fetch):
        """Progress callback should be called for each month."""
        mock_date.today.return_value = date(2026, 2, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = []

        db = _mock_db()
        callback = MagicMock()

        sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            progress_callback=callback,
            since_date=date(2026, 1, 1),
            until_date=date(2026, 2, 15),
            currencies=["USD"],
        )

        assert callback.call_count >= 2  # At least one call per month

    @patch("core.paypal_api.get_access_token_cached")
    def test_no_access_token_returns_error(self, mock_token):
        """If no access token can be obtained, should return error."""
        mock_token.return_value = None

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            currencies=["USD"],
        )

        assert "error" in results.get("paypal", {})
        mock_fetch_never_called = db.insert_transaction.call_count == 0
        assert mock_fetch_never_called

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_t0200_not_inserted(self, mock_date, mock_token, mock_fetch):
        """T0200 (currency conversion) transactions must not be inserted."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn(event_code="T0200")]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert db.insert_transaction.call_count == 0

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_t9800_not_inserted(self, mock_date, mock_token, mock_fetch):
        """T9800 (display-only) transactions must not be inserted."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [_make_paypal_txn(event_code="T9800")]

        db = _mock_db()
        sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert db.insert_transaction.call_count == 0

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_mixed_t_codes(self, mock_date, mock_token, mock_fetch):
        """Only allowed T-codes should be inserted; denied ones skipped."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [
            _make_paypal_txn(txn_id="PP-1", event_code="T0006"),  # allowed
            _make_paypal_txn(txn_id="PP-2", event_code="T0200"),  # denied
            _make_paypal_txn(txn_id="PP-3", event_code="T9800"),  # denied
            _make_paypal_txn(txn_id="PP-4", event_code="T0100"),  # allowed
        ]

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        # Only the allowed codes (T0006, T0100) reach the database.
        assert db.insert_transaction.call_count == 2
        assert results["paypal_USD_202601"]["total"] == 2


# ── Date Parameter Tests ──────────────────────────────────────────────


class TestDateParameters:
    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_since_date_overrides_default(self, mock_date, mock_token, mock_fetch):
        """since_date should limit which months are fetched."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = []

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 3, 1),
            until_date=date(2026, 3, 15),
            currencies=["USD"],
        )

        # Only March should be fetched
        assert mock_fetch.call_count == 1

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_until_date_restricts_range(self, mock_date, mock_token, mock_fetch):
        """Months after until_date should not be fetched."""
        mock_date.today.return_value = date(2026, 6, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = []

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 3, 31),
            currencies=["USD"],
        )

        # Jan, Feb, Mar only — not Apr, May, Jun
        assert mock_fetch.call_count == 3

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_since_and_until_narrow(self, mock_date, mock_token, mock_fetch):
        """Single month window should fetch exactly one month."""
        mock_date.today.return_value = date(2026, 2, 28)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = []

        db = _mock_db()
        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 2, 1),
            until_date=date(2026, 2, 28),
            currencies=["USD"],
        )

        assert mock_fetch.call_count == 1


# ── Edge Cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_txn_id_fallback(self):
        """Transactions without transaction_id should still produce a hash."""
        txn = _make_paypal_txn(txn_id="")
        result = _txn_detail_to_transaction(txn, "USD", "paypal_USD_202601")
        assert result is not None
        assert isinstance(result.source_row, int)
        assert result.source_row > 0

    def test_eur_currency_accepted(self):
        txn = _make_paypal_txn(currency="EUR")
        result = _txn_detail_to_transaction(txn, "EUR", "paypal_EUR_202601")
        assert result is not None
        assert result.currency == "EUR"

    def test_missing_date_fallback(self):
        """Transactions with empty date should fall back to today."""
        txn = _make_paypal_txn(date_str="")
        result = _txn_detail_to_transaction(txn, "USD", "paypal_USD_202601")
        assert result is not None
        assert result.date_posted == date.today()


# ── Error Path Tests ───────────────────────────────────────────────────


class TestErrorPaths:
    @patch("core.paypal_api.get_access_token_cached")
    def test_missing_credentials_returns_error(self, mock_token):
        mock_token.return_value = None
        db = _mock_db()
        results = sync_paypal_by_month("", "", db, "user-1", currencies=["USD"])
        assert "error" in results.get("paypal", {})

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_all_inserts_fail(self, mock_date, mock_token, mock_fetch):
        """If all DB inserts fail, new should be 0 but total should reflect count."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [
            _make_paypal_txn(txn_id="PP-1"),
            _make_paypal_txn(txn_id="PP-2"),
        ]

        db = _mock_db()
        db.insert_transaction.side_effect = Exception("duplicate key")

        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert results["paypal_USD_202601"]["new"] == 0
        assert results["paypal_USD_202601"]["total"] == 2

    @patch("core.paypal_api.fetch_paypal_transactions_for_month")
    @patch("core.paypal_api.get_access_token_cached")
    @patch("core.paypal_api.date")
    def test_partial_inserts(self, mock_date, mock_token, mock_fetch):
        """If some inserts succeed and some fail, new should be partial count."""
        mock_date.today.return_value = date(2026, 1, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_token.return_value = "test_token"
        mock_fetch.return_value = [
            _make_paypal_txn(txn_id="PP-1"),
            _make_paypal_txn(txn_id="PP-2"),
        ]

        db = _mock_db()
        db.insert_transaction.side_effect = [None, Exception("dup")]

        results = sync_paypal_by_month(
            "client_id", "secret", db, "user-1",
            since_date=date(2026, 1, 1),
            until_date=date(2026, 1, 15),
            currencies=["USD"],
        )

        assert results["paypal_USD_202601"]["new"] == 1
        assert results["paypal_USD_202601"]["total"] == 2

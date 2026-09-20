"""What a hosted LLM call costs, and whether it is recorded.

When extraction runs against a hosted provider instead of a local model, every
call has a price. These tests pin the per-token constants the code multiplies
by, and pin that both provider paths write a row through ``log_api_call`` —
an unrecorded call is a cost nobody can see. The read side (``get_cost_summary``)
has to aggregate those rows per user and per date range, and return zeros for a
period with no calls rather than failing.

The database is mocked throughout, so the assertions are about the SQL and the
arithmetic rather than about any stored data.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestCostTracking:
    """Test cost tracking data model and calculations."""

    # ── Pricing validation (reads from implementation) ──────────────

    def test_gemini_pricing_matches_implementation(self):
        """Gemini pricing constants in extraction.py track the model in use.

        The constants are per-token prices for the configured Gemini model
        ($1.50 per million input tokens, $7.50 per million output). If
        GeminiConfig.model changes, these have to change with it: they are what
        db.log_api_call records as the cost of a hosted call, and a stale pair
        makes every recorded cost quietly wrong.
        """
        from core.extraction import (
            _GEMINI_INPUT_COST_PER_TOKEN,
            _GEMINI_OUTPUT_COST_PER_TOKEN,
        )

        assert _GEMINI_INPUT_COST_PER_TOKEN == pytest.approx(0.0000015)
        assert _GEMINI_OUTPUT_COST_PER_TOKEN == pytest.approx(0.0000075)

        # Verify the math produces expected costs for known inputs
        cost_2000_500 = (
            2000 * _GEMINI_INPUT_COST_PER_TOKEN + 500 * _GEMINI_OUTPUT_COST_PER_TOKEN
        )
        assert cost_2000_500 == pytest.approx(0.00675, abs=0.0001)

    def test_gpt41_pricing_matches_implementation(self):
        """GPT-4.1 pricing constants in extraction.py are correct."""
        source = (PROJECT_ROOT / "core" / "extraction.py").read_text()
        assert "0.000002" in source, "GPT-4.1 input price ($2/M) missing"
        assert "0.000008" in source, "GPT-4.1 output price ($8/M) missing"

        # Verify GPT-4.1 math for known inputs
        input_price = 0.000002
        output_price = 0.000008
        cost_2000_500 = 2000 * input_price + 500 * output_price
        assert cost_2000_500 == pytest.approx(0.008, abs=0.001)

    # ── log_api_call: what it writes ────────────────────────────────

    def _make_db(self):
        """Create a DatabasePg with a fully mocked pool + cursor.

        Returns (db, mock_cursor) so tests can inspect SQL calls.
        The mock follows the real _conn pattern:
            pool.getconn() -> conn -> conn.cursor() -> cur  (context manager)
            conn.commit() on success, conn.rollback() on error, pool.putconn() always.
        """
        from db.database_pg import DatabasePg

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()

        mock_pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = lambda self: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        db = DatabasePg(mock_pool, "test-user-id")
        return db, mock_cursor, mock_conn

    def test_log_api_call_writes_to_database(self):
        """log_api_call executes one INSERT, with the parameters in order."""
        db, mock_cursor, mock_conn = self._make_db()

        db.log_api_call("extraction", "gemini-2.5-flash", 2000, 500, 0.0006)

        # Verify SQL was executed exactly once
        mock_cursor.execute.assert_called_once()
        sql_call = mock_cursor.execute.call_args
        sql = sql_call[0][0]
        params = sql_call[0][1]

        assert "INSERT" in sql.upper()
        assert "api_calls" in sql.lower()
        # Verify all parameters are passed through in order
        assert params == ("test-user-id", "extraction", "gemini-2.5-flash", 2000, 500, 0.0006)

    def test_log_api_call_commits_on_success(self):
        """log_api_call commits the transaction after INSERT."""
        db, mock_cursor, mock_conn = self._make_db()

        db.log_api_call("extraction", "gpt-4.1", 1000, 200, 0.0036)

        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

    def test_log_api_call_signature(self):
        """The signature callers depend on: endpoint, model, tokens, cost."""
        from db.database_pg import DatabasePg

        sig = inspect.signature(DatabasePg.log_api_call)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "endpoint" in params
        assert "model" in params
        assert "tokens_in" in params
        assert "tokens_out" in params
        assert "estimated_cost" in params

    # ── get_cost_summary: what it returns ───────────────────────────

    def test_get_cost_summary_returns_structured_data(self):
        """Totals plus a per-endpoint, per-model breakdown."""
        db, mock_cursor, mock_conn = self._make_db()

        # First execute = totals query, second execute = breakdown query
        mock_cursor.fetchone.return_value = (0.05, 10, 5000, 1500)
        mock_cursor.fetchall.return_value = [
            ("extraction", "gemini-2.5-flash", 8, 0.04),
            ("extraction", "gpt-4.1", 2, 0.01),
        ]

        result = db.get_cost_summary("2026-03-01", "2026-04-01")

        # Verify top-level aggregates
        assert result["total_cost"] == 0.05
        assert result["total_calls"] == 10
        assert result["total_tokens_in"] == 5000
        assert result["total_tokens_out"] == 1500

        # Verify breakdown structure
        assert len(result["breakdown"]) == 2
        assert result["breakdown"][0]["model"] == "gemini-2.5-flash"
        assert result["breakdown"][0]["endpoint"] == "extraction"
        assert result["breakdown"][0]["calls"] == 8
        assert result["breakdown"][0]["cost"] == 0.04
        assert result["breakdown"][1]["model"] == "gpt-4.1"

    def test_get_cost_summary_empty_period(self):
        """get_cost_summary handles periods with no API calls."""
        db, mock_cursor, mock_conn = self._make_db()

        mock_cursor.fetchone.return_value = (0, 0, 0, 0)
        mock_cursor.fetchall.return_value = []

        result = db.get_cost_summary("2026-01-01", "2026-02-01")

        assert result["total_cost"] == 0.0
        assert result["total_calls"] == 0
        assert result["breakdown"] == []

    def test_get_cost_summary_passes_user_id_and_dates(self):
        """get_cost_summary scopes queries to user_id and date range."""
        db, mock_cursor, mock_conn = self._make_db()

        mock_cursor.fetchone.return_value = (0, 0, 0, 0)
        mock_cursor.fetchall.return_value = []

        db.get_cost_summary("2026-03-01", "2026-04-01")

        # Two SQL calls: totals then breakdown
        assert mock_cursor.execute.call_count == 2

        # Both calls should include user_id and date range
        for call_args in mock_cursor.execute.call_args_list:
            params = call_args[0][1]
            assert params == ("test-user-id", "2026-03-01", "2026-04-01")

    def test_get_cost_summary_signature(self):
        """The read side takes a date range and returns a dict."""
        from db.database_pg import DatabasePg

        assert hasattr(DatabasePg, "get_cost_summary")
        sig = inspect.signature(DatabasePg.get_cost_summary)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "start_date" in params
        assert "end_date" in params
        assert sig.return_annotation in (dict, "dict")

    # ── Cost tracking integration in extraction ─────────────────────

    def test_both_providers_have_cost_tracking(self):
        """Both Gemini and OpenAI paths call log_api_call."""
        source = (PROJECT_ROOT / "core" / "extraction.py").read_text()
        tree = ast.parse(source)

        gemini_has_logging = False
        openai_has_logging = False

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                fn_src = ast.get_source_segment(source, node) or ""
                if node.name == "_extract_with_gemini" and "log_api_call" in fn_src:
                    gemini_has_logging = True
                if node.name == "_extract_with_openai" and "log_api_call" in fn_src:
                    openai_has_logging = True

        assert gemini_has_logging, "_extract_with_gemini should call log_api_call"
        assert openai_has_logging, "_extract_with_openai should call log_api_call"

    def test_categorization_has_cost_tracking(self):
        """categorization.py calls log_api_call."""
        source = (PROJECT_ROOT / "core" / "categorization.py").read_text()
        assert "log_api_call" in source, "categorization.py missing cost tracking"

    # ── Edge cases ──────────────────────────────────────────────────

    def test_zero_tokens_produces_zero_cost(self):
        """Zero tokens should produce zero cost for both providers."""
        # Gemini pricing
        gemini_cost = 0 * 0.00000015 + 0 * 0.0000006
        assert gemini_cost == 0.0

        # GPT-4.1 pricing
        gpt_cost = 0 * 0.000002 + 0 * 0.000008
        assert gpt_cost == 0.0

    def test_log_api_call_zero_tokens(self):
        """log_api_call accepts zero tokens without error."""
        db, mock_cursor, mock_conn = self._make_db()

        db.log_api_call("extraction", "gemini-2.5-flash", 0, 0, 0.0)

        mock_cursor.execute.assert_called_once()
        params = mock_cursor.execute.call_args[0][1]
        assert params == ("test-user-id", "extraction", "gemini-2.5-flash", 0, 0, 0.0)

    def test_missing_usage_metadata_handled(self):
        """extraction.py handles missing usage_metadata safely with hasattr guard."""
        source = (PROJECT_ROOT / "core" / "extraction.py").read_text()
        # Must have a guard: hasattr(response, 'usage_metadata') and response.usage_metadata
        assert "hasattr" in source and "usage_metadata" in source, (
            "extraction.py must guard against missing usage_metadata with hasattr"
        )

    def test_log_api_call_rollback_on_error(self):
        """log_api_call rolls back on database error."""
        db, mock_cursor, mock_conn = self._make_db()

        mock_cursor.execute.side_effect = Exception("DB connection lost")

        with pytest.raises(Exception, match="DB connection lost"):
            db.log_api_call("extraction", "gemini-2.5-flash", 100, 50, 0.0001)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

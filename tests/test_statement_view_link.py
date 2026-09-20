"""A finished statement job says which tab its "View" link should open.

The transactions page selects a statement tab by ``transactions.source_file``
(``web/src/pages/Transactions.tsx``). A statement that imported at least one row
of its own therefore *is* a tab, under its own filename — but one whose every row
had already been imported by an earlier file imports none and has no tab at all.
Handing that filename to ``?statement=`` anyway makes the page fall back to
whichever tab sorts first, so "View" on March shows January's rows as if they
were March's.

``result.statement_tab`` (and the same key inside ``result.summary``) is the
server's answer to "which tab holds this statement's rows": its own filename
normally, the file that actually holds them when it imported none, and ``None``
when there is no tab to open at all — which is the frontend's cue to link to the
statements list instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from server.api.transactions import _statement_summary, _statement_tab_for


def _txn(account: str = "CAD", day: int = 3) -> MagicMock:
    txn = MagicMock()
    txn.account = MagicMock(value=account)
    txn.date_posted = date(2026, 3, day)
    txn.amount = Decimal("-12.34")
    return txn


def _db() -> MagicMock:
    db = MagicMock()
    db.source_file_holding_period = MagicMock(return_value=None)
    return db


class TestTheTabAStatementOwns:
    def test_a_statement_that_imported_rows_is_its_own_tab(self):
        db = _db()
        tab = _statement_tab_for(
            db, filename="bmo march 2026.pdf", imported=31, transactions=[_txn()]
        )
        assert tab == "bmo march 2026.pdf"
        db.source_file_holding_period.assert_not_called()

    def test_zero_imported_points_at_the_file_that_holds_the_rows(self):
        db = _db()
        db.source_file_holding_period.return_value = "bmo march 2026.pdf"
        tab = _statement_tab_for(
            db,
            filename="bmo-march-2026 (1).pdf",
            imported=0,
            transactions=[_txn(day=1), _txn(day=28)],
        )
        assert tab == "bmo march 2026.pdf"
        args = db.source_file_holding_period.call_args.args
        assert args[0] == "CAD"
        assert args[1] == date(2026, 3, 1)
        assert args[2] == date(2026, 3, 28)

    def test_no_tab_at_all_when_nothing_parsed(self):
        assert _statement_tab_for(
            _db(), filename="empty.pdf", imported=0, transactions=[]
        ) is None

    def test_no_tab_is_guessed_when_the_rows_span_two_accounts(self):
        db = _db()
        db.source_file_holding_period.return_value = "whatever.pdf"
        assert _statement_tab_for(
            db,
            filename="combined.pdf",
            imported=0,
            transactions=[_txn("CAD"), _txn("USD")],
        ) is None
        db.source_file_holding_period.assert_not_called()

    def test_a_store_that_cannot_answer_is_not_a_tab(self):
        """None is a real answer the frontend handles; an exception is not."""
        db = _db()
        db.source_file_holding_period.side_effect = RuntimeError("pool exhausted")
        assert _statement_tab_for(
            db, filename="march.pdf", imported=0, transactions=[_txn()]
        ) is None


class TestTheSummaryCarriesIt:
    def test_the_summary_block_carries_the_tab(self):
        summary = _statement_summary(
            filename="bmo march 2026.pdf",
            imported=31,
            duplicates=0,
            replaced=0,
            account="CAD",
            institution="BMO",
            statement_tab="bmo march 2026.pdf",
        )
        assert summary["statement_tab"] == "bmo march 2026.pdf"
        assert summary["source_file"] == "bmo march 2026.pdf"

    def test_already_in_your_books_keeps_source_file_but_moves_the_tab(self):
        """The two keys answer different questions and must not be conflated:
        ``source_file`` is what the user uploaded, ``statement_tab`` is where its
        rows are."""
        summary = _statement_summary(
            filename="bmo-march-2026 (1).pdf",
            imported=0,
            duplicates=31,
            replaced=0,
            account="CAD",
            institution="BMO",
            statement_tab="bmo march 2026.pdf",
        )
        assert summary["source_file"] == "bmo-march-2026 (1).pdf"
        assert summary["statement_tab"] == "bmo march 2026.pdf"
        assert summary["rows"] == 0
        assert summary["duplicates"] == 31

    def test_no_tab_is_an_explicit_none_rather_than_a_missing_key(self):
        summary = _statement_summary(
            filename="empty.pdf",
            imported=0,
            duplicates=0,
            replaced=0,
            account=None,
            institution=None,
        )
        assert "statement_tab" in summary
        assert summary["statement_tab"] is None


class TestTheLookupSpeaksTheColumnsLanguage:
    """``transactions.date_posted`` is TEXT holding ISO ``YYYY-MM-DD``.

    Binding a ``datetime.date`` there makes Postgres look for a ``text >= date``
    operator, find none, and raise — which ``_statement_tab_for`` catches, so the
    only symptom is a View link that quietly stops working. Nothing above this
    layer can notice that, so the bound parameters are asserted here against the
    real method rather than a stub.
    """

    @staticmethod
    def _db_with_capturing_cursor(row):
        cur = MagicMock()
        cur.fetchone.return_value = row
        # `_conn` checks the role switch took effect before yielding.
        cur.fetchall.return_value = []

        def _execute(sql, params=None):
            if "current_user" in str(sql).lower():
                cur.fetchone.return_value = ("authenticated",)
            else:
                cur.fetchone.return_value = row

        cur.execute.side_effect = _execute

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool = MagicMock()
        pool.getconn.return_value = conn

        from db.database_pg import DatabasePg

        return DatabasePg(pool, user_id="user-view-link"), cur

    def test_the_date_bounds_are_bound_as_iso_strings(self):
        db, cur = self._db_with_capturing_cursor(("bmo march 2026.pdf", 31))
        found = db.source_file_holding_period("CAD", date(2026, 3, 1), date(2026, 3, 31))

        assert found == "bmo march 2026.pdf"
        call = next(
            c for c in cur.execute.call_args_list
            if "source_file" in str(c.args[0]) and "date_posted" in str(c.args[0])
        )
        params = call.args[1]
        assert params[1] == "CAD"
        assert params[2] == "2026-03-01"
        assert params[3] == "2026-03-31"
        assert not any(isinstance(p, date) for p in params), (
            "date_posted is a TEXT column - a date object raises rather than matching"
        )

    def test_strings_pass_through_unchanged(self):
        db, cur = self._db_with_capturing_cursor(None)
        assert db.source_file_holding_period("CAD", "2026-03-01", "2026-03-31") is None

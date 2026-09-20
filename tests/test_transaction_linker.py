"""The deterministic transfer linker: both legs of a credit-card payment.

Every transaction, id, amount, date, account string and description in this
module is invented — see ``tests/fixtures/README.md``. What the descriptions do
carry is the descriptor grammar of the BMO export format (a chequing
"OnlineTransfer,TF…" debit facing a card "TRSF FROM/DE ACCT/CPT …" credit),
because that grammar is what the keyword rules are written against.

The scenario: four rows that are two movements of money, shown as four
unexplained "no receipt" rows.

    CAD chequing  DEBIT    81.45  2026-03-09  "OnlineTransfer,TF000000-1234"
    Credit card   CREDIT  -81.45  2026-03-09  "TRSF FROM/DE ACCT/CPT 0000-XXXX-0000"
    CAD chequing  DEBIT   480.00  2026-03-16  "OnlineTransfer,TF000000-1234"
    Credit card   CREDIT -480.00  2026-03-17  "TRSF FROM/DE ACCT/CPT 0000-XXXX-0000"

Three things have to hold at once for the pair to be found, and each has a test
here:

1. the card leg must match a transfer keyword of its own. With ``TRSF FROM``
   configured only for CAD/USD chequing (as INTERNAL_TRANSFER), the candidate
   pair is never generated at all;
2. "Credit card payment" is a *non-reconcilable* rule, so both legs are stamped
   IGNORED — and a linker fed only UNMATCHED rows loses a leg for good as soon
   as the first statement is reconciled;
3. the LLM 4th pass is skipped outright on statement-scoped reconciles, so
   nothing downstream picks the pair up instead.

The signs and statuses below follow the storage convention: a CAD debit is a
positive amount with ``transaction_type='DEBIT'``, the card credit is negative
with ``transaction_type='CREDIT'``, and both sit at IGNORED.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.transaction_linker import (
    AUTO_APPROVE_THRESHOLD,
    _generate_candidate_pairs,
    _load_keywords,
    _match_keywords,
    run_transaction_linking,
)
from models.transaction import Transaction, TransactionStatus

CAD_LEG_DESC = "OnlineTransfer,TF000000-1234"
CARD_LEG_DESC = "TRSF FROM/DE ACCT/CPT 0000-XXXX-0000"

CAD_SOURCE_FILE = "statement-2026-03-chequing.pdf"
CARD_SOURCE_FILE = "statement-2026-03-card.pdf"


def _cad_debit(
    txn_id: int,
    amount: str,
    day: date,
    *,
    description: str = CAD_LEG_DESC,
    status: TransactionStatus = TransactionStatus.IGNORED,
) -> Transaction:
    """The chequing leg: positive amount, DEBIT, account "CAD"."""
    return Transaction(
        id=txn_id,
        account="CAD",
        transaction_type="DEBIT",
        date_posted=day,
        amount=Decimal(amount),
        currency="CAD",
        description=description,
        source_file=CAD_SOURCE_FILE,
        source_row=txn_id,
        status=status,
        category="Credit card payment",
    )


def _card_credit(
    txn_id: int,
    amount: str,
    day: date,
    *,
    description: str = CARD_LEG_DESC,
    status: TransactionStatus = TransactionStatus.IGNORED,
) -> Transaction:
    """The card leg: negative amount, CREDIT, account "CreditCard"."""
    return Transaction(
        id=txn_id,
        account="CreditCard",
        transaction_type="CREDIT",
        date_posted=day,
        amount=Decimal(amount),
        currency="CAD",
        description=description,
        source_file=CARD_SOURCE_FILE,
        source_row=txn_id,
        status=status,
        category="Credit card payment",
    )


def _paired_rows() -> list[Transaction]:
    """The four rows of the scenario: two card payments, two legs each."""
    return [
        _cad_debit(501, "81.45", date(2026, 3, 9)),
        _card_credit(502, "-81.45", date(2026, 3, 9)),
        _cad_debit(503, "480.00", date(2026, 3, 16)),
        _card_credit(504, "-480.00", date(2026, 3, 17)),
    ]


# ── 1. Both legs must match a credit-card-payment keyword ──────────────

def test_both_legs_match_credit_card_payment_keywords():
    """Without a keyword of its own on the card side, no pair is generated."""
    keywords = _load_keywords()

    cad = _match_keywords(CAD_LEG_DESC, "CAD", keywords)
    card = _match_keywords(CARD_LEG_DESC, "CreditCard", keywords)

    assert any(
        kw.transfer_type == "CREDIT_CARD_PAYMENT" and kw.side == "SOURCE"
        for kw in cad
    ), "the chequing leg must be a CREDIT_CARD_PAYMENT SOURCE"
    assert any(
        kw.transfer_type == "CREDIT_CARD_PAYMENT" and kw.side == "TARGET"
        for kw in card
    ), "the card leg must be a CREDIT_CARD_PAYMENT TARGET"


def test_trsf_from_stays_an_internal_transfer_on_chequing():
    """Adding the card-side entry must not disturb the chequing-side meaning."""
    keywords = _load_keywords()
    for account in ("CAD", "USD"):
        matches = _match_keywords(CARD_LEG_DESC, account, keywords)
        assert any(kw.transfer_type == "INTERNAL_TRANSFER" for kw in matches)
        assert not any(kw.transfer_type == "CREDIT_CARD_PAYMENT" for kw in matches)


def test_the_pair_is_generated_as_a_candidate():
    cad, card = _cad_debit(501, "81.45", date(2026, 3, 9)), _card_credit(
        502, "-81.45", date(2026, 3, 9)
    )
    pairs = _generate_candidate_pairs([cad, card])
    assert [(s.id, t.id) for s, t in pairs] == [(501, 502)]


# ── 2. IGNORED rows are part of the linker's input set ─────────────────

def test_ignored_rows_are_still_linked():
    """An IGNORED row is the *normal* state of a transfer leg, not a reason to skip it."""
    rows = _paired_rows()
    assert all(t.status is TransactionStatus.IGNORED for t in rows)

    result = run_transaction_linking(rows)

    assert len(result.links) == 2
    assert {(link.source_transaction_id, link.target_transaction_id)
            for link in result.links} == {(501, 502), (503, 504)}
    assert result.unlinked_transfers == []


def test_already_linked_or_matched_rows_are_not_fed_back_in():
    """With one leg of each pair spoken for, the two survivors are a week and
    $398.55 apart — far below the review floor, so nothing is proposed."""
    rows = _paired_rows()
    rows[0].status = TransactionStatus.LINKED
    rows[3].status = TransactionStatus.MATCHED

    result = run_transaction_linking(rows)

    assert result.links == []


# ── 3. Both pairs link as CREDIT_CARD_PAYMENT, auto-approved ───────────

@pytest.mark.parametrize(
    ("source_id", "target_id", "amount"),
    [(501, 502, Decimal("81.45")), (503, 504, Decimal("480.00"))],
)
def test_credit_card_payments_auto_approve(source_id, target_id, amount):
    result = run_transaction_linking(_paired_rows())

    link = next(
        link for link in result.links if link.source_transaction_id == source_id
    )
    assert link.target_transaction_id == target_id, "the chequing debit is the source"
    assert link.link_type == "CREDIT_CARD_PAYMENT"
    assert link.amount_score == 1.0
    assert link.keyword_score == 1.0
    assert link.account_pair_score == 1.0
    assert link.confidence_score >= AUTO_APPROVE_THRESHOLD
    assert link in result.auto_approved
    assert str(amount) in link.explanation


def test_result_is_the_same_whatever_order_the_statements_arrived_in():
    """Run order must not decide the outcome."""
    forwards = run_transaction_linking(_paired_rows())
    backwards = run_transaction_linking(list(reversed(_paired_rows())))

    def signature(result):
        return sorted(
            (link.source_transaction_id, link.target_transaction_id,
             link.link_type, link.confidence_score)
            for link in result.links
        )

    assert signature(forwards) == signature(backwards)


# ── The certainty floor, and its limits ────────────────────────────────

@pytest.mark.parametrize("gap_days", [0, 1, 2, 3, 4, 5, 6, 7])
def test_exact_amount_both_keywords_within_the_window_auto_approves(gap_days):
    """A card payment that clears days later is the same movement of money."""
    rows = [
        _cad_debit(511, "104.55", date(2026, 3, 5)),
        _card_credit(512, "-104.55", date(2026, 3, 5 + gap_days)),
    ]
    result = run_transaction_linking(rows)

    assert len(result.links) == 1
    link = result.links[0]
    assert link.link_type == "CREDIT_CARD_PAYMENT"
    assert link.confidence_score >= AUTO_APPROVE_THRESHOLD
    assert result.pending_review == []


def test_beyond_the_date_window_nothing_is_linked():
    """Eight days is one past CREDIT_CARD_PAYMENT's window."""
    rows = [
        _cad_debit(511, "104.55", date(2026, 3, 5)),
        _card_credit(512, "-104.55", date(2026, 3, 13)),
    ]
    assert run_transaction_linking(rows).links == []


@pytest.mark.parametrize("card_amount", ["-104.54", "-104.25", "-103.15"])
def test_amounts_that_differ_never_auto_approve(card_amount):
    """Any variance at all stays a human decision — the floor requires an exact
    match. A cent, thirty cents and $1.40 against the same $104.55 debit."""
    rows = [
        _cad_debit(511, "104.55", date(2026, 3, 5)),
        _card_credit(512, card_amount, date(2026, 3, 5)),
    ]
    result = run_transaction_linking(rows)

    assert result.auto_approved == []
    assert result.links, "the pair is still proposed — as a review"
    for link in result.links:
        assert link.confidence_score < AUTO_APPROVE_THRESHOLD
        assert link in result.pending_review


# ── A rejected pair is never proposed again ────────────────────────────

def test_a_rejected_pair_is_not_re_proposed():
    rows = _paired_rows()

    result = run_transaction_linking(rows, rejected_pairs={(501, 502)})

    assert {(link.source_transaction_id, link.target_transaction_id)
            for link in result.links} == {(503, 504)}


def test_a_rejection_holds_whichever_way_round_it_was_stored():
    rows = _paired_rows()

    result = run_transaction_linking(rows, rejected_pairs={(502, 501)})

    assert all(
        link.source_transaction_id != 501 and link.target_transaction_id != 502
        for link in result.links
    )


def test_a_rejected_pair_does_not_block_its_rows_from_linking_elsewhere():
    """A rejection bans the pair, not the two rows."""
    rows = [
        _cad_debit(501, "81.45", date(2026, 3, 9)),
        _card_credit(502, "-81.45", date(2026, 3, 9)),
        _card_credit(599, "-81.45", date(2026, 3, 10)),
    ]
    result = run_transaction_linking(rows, rejected_pairs={(501, 502)})

    assert [(link.source_transaction_id, link.target_transaction_id)
            for link in result.links] == [(501, 599)]


# ── The pool the linker is fed from ────────────────────────────────────

class _FakeCursor:
    """Enough of a psycopg2 cursor for DatabasePg._conn plus one SELECT."""

    def __init__(self, rows: list[tuple], executed: list[tuple]) -> None:
        self._rows = rows
        self._executed = executed
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self._executed.append((sql, params))

    def fetchone(self):
        # The only thing _conn() reads back is the effective role.
        return ("authenticated",) if "current_user" in self._last else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows, executed):
        self._rows = rows
        self._executed = executed

    def cursor(self):
        return _FakeCursor(self._rows, self._executed)

    def commit(self):
        pass

    def rollback(self):  # pragma: no cover - only on failure
        pass


class _FakePool:
    def __init__(self, rows, executed):
        self._conn = _FakeConn(rows, executed)

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass


def _linkable_query(executed: list[tuple]) -> str:
    return next(sql for sql, _ in executed if "FROM transactions t" in sql)


def test_linkable_pool_takes_ignored_rows_and_drops_linked_ones():
    """The input set is UNMATCHED *and* IGNORED, minus anything already linked."""
    from db.database_pg import DatabasePg

    rows = [
        (501, "CAD", "DEBIT", "2026-03-09", "81.45", "CAD", CAD_LEG_DESC,
         CAD_SOURCE_FILE, 4, "IGNORED", None, "Credit card payment", None),
        (502, "CreditCard", "CREDIT", "2026-03-09", "-81.45", "CAD", CARD_LEG_DESC,
         CARD_SOURCE_FILE, 9, "IGNORED", None, "Credit card payment", None),
    ]
    executed: list[tuple] = []
    db = DatabasePg(_FakePool(rows, executed), "user-under-test")

    txns = db.get_linkable_transactions()

    assert [t.id for t in txns] == [501, 502]
    assert all(t.status is TransactionStatus.IGNORED for t in txns)

    sql = " ".join(_linkable_query(executed).split())
    assert "t.status IN ('UNMATCHED', 'IGNORED')" in sql
    assert "NOT EXISTS" in sql and "transaction_links" in sql
    assert "l.status <> 'USER_REJECTED'" in sql
    # Not date- or statement-scoped: a transfer's two legs live on two
    # different statements, and often in two different months. date_posted may
    # only appear as a selected column and in the ORDER BY, never as a filter.
    assert "date_posted >=" not in sql and "date_posted <=" not in sql
    assert "source_file =" not in sql
    assert sql.rstrip().endswith("ORDER BY t.date_posted, t.id")


def test_rejected_link_pairs_come_back_direction_free():
    from db.database_pg import DatabasePg

    executed: list[tuple] = []
    db = DatabasePg(_FakePool([(502, 501), (503, 504)], executed), "user-under-test")

    assert db.get_user_rejected_link_pairs() == {(501, 502), (503, 504)}
    sql = " ".join(executed[-1][0].split())
    assert "status = 'USER_REJECTED'" in sql


# ── An unlink from the UI is remembered ────────────────────────────────
#
# The linker above reconsiders every UNMATCHED/IGNORED row without an active
# link, on every run. So a hard DELETE of the link row is not "undo" — it is
# amnesia: the next Match run re-creates the very link the user just removed
# and auto-approves it again. The unlink verb therefore stamps the row
# USER_REJECTED and leaves it standing, which is exactly the memory
# get_user_rejected_link_pairs reads back.


class _LinkStore:
    """One ``transaction_links`` row — as much of it as the unlink path touches."""

    def __init__(
        self,
        link_id: int = 4102,
        source: int = 501,
        target: int = 502,
        link_type: str = "CREDIT_CARD_PAYMENT",
        status: str = "AUTO_APPROVED",
    ) -> None:
        self.link_id = link_id
        self.source = source
        self.target = target
        self.link_type = link_type
        self.status = status
        self.reviewed_by: str | None = None
        self.deleted = False


class _LinkStoreCursor:
    """A cursor over one _LinkStore, so a write is visible to the next read."""

    def __init__(self, store: _LinkStore, executed: list[tuple]) -> None:
        self._store = store
        self._executed = executed
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self._executed.append((flat, params))
        store = self._store
        if "current_user" in flat:
            self._result = [("authenticated",)]
        elif flat.startswith("UPDATE transaction_links"):
            store.status = "USER_REJECTED"
            store.reviewed_by = params[0] if params else None
            self._result = []
        elif flat.startswith("DELETE FROM transaction_links"):
            store.deleted = True
            self._result = []
        elif flat.startswith(
            "SELECT source_transaction_id, target_transaction_id, link_type"
        ):
            self._result = [] if store.deleted else [
                (store.source, store.target, store.link_type, store.status)
            ]
        elif flat.startswith(
            "SELECT source_transaction_id, target_transaction_id FROM transaction_links"
        ):
            live = store.status == "USER_REJECTED" and not store.deleted
            self._result = [(store.source, store.target)] if live else []
        else:
            # _resync_transaction_statuses probes the transactions table; those
            # rows are not modelled here, so it finds nothing and writes nothing.
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _LinkStoreConn:
    def __init__(self, store, executed):
        self._store = store
        self._executed = executed

    def cursor(self):
        return _LinkStoreCursor(self._store, self._executed)

    def commit(self):
        pass

    def rollback(self):  # pragma: no cover - only on failure
        pass


class _LinkStorePool:
    def __init__(self, store, executed):
        self._conn_obj = _LinkStoreConn(store, executed)

    def getconn(self):
        return self._conn_obj

    def putconn(self, conn):
        pass


def _unlink_db(store: _LinkStore):
    from db.database_pg import DatabasePg

    executed: list[tuple] = []
    return DatabasePg(_LinkStorePool(store, executed), "user-under-test"), executed


def test_an_unlink_rejects_the_row_instead_of_deleting_it():
    store = _LinkStore()
    db, executed = _unlink_db(store)

    result = db.reject_transaction_link(store.link_id, reviewed_by="user-under-test")

    assert store.deleted is False, "the row IS the memory — it must survive"
    assert store.status == "USER_REJECTED"
    assert store.reviewed_by == "user-under-test"
    assert result["status"] == "USER_REJECTED"
    assert result["remembered"] is True

    statements = [sql for sql, _ in executed]
    assert not any(s.startswith("DELETE FROM transaction_links") for s in statements)
    update = next(s for s in statements if s.startswith("UPDATE transaction_links"))
    assert "reviewed_by = %s" in update and "reviewed_at = NOW()" in update


def test_an_unlink_re_derives_both_sides_and_audits_the_action():
    store = _LinkStore()
    db, executed = _unlink_db(store)

    db.reject_transaction_link(store.link_id)

    # Both legs are handed to the status re-derivation, in the same DB
    # transaction as the rejection.
    resynced = [
        params[1]
        for sql, params in executed
        if sql.startswith("SELECT t.status,") and params
    ]
    assert resynced == [store.source, store.target]
    assert any("'UNLINK'" in sql for sql, _ in executed)


def test_a_manual_link_is_unlinked_the_same_way():
    """A rejection marker is harmless on the user's own link, and useful.

    It stops the auto linker re-proposing that pair, and the dedup index is
    partial on ``status != 'USER_REJECTED'`` so the same pair can still be
    linked again by hand.
    """
    store = _LinkStore(status="USER_APPROVED")
    db, _ = _unlink_db(store)

    db.reject_transaction_link(store.link_id)

    assert store.deleted is False
    assert store.status == "USER_REJECTED"


def test_the_unlinked_pair_is_not_re_proposed_by_the_next_run():
    """The whole point, end to end: unlink, then run the linker again.

    The rejected set is read back through the real ``get_user_rejected_link_pairs``
    contract — the same method the reconciliation endpoint calls — so this test
    fails if the unlink ever goes back to deleting the row.
    """
    store = _LinkStore(source=501, target=502)
    db, _ = _unlink_db(store)

    assert db.get_user_rejected_link_pairs() == set(), "nothing rejected yet"
    db.reject_transaction_link(store.link_id)
    rejected = db.get_user_rejected_link_pairs()
    assert rejected == {(501, 502)}

    result = run_transaction_linking(_paired_rows(), rejected_pairs=rejected)

    assert {(link.source_transaction_id, link.target_transaction_id)
            for link in result.links} == {(503, 504)}, \
        "only the pair the user never touched may be re-linked"


def test_a_rejected_leg_keeps_its_chain_and_is_filtered_out_of_chain_reads():
    """Rejecting one leg keeps chain_id/chain_position; the readers exclude it."""
    import inspect

    from db.database_pg import DatabasePg

    store = _LinkStore()
    db, executed = _unlink_db(store)
    db.reject_transaction_link(store.link_id)

    assert not any("chain_id" in sql for sql, _ in executed), (
        "a rejected leg keeps the chain it was part of — the unlink neither "
        "clears the chain_id nor renumbers the siblings"
    )
    for reader in (
        DatabasePg.get_transaction_chain,
        DatabasePg.get_transaction_links,
        DatabasePg.get_linked_transaction_info,
    ):
        assert "USER_REJECTED" in inspect.getsource(reader), (
            f"{reader.__name__} must not read a rejected leg back into a chain"
        )


# ── What the two rows become once the link is rejected ─────────────────

class _ResyncCursor:
    """Answers the one probe _resync_transaction_statuses makes per row."""

    def __init__(self, current: str, has_approved=False, has_pending=False,
                 has_link=False):
        self._row = (current, has_approved, has_pending, has_link)
        self._is_probe = False
        self.writes: list[tuple] = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self._is_probe = flat.startswith("SELECT t.status,")
        if flat.startswith("UPDATE transactions"):
            self.writes.append((flat, params))

    def fetchone(self):
        return self._row if self._is_probe else None


def _resync_one(current: str, **flags) -> tuple[list[dict], _ResyncCursor]:
    from db.database_pg import DatabasePg

    db = DatabasePg(_FakePool([], []), "user-under-test")
    cur = _ResyncCursor(current, **flags)
    return db._resync_transaction_statuses(cur, [501]), cur


def test_a_row_whose_only_link_is_rejected_leaves_linked():
    """LINKED with nothing left proving it is UNMATCHED, not LINKED.

    The "OnlineTransfer,TF…" leg therefore stops claiming a counterpart it no
    longer has. It does not go straight back to IGNORED — the next Match run's
    non-reconcilable rule re-stamps it, which is the same path that put it
    there the first time.
    """
    changed, cur = _resync_one("LINKED")

    assert changed == [{"transaction_id": 501, "from": "LINKED", "to": "UNMATCHED"}]
    assert cur.writes and cur.writes[0][1][0] == "UNMATCHED"


def test_a_rejected_link_never_un_proves_a_matched_row():
    """A leg with a receipt of its own keeps its own proof."""
    changed, _ = _resync_one("LINKED", has_approved=True)

    assert changed == [{"transaction_id": 501, "from": "LINKED", "to": "MATCHED"}]


def test_one_rejected_leg_of_a_fan_out_does_not_unlink_the_anchor():
    """The anchor still carries other legs, so it stays LINKED."""
    changed, cur = _resync_one("LINKED", has_link=True)

    assert changed == []
    assert cur.writes == []


# ── The LLM 4th pass honours the same rejections ───────────────────────

def test_the_fourth_pass_drops_a_rejected_pair_at_candidate_generation():
    from core.llm_transaction_linker import (
        _generate_candidate_pairs as llm_generate_candidate_pairs,
    )

    rows = [
        _cad_debit(501, "81.45", date(2026, 3, 9)),
        _card_credit(502, "-81.45", date(2026, 3, 9)),
    ]
    assert llm_generate_candidate_pairs(rows), "sanity: the pair is a candidate"

    assert llm_generate_candidate_pairs(rows, rejected_pairs={(501, 502)}) == []
    assert llm_generate_candidate_pairs(rows, rejected_pairs={(502, 501)}) == [], \
        "a rejection holds whichever way round the link row stored it"


def test_a_rejected_pair_is_never_sent_to_the_model():
    """Not merely dropped from the result — never asked about in the first place."""
    from unittest.mock import patch

    from core.llm_transaction_linker import run_llm_transaction_linking

    rows = [
        _cad_debit(501, "81.45", date(2026, 3, 9)),
        _card_credit(502, "-81.45", date(2026, 3, 9)),
    ]
    with patch("core.llm_transaction_linker._call_llm_for_linking") as mock_llm:
        mock_llm.return_value = {"evaluations": []}
        result = run_llm_transaction_linking(rows, rejected_pairs={(501, 502)})

    assert not mock_llm.called
    assert result.links == []


def test_both_fourth_pass_call_sites_pass_the_rejected_pairs():
    """The gate is worth nothing if the endpoint forgets to hand the set over."""
    import inspect
    import re

    import server.api.reconciliation as recon

    source = inspect.getsource(recon)
    calls = re.findall(
        r"run_llm_transaction_linking\((.*?)\n\s*\)", source, flags=re.S
    )
    assert len(calls) == 2, f"expected 2 call sites, found {len(calls)}"
    for call in calls:
        assert "rejected_pairs=db.get_user_rejected_link_pairs()" in call

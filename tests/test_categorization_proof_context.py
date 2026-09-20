"""What counts as PROOF when the categorization agent re-keys a transaction.

Every row, id, vendor and amount in this module is invented — see
``tests/fixtures/README.md``.

``DatabasePg.load_categorization_proof_context`` decides which receipt gets to
speak for a transaction: the vendor it returns replaces the transaction's
description as the clustering key, and its line items become the primary signal
in the later categorization prompts.

Only a match some decision stands behind may do that. PENDING_REVIEW is the
engine saying "I am not sure, a human should look", which is precisely the case
where the receipt must NOT be allowed to rewrite what the transaction is. In
the scenario modelled below a small payment-platform transfer fee is
tentatively paired with an unrelated courier receipt at 0.58 confidence:
accepting that pair as proof would give the fee the courier's vendor key and
file it under delivery, and two rows down a second fee would become a
restaurant meal. Rejecting either match afterwards does not undo it, because
the vendor key is already written.

The fake cursor below honours the status predicate in the SQL it is given, so
widening that predicate makes these tests fail rather than pass quietly.
"""

from __future__ import annotations

import re

import pytest

from db.database_pg import DatabasePg

USER = "user-proof-context"

_IN_LIST = re.compile(r"status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_NOT_EQUAL = re.compile(r"status\s*!=\s*'([A-Z_]+)'", re.IGNORECASE)


def _sql_allows(sql: str, status: str) -> bool:
    """Apply the SQL's own status predicate to one candidate row."""
    in_list = _IN_LIST.search(sql)
    if in_list:
        allowed = {part.strip().strip("'") for part in in_list.group(1).split(",")}
        return status in allowed
    not_equal = _NOT_EQUAL.search(sql)
    if not_equal:
        return status != not_equal.group(1)
    return True


# (transaction_id, status, vendor, total, currency, confidence)
#
# 301 and 302 are the two transfer fees the engine is unsure about; 303 and 304
# are receipts a decision stands behind; 305 is one a human threw out.
MATCH_ROWS = [
    (301, "PENDING_REVIEW", "Meridian Courier", "18.90", "CAD", 0.58),
    (302, "PENDING_REVIEW", "Sample Street Cafe", "24.75", "CAD", 0.48),
    (303, "AUTO_APPROVED", "Harbourline Logistics Ltd.", "9876.54", "CAD", 0.97),
    (304, "USER_APPROVED", "Bluepeak Software Inc", "300.00", "CAD", 0.91),
    (305, "USER_REJECTED", "Cedarview Supplies Ltd", "104.55", "CAD", 0.31),
]

# (source_transaction_id, target_transaction_id, status)
LINK_ROWS = [
    (304, 310, "USER_APPROVED"),    # approved chain: 310 inherits Bluepeak
    (303, 311, "PENDING_REVIEW"),   # unreviewed chain: 311 inherits nothing
]


class _Cursor:
    def __init__(self) -> None:
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        if "reconciliation_matches" in sql:
            self._rows = [
                (txn_id, vendor, total, currency, None, None, None, "f.pdf", conf)
                for txn_id, status, vendor, total, currency, conf in MATCH_ROWS
                if _sql_allows(sql, status)
            ]
            self._rows.sort(key=lambda r: r[8], reverse=True)
        elif "transaction_links" in sql:
            self._rows = [
                (src, tgt) for src, tgt, status in LINK_ROWS
                if _sql_allows(sql, status)
            ]
        else:  # pragma: no cover - the method issues only these two queries
            raise AssertionError(f"unexpected query: {sql[:80]}")

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def cursor(self):
        return _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def proof() -> dict[int, dict]:
    db = DatabasePg.__new__(DatabasePg)
    db.user_id = USER
    db._conn = lambda: _Conn()
    return db.load_categorization_proof_context()


def test_pending_review_match_is_not_proof(proof):
    """The two fee rows that would have acquired a courier and a cafe identity."""
    assert 301 not in proof
    assert 302 not in proof


def test_approved_matches_are_proof(proof):
    assert proof[303]["vendor"] == "Harbourline Logistics Ltd."
    assert proof[303]["source"] == "direct"
    assert proof[304]["vendor"] == "Bluepeak Software Inc"


def test_rejected_match_is_not_proof(proof):
    assert 305 not in proof


def test_proof_travels_along_an_approved_link(proof):
    assert proof[310]["vendor"] == "Bluepeak Software Inc"
    assert proof[310]["source"] == "linked"
    assert proof[310]["link_depth"] == 1


def test_proof_does_not_travel_along_an_unreviewed_link(proof):
    """An unreviewed link is the same kind of guess as an unreviewed match."""
    assert 311 not in proof


def test_the_fake_cursor_can_actually_fail():
    """Guard the guard: a predicate that merely excludes USER_REJECTED lets the
    PENDING_REVIEW rows through, so the tests above fail if the SQL is widened
    rather than passing on a fake that cannot tell the difference."""
    wide = "WHERE rm.user_id = %s AND rm.status != 'USER_REJECTED'"
    assert _sql_allows(wide, "PENDING_REVIEW") is True
    assert _sql_allows(wide, "USER_REJECTED") is False
    narrow = "WHERE rm.user_id = %s AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')"
    assert _sql_allows(narrow, "PENDING_REVIEW") is False
    assert _sql_allows(narrow, "AUTO_APPROVED") is True

"""POST /api/transactions/reviews/approve-all only approves confident matches.

"Approve all" settles a batch nobody looked at one by one, so it may only
settle what the matcher was already confident about. The floor is
``RECONCILIATION_BULK_APPROVE_MIN_CONFIDENCE``; a request may raise it for
itself and never lower it. Everything under the bar stays PENDING_REVIEW,
writes no audit row, and comes back in ``skipped_below_threshold``.

The database is a mock: the endpoint reads ``db.get_pending_reviews()`` and
then writes through a raw cursor, so the assertions below read the SQL that
cursor was actually handed.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import config.settings as settings
from models.match import MatchStatus, MatchType, ReconciliationMatch
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

ENDPOINT = "/api/transactions/reviews/approve-all"

MOCK_USER = AuthUser(
    id="test-uuid-bulk-approve",
    email="reviewer@example.com",
    role="user",
    email_verified=True,
)

SETTING_DEFAULT = 0.85


# ── Helpers ──────────────────────────────────────────────────────────────


def _match(match_id: int, confidence: float) -> ReconciliationMatch:
    return ReconciliationMatch(
        id=match_id,
        transaction_id=100 + match_id,
        document_id=200 + match_id,
        confidence_score=confidence,
        amount_score=confidence,
        date_score=confidence,
        vendor_score=confidence,
        match_type=MatchType.ONE_TO_ONE,
        status=MatchStatus.PENDING_REVIEW,
    )


@dataclasses.dataclass
class _ScorelessMatch:
    """A pending row whose confidence_score came back NULL.

    ``ReconciliationMatch`` requires a float, but the endpoint reads whatever
    ``db.get_pending_reviews()`` hands it, and the column is nullable. A row
    with no score has not cleared any bar, so it must be skipped rather than
    crash the request or slip through.
    """

    id: int
    confidence_score: None = None


def _mock_db(pending: list[ReconciliationMatch]):
    """A DatabasePg stand-in whose cursor records every statement executed."""
    db = MagicMock()
    db.user_id = MOCK_USER.id
    db.get_pending_reviews = MagicMock(return_value=pending)

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    conn_ctx = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    db._conn.return_value = conn_ctx

    return db, cursor


def _statements(cursor, keyword: str) -> list[tuple]:
    """Params of every statement whose SQL contains ``keyword``."""
    return [
        call.args[1]
        for call in cursor.execute.call_args_list
        if keyword in call.args[0]
    ]


def _approved_ids(cursor) -> set[int]:
    # UPDATE ... WHERE id = %s AND user_id = %s  →  (reviewed_by, id, user_id)
    return {params[1] for params in _statements(cursor, "UPDATE reconciliation_matches")}


def _audited_ids(cursor) -> set[int]:
    # INSERT INTO audit_log ... VALUES (%s, 'match', %s, ...)  →  (user, id, user)
    return {params[1] for params in _statements(cursor, "INSERT INTO audit_log")}


@pytest.fixture()
def client():
    async def _user():
        return MOCK_USER

    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def default_setting(monkeypatch):
    """Pin the floor so the suite does not depend on the ambient environment."""
    monkeypatch.setattr(
        settings,
        "reconciliation_config",
        dataclasses.replace(
            settings.reconciliation_config,
            bulk_approve_min_confidence=SETTING_DEFAULT,
        ),
    )
    return SETTING_DEFAULT


# ── The setting is the floor ─────────────────────────────────────────────


class TestBulkApproveFloor:
    def test_setting_default_is_085(self):
        """A fresh checkout approves at 0.85 — the floor the UI names to the user."""
        assert (
            float(settings.ReconciliationConfig().bulk_approve_min_confidence)
            == SETTING_DEFAULT
        )

    def test_below_threshold_rows_are_skipped_and_stay_pending(
        self, client, default_setting
    ):
        db, cursor = _mock_db([_match(1, 0.84), _match(2, 0.50), _match(3, 0.90)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert body["approved"] == 1
        assert body["skipped_below_threshold"] == 2
        # Only the confident one was touched; 1 and 2 are still PENDING_REVIEW
        # because no statement of any kind was executed against them.
        assert _approved_ids(cursor) == {3}

    def test_skipped_rows_are_not_audit_logged(self, client, default_setting):
        db, cursor = _mock_db([_match(1, 0.10), _match(2, 0.99)])
        app.dependency_overrides[get_db] = lambda: db

        client.post(ENDPOINT)

        assert _audited_ids(cursor) == {2}

    def test_approved_rows_are_audit_logged(self, client, default_setting):
        db, cursor = _mock_db([_match(1, 0.86), _match(2, 0.99)])
        app.dependency_overrides[get_db] = lambda: db

        client.post(ENDPOINT)

        assert _approved_ids(cursor) == {1, 2}
        assert _audited_ids(cursor) == {1, 2}

    def test_exactly_at_the_threshold_is_approved(self, client, default_setting):
        """The comparison is >=, not >. A match on the bar is a match."""
        db, cursor = _mock_db([_match(1, SETTING_DEFAULT)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert body["approved"] == 1
        assert body["skipped_below_threshold"] == 0
        assert _approved_ids(cursor) == {1}

    def test_a_missing_confidence_is_below_the_bar(self, client, default_setting):
        db, cursor = _mock_db([_ScorelessMatch(id=1)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert body == {
            "approved": 0,
            "skipped_below_threshold": 1,
            "min_confidence": SETTING_DEFAULT,
        }
        assert cursor.execute.call_count == 0

    def test_a_raised_setting_is_honoured(self, client, monkeypatch):
        monkeypatch.setattr(
            settings,
            "reconciliation_config",
            dataclasses.replace(
                settings.reconciliation_config, bulk_approve_min_confidence=0.95
            ),
        )
        db, cursor = _mock_db([_match(1, 0.90), _match(2, 0.96)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert body["min_confidence"] == 0.95
        assert body["approved"] == 1
        assert _approved_ids(cursor) == {2}


# ── min_confidence may only raise the bar ────────────────────────────────


class TestBulkApproveRequestThreshold:
    def test_a_higher_min_confidence_raises_the_bar(self, client, default_setting):
        db, cursor = _mock_db([_match(1, 0.86), _match(2, 0.94), _match(3, 0.99)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT, json={"min_confidence": 0.95}).json()

        assert body["min_confidence"] == 0.95
        assert body["approved"] == 1
        assert body["skipped_below_threshold"] == 2
        assert _approved_ids(cursor) == {3}

    def test_a_lower_min_confidence_is_ignored_not_rejected(
        self, client, default_setting
    ):
        """The endpoint can never approve MORE than the configured floor allows."""
        db, cursor = _mock_db([_match(1, 0.10), _match(2, 0.84), _match(3, 0.85)])
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(ENDPOINT, json={"min_confidence": 0.0})

        assert resp.status_code == 200
        body = resp.json()
        assert body["min_confidence"] == SETTING_DEFAULT
        assert body["approved"] == 1
        assert body["skipped_below_threshold"] == 2
        assert _approved_ids(cursor) == {3}

    def test_min_confidence_equal_to_the_setting_changes_nothing(
        self, client, default_setting
    ):
        db, _cursor = _mock_db([_match(1, 0.85)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT, json={"min_confidence": SETTING_DEFAULT}).json()

        assert body["min_confidence"] == SETTING_DEFAULT
        assert body["approved"] == 1

    def test_null_min_confidence_uses_the_setting(self, client, default_setting):
        db, _cursor = _mock_db([_match(1, 0.9)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT, json={"min_confidence": None}).json()

        assert body["min_confidence"] == SETTING_DEFAULT
        assert body["approved"] == 1

    def test_empty_body_uses_the_setting(self, client, default_setting):
        db, _cursor = _mock_db([_match(1, 0.9)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT, json={}).json()

        assert body["min_confidence"] == SETTING_DEFAULT

    @pytest.mark.parametrize("value", [1.01, 2, -0.01, -1, 100])
    def test_out_of_range_min_confidence_is_422(self, client, default_setting, value):
        db, cursor = _mock_db([_match(1, 0.99)])
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(ENDPOINT, json={"min_confidence": value})

        assert resp.status_code == 422
        assert cursor.execute.call_count == 0

    @pytest.mark.parametrize("value", [0.0, 1.0])
    def test_the_range_bounds_are_accepted(self, client, default_setting, value):
        db, _cursor = _mock_db([])
        app.dependency_overrides[get_db] = lambda: db

        assert client.post(ENDPOINT, json={"min_confidence": value}).status_code == 200


# ── Response contract ────────────────────────────────────────────────────


class TestBulkApproveResponse:
    def test_response_keys(self, client, default_setting):
        db, _cursor = _mock_db([_match(1, 0.9), _match(2, 0.1)])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert set(body) == {"approved", "skipped_below_threshold", "min_confidence"}
        assert body == {
            "approved": 1,
            "skipped_below_threshold": 1,
            "min_confidence": SETTING_DEFAULT,
        }

    def test_nothing_pending_returns_zeroes(self, client, default_setting):
        db, cursor = _mock_db([])
        app.dependency_overrides[get_db] = lambda: db

        body = client.post(ENDPOINT).json()

        assert body == {
            "approved": 0,
            "skipped_below_threshold": 0,
            "min_confidence": SETTING_DEFAULT,
        }
        assert cursor.execute.call_count == 0

    def test_min_confidence_is_the_effective_threshold(self, client, default_setting):
        db, _cursor = _mock_db([])
        app.dependency_overrides[get_db] = lambda: db

        assert (
            client.post(ENDPOINT, json={"min_confidence": 0.93}).json()["min_confidence"]
            == 0.93
        )
        assert (
            client.post(ENDPOINT, json={"min_confidence": 0.10}).json()["min_confidence"]
            == SETTING_DEFAULT
        )

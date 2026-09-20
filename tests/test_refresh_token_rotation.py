"""Refresh-token rotation: the 60s grace, and theft that costs the family.

Strictly single-use rotation signs people out mid-run: with several tabs open on
a long bulk upload, the second concurrent ``POST /api/auth/refresh`` presents an
already-rotated token, gets a 401, and the SPA clears the session for the whole
browser. A grace window is what separates that from genuine theft.

So the rules are:

  * a refresh rotates the presented token into a new one in the same family;
  * presenting the old token again within ``_REFRESH_GRACE_SECONDS`` returns the
    *same* successor, as many times as it is asked, and mutates nothing - which
    is what makes racing tabs converge on one live pair;
  * presenting it after that, or presenting one whose successor has itself been
    rotated, is theft: the entire family is revoked and the answer is 401;
  * logout and a password change still end a session instantly, grace or not;
  * an unknown or expired token is refused and revokes nothing.

Everything below runs over an in-memory ``FakeStore`` and an injected clock - no
PostgreSQL, no sleeping on a 60-second boundary - the same split
``tests/test_document_queue.py`` uses for the queue.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# Import order matters, and not decoratively. server/local_auth.py reaches back
# into server.app for the slowapi limiter while its own module body is still
# running (the @_rate_limit on /token), so importing local_auth *first* hands
# server.app a router that has no routes on it yet - and every /api/auth/* POST
# answers 405. Importing the app first is the order the server process uses.
import server.app  # noqa: F401  (see above - must precede server.local_auth)
from server import local_auth
from server.local_auth import _REFRESH_GRACE_SECONDS, rotate_refresh_token

T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
USER = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# An in-memory stand-in for auth.refresh_tokens
# ---------------------------------------------------------------------------

class FakeStore:
    """The four statements rotation needs, over a dict of rows.

    Faithful where the rules depend on it: ``get`` hands back a *copy* (a real
    read cannot be mutated in place), ``mark_rotated`` only wins while
    ``rotated_at`` is still NULL, and ``revoke_family`` skips rows already
    revoked so the first reason recorded is the one that sticks.
    """

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.revocations: list[tuple[str, str]] = []

    # -- test helpers ------------------------------------------------------
    def seed(
        self,
        *,
        user_id: str = USER,
        family_id: str | None = None,
        generation: int = 0,
        now: datetime = T0,
        ttl_days: int = 30,
        revoked: bool = False,
        revoked_reason: str | None = None,
    ) -> str:
        """Write a generation-0 token the way a fresh sign-in would."""
        token = f"tok-{next(self._ids):04d}"
        self.rows[token] = {
            "token": token,
            "user_id": user_id,
            "family_id": family_id or f"fam-{token}",
            "generation": generation,
            "revoked": revoked,
            "revoked_at": now if revoked else None,
            "revoked_reason": revoked_reason,
            "rotated_at": None,
            "replaced_by": None,
            "expires_at": now + timedelta(days=ttl_days),
            "created_at": now,
        }
        return token

    def family_of(self, token: str) -> list[dict]:
        family_id = self.rows[token]["family_id"]
        return [r for r in self.rows.values() if r["family_id"] == family_id]

    # -- the store interface ----------------------------------------------
    def get(self, token: str) -> dict | None:
        row = self.rows.get(token)
        return dict(row) if row else None

    def insert(
        self,
        *,
        token: str,
        user_id: str,
        family_id: str,
        generation: int,
        expires_at: datetime,
    ) -> None:
        assert token not in self.rows, "token collision - the mint is broken"
        self.rows[token] = {
            "token": token,
            "user_id": user_id,
            "family_id": family_id,
            "generation": generation,
            "revoked": False,
            "revoked_at": None,
            "revoked_reason": None,
            "rotated_at": None,
            "replaced_by": None,
            "expires_at": expires_at,
            "created_at": None,
        }

    def mark_rotated(self, token: str, *, replaced_by: str, rotated_at: datetime) -> bool:
        row = self.rows[token]
        if row["rotated_at"] is not None:
            return False
        row["rotated_at"] = rotated_at
        row["replaced_by"] = replaced_by
        return True

    def revoke_family(self, family_id: str, *, reason: str, at: datetime) -> int:
        self.revocations.append((family_id, reason))
        touched = 0
        for row in self.rows.values():
            if row["family_id"] == family_id and not row["revoked"]:
                row["revoked"] = True
                row["revoked_at"] = at
                row["revoked_reason"] = reason
                touched += 1
        return touched


@pytest.fixture()
def store() -> FakeStore:
    return FakeStore()


# ---------------------------------------------------------------------------
# The ordinary path
# ---------------------------------------------------------------------------

class TestRotation:
    def test_rotation_issues_a_new_token_and_retires_the_old(self, store: FakeStore) -> None:
        first = store.seed()

        decision = rotate_refresh_token(store, first, now=T0, ttl_days=30)

        assert decision.ok
        assert decision.outcome == "rotated"
        assert decision.user_id == USER
        assert decision.refresh_token and decision.refresh_token != first

        old = store.rows[first]
        assert old["rotated_at"] == T0, "the presented token must be marked rotated"
        assert old["replaced_by"] == decision.refresh_token
        assert old["revoked"] is False, "rotated is not revoked - the grace needs this row alive"

        new = store.rows[decision.refresh_token]
        assert new["family_id"] == old["family_id"], "a rotation stays inside the family"
        assert new["generation"] == 1
        assert new["expires_at"] == T0 + timedelta(days=30)
        assert store.revocations == []

    def test_the_successor_rotates_again_the_same_way(self, store: FakeStore) -> None:
        first = store.seed()
        second = rotate_refresh_token(store, first, now=T0).refresh_token
        assert second is not None

        later = T0 + timedelta(minutes=55)
        third = rotate_refresh_token(store, second, now=later)

        assert third.outcome == "rotated"
        assert store.rows[third.refresh_token]["generation"] == 2
        assert store.revocations == []


# ---------------------------------------------------------------------------
# The grace window
# ---------------------------------------------------------------------------

class TestGraceWindow:
    def test_replay_inside_the_grace_returns_the_same_successor_twice(self, store: FakeStore) -> None:
        """Two more tabs present the same old token; all three end up on one pair."""
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token

        second_tab = rotate_refresh_token(store, first, now=T0 + timedelta(seconds=2))
        third_tab = rotate_refresh_token(store, first, now=T0 + timedelta(seconds=9))

        assert second_tab.ok and third_tab.ok
        assert second_tab.outcome == "replayed" and third_tab.outcome == "replayed"
        assert second_tab.refresh_token == successor
        assert third_tab.refresh_token == successor
        assert second_tab.user_id == USER

        # Nothing was revoked and nothing else was minted - a replay is a read.
        assert store.revocations == []
        assert all(not r["revoked"] for r in store.rows.values())
        assert len(store.rows) == 2

    def test_replay_on_the_grace_boundary_is_still_honoured(self, store: FakeStore) -> None:
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token

        at_boundary = rotate_refresh_token(
            store, first, now=T0 + timedelta(seconds=_REFRESH_GRACE_SECONDS)
        )

        assert at_boundary.outcome == "replayed"
        assert at_boundary.refresh_token == successor
        assert store.revocations == []

    def test_replay_after_the_grace_revokes_the_whole_family(self, store: FakeStore) -> None:
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token

        late = rotate_refresh_token(
            store, first, now=T0 + timedelta(seconds=_REFRESH_GRACE_SECONDS + 1)
        )

        assert not late.ok
        assert late.outcome == "reuse_detected"
        assert late.refresh_token is None
        assert store.revocations == [(store.rows[first]["family_id"], "reuse_detected")]
        assert all(r["revoked"] for r in store.family_of(first))
        assert store.rows[successor]["revoked_reason"] == "reuse_detected"

    def test_a_revoked_family_refuses_its_newest_token(self, store: FakeStore) -> None:
        """The live tab loses its session too - that is the point of a family."""
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token
        rotate_refresh_token(store, first, now=T0 + timedelta(minutes=5))  # theft

        after = rotate_refresh_token(store, successor, now=T0 + timedelta(minutes=6))

        assert not after.ok
        assert after.outcome == "revoked"
        assert len(store.revocations) == 1, "a refused revoked token revokes nothing further"


# ---------------------------------------------------------------------------
# Theft detection
# ---------------------------------------------------------------------------

class TestReuseDetection:
    def test_two_generations_behind_revokes_the_family_inside_the_grace(self, store: FakeStore) -> None:
        """gen 0 is replayed 1s after gen 1 rotated - but gen 1 is already spent."""
        gen0 = store.seed()
        gen1 = rotate_refresh_token(store, gen0, now=T0).refresh_token
        gen2 = rotate_refresh_token(store, gen1, now=T0 + timedelta(seconds=30)).refresh_token
        assert gen2 is not None

        replay = rotate_refresh_token(store, gen0, now=T0 + timedelta(seconds=31))

        assert not replay.ok
        assert replay.outcome == "reuse_detected"
        assert store.revocations == [(store.rows[gen0]["family_id"], "reuse_detected")]
        assert all(r["revoked"] for r in store.family_of(gen0))

    def test_a_missing_successor_is_treated_as_theft(self, store: FakeStore) -> None:
        """replaced_by pointing at nothing cannot prove a live tab, so it is reuse."""
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token
        del store.rows[successor]

        replay = rotate_refresh_token(store, first, now=T0 + timedelta(seconds=1))

        assert replay.outcome == "reuse_detected"
        assert store.rows[first]["revoked"] is True

    def test_theft_of_one_family_leaves_another_alone(self, store: FakeStore) -> None:
        """Two devices are two families; one being stolen must not sign out the other."""
        device_a = store.seed()
        device_b = store.seed()
        rotate_refresh_token(store, device_a, now=T0)
        live_b = rotate_refresh_token(store, device_b, now=T0).refresh_token

        stolen = rotate_refresh_token(store, device_a, now=T0 + timedelta(minutes=10))
        assert stolen.outcome == "reuse_detected"

        still_fine = rotate_refresh_token(store, live_b, now=T0 + timedelta(minutes=11))
        assert still_fine.outcome == "rotated"
        assert store.rows[device_b]["revoked"] is False


# ---------------------------------------------------------------------------
# Refusals that revoke nothing
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_unknown_token_revokes_nothing(self, store: FakeStore) -> None:
        live = store.seed()

        decision = rotate_refresh_token(store, "not-a-token-anyone-issued", now=T0)

        assert not decision.ok
        assert decision.outcome == "unknown"
        assert store.revocations == []
        assert store.rows[live]["revoked"] is False

    def test_expired_token_revokes_nothing(self, store: FakeStore) -> None:
        first = store.seed(ttl_days=30)

        decision = rotate_refresh_token(store, first, now=T0 + timedelta(days=31))

        assert not decision.ok
        assert decision.outcome == "expired"
        assert store.revocations == []
        assert store.rows[first]["rotated_at"] is None

    def test_logout_revocation_is_refused_without_further_revocation(self, store: FakeStore) -> None:
        """sign_out() / set_password() revoke by user_id; that must outrank the grace."""
        first = store.seed(revoked=True, revoked_reason=local_auth.REVOKE_REASON_LOGOUT)

        decision = rotate_refresh_token(store, first, now=T0)

        assert not decision.ok
        assert decision.outcome == "revoked"
        assert store.revocations == []
        assert store.rows[first]["revoked_reason"] == "logout"

    def test_a_token_revoked_mid_grace_stops_working(self, store: FakeStore) -> None:
        first = store.seed()
        successor = rotate_refresh_token(store, first, now=T0).refresh_token
        # Someone signs out: every one of this user's tokens is revoked.
        for row in store.rows.values():
            row["revoked"] = True
            row["revoked_reason"] = local_auth.REVOKE_REASON_LOGOUT

        assert rotate_refresh_token(store, first, now=T0 + timedelta(seconds=5)).outcome == "revoked"
        assert rotate_refresh_token(store, successor, now=T0 + timedelta(seconds=5)).outcome == "revoked"
        assert store.revocations == []


# ---------------------------------------------------------------------------
# Two requests, one token, same instant
# ---------------------------------------------------------------------------

class TestConcurrentRotation:
    def test_a_lost_rotation_race_converges_on_the_winners_token(self, store: FakeStore) -> None:
        """Both requests read an un-rotated row; only one mark_rotated may win."""
        first = store.seed()
        winner_token = "tok-winner"

        original_mark = store.mark_rotated
        def steal_the_row(token: str, *, replaced_by: str, rotated_at: datetime) -> bool:
            # Stand in for the other request committing microseconds earlier.
            store.insert(
                token=winner_token,
                user_id=USER,
                family_id=store.rows[token]["family_id"],
                generation=1,
                expires_at=rotated_at + timedelta(days=30),
            )
            original_mark(token, replaced_by=winner_token, rotated_at=rotated_at)
            store.mark_rotated = original_mark  # only the first call races
            return original_mark(token, replaced_by=replaced_by, rotated_at=rotated_at)
        store.mark_rotated = steal_the_row  # type: ignore[method-assign]

        decision = rotate_refresh_token(store, first, now=T0)

        assert decision.ok
        assert decision.refresh_token == winner_token, "the loser adopts the winner's successor"
        assert store.rows[first]["replaced_by"] == winner_token
        assert store.revocations == []


# ---------------------------------------------------------------------------
# The session the route hands back
# ---------------------------------------------------------------------------

def _user_record() -> dict:
    return {
        "id": USER,
        "email": "demo@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "email_confirmed_at": "2026-09-01T00:00:00+00:00",
        "confirmed_at": "2026-09-01T00:00:00+00:00",
        "last_sign_in_at": None,
        "created_at": "2026-09-01T00:00:00+00:00",
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {"email_verified": True},
        "is_active": True,
        "_email_confirmed_at_dt": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


class TestBuildSession:
    def test_a_supplied_refresh_token_is_handed_back_unchanged(self) -> None:
        session = local_auth.build_session(_user_record(), refresh_token="rotation-chose-this")

        assert session["refresh_token"] == "rotation-chose-this"
        assert session["access_token"], "a replay still gets a fresh access token"
        assert session["token_type"] == "bearer"
        assert "_email_confirmed_at_dt" not in session["user"], "private keys are stripped"

    def test_no_refresh_token_starts_a_new_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sign-in, signup, confirm and the Google exchange all take this path."""
        calls: list[str] = []
        monkeypatch.setattr(
            local_auth, "_issue_refresh_token", lambda uid: calls.append(uid) or "brand-new"
        )

        session = local_auth.build_session(_user_record())

        assert calls == [USER]
        assert session["refresh_token"] == "brand-new"


class TestRefreshEndpoint:
    """POST /api/auth/refresh, with the store and the user lookup faked out."""

    @pytest.fixture()
    def client(self, store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from server.app import app

        monkeypatch.setattr(local_auth, "_refresh_store", lambda: store)
        monkeypatch.setattr(local_auth, "get_user_by_id", lambda uid: _user_record())
        return TestClient(app)

    def test_rotation_answers_200_with_the_new_pair(self, client: TestClient, store: FakeStore) -> None:
        first = store.seed(now=datetime.now(timezone.utc))

        resp = client.post("/api/auth/refresh", json={"refresh_token": first})

        assert resp.status_code == 200
        body = resp.json()
        assert body["refresh_token"] != first
        assert body["access_token"]
        assert body["user"]["id"] == USER
        assert store.rows[first]["rotated_at"] is not None

    def test_a_second_tab_inside_the_grace_gets_the_same_token(
        self, client: TestClient, store: FakeStore
    ) -> None:
        first = store.seed(now=datetime.now(timezone.utc))

        one = client.post("/api/auth/refresh", json={"refresh_token": first})
        two = client.post("/api/auth/refresh", json={"refresh_token": first})

        assert one.status_code == 200 and two.status_code == 200
        assert one.json()["refresh_token"] == two.json()["refresh_token"]
        assert two.json()["access_token"], "the replay still carries a usable access token"
        assert store.revocations == []

    def test_reuse_after_the_grace_answers_401(self, client: TestClient, store: FakeStore) -> None:
        now = datetime.now(timezone.utc)
        first = store.seed(now=now)
        # Rotated two minutes ago: well outside the 60s window.
        successor = store.seed(now=now, family_id=store.rows[first]["family_id"], generation=1)
        store.rows[first]["rotated_at"] = now - timedelta(minutes=2)
        store.rows[first]["replaced_by"] = successor

        resp = client.post("/api/auth/refresh", json={"refresh_token": first})

        assert resp.status_code == 401
        assert resp.json() == {"error": "Invalid or expired refresh token."}
        assert store.rows[successor]["revoked"] is True

    def test_an_unknown_token_answers_401(self, client: TestClient) -> None:
        resp = client.post("/api/auth/refresh", json={"refresh_token": "nope"})

        assert resp.status_code == 401
        assert resp.json() == {"error": "Invalid or expired refresh token."}

    def test_a_deactivated_account_answers_401(
        self, client: TestClient, store: FakeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            local_auth, "get_user_by_id", lambda uid: {**_user_record(), "is_active": False}
        )
        first = store.seed(now=datetime.now(timezone.utc))

        resp = client.post("/api/auth/refresh", json={"refresh_token": first})

        assert resp.status_code == 401
        assert resp.json() == {"error": "Account unavailable."}

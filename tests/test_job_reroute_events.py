"""Re-filing a statement as a receipt is not a parse failure, and says so.

``POST /api/jobs/{id}/reroute`` is the "no, it's a receipt" button: the auto zone
guessed statement, the user disagreed, and the file gets re-filed without
re-uploading it and without extracting it twice.

Closing the ``statements`` row is part of that — leaving it in ``processing``
spins the transactions page forever — but it must not be closed through
``fail_statement_row``, which fires ``statement_failed`` **and**
``statement_parse_error``: the pair the parsing card renders as an
unreadable-statement error. Correcting the app's own guess must not put a red
error in front of the person who just corrected it.

So the reroute emits ``statement_rerouted`` and neither failure event.
``job_updated`` still goes out for the new receipt job, because that is the row
the user watches from here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db
from server.jobs import document_queue as dq

FAKE_USER = AuthUser(
    id="user-reroute", email="reroute@example.com", role="authenticated", email_verified=True
)

STATEMENT_JOB = {
    "id": "aaaaaaaa-1111-2222-3333-444444444444",
    "user_id": FAKE_USER.id,
    "kind": "statement",
    "filename": "not actually a statement.pdf",
    "stored_path": "stored/mystery.pdf",
    "file_hash": "hash-mystery",
    "status": "queued",
    "position": 2,
    "attempts": 0,
    "max_attempts": 3,
    "transient_waits": 0,
    "next_attempt_at": None,
    "pages_total": 1,
    "pages_done": 0,
    "error_code": None,
    "error_message": None,
    "result": {"statement_id": "stmt-to-close", "detected_as": "statement"},
    "created_at": None,
    "started_at": None,
    "finished_at": None,
    "updated_at": None,
}


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.user_id = FAKE_USER.id
    db.get_processing_job = MagicMock(return_value=dict(STATEMENT_JOB))
    db.delete_processing_job = MagicMock(return_value=True)
    db.update_statement_status = MagicMock(return_value=1)
    db.count_processing_jobs = MagicMock(return_value={"queued": 1, "running": 0})
    db.processing_job_batch = MagicMock(return_value={"total": 1, "finished": 0})

    def _insert_job(**kwargs):
        job = dict(STATEMENT_JOB)
        job.update({
            "id": "bbbbbbbb-5555-6666-7777-888888888888",
            "kind": kwargs["kind"],
            "filename": kwargs["filename"],
            "stored_path": kwargs.get("stored_path"),
            "file_hash": kwargs.get("file_hash"),
            "pages_total": kwargs.get("pages_total"),
            "status": "queued",
            "position": 9,
            "result": kwargs.get("result"),
        })
        return job

    db.insert_processing_job = MagicMock(side_effect=_insert_job)
    return db


@pytest.fixture
def db() -> MagicMock:
    return _fake_db()


@pytest.fixture
def events(db):
    """Every SSE event the reroute produces, in order, as (name, payload)."""
    async def _user():
        return FAKE_USER

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: db

    seen: list[tuple[str, dict]] = []

    def _send(user_id, event_type, data=None):
        seen.append((event_type, data or {}))

    # ``emit_job_event`` builds its own store for the ``ahead`` count and the
    # summary block; give it one that answers so ``job_updated`` really goes out.
    store = MagicMock()
    store.ahead_for.return_value = 0
    store.counts.return_value = {"queued": 1}
    store.batch.return_value = {"total": 1, "finished": 0}

    with patch("server.api.events.send_event", _send), \
         patch.object(dq, "PgJobStore", return_value=store), \
         patch.object(dq, "wake_worker"):
        yield seen
    app.dependency_overrides.clear()


@pytest.fixture
def client(events):
    return TestClient(app, raise_server_exceptions=False)


def _reroute(client: TestClient):
    return client.post(f"/api/jobs/{STATEMENT_JOB['id']}/reroute")


def test_reroute_emits_statement_rerouted_and_not_the_failure_pair(client, events):
    resp = _reroute(client)
    assert resp.status_code == 200, resp.text

    names = [name for name, _ in events]
    assert "statement_rerouted" in names
    assert "statement_failed" not in names, (
        "the user said it was never a statement - that is not a parse failure"
    )
    assert "statement_parse_error" not in names


def test_job_updated_still_goes_out_for_the_new_receipt_job(client, events):
    resp = _reroute(client)
    assert resp.status_code == 200

    updates = [payload for name, payload in events if name == "job_updated"]
    assert updates, "the queue panel has nothing to redraw without this"
    assert updates[-1]["kind"] == "receipt"
    assert updates[-1]["result"]["rerouted_from"] == "statement"


def test_the_rerouted_event_says_what_happened_and_offers_no_retry(client, events):
    assert _reroute(client).status_code == 200

    payload = next(p for name, p in events if name == "statement_rerouted")
    assert payload["statement_id"] == "stmt-to-close"
    assert payload["source_file"] == STATEMENT_JOB["filename"]
    assert payload["error_code"] == "REROUTED"
    assert payload["rerouted_to"] == "receipt"
    assert payload["retry_allowed"] is False, (
        "there is nothing to retry - the receipt job is the thing to watch now"
    )


def test_the_statements_row_is_still_closed(client, events, db):
    """Whatever events go out, the statements row must not be left spinning."""
    assert _reroute(client).status_code == 200
    db.update_statement_status.assert_called_once()
    assert db.update_statement_status.call_args.kwargs["status"] == "failed"


def test_a_genuine_parse_failure_still_emits_both_failure_events(db):
    """A genuine parse failure goes through the same helper, and that path must
    still raise both failure events."""
    from server.api.transactions import fail_statement_row

    seen: list[str] = []

    def _send(user_id, event_type, data=None):
        seen.append(event_type)

    with patch("server.api.events.send_event", _send):
        fail_statement_row(
            db, FAKE_USER.id, "stmt-x", "march.pdf",
            error_code="unable_to_parse", message="could not read it",
        )

    assert seen == ["statement_failed", "statement_parse_error"]

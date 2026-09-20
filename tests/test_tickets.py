"""Unit tests for server/tickets.py — the single-use auth ticket vault.

Some surfaces cannot carry an ``Authorization`` header: an ``EventSource`` SSE
subscription and a file download opened in a new tab both go out as a plain GET.
Those callers ask for a short-lived ticket instead, and these tests prove the
ticket is worth no more than the header would have been: it is bound to one user
and one scope, it can be spent exactly once, it expires, an unknown value buys
nothing, the tokens carry real entropy, and the vault refuses to grow without
bound.
"""

from __future__ import annotations

import time

import pytest

from server import tickets


@pytest.fixture(autouse=True)
def _clean_vault():
    tickets._vault.clear()
    yield
    tickets._vault.clear()


def test_issued_ticket_consumes_once_to_correct_user() -> None:
    t = tickets.issue("user-abc", "sse", ttl_seconds=60)
    assert tickets.consume(t, "sse") == "user-abc"


def test_ticket_is_single_use() -> None:
    t = tickets.issue("user-abc", "sse", ttl_seconds=60)
    assert tickets.consume(t, "sse") == "user-abc"
    assert tickets.consume(t, "sse") is None


def test_wrong_scope_does_not_consume_nor_leak_user() -> None:
    t = tickets.issue("user-abc", "sse", ttl_seconds=60)
    # Wrong scope: returns None
    assert tickets.consume(t, "file_download") is None
    # But the ticket is still usable for the correct scope.
    assert tickets.consume(t, "sse") == "user-abc"


def test_expired_ticket_is_rejected_and_pruned() -> None:
    t = tickets.issue("user-abc", "sse", ttl_seconds=60)
    # Rewind expiry
    entry = tickets._vault[t]
    tickets._vault[t] = tickets._TicketEntry(
        user_id=entry.user_id, scope=entry.scope, expires_at=time.time() - 1
    )
    assert tickets.consume(t, "sse") is None
    # Entry removed on expired-consume
    assert t not in tickets._vault


def test_unknown_ticket_returns_none() -> None:
    assert tickets.consume("definitely-not-a-real-ticket", "sse") is None


def test_file_download_scope_distinct_from_sse_scope() -> None:
    t_sse = tickets.issue("user-a", "sse", ttl_seconds=60)
    t_dl = tickets.issue("user-a", "file_download", ttl_seconds=60)
    assert tickets.consume(t_sse, "file_download") is None
    assert tickets.consume(t_dl, "sse") is None
    assert tickets.consume(t_sse, "sse") == "user-a"
    assert tickets.consume(t_dl, "file_download") == "user-a"


def test_issued_tokens_have_high_entropy() -> None:
    toks = {tickets.issue(f"u-{i}", "sse") for i in range(100)}
    # No collisions in a small sample means the generator is not a trivial one.
    assert len(toks) == 100
    # token_urlsafe(32) yields ~43-char URL-safe strings
    for t in toks:
        assert len(t) >= 40
        assert all(c.isalnum() or c in "-_" for c in t)


def test_vault_cap_enforced() -> None:
    original_cap = tickets._MAX_TICKETS
    try:
        tickets._MAX_TICKETS = 5
        for i in range(5):
            tickets.issue(f"u-{i}", "sse", ttl_seconds=3600)
        with pytest.raises(RuntimeError):
            tickets.issue("u-overflow", "sse", ttl_seconds=3600)
    finally:
        tickets._MAX_TICKETS = original_cap

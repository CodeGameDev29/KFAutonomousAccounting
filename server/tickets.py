"""Short-lived, single-use tickets for URL-based authentication.

Why this exists
---------------
Browsers can't set an `Authorization` header on plain navigations or on
`EventSource`. The obvious workaround — passing the raw session JWT as a
`?token=...` query parameter on SSE subscriptions and file downloads — puts a
credential in a URL, and URLs end up in:

- browser history + tab titles
- `Referer` headers sent to third-party images/fonts
- reverse-proxy access logs
- shared screenshots, crash reports, support tickets

A ticket is the alternative. The flow is:

1. Client POSTs `/api/events/ticket` (or `/api/files/ticket`) with its Bearer
   JWT in the Authorization header — the normal auth path.
2. Server mints an opaque random token bound to `(user_id, scope, expiry)`,
   stores it in the in-memory ticket vault, and returns it.
3. Client uses `?ticket=<opaque>` on the subsequent navigation or SSE connect.
4. Server consumes the ticket: verifies scope + expiry, marks it used (or
   deletes it), and proceeds as the authenticated user.

Properties
----------
- 60-second default TTL (configurable per issue).
- Single-use for downloads (deleted on consume).
- Multi-use inside a single SSE lifetime is NOT supported — SSE tickets are
  also consumed at connect time; the stream persists via the established
  connection, not via repeated auth.
- Opaque random token (`secrets.token_urlsafe(32)` → 256 bits of entropy).
- Scoped: a ticket minted for `sse` cannot be used to download a file.
- In-memory only. The FastAPI process is the source of truth, so every
  request that consumes a ticket has to reach the process that issued it.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

_logger = logging.getLogger(__name__)

TicketScope = Literal["sse", "file_download", "oauth_session"]

_DEFAULT_TTL_SECONDS = 60
_MAX_TICKETS = 10_000  # panic cap to prevent memory blow-up on a runaway client


@dataclass(frozen=True)
class _TicketEntry:
    user_id: str
    scope: TicketScope
    expires_at: float  # unix seconds
    email: Optional[str] = None
    provider: str = "email"
    email_verified: bool = False


@dataclass(frozen=True)
class TicketUser:
    """Snapshot of the AuthUser fields the ticket vault carries through.

    Stored at issue time, when the Bearer-resolved user is in hand, and
    returned at consume time so a downstream gate that branches on email or
    provider behaves the same as it would on the Bearer path.
    """
    user_id: str
    email: Optional[str]
    provider: str
    email_verified: bool


_vault: dict[str, _TicketEntry] = {}
_lock = threading.Lock()


def _sweep_expired_locked(now: float) -> None:
    """Caller must hold `_lock`."""
    if len(_vault) < 256:
        return  # avoid O(n) sweep on every issue in steady state
    stale = [k for k, v in _vault.items() if v.expires_at < now]
    for k in stale:
        _vault.pop(k, None)


def issue(
    user_id: str,
    scope: TicketScope,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    *,
    email: Optional[str] = None,
    provider: str = "email",
    email_verified: bool = False,
) -> str:
    """Mint a new ticket bound to (user_id, scope).

    Email/provider/email_verified are optional snapshots of the AuthUser
    that minted the ticket; they are returned by ``consume_user`` so that a
    downstream gate branching on email sees the same user shape as it would on
    the Bearer-auth path.

    Raises RuntimeError if the vault is pathologically full — this should
    never happen in practice and means something is issuing without consuming.
    """
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _lock:
        _sweep_expired_locked(now)
        if len(_vault) >= _MAX_TICKETS:
            # Aggressive sweep — drop anything expired regardless of vault size.
            stale = [k for k, v in _vault.items() if v.expires_at < now]
            for k in stale:
                _vault.pop(k, None)
            if len(_vault) >= _MAX_TICKETS:
                _logger.error(
                    "ticket vault full (%d) — refusing to issue for user=%s scope=%s",
                    len(_vault), user_id[:8], scope,
                )
                raise RuntimeError("Ticket vault is full")
        _vault[token] = _TicketEntry(
            user_id=user_id,
            scope=scope,
            expires_at=now + ttl_seconds,
            email=email,
            provider=provider,
            email_verified=email_verified,
        )
    return token


def consume(token: str, scope: TicketScope) -> Optional[str]:
    """Back-compat: returns user_id only. Prefer ``consume_user``."""
    user = consume_user(token, scope)
    return user.user_id if user else None


def consume_user(token: str, scope: TicketScope) -> Optional[TicketUser]:
    """Validate + consume a ticket. Returns the full TicketUser snapshot
    on success, None otherwise.

    A consumed ticket is removed from the vault — it cannot be replayed. A
    ticket minted for a different scope cannot be used here either; it remains
    in the vault (so a later consume with the right scope still works) but a
    wrong-scope consume leaks no information to the caller beyond "no match."
    """
    now = time.time()
    with _lock:
        entry = _vault.get(token)
        if entry is None:
            return None
        if entry.expires_at < now:
            _vault.pop(token, None)
            return None
        if entry.scope != scope:
            return None
        _vault.pop(token, None)
    return TicketUser(
        user_id=entry.user_id,
        email=entry.email,
        provider=entry.provider,
        email_verified=entry.email_verified,
    )


def stats() -> dict:
    """For /health or debugging only — never expose over a public endpoint."""
    now = time.time()
    with _lock:
        total = len(_vault)
        live = sum(1 for v in _vault.values() if v.expires_at >= now)
    return {"total": total, "live": live}

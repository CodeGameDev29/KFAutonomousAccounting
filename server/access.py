"""Access rules: every signed-in user has full access.

Access is binary. An account either exists and is signed in — in which case it
can do everything — or it does not. The one gate belongs to plain local
accounts: an email/password signup must have confirmed its address before it
may write.
(``AUTH_AUTOCONFIRM=1``, the default, confirms at signup, so the gate does not
fire with the shipped defaults; accounts created through Sign in with Google are
confirmed by the provider.)

The gate is a FastAPI dependency rather than an inline check so that a route
declares its requirement in its signature.
"""

from __future__ import annotations

import logging
import time as _time

from fastapi import Depends, HTTPException

from server.auth import AuthUser, get_current_user

logger = logging.getLogger(__name__)


# ── Email verification ───────────────────────────────────────────────

# Cache: user_id -> (verified: bool, expires_at: float)
_email_verified_cache: dict[str, tuple[bool, float]] = {}
_VERIFIED_CACHE_TTL = float("inf")  # permanent for verified users
_UNVERIFIED_CACHE_TTL = 30.0        # 30s for unverified (re-check soon)


def invalidate_email_verified_cache(user_id: str) -> None:
    """Drop any cached verification state for a user.

    Called when a user clicks the confirm link so subsequent requests
    don't see a stale 'unverified' entry.
    """
    _email_verified_cache.pop(user_id, None)


def is_email_verified(user: "AuthUser | str") -> bool:
    """Check whether a user has confirmed their email.

    Prefers the JWT claim (carried on ``AuthUser.email_verified``) since it's
    already authenticated and avoids a database round-trip. Falls back to
    reading ``auth.users`` directly for users whose JWT does not surface the
    claim.

    Fail-closed on any error for unknown-state users — an unverified user MUST
    NOT be silently allowed through. The short unverified-TTL means a freshly
    confirmed user retries quickly.

    Accepts either an ``AuthUser`` (preferred — gives us the JWT claim) or a
    bare ``user_id`` string.
    """
    jwt_verified = False
    user_id: str
    if isinstance(user, str):
        user_id = user
    else:
        user_id = user.id
        jwt_verified = bool(getattr(user, "email_verified", False))

    if jwt_verified:
        # Trust the positive JWT claim — no round-trip needed.
        _email_verified_cache[user_id] = (True, _time.time() + _VERIFIED_CACHE_TTL)
        return True

    now = _time.time()
    cached = _email_verified_cache.get(user_id)
    if cached:
        verified, expires_at = cached
        if now < expires_at:
            return verified

    try:
        from db.connection import get_pool

        pool = get_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email_confirmed_at FROM auth.users WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            pool.putconn(conn)

        # A missing row means the user does not exist — not verified.
        verified = bool(row and row[0] is not None)

        ttl = _VERIFIED_CACHE_TTL if verified else _UNVERIFIED_CACHE_TTL
        _email_verified_cache[user_id] = (verified, now + ttl)
        return verified
    except Exception as e:
        logger.warning("Email verification check failed (fail-closed): %s", e)
        return False


# Private alias: call sites outside this module import this spelling.
_is_email_verified = is_email_verified


# ── FastAPI dependency ───────────────────────────────────────────────

async def require_verified_email(
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """Require the user's email to be confirmed.

    Applied to every *mutation* endpoint (upload, reconcile, connect, …). Read
    endpoints keep plain ``get_current_user`` so an unconfirmed user can still
    see their own (empty) dashboard and understand what to do.

    Google-OAuth users are always treated as verified (the provider guarantees
    it), and with ``AUTH_AUTOCONFIRM=1`` so is everyone else.
    """
    if not is_email_verified(user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_unverified",
                "message": "Please confirm your email address before using this feature.",
            },
        )
    return user

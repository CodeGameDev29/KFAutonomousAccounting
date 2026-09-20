"""JWT authentication for FastAPI, issued and verified by this application.

Identity is not delegated. Delegating it to a hosted provider would mean
fetching a JWKS document over the network on the first request after every
cache expiry — a hard runtime dependency on another service's uptime.

``server/local_auth.py`` mints the tokens and this module verifies them, both
keyed on ``AUTH_JWT_SECRET`` from ``.env``. HS256 is the right algorithm here
precisely because it is symmetric: issuer and verifier are the same process, so
there is no third party who needs a public key.

The claim shape is the one route modules already expect, so every module that
depends on ``AuthUser`` reads the same fields:

    sub, email, role, aud="authenticated", app_metadata, user_metadata,
    email_confirmed_at, iat, exp
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "authenticated"

# Tolerate small clock skew between token issuance and verification. Both
# happen in this process, so this only matters across a system clock change.
_LEEWAY = timedelta(seconds=30)


def jwt_secret() -> str:
    """Return the signing secret, or raise if it is not configured.

    Read at call time rather than import time so tests and the launcher can set
    it after this module is imported.
    """
    secret = os.environ.get("AUTH_JWT_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured (AUTH_JWT_SECRET unset)",
        )
    return secret


def access_token_ttl() -> int:
    """Access-token lifetime in seconds."""
    return int(os.environ.get("AUTH_ACCESS_TOKEN_TTL_SECONDS", "3600"))


def mint_access_token(
    *,
    user_id: str,
    email: str | None,
    email_confirmed_at: datetime | None = None,
    provider: str = "email",
) -> tuple[str, int]:
    """Mint an access token for a user. Returns ``(token, expires_at_epoch)``.

    ``expires_at`` is returned alongside because the frontend session object
    carries an absolute expiry - it schedules its own proactive refresh from it
    rather than trusting a relative TTL across a page reload.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=access_token_ttl())
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": "authenticated",
        "aud": JWT_AUDIENCE,
        "app_metadata": {"provider": provider, "providers": [provider]},
        "user_metadata": {"email_verified": email_confirmed_at is not None},
        "email_confirmed_at": (
            email_confirmed_at.isoformat() if email_confirmed_at else None
        ),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = pyjwt.encode(claims, jwt_secret(), algorithm=JWT_ALGORITHM)
    return token, int(expires_at.timestamp())


def _verify_token(token: str) -> dict:
    """Verify a locally-issued JWT and return its claims.

    The algorithm is fixed here, never read from the token header - taking
    ``alg`` from unverified input is CWE-327 and is how ``alg: none`` and
    HMAC-vs-RSA confusion attacks land.
    """
    return pyjwt.decode(
        token,
        jwt_secret(),
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        leeway=_LEEWAY,
    )


class AuthUser(BaseModel):
    """Authenticated user, decoded from the JWT."""

    id: str  # UUID from auth.users
    email: str | None = None
    role: str = "authenticated"
    provider: str = "email"  # "email" for password accounts
    # True iff the JWT carries a positive email-confirmation claim.
    email_verified: bool = False


def _user_from_claims(payload: dict) -> AuthUser:
    """Build an AuthUser from verified claims. Shared by every entry point."""
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: missing sub claim")

    app_metadata = payload.get("app_metadata", {})
    provider = (
        app_metadata.get("provider", "email")
        if isinstance(app_metadata, dict)
        else "email"
    )

    user_metadata = payload.get("user_metadata", {})
    if not isinstance(user_metadata, dict):
        user_metadata = {}
    email_verified = bool(user_metadata.get("email_verified")) or bool(
        payload.get("email_confirmed_at")
    )

    return AuthUser(
        id=user_id,
        email=payload.get("email"),
        role=payload.get("role", "authenticated"),
        provider=provider,
        email_verified=email_verified,
    )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthUser:
    """Verify the bearer JWT and return the authenticated user.

    Usage: add ``user: AuthUser = Depends(get_current_user)`` to any route.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    try:
        payload = _verify_token(credentials.credentials)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("JWT verification failed: %s (%s)", exc, type(exc).__name__)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return _user_from_claims(payload)


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthUser | None:
    """Like get_current_user but returns None for unauthenticated requests.

    Used for endpoints that work both authenticated and unauthenticated
    (e.g. shared reports with a public link).
    """
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


async def get_current_user_header_or_query(
    request: Request,
    ticket: Optional[str] = Query(default=None, alias="ticket"),
    token: Optional[str] = Query(default=None, alias="token"),
) -> AuthUser:
    """Verify auth from (in order) Authorization header, single-use ticket,
    or legacy ``?token=`` query parameter.

    Used for file download endpoints triggered by a direct browser navigation
    (``<a href>`` or ``window.location``), where custom request headers cannot
    be set. The preferred path is:

        1. POST /api/files/ticket  -> returns a 60s ticket
        2. navigate to /api/reports/.../pdf?ticket=<ticket>

    The ``?token=`` path is deprecated: it still works, and logs a warning
    naming the ticket endpoint that replaces it.
    """
    # Read the Authorization header directly off the raw request (avoids the
    # HTTPBearer security dependency quirks when combined with Query).
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=auth_header[7:]
        )
        return await get_current_user(creds)

    # Preferred path: single-use ticket minted via POST /api/files/ticket
    if ticket:
        from server import tickets as _tickets
        ticket_user = _tickets.consume_user(ticket, "file_download")
        if ticket_user:
            return AuthUser(
                id=ticket_user.user_id,
                email=ticket_user.email,
                role="authenticated",
                provider=ticket_user.provider,
                email_verified=ticket_user.email_verified,
            )
        raise HTTPException(status_code=401, detail="Invalid or expired ticket")

    # Deprecated path: raw JWT in ?token=. Still accepted; warns in the log.
    if not token:
        raise HTTPException(status_code=401, detail="Missing authorization")

    logger.warning(
        "Download auth used legacy ?token= query param; migrate to /api/files/ticket"
    )

    try:
        payload = _verify_token(token)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return _user_from_claims(payload)

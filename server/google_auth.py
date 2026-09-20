"""Sign in with Google, straight to Google.

An account created through Google can have no password at all, so for that
account this is the only way in.

There is no identity broker in between: this module talks to Google directly,
verifies the ID token itself, and then issues a local session from
``server/local_auth.py``. It depends on no third party beyond Google.

The flow, and why it is shaped this way:

  GET  /api/auth/google/start     -> 302 to Google, carrying a signed state
  GET  /api/auth/google/callback  -> Google returns here with ?code
                                     state is verified, the code exchanged and
                                     the ID token's signature+audience checked
  302 /auth/google/complete?ticket=...
  POST /api/auth/google/exchange  -> the SPA trades the ticket for a session

The ticket hop exists so tokens never travel in a URL. A redirect carrying
access/refresh tokens in the query string or fragment would put them in browser
history, and any ``Referer`` the next page sends. The ticket is single-use, 60
seconds, and useless to anyone who does not immediately POST it back.

**This endpoint can never create an account.** Registration is closed
(``AUTH_ALLOW_SIGNUP``), and a Google identity that does not already match a row
in ``auth.users`` is refused. Sign-in, never sign-up.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from server import tickets
from server.auth import jwt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/google", tags=["auth-google"])

_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = "openid email profile"

# How long a start->callback round trip may take. Generous enough for a slow
# consent screen, short enough that a leaked state is not reusable later.
_STATE_TTL_SECONDS = 600


def google_client() -> tuple[str, str]:
    """Return (client_id, client_secret), or ("", "") when unconfigured."""
    return (
        os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
    )


def google_signin_enabled() -> bool:
    """True when a Google OAuth client is configured for sign-in."""
    cid, secret = google_client()
    return bool(cid and secret)


def _redirect_uri() -> str:
    """The callback URL registered with the Google OAuth client.

    Must match byte-for-byte what is listed in the Google Cloud console, so it
    is derived from one place rather than reconstructed per request from an
    attacker-influenced Host header.
    """
    base = (
        os.environ.get("APP_URL")
        or os.environ.get("BASE_URL")
        or "http://localhost:8080"
    ).rstrip("/")
    return f"{base}/api/auth/google/callback"


# -- CSRF state -------------------------------------------------------
# Signed rather than stored, so a restart mid-flow does not strand the user.

def _sign_state(nonce: str, issued_at: int) -> str:
    payload = f"{nonce}.{issued_at}"
    mac = hmac.new(jwt_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{mac}"


def _verify_state(state: str) -> bool:
    try:
        nonce, issued_raw, mac = state.rsplit(".", 2)
        issued_at = int(issued_raw)
    except Exception:
        return False
    if abs(time.time() - issued_at) > _STATE_TTL_SECONDS:
        return False
    expected = hmac.new(
        jwt_secret().encode(), f"{nonce}.{issued_at}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, mac)


def _fail(reason: str) -> RedirectResponse:
    """Send the user back to the login page with a short, non-leaky reason."""
    return RedirectResponse(
        url=f"/login?google_error={urllib.parse.quote(reason)}", status_code=302
    )


# -- Routes -----------------------------------------------------------

@router.get("/start")
async def google_start(request: Request):
    """Begin the Google sign-in flow."""
    cid, _ = google_client()
    if not google_signin_enabled():
        logger.warning("Google sign-in attempted but GOOGLE_CLIENT_ID/SECRET are unset")
        return _fail("not_configured")

    state = _sign_state(secrets.token_urlsafe(16), int(time.time()))
    params = {
        "client_id": cid,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _SCOPES,
        "state": state,
        # Force the account chooser. Without it Google silently reuses the most
        # recent session, so a user who signed out cannot switch accounts.
        "prompt": "select_account",
        "access_type": "online",
    }
    return RedirectResponse(
        url=f"{_GOOGLE_AUTH_URI}?{urllib.parse.urlencode(params)}", status_code=302
    )


@router.get("/callback")
async def google_callback(request: Request):
    """Google redirects here. Verify everything, then hand back a ticket."""
    if not google_signin_enabled():
        return _fail("not_configured")

    params = request.query_params
    if params.get("error"):
        # User pressed cancel, or Google refused. Not an error worth logging loudly.
        logger.info("Google sign-in returned error=%s", params.get("error"))
        return _fail("cancelled")

    state = params.get("state", "")
    if not _verify_state(state):
        logger.warning("Google callback with bad/expired state")
        return _fail("bad_state")

    code = params.get("code", "")
    if not code:
        return _fail("no_code")

    cid, secret = google_client()

    # --- Exchange the code, then VERIFY the ID token ---
    # Verification is the security boundary. The token endpoint is reached over
    # TLS, but the ID token is still checked for signature, issuer and audience
    # so a token minted for a different client can never be replayed here.
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token
        import requests as _requests

        resp = _requests.post(
            _GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning("Google token exchange failed: HTTP %s", resp.status_code)
            return _fail("exchange_failed")

        id_token_str = resp.json().get("id_token", "")
        if not id_token_str:
            return _fail("no_id_token")

        claims = google.oauth2.id_token.verify_oauth2_token(
            id_token_str,
            google.auth.transport.requests.Request(),
            cid,
            clock_skew_in_seconds=30,
        )
    except Exception as e:
        logger.warning("Google ID token verification failed: %s", e)
        return _fail("verify_failed")

    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return _fail("bad_issuer")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        return _fail("no_email")

    # Google reports whether it has verified the address. Trusting an
    # unverified one would let anyone who can set that address on a Google
    # account sign in as the owner of a local account.
    if not claims.get("email_verified"):
        logger.warning("Google sign-in refused: address not verified by Google")
        return _fail("email_unverified")

    # --- Sign in only. Never sign up. ---
    from server import local_auth

    user = local_auth.get_user_by_email(email)
    if not user or not user.get("is_active"):
        logger.info("Google sign-in refused for an address with no account")
        return _fail("no_account")

    local_auth.mark_google_signin(user["id"])

    ticket = tickets.issue(
        user["id"],
        "oauth_session",
        ttl_seconds=60,
        email=user["email"],
        provider="google",
        email_verified=True,
    )
    logger.info("Google sign-in succeeded for user_id=%s", user["id"])
    return RedirectResponse(url=f"/auth/google/complete?ticket={ticket}", status_code=302)


class ExchangeRequest(BaseModel):
    ticket: str


@router.post("/exchange")
async def google_exchange(body: ExchangeRequest):
    """Trade the single-use ticket for a real session."""
    ticket_user = tickets.consume_user(body.ticket, "oauth_session")
    if not ticket_user:
        return JSONResponse(status_code=401, content={"error": "Invalid or expired sign-in."})

    from server import local_auth

    user = local_auth.get_user_by_id(ticket_user.user_id)
    if not user or not user.get("is_active"):
        return JSONResponse(status_code=401, content={"error": "Account unavailable."})

    return local_auth.build_session(user)


@router.get("/status")
async def google_status():
    """Whether the UI should offer the Google button at all."""
    return {"enabled": google_signin_enabled()}

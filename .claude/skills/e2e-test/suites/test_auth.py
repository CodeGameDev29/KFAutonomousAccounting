"""Authentication & session management tests.

These run against the app's own local identity service (`/api/auth/*`) on the
same origin as the API — there is no external auth provider, so there is nothing
here to skip on.

Tests that need a session declare `login` as a precondition in the runner's
registry rather than checking `client.token` themselves: a failed sign-in is one
failure with a cause, not sixty.
"""

from __future__ import annotations

import base64
import json
import secrets

from lib.api_client import APIClient, signup_open, throwaway_credentials
from lib.test_framework import NotExercised, assert_eq, assert_key, assert_status
from profiles import UserProfile


def test_health_check(client: APIClient):
    """Server returns healthy status without auth.

    `status` is "degraded" whenever the database or the local storage root is
    unreachable, so it is the assertion that actually carries weight.
    """
    resp = client.get("/health")
    assert_status(resp, 200, "Health check")
    data = resp.json()
    assert_eq(data["status"], "ok", "Health status")
    assert_key(data, "storage", "Health response")
    assert_eq(data["storage"], "ok", "Local file storage reachable and writable")
    assert_key(data, "extraction", "Health response")


def test_signup_when_open(client: APIClient, profile: UserProfile):
    """Account creation, but only if this install creates accounts.

    Registration is closed by default (`AUTH_ALLOW_SIGNUP=0`) and that is the
    intended posture, not a defect — so it is NOT-EXERCISED rather than a
    failure. When the flag is open, signup must work end to end and, with
    `AUTH_AUTOCONFIRM=1`, give back a live session with no inbox involved.

    The account it creates uses a reserved documentation domain and a random
    password. Nothing deletes it afterwards: there is no account-deletion
    endpoint, which is one more reason to run this against a throwaway install.
    """
    if not signup_open(client.base_url):
        raise NotExercised(
            "registration is closed (GET /health reports signup_enabled=false); no account was "
            "created. Set AUTH_ALLOW_SIGNUP=1 in .env and restart the server to exercise signup."
        )
    fresh = APIClient(client.base_url)
    try:
        email, password = throwaway_credentials("signup")
        result = fresh.signup(email, password)
        if result.get("rate_limited"):
            raise NotExercised(f"signup is rate-limited right now: {result['error']}")
        if "error" in result:
            raise AssertionError(f"Signup failed while the flag was open: {result['error']}")
        assert result.get("success") is True, f"Signup should report success: {result}"
        assert fresh.token is not None, "Signup with AUTH_AUTOCONFIRM=1 should return a session"
    finally:
        fresh.close()


def test_login(client: APIClient, profile: UserProfile):
    """Sign in to the test account. Everything else in the run depends on this."""
    result = client.login(profile.email, profile.password)
    if "error" in result:
        raise AssertionError(
            f"Login failed: {result['error']} — check AA_E2E_EMAIL / AA_E2E_PASSWORD and that "
            "the account exists on this install."
        )
    assert_eq(result.get("token_type"), "bearer", "Session token_type")
    assert client.token is not None, "Token should be set after login"
    assert client.user_id is not None, "User ID should be set after login"
    assert client.refresh_token is not None, "Refresh token should be set after login"


def test_login_wrong_password(client: APIClient, profile: UserProfile):
    """Login with a wrong password returns the generic credentials error.

    Wrong password, unknown address and disabled account all return the same
    400 body by design, so assert on that exact string rather than on anything
    that would reveal which case it was.
    """
    temp_client = APIClient(client.base_url)
    try:
        result = temp_client.login(profile.email, f"Wrong-{secrets.token_urlsafe(9)}-aA1!")
        assert "error" in result, "Wrong password should return error"
        assert_eq(result["error"], "Invalid login credentials.", "Sign-in failure message")
        assert temp_client.token is None, "No token should be issued on a failed login"
    finally:
        temp_client.close()


def test_unauthenticated_access_blocked(client: APIClient):
    """API endpoints reject requests without an auth token."""
    unauthed = APIClient(client.base_url)
    try:
        resp = unauthed.get("/api/dashboard/summary")
        assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"
    finally:
        unauthed.close()


def test_session_persistence(client: APIClient, profile: UserProfile):
    """The token from login works for subsequent API calls."""
    assert client.token, "No access token held after a passing login — the client lost it"
    resp = client.get("/api/onboarding/status")
    assert_status(resp, 200, "Authenticated request")


def test_token_refresh(client: APIClient, profile: UserProfile):
    """A refresh token exchanges for a working new session."""
    assert client.refresh_token, "No refresh token held after a passing login"
    result = client.refresh_session()
    if "error" in result:
        raise AssertionError(f"Refresh failed: {result['error']}")
    assert client.token is not None, "Refresh should yield a new access token"
    resp = client.get("/api/onboarding/status")
    assert_status(resp, 200, "Request with refreshed token")


def _forged_jwt() -> str:
    """A structurally valid HS256 token signed with nothing the server knows."""
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = part({"alg": "HS256", "typ": "JWT"})
    payload = part({"sub": "00000000-0000-0000-0000-000000000000", "role": "authenticated"})
    return f"{header}.{payload}.{secrets.token_urlsafe(32)}"


def test_invalid_token_rejected(client: APIClient):
    """A forged token is rejected."""
    forged = APIClient(client.base_url)
    try:
        forged.token = _forged_jwt()
        resp = forged.get("/api/dashboard/summary")
        assert resp.status_code in (401, 403), (
            f"Forged token: expected 401/403, got {resp.status_code}"
        )
    finally:
        forged.close()

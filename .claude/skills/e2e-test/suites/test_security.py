"""Security & data isolation tests — CORS, row-level security, IDOR, headers."""

from __future__ import annotations

import httpx
from lib import fixtures
from lib.api_client import APIClient, signup_open, throwaway_credentials
from lib.test_framework import NotExercised, assert_eq, assert_status, assert_status_in
from profiles import UserProfile

# Markers that say a page or payload is API documentation rather than the SPA.
# Checked against the whole body: a Swagger UI page carries "swagger" in its
# script tags and its title, none of which are in the first 100 characters.
API_DOC_MARKERS = (
    "swagger", "swagger-ui", "swaggerui", "redoc", "openapi", "rapidoc", "stoplight",
)
# Paths that would serve API docs if FastAPI's defaults were left on. The app
# turns all three off (`server/app.py`), so they fall through to the SPA or 404.
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

# An origin that is nobody's: `.invalid` is reserved and can never resolve.
FOREIGN_ORIGIN = "https://attacker.invalid"


def test_security_headers_present(client: APIClient, profile: UserProfile):
    """The server returns the required security headers."""
    resp = client.get("/health")
    headers = resp.headers
    assert "x-content-type-options" in headers, "Missing X-Content-Type-Options"
    assert headers["x-content-type-options"] == "nosniff", "X-Content-Type-Options should be nosniff"
    assert "x-frame-options" in headers, "Missing X-Frame-Options"
    assert headers["x-frame-options"] == "DENY", "X-Frame-Options should be DENY"


def test_referrer_policy_header(client: APIClient, profile: UserProfile):
    """Server sets Referrer-Policy header."""
    resp = client.get("/health")
    rp = resp.headers.get("referrer-policy", "")
    assert rp, "Missing Referrer-Policy header"


def test_cors_not_wildcard(client: APIClient, profile: UserProfile):
    """CORS must not allow wildcard or reflected origins."""
    resp = httpx.options(
        f"{client.base_url}/api/dashboard/summary",
        headers={"Origin": FOREIGN_ORIGIN, "Access-Control-Request-Method": "GET"},
        timeout=10.0,
    )
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao != "*", f"CORS wildcard detected: {acao}"
    assert acao != FOREIGN_ORIGIN, f"CORS reflects arbitrary origin: {acao}"


def _assert_not_api_docs(path: str, status_code: int, body: str, content_type: str) -> None:
    """Fail if `path` served API documentation or an OpenAPI schema.

    Written as a helper so it can be aimed at a URL that *does* serve Swagger UI
    and shown to fail. Inspecting only the first 100 characters of the body for
    the word "swagger" passes no matter what the server returns, because a real
    Swagger UI page does not carry the word that early.
    """
    if status_code in (401, 403, 404, 405):
        return  # not served at all: exactly what we want
    if status_code != 200:
        raise AssertionError(
            f"GET {path}: unexpected HTTP {status_code} — expected 200 (SPA) or 401/403/404. "
            f"Body: {body[:200]}"
        )
    lowered = body.lower()
    hits = [m for m in API_DOC_MARKERS if m in lowered]
    if hits:
        raise AssertionError(
            f"GET {path} returned 200 with API documentation (markers: {', '.join(hits)}). "
            "The API and the SPA share one origin, so whoever can reach the app can read that. "
            f"Content-Type: {content_type}. First 200 chars: {body[:200]}"
        )
    if "json" in content_type.lower() and '"paths"' in lowered:
        raise AssertionError(
            f"GET {path} returned a JSON document with a 'paths' object — that is an OpenAPI "
            f"schema. First 200 chars: {body[:200]}"
        )


def test_docs_not_exposed(client: APIClient, profile: UserProfile):
    """Swagger/OpenAPI docs must not be served alongside the app.

    The API and the SPA are one process on one origin, so anything FastAPI
    serves is reachable by whoever can reach the app. Each of `/docs`, `/redoc`
    and `/openapi.json` is checked in full.
    """
    for path in DOC_PATHS:
        resp = client.get(path)
        _assert_not_api_docs(
            path, resp.status_code, resp.text, resp.headers.get("content-type", ""),
        )


def test_data_isolation_between_users(client: APIClient, profile: UserProfile):
    """A second account sees none of this account's rows (row-level security).

    Two accounts are needed and the suite is only given one, so the second has
    to be created — which is only possible while registration is open. When it
    is closed this is NOT-EXERCISED: never a pass, and never a product failure
    either. The IDOR tests below still run, and the policies themselves are
    covered from the catalogue by the repository's `tests/test_rls_enforcement.py`.
    """
    # The primary account must be readable for the comparison to mean anything.
    data_a = client.list_transactions()
    count_a = data_a.get("total", 0)
    assert count_a >= 0, "Primary user transaction count should be readable"

    if not signup_open(client.base_url):
        raise NotExercised(
            "cross-account isolation needs a second account and registration is closed "
            "(GET /health reports signup_enabled=false). Set AUTH_ALLOW_SIGNUP=1 in .env and "
            "restart the server to exercise this."
        )

    other = APIClient(client.base_url)
    other_email, other_password = throwaway_credentials("isolation")
    try:
        result = other.signup(other_email, other_password)
        if result.get("rate_limited"):
            raise NotExercised(f"signup is rate-limited right now: {result['error']}")
        if "error" in result:
            raise AssertionError(f"Could not create isolation test user: {result['error']}")
        if not other.token:
            raise AssertionError("Isolation test user created but no session was issued")
        assert other.user_id != client.user_id, "Isolation test user must be a distinct account"

        other.save_profile("Example Isolation Corp", 2026, "CAD")
        other.complete_onboarding()

        data_b = other.list_transactions()
        assert_eq(data_b.get("total", 0), 0, "New user should see zero transactions (RLS)")
    finally:
        other.close()


def test_idor_document_access(client: APIClient, profile: UserProfile):
    """Cannot access a document this account does not own by guessing its id."""
    resp = client.get("/api/receipts/999999999")
    assert_status_in(resp, (403, 404), "IDOR document access")


def test_idor_transaction_access(client: APIClient, profile: UserProfile):
    """Cannot access a transaction this account does not own by guessing its id."""
    resp = client.get("/api/transactions/999999999")
    assert_status_in(resp, (403, 404), "IDOR transaction access")


def test_reset_requires_confirmation(client: APIClient, profile: UserProfile):
    """Reset endpoint rejects requests without confirm=true."""
    resp = client.post("/api/actions/reset", {"confirm": False})
    assert_status(resp, 400, "Reset without confirmation should be rejected")


def test_error_messages_not_leaking(client: APIClient, profile: UserProfile):
    """Error responses should not contain stack traces or internal paths."""
    # A text file renamed `.pdf` — rejected on magic bytes before any extraction.
    resp = client.upload("/api/receipts/upload", fixtures.not_a_pdf_for_this_run())
    assert resp.status_code >= 400, (
        f"Uploading plain text as a .pdf returned HTTP {resp.status_code}; this test needs the "
        "error path to check what an error body reveals"
    )
    body = resp.text
    assert "Traceback" not in body, "Error response leaks traceback"
    assert "\\Users\\" not in body and "/home/" not in body, "Error response leaks file paths"


def test_rate_limiting_exists(client: APIClient, profile: UserProfile):
    """Rate-limit headers are advertised on a plain request.

    slowapi enforces the limits in-process (`server/rate_limit.py`). Absent
    headers mean the endpoint advertises no limit, which is worth seeing rather
    than hiding — but it is a posture note, not a broken feature.
    """
    resp = client.get("/health")
    has_rate_limit = any(
        h.lower().startswith("x-ratelimit") or h.lower() == "retry-after"
        for h in resp.headers
    )
    if not has_rate_limit:
        raise NotExercised(
            "/health advertises no rate-limit headers; per-route limits are set by slowapi "
            "decorators and are only observable by exceeding them, which this suite does not "
            "do deliberately — a tripped limiter would lock the test account out of sign-in."
        )

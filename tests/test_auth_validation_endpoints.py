"""server/api/auth_validation.py — the public, pre-authentication surface.

These are the endpoints a new user reaches *before* they have an account, so
they answer to anyone on the internet with no JWT at all. That makes two
properties load-bearing: input validation has to be strict (password strength
is decided here, not in the browser), and nothing may leak whether a given
address is already registered.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    # Clear any prior import so the module is rebuilt with patches applied.
    for name in list(sys.modules):
        if name.startswith("server.api.auth_validation"):
            del sys.modules[name]

    # Imported here on purpose: the module must be re-imported after the
    # cache eviction above, not bound once at module import time.
    from server.api import auth_validation

    app = FastAPI()
    app.include_router(auth_validation.router)
    return TestClient(app)


def test_password_strength_accepts_strong(client: TestClient) -> None:
    from server.api import auth_validation as av

    assert av._validate_password_strength("GoodPass1!") is None


@pytest.mark.parametrize(
    "password,fragment",
    [
        ("short1!", "8 characters"),
        ("alllowercase1!", "uppercase"),
        ("ALLUPPERCASE1!", "lowercase"),
        ("NoNumbers!", "number"),
        ("NoSpecials1", "special character"),
    ],
)
def test_password_strength_rejects_weak(client: TestClient, password: str, fragment: str) -> None:
    from server.api import auth_validation as av

    msg = av._validate_password_strength(password)
    assert msg is not None, f"Expected password {password!r} to fail"
    assert fragment.lower() in msg.lower()


def test_validate_email_rejects_missing_at(client: TestClient) -> None:
    resp = client.post("/api/auth/validate-email", json={"email": "no-at-sign"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["valid"] is False
    assert body["reason"] == "invalid_email"


def test_resolve_origin_only_echoes_allowlisted_origins() -> None:
    """Defense against origin-header smuggling into the confirm URL."""
    from fastapi import Request

    from server.api import auth_validation as av

    # Build a minimal ASGI scope mocking a request with an attacker Origin header.
    def _request(origin: str) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/signup",
            "headers": [(b"origin", origin.encode())],
        }
        return Request(scope)

    attacker = _request("https://evil.example.com")
    resolved = av._resolve_origin(attacker)
    assert resolved != "https://evil.example.com"
    assert resolved == av._DEFAULT_ORIGIN

    # An allowlisted origin IS echoed back
    if av._ALLOWED_ORIGINS:
        good = next(iter(av._ALLOWED_ORIGINS))
        assert av._resolve_origin(_request(good)) == good

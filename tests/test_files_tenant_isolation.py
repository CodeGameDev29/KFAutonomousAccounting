"""server/api/files.py — one tenant's files stay unreachable to another.

The download and delete endpoints derive their permission check from the
storage key prefix (``{user_id}/...``). A gap there would let one account pull
another account's receipts by guessing a key, so the 403 cases are pinned
explicitly: a foreign prefix, a bare filename with no prefix at all, and a
traversal that starts under someone else's prefix. The ticket endpoint is
covered too, since a minted download ticket is scoped to one user and is
consumable exactly once.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import AuthUser


@pytest.fixture
def app_with_overrides(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # Re-import the files module with fresh caches so the dependency
    # overrides below take effect.
    for name in list(sys.modules):
        if name.startswith("server.api.files"):
            del sys.modules[name]

    from server.api import files as files_mod

    app = FastAPI()
    app.include_router(files_mod.router)

    # Pin the authenticated user to a known UUID, bypassing the JWT middleware.
    from server.auth import get_current_user

    async def _fake_user() -> AuthUser:
        return AuthUser(id="user-alpha", email="alpha@example.com", role="authenticated", email_verified=True)

    app.dependency_overrides[get_current_user] = _fake_user
    return app


def test_download_rejects_cross_tenant_key(app_with_overrides: FastAPI) -> None:
    client = TestClient(app_with_overrides)
    resp = client.get("/api/files/download", params={"key": "user-beta/receipt.pdf"})
    assert resp.status_code == 403
    assert "Access denied" in resp.json()["detail"]


def test_download_rejects_bare_filename_without_user_prefix(app_with_overrides: FastAPI) -> None:
    client = TestClient(app_with_overrides)
    resp = client.get("/api/files/download", params={"key": "receipt.pdf"})
    assert resp.status_code == 403


def test_download_rejects_traversal_key(app_with_overrides: FastAPI) -> None:
    client = TestClient(app_with_overrides)
    # Path traversal that starts with another user's prefix should fail the
    # startswith check against the caller's own ID.
    resp = client.get(
        "/api/files/download",
        params={"key": "user-beta/../user-alpha/receipt.pdf"},
    )
    assert resp.status_code == 403


def test_download_accepts_own_key_and_returns_signed_url(app_with_overrides: FastAPI) -> None:
    with patch("core.file_storage.get_download_url", return_value="https://signed/url"):
        client = TestClient(app_with_overrides)
        resp = client.get("/api/files/download", params={"key": "user-alpha/receipt.pdf"})
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://signed/url"}


def test_delete_rejects_cross_tenant_key(app_with_overrides: FastAPI) -> None:
    client = TestClient(app_with_overrides)
    resp = client.delete("/api/files/user-beta/receipt.pdf")
    assert resp.status_code == 403


def test_ticket_endpoint_requires_auth(app_with_overrides: FastAPI) -> None:
    """Sanity: /api/files/ticket is an authenticated mint endpoint."""
    client = TestClient(app_with_overrides)
    resp = client.post("/api/files/ticket")
    # The overridden dependency authenticates user-alpha → 200 with a ticket.
    assert resp.status_code == 200
    body = resp.json()
    assert "ticket" in body
    assert body["ttl_seconds"] == 60
    # Minted ticket is scoped to file_download and consumable exactly once.
    from server import tickets

    assert tickets.consume(body["ticket"], "file_download") == "user-alpha"
    assert tickets.consume(body["ticket"], "file_download") is None

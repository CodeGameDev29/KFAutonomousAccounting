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


# ── A key with the caller's own prefix that climbs into a neighbour ──────────
#
# ``user-alpha/../user-beta/x.pdf`` passes a ``startswith("user-alpha/")`` check
# and stays inside the storage root, so ``resolve_path`` alone accepts it. The
# ownership check has to be made on the resolved path.

_ESCAPING_KEYS = [
    "user-alpha/../user-beta/202601/abcd_receipt.pdf",
    "user-alpha/202601/../../user-beta/202601/abcd_receipt.pdf",
    r"user-alpha/..\user-beta\202601\abcd_receipt.pdf",
    "user-alpha/../user-alpha-evil/x.pdf",
]


@pytest.mark.parametrize("key", _ESCAPING_KEYS)
def test_download_rejects_own_prefix_that_escapes_into_another_tenant(
    app_with_overrides: FastAPI, tmp_path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    with patch("core.file_storage.get_download_url", return_value="https://signed/url") as signer:
        client = TestClient(app_with_overrides)
        resp = client.get("/api/files/download", params={"key": key})
    assert resp.status_code == 403
    signer.assert_not_called()


def test_delete_rejects_own_prefix_that_escapes_into_another_tenant(
    app_with_overrides: FastAPI, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    victim = tmp_path / "user-beta" / "202601" / "abcd_receipt.pdf"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"%PDF-1.4 synthetic")
    client = TestClient(app_with_overrides)
    resp = client.delete("/api/files/user-alpha/../user-beta/202601/abcd_receipt.pdf")
    # Some HTTP stacks normalise ``..`` out of a URL path before routing, in
    # which case this is the plain cross-tenant 403; either way the file stays.
    assert resp.status_code in (403, 404)
    assert victim.exists()


def test_resolve_user_path_confines_to_the_owner(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    from core import file_storage

    ok = file_storage.resolve_user_path("user-alpha", "user-alpha/202601/abcd_r.pdf")
    assert ok == (tmp_path / "user-alpha" / "202601" / "abcd_r.pdf").resolve()
    for key in _ESCAPING_KEYS + ["user-beta/x.pdf", "user-alpha"]:
        with pytest.raises(ValueError):
            file_storage.resolve_user_path("user-alpha", key)
    with pytest.raises(ValueError):
        file_storage.list_user_files("user-alpha", "../user-beta")

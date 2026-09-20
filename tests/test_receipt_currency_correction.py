"""A receipt that never stated a currency must not be handed one.

``PATCH /api/receipts/{id}/correct`` stamps any currency it receives as
``currency_source='stated'`` — the strongest provenance there is, the one that
beats what the receipt's own tax lines would otherwise infer
(``core/document_currency.py``). So the field may only travel when a human
actually typed it.

The review panel is the other half of that rule. Prefilling its currency editor
with ``doc.currency ?? "CAD"`` would make correcting the *vendor* submit
``currency: "CAD"`` as well, and the row would come back stamped "stated CAD" on
the strength of a default the user never saw as a choice.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

FAKE_USER = AuthUser(
    id="user-currency", email="currency@example.com", role="authenticated",
    email_verified=True,
)

# documents row as the endpoint reads it: id, vendor, document_date, total,
# currency, subtotal, 4x tax, payment_method, invoice_number, currency_source.
# Currency is NULL — this receipt never said what money it was in.
DOC_ROW = (1, "Example Vendor", "2026-03-04", "5.60", None,
           None, None, None, None, None, None, None, None)


class _Cursor:
    def __init__(self, executed: list[tuple]) -> None:
        self._executed = executed
        self._one = None

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self._executed.append((flat, params))
        self._one = DOC_ROW if flat.startswith("SELECT id, vendor") else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDb:
    def __init__(self) -> None:
        self.user_id = FAKE_USER.id
        self.executed: list[tuple] = []

    def _conn(self):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = _Cursor(self.executed)
        return conn

    def update_sql(self) -> str:
        return next(sql for sql, _ in self.executed if sql.startswith("UPDATE documents"))


@pytest.fixture()
def client():
    db = _FakeDb()

    async def _user():
        return FAKE_USER

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, raise_server_exceptions=False), db
    finally:
        for key in (get_current_user, get_db):
            app.dependency_overrides.pop(key, None)


def test_correcting_another_field_does_not_stamp_a_currency(client):
    http, db = client
    resp = http.patch("/api/receipts/7/correct", json={"vendor": "Bluepeak Software Inc"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected_fields"] == ["vendor"]
    assert "currency" not in db.update_sql()


def test_an_empty_currency_is_not_a_correction(client):
    """Silence is what a receipt that never stated a currency deserves."""
    http, db = client
    resp = http.patch(
        "/api/receipts/7/correct",
        json={"vendor": "Bluepeak Software Inc", "currency": "   "},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected_fields"] == ["vendor"]
    assert "currency_source" not in db.update_sql()


def test_an_empty_currency_on_its_own_corrects_nothing(client):
    http, _db = client
    resp = http.patch("/api/receipts/7/correct", json={"currency": ""})

    assert resp.status_code == 400
    assert "No fields provided" in resp.json()["detail"]


def test_a_currency_the_user_typed_is_stated(client):
    http, db = client
    resp = http.patch("/api/receipts/7/correct", json={"currency": " usd "})

    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected_fields"] == ["currency", "currency_source"]

    sql, params = next(
        (sql, params) for sql, params in db.executed if sql.startswith("UPDATE documents")
    )
    assert "currency = %s" in sql and "currency_source = %s" in sql
    assert params[0] == "usd", "the value is trimmed, not re-cased or re-guessed"
    assert params[1] == "stated"


# ── The editor must not invent the default ──

def test_the_review_panel_prefills_the_stored_currency_or_nothing():
    panel = (
        Path(__file__).resolve().parent.parent
        / "web" / "src" / "components" / "ReceiptReviewPanel.tsx"
    )
    source = panel.read_text(encoding="utf-8")

    assert 'currency: doc.currency ?? "CAD"' not in source, (
        "prefilling CAD makes the very next save stamp 'stated CAD' on a "
        "receipt that never stated one"
    )
    assert 'currency: doc.currency ?? ""' in source

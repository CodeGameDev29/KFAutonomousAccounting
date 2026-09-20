"""The standardized error envelope (server/errors.py).

Every API failure answers in one shape — ``{"error": {"code", "message"}}``,
with an optional ``hint`` — so the frontend can render any error without
special-casing each endpoint, and so an internal exception never travels to
the browser as its own text.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

from server.errors import api_error, error_envelope


def test_envelope_shape_minimal() -> None:
    out = error_envelope("WIDGET_MISSING", "Widget not found.")
    assert out == {"error": {"code": "WIDGET_MISSING", "message": "Widget not found."}}


def test_envelope_shape_with_hint() -> None:
    out = error_envelope("UPSTREAM_DOWN", "Upstream unreachable.", hint="Retry in 60s.")
    assert out == {
        "error": {
            "code": "UPSTREAM_DOWN",
            "message": "Upstream unreachable.",
            "hint": "Retry in 60s.",
        }
    }


def test_api_error_returns_httpexception_with_envelope_detail() -> None:
    exc = api_error(400, "BAD_REQ", "Missing 'foo'.")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 400
    assert exc.detail == {"error": {"code": "BAD_REQ", "message": "Missing 'foo'."}}


def test_api_error_log_exc_captures_original(caplog: pytest.LogCaptureFixture) -> None:
    inner = ValueError("secret internal state — DO NOT LEAK")
    with caplog.at_level(logging.ERROR, logger="server.errors"):
        exc = api_error(500, "SECRET_FAIL", "Operation failed.", log_exc=inner)
    # Internal message is in the log, NOT in the response detail.
    assert any("secret internal state" in record.getMessage() for record in caplog.records)
    assert "secret internal state" not in str(exc.detail)
    assert exc.detail["error"]["message"] == "Operation failed."


def test_api_error_without_hint_omits_hint_key() -> None:
    exc = api_error(403, "FORBID", "Nope.")
    assert "hint" not in exc.detail["error"]

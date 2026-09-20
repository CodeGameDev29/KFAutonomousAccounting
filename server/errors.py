"""Standardized error envelope for the API.

Every handler should emit errors in the same shape so the frontend can branch
on `code` rather than parsing free-text `detail`. The shape is:

    {
        "error": {
            "code": "UPPER_SNAKE_CODE",
            "message": "Human-friendly sentence",
            "hint": "Optional actionable hint",
        }
    }

Usage:

    from server.errors import api_error

    raise api_error(400, "GMAIL_RECONNECT_FAILED", "Gmail reconnect failed.")

Or for a non-raising construction (e.g. to embed in a bulk-result payload):

    return error_envelope("WISE_SYNC_FAILED", "Wise sync failed.")
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

_logger = logging.getLogger(__name__)


def error_envelope(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    """Build a standardized error envelope dict (no HTTP raising)."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return {"error": payload}


def api_error(
    status_code: int,
    code: str,
    message: str,
    hint: str | None = None,
    log_exc: BaseException | None = None,
) -> HTTPException:
    """Build an HTTPException carrying the standard envelope in `detail`.

    Pass `log_exc` to have the exception logged server-side at ERROR; its string
    form is intentionally NOT leaked to clients.
    """
    if log_exc is not None:
        _logger.error("api_error %s (%s): %s", code, status_code, log_exc, exc_info=True)
    return HTTPException(status_code=status_code, detail=error_envelope(code, message, hint))

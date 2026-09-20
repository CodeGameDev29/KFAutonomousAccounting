"""Account display names — the server's copy of ``config/accounts.yaml``.

The account keys stored in ``transactions.account`` ("CAD", "USD", "Wise",
"CreditCard", …) are internal. What a human sees next to them is configuration,
written down once in ``config/accounts.yaml``, where the XLSX binder, the HTML
report and the PDF report read it from. The browser reads it from here so the
same file governs the UI too, instead of a hard-coded map in the bundle.

Authenticated on purpose — the mapping is configuration that may name a
financial institution, so it does not belong on a public endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from config.data_files import account_display, account_order
from server.auth import AuthUser, get_current_user

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("/display-names")
async def account_display_names(
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Return ``{display: {key: name}, order: [key, ...]}`` from ``accounts.yaml``.

    Both halves degrade the way ``config/data_files.py`` degrades: a missing or
    unreadable file yields an empty mapping and the caller falls back to the
    raw account key.
    """
    return {"display": account_display(), "order": account_order()}

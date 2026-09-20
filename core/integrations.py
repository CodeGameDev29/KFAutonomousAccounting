"""Integration registry — metadata for all financial data source integrations.

Each integration is described by a dict with:
- source_type: DB key used in gather_sources, connection_health
- account_type: Value for transactions.account column
- credential_keys: List of CloudCredentialStore keys needed for connection
- has_sync: Whether this integration supports automatic API sync (vs manual import)
- display_name: Human-readable name for UI
- description: Short description for ConnectionCard
"""

from __future__ import annotations

from typing import TypedDict


class IntegrationSpec(TypedDict, total=False):
    source_type: str
    account_type: str
    credential_keys: list[str]
    has_sync: bool
    display_name: str
    description: str


INTEGRATIONS: dict[str, IntegrationSpec] = {
    "wise": {
        "source_type": "wise",
        "account_type": "Wise",
        "credential_keys": ["wise_api_token", "wise_profile_id"],
        "has_sync": True,
        "display_name": "Wise",
        "description": "Sync multi-currency transactions and transfer documents.",
    },
    "paypal": {
        "source_type": "paypal",
        "account_type": "PayPal",
        "credential_keys": ["paypal_client_id", "paypal_client_secret"],
        "has_sync": True,
        "display_name": "PayPal",
        "description": "Sync PayPal transactions for reconciliation.",
    },
    "amazon": {
        "source_type": "amazon",
        "account_type": "Amazon",
        "credential_keys": [],  # No stored credentials — CSV upload only
        "has_sync": False,  # User-initiated CSV import, not API sync
        "display_name": "Amazon",
        "description": "Import Amazon orders from CSV exports. Nothing here signs in to Amazon.",
    },
}


# Valid source types for DB constraints
ALL_SOURCE_TYPES = {"gmail", "wise", "paypal", "manual", "email_forwarding", "amazon"}

# Valid account types for DB constraints
ALL_ACCOUNT_TYPES = {"CAD", "USD", "Wise", "CreditCard", "PayPal", "Amazon"}


def get_integration(name: str) -> IntegrationSpec | None:
    """Look up an integration by name."""
    return INTEGRATIONS.get(name)


def is_connected(creds, integration_name: str) -> bool:
    """Check if an integration has valid credentials stored.

    For Amazon (no credentials), checks gather_sources for prior import.
    """
    spec = INTEGRATIONS.get(integration_name)
    if not spec:
        return False
    if not spec["credential_keys"]:
        # No credential-based check (e.g. Amazon) — caller must check gather_sources
        return False
    for key in spec["credential_keys"]:
        val = creds.retrieve(key)
        if not val or val == "":
            return False
    return True

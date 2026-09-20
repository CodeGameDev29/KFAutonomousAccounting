"""Cloud credential storage — per-user encrypted credentials in PostgreSQL.

Credentials are encrypted at rest with a key derived from the server secret
+ user_id, then stored in the user's business_profile row (config_json).
Keying on the server secret rather than on a machine identity (the scheme in
``core/credentials.py``) lets any server process holding that secret read them.

The interface matches ``CredentialStore`` exactly (store, retrieve, delete,
list_keys), so callers such as GmailGatherer and WiseAPI work against either
backend unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Namespace constant prevents key reuse if the same secret is shared across
# services.  Changing this value invalidates all previously stored credentials.
_KEY_NAMESPACE = os.getenv("AA_CLOUD_CREDENTIAL_NAMESPACE", "CHANGE_ME_CREDENTIAL_NAMESPACE")


def _credential_secret() -> str:
    """Return the server secret that per-user credential keys derive from.

    Read in priority order, and the order matters:

    1. ``CREDENTIAL_ENCRYPTION_SECRET`` — set this to decouple credential
       encryption from session signing, so ``AUTH_JWT_SECRET`` can be rotated
       (which only logs everyone out) without also making every stored Gmail,
       Wise and PayPal credential permanently undecryptable.
    2. ``AUTH_JWT_SECRET`` — the default when the first is unset.

    Rotating whichever value is in force invalidates every stored credential.
    """
    for name in (
        "CREDENTIAL_ENCRYPTION_SECRET",
        "AUTH_JWT_SECRET",
    ):
        value = os.environ.get(name, "")
        if value:
            return value
    raise RuntimeError(
        "No credential-encryption secret configured. Set AUTH_JWT_SECRET (or "
        "CREDENTIAL_ENCRYPTION_SECRET) in .env — credentials cannot be stored "
        "safely without a server secret."
    )


def _derive_cloud_key(user_id: str) -> bytes:
    """Derive a Fernet-compatible key from server secret + user_id.

    Returns 32-byte url-safe base64-encoded key required by Fernet.
    The derivation is deterministic: same (secret, user_id) always
    produces the same key.
    """
    seed = f"{_credential_secret()}:{user_id}:{_KEY_NAMESPACE}"
    digest = hashlib.sha256(seed.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class CloudCredentialStore:
    """Per-user encrypted credential storage backed by PostgreSQL.

    Where ``core.credentials.CredentialStore`` derives its key from the machine
    UUID, this derives the encryption key from the server-side JWT
    secret + user_id.  The encrypted blob is stored as a JSON string
    inside the ``config_json`` column of the ``business_profile`` table
    under the ``_encrypted_creds`` key.

    Parameters
    ----------
    db:
        A database instance that exposes ``get_business_profile()`` and
        ``upsert_business_profile(data)`` — compatible with both the
        SQLite ``Database`` and the PostgreSQL ``DatabasePg``.
    user_id:
        The user's UUID.  Used to derive the encryption key.
    """

    def __init__(self, db, user_id: str) -> None:  # noqa: ANN001 — db is duck-typed
        self._db = db
        self._user_id = user_id
        self._fernet = Fernet(_derive_cloud_key(user_id))
        self._data: dict[str, str] = {}
        self._loaded = False
        self._load()

    # ------------------------------------------------------------------
    # Internal persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load and decrypt credentials from the database."""
        try:
            profile = self._db.get_business_profile()
            if not profile:
                self._data = {}
                self._loaded = True
                return

            config = profile.get("config_json")
            if not config or not isinstance(config, dict):
                self._data = {}
                self._loaded = True
                return

            encrypted = config.get("_encrypted_creds")
            if not encrypted:
                self._data = {}
                self._loaded = True
                return

            decrypted = self._fernet.decrypt(encrypted.encode())
            self._data = json.loads(decrypted)
            self._loaded = True
        except InvalidToken:
            logger.warning(
                "Could not decrypt cloud credentials for user %s — "
                "key may have changed; starting fresh",
                self._user_id,
            )
            self._data = {}
            self._loaded = True
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Could not load cloud credentials: %s", exc)
            self._data = {}
            self._loaded = True

    def _save(self) -> None:
        """Encrypt and persist credentials to the database."""
        raw = json.dumps(self._data).encode()
        encrypted = self._fernet.encrypt(raw).decode()

        # Read current config_json to avoid overwriting other keys
        profile = self._db.get_business_profile()
        if profile and profile.get("config_json") and isinstance(profile["config_json"], dict):
            config = dict(profile["config_json"])
        else:
            config = {}

        config["_encrypted_creds"] = encrypted
        self._db.upsert_business_profile({"config_json": config})

    # ------------------------------------------------------------------
    # Public API — matches CredentialStore interface exactly
    # ------------------------------------------------------------------

    def store(self, key: str, value: str) -> None:
        """Store a credential (e.g., an OAuth token or API key)."""
        self._data[key] = value
        self._save()

    def retrieve(self, key: str) -> str | None:
        """Retrieve a credential, or None if not found."""
        return self._data.get(key)

    def delete(self, key: str) -> bool:
        """Delete a credential. Returns True if it existed."""
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False

    def list_keys(self) -> list[str]:
        """List all stored credential keys (names only, not values)."""
        return list(self._data.keys())

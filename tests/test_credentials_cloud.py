"""core/credentials_cloud.py — the encrypted per-user credential store.

Third-party tokens (a Wise API token, a mailbox refresh token) are stored
encrypted inside the user's business-profile row rather than in plaintext. The
properties that matter: a value survives a round trip and a new instance, the
plaintext never appears in what is persisted, the key is derived per user so
one user's blob is unreadable by another, corrupted data degrades to an empty
store instead of an exception, and a missing server secret is a hard failure
rather than a key derived from the empty string.

A mock database object stands in for PostgreSQL throughout.
"""

from __future__ import annotations

import pytest

from core.credentials_cloud import CloudCredentialStore, _derive_cloud_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeDB:
    """In-memory mock of the database interface expected by CloudCredentialStore.

    Exposes ``get_business_profile()`` and ``upsert_business_profile(data)``
    with a simple dict backing store, so round-trip persistence works within
    a single test without touching PostgreSQL.
    """

    def __init__(self, initial_profile: dict | None = None) -> None:
        self._profile: dict | None = initial_profile

    def get_business_profile(self) -> dict | None:
        return self._profile

    def upsert_business_profile(self, data: dict) -> None:
        if self._profile is None:
            self._profile = {}
        # Merge incoming data into profile (mirrors real upsert behaviour)
        self._profile.update(data)


def _make_store(
    monkeypatch,
    user_id: str = "test-user-001",
    secret: str = "test-secret-for-unit-tests",
    db: FakeDB | None = None,
) -> tuple[CloudCredentialStore, FakeDB]:
    """Convenience factory — sets env vars, creates a FakeDB, returns (store, db)."""
    monkeypatch.setenv("AUTH_JWT_SECRET", secret)
    if db is None:
        db = FakeDB()
    store = CloudCredentialStore(db, user_id)
    return store, db


# ---------------------------------------------------------------------------
# A stored value comes back, and is persisted encrypted
# ---------------------------------------------------------------------------

class TestStoreAndRetrieve:
    def test_store_and_retrieve_round_trip(self, monkeypatch):
        """Store 'wise_api_token' -> 'test-token-123', then retrieve returns it."""
        store, _ = _make_store(monkeypatch)
        store.store("wise_api_token", "test-token-123")
        assert store.retrieve("wise_api_token") == "test-token-123"

    def test_persisted_to_db(self, monkeypatch):
        """After store(), the encrypted blob appears in the DB profile."""
        store, db = _make_store(monkeypatch)
        store.store("wise_api_token", "test-token-123")
        profile = db.get_business_profile()
        assert profile is not None
        config = profile.get("config_json", {})
        assert "_encrypted_creds" in config
        # Encrypted blob should NOT contain the plaintext value
        assert "test-token-123" not in config["_encrypted_creds"]

    def test_round_trip_across_new_instance(self, monkeypatch):
        """A second CloudCredentialStore instance reads back what the first wrote."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        db = FakeDB()
        store1 = CloudCredentialStore(db, "test-user-001")
        store1.store("wise_api_token", "test-token-123")

        # Create a second instance against the same DB
        store2 = CloudCredentialStore(db, "test-user-001")
        assert store2.retrieve("wise_api_token") == "test-token-123"


# ---------------------------------------------------------------------------
# Retrieving a key that was never stored returns None
# ---------------------------------------------------------------------------

class TestRetrieveMissingKey:
    def test_retrieve_nonexistent_key(self, monkeypatch):
        """Retrieving a key that was never stored returns None."""
        store, _ = _make_store(monkeypatch)
        assert store.retrieve("nonexistent_key") is None


# ---------------------------------------------------------------------------
# Deleting an existing key returns True, and the deletion sticks
# ---------------------------------------------------------------------------

class TestDeleteExistingKey:
    def test_delete_existing_key(self, monkeypatch):
        """Delete returns True and the key is gone afterwards."""
        store, _ = _make_store(monkeypatch)
        store.store("key1", "val1")
        assert store.delete("key1") is True
        assert store.retrieve("key1") is None

    def test_delete_persists_removal(self, monkeypatch):
        """Deletion is persisted — a new instance also sees the key as gone."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        db = FakeDB()
        store1 = CloudCredentialStore(db, "test-user-001")
        store1.store("key1", "val1")
        store1.delete("key1")

        store2 = CloudCredentialStore(db, "test-user-001")
        assert store2.retrieve("key1") is None


# ---------------------------------------------------------------------------
# Deleting a key that never existed returns False
# ---------------------------------------------------------------------------

class TestDeleteMissingKey:
    def test_delete_nonexistent_key(self, monkeypatch):
        """Deleting a key that never existed returns False."""
        store, _ = _make_store(monkeypatch)
        assert store.delete("nonexistent") is False


# ---------------------------------------------------------------------------
# list_keys returns every stored key, and nothing when empty
# ---------------------------------------------------------------------------

class TestListKeys:
    def test_list_keys(self, monkeypatch):
        """list_keys returns the set of all stored keys."""
        store, _ = _make_store(monkeypatch)
        store.store("key_a", "val_a")
        store.store("key_b", "val_b")
        store.store("key_c", "val_c")
        assert set(store.list_keys()) == {"key_a", "key_b", "key_c"}

    def test_list_keys_empty(self, monkeypatch):
        """list_keys returns an empty list when nothing is stored."""
        store, _ = _make_store(monkeypatch)
        assert store.list_keys() == []


# ---------------------------------------------------------------------------
# Storing the same key twice overwrites rather than duplicates
# ---------------------------------------------------------------------------

class TestOverwriteExistingKey:
    def test_overwrite_existing_key(self, monkeypatch):
        """Storing the same key twice overwrites the old value."""
        store, _ = _make_store(monkeypatch)
        store.store("token", "old")
        store.store("token", "new")
        assert store.retrieve("token") == "new"

    def test_overwrite_does_not_duplicate_keys(self, monkeypatch):
        """Overwriting a key does not produce a duplicate in list_keys."""
        store, _ = _make_store(monkeypatch)
        store.store("token", "old")
        store.store("token", "new")
        assert store.list_keys().count("token") == 1


# ---------------------------------------------------------------------------
# The encryption key is derived per user, so blobs do not cross users
# ---------------------------------------------------------------------------

class TestPerUserKeyDerivation:
    def test_different_users_different_keys(self, monkeypatch):
        """_derive_cloud_key returns different keys for different user_ids."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        key_a = _derive_cloud_key("user-aaa")
        key_b = _derive_cloud_key("user-bbb")
        assert key_a != key_b

    def test_same_user_same_key(self, monkeypatch):
        """_derive_cloud_key is deterministic for the same inputs."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        key1 = _derive_cloud_key("user-aaa")
        key2 = _derive_cloud_key("user-aaa")
        assert key1 == key2

    def test_cross_user_isolation(self, monkeypatch):
        """Data encrypted by user A cannot be decrypted by user B's store."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        db_a = FakeDB()
        store_a = CloudCredentialStore(db_a, "user-aaa")
        store_a.store("secret_key", "user_a_secret")

        # Hand user A's database to user B — B should not be able to read it
        store_b = CloudCredentialStore(db_a, "user-bbb")
        # B's _load will hit InvalidToken and start fresh (empty _data)
        assert store_b.retrieve("secret_key") is None


# ---------------------------------------------------------------------------
# Corrupted stored data starts the store fresh instead of raising
# ---------------------------------------------------------------------------

class TestCorruptedStoreRecovery:
    def test_corrupted_data_starts_fresh(self, monkeypatch):
        """When the DB contains garbage in _encrypted_creds, the store
        initializes with empty data and does not raise."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        db = FakeDB(
            initial_profile={
                "config_json": {"_encrypted_creds": "CORRUPTED_GARBAGE"}
            }
        )
        # Should not raise
        store = CloudCredentialStore(db, "test-user-001")
        assert store.retrieve("anything") is None
        assert store.list_keys() == []

    def test_corrupted_data_can_store_afterwards(self, monkeypatch):
        """After recovering from corruption, new credentials can be stored."""
        monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-unit-tests")
        db = FakeDB(
            initial_profile={
                "config_json": {"_encrypted_creds": "CORRUPTED_GARBAGE"}
            }
        )
        store = CloudCredentialStore(db, "test-user-001")
        store.store("fresh_key", "fresh_value")
        assert store.retrieve("fresh_key") == "fresh_value"


# ---------------------------------------------------------------------------
# No secret configured is a hard failure, not a silent weak key
# ---------------------------------------------------------------------------

class TestMissingServerSecret:
    """An unset secret must raise rather than derive a key from the empty string.

    A key derived from an empty secret is the same key everywhere, so anyone who
    left the secret unset would be sharing it — a credential store with no
    server secret must not pretend to work."""

    def test_empty_secret_raises(self, monkeypatch):
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_SECRET", raising=False)
        monkeypatch.setenv("AUTH_JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="credential-encryption secret"):
            _derive_cloud_key("any-user")

    def test_credential_encryption_secret_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_SECRET", "decoupled-secret")
        monkeypatch.setenv("AUTH_JWT_SECRET", "session-secret")
        db = FakeDB()
        store = CloudCredentialStore(db, "test-user-001")
        store.store("key", "value")
        assert store.retrieve("key") == "value"
        # Rotating only the session secret must not break decryption.
        monkeypatch.setenv("AUTH_JWT_SECRET", "rotated-session-secret")
        assert store.retrieve("key") == "value"

    def test_missing_secret_env_var(self, monkeypatch):
        """If no secret is configured at all, constructing the store raises
        rather than encrypting under a key derived from the empty string."""
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_SECRET", raising=False)
        monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="credential-encryption secret"):
            CloudCredentialStore(FakeDB(), "test-user-001")


# ---------------------------------------------------------------------------
# A long value survives the round trip intact (5000 chars)
# ---------------------------------------------------------------------------

class TestLargeValueRoundTrip:
    def test_5000_char_value_round_trip(self, monkeypatch):
        """Store a 5000-character value, retrieve it, and assert the full
        value is preserved without truncation or corruption."""
        store, _ = _make_store(monkeypatch)
        large_value = "A" * 5000
        store.store("large_key", large_value)
        retrieved = store.retrieve("large_key")
        assert retrieved == large_value
        assert len(retrieved) == 5000

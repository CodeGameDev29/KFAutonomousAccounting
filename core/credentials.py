"""Encrypted credential storage using Fernet symmetric encryption.

Derives a machine-specific encryption key from machine UUID + username,
so no password is needed. Stores encrypted credentials in a JSON file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CREDENTIALS_FILE = _DATA_DIR / "credentials.enc.json"


def _get_machine_id() -> str:
    """Get a stable machine identifier."""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True, text=True, timeout=5,
            )
            lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            if len(lines) >= 2:
                return lines[-1]
        elif system == "Darwin":
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        else:
            machine_id_path = Path("/etc/machine-id")
            if machine_id_path.exists():
                return machine_id_path.read_text().strip()
    except Exception:
        pass
    return os.getenv("AA_MACHINE_ID_FALLBACK", "CHANGE_ME_MACHINE_ID_FALLBACK")


def _derive_key() -> bytes:
    """Derive a Fernet key from machine ID + username."""
    machine_id = _get_machine_id()
    username = os.getenv("USERNAME", os.getenv("USER", "default"))
    seed = f"{machine_id}:{username}:" + os.getenv("AA_CREDENTIAL_NAMESPACE", "aa-credentials-v1")
    digest = hashlib.sha256(seed.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class CredentialStore:
    """Encrypted credential storage backed by a local JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _CREDENTIALS_FILE
        self._fernet = Fernet(_derive_key())
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            raw = self._path.read_bytes()
            decrypted = self._fernet.decrypt(raw)
            self._data = json.loads(decrypted)
        except (InvalidToken, json.JSONDecodeError):
            logger.warning("Could not decrypt credentials file — starting fresh")
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self._data).encode()
        encrypted = self._fernet.encrypt(raw)
        self._path.write_bytes(encrypted)

    def store(self, key: str, value: str) -> None:
        """Store a credential."""
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
        """List all stored credential keys."""
        return list(self._data.keys())

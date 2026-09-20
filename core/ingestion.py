"""Ingestion module — file type detection, receipt processing, and bank CSV processing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from config.settings import SUPPORTED_RECEIPT_EXTENSIONS
from core.bank_parser import parse_csv as parse_bank_csv  # noqa: F401

# ---------------------------------------------------------------------------
# Imported here rather than called through their own modules, so
# "core.ingestion.<name>" is a single substitution point for both helpers.
# ---------------------------------------------------------------------------
from core.extraction import extract_document as extract_receipt_data  # noqa: F401

# ---------------------------------------------------------------------------
# File-type helpers
# ---------------------------------------------------------------------------

_RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp"}


def detect_file_type(file_path: Path) -> str:
    """Return ``'receipt'`` or ``'csv'`` based on file extension.

    Raises ``ValueError`` for unsupported extensions.
    """
    ext = file_path.suffix.lower()
    if ext in _RECEIPT_EXTENSIONS:
        return "receipt"
    if ext == ".csv":
        return "csv"
    raise ValueError(f"Unsupported file type: {ext}")


def is_supported_receipt(file_path: Path) -> bool:
    """Return True if *file_path* has a supported receipt extension."""
    return file_path.suffix.lower() in SUPPORTED_RECEIPT_EXTENSIONS


def is_supported_csv(file_path: Path) -> bool:
    """Return True if *file_path* has a ``.csv`` extension."""
    return file_path.suffix.lower() == ".csv"


# ---------------------------------------------------------------------------
# Hashing helper
# ---------------------------------------------------------------------------

def _compute_file_hash(file_path: Path) -> str:
    """Return the SHA-256 hex digest of *file_path*."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Receipt processing
# ---------------------------------------------------------------------------

async def process_receipt(
    file_path: Path,
    db: Any,
    *,
    openai_client: Any = None,
) -> dict:
    """Process a single receipt file.

    1. Compute SHA-256 hash of the file.
    2. Check the database for an existing document with the same hash.
    3. If duplicate → return early with status ``"duplicate"``.
    4. Otherwise extract data via LLM, store the document, and return success.
    """
    file_hash = _compute_file_hash(file_path)

    existing = await db.get_document_by_hash(file_hash)
    if existing:
        return {"status": "duplicate", "message": "File already processed"}

    extraction_data = await extract_receipt_data(file_path, openai_client)

    doc_id = await db.store_document(extraction_data)

    return {
        "status": "success",
        "document_id": doc_id,
        "message": "Processed successfully",
    }


# ---------------------------------------------------------------------------
# Bank CSV processing
# ---------------------------------------------------------------------------

async def process_bank_csv(file_path: Path, db: Any) -> dict:
    """Parse a bank CSV and store new transactions.

    Returns a dict with ``new_count``, ``duplicate_count``, ``matched_count``,
    and ``status``.
    """
    transactions = parse_bank_csv(file_path)

    new_count = 0
    duplicate_count = 0
    matched_count = 0

    for txn in transactions:
        if await db.transaction_exists(txn):
            duplicate_count += 1
        else:
            txn_id = await db.store_transaction(txn)
            new_count += 1
            match_result = await db.attempt_reconciliation(txn_id)
            if match_result:
                matched_count += 1

    return {
        "status": "success",
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "matched_count": matched_count,
    }

"""core/ingestion.py — file-type detection and the two ingest entry points.

A dropped file is routed by extension alone (receipt, CSV, or refused), and
both ingest paths report what they did with each item: a receipt whose hash is
already stored is a duplicate rather than a second document, and a bank CSV
returns new/duplicate/matched counts the caller can show.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ingestion import (
    detect_file_type,
    is_supported_csv,
    is_supported_receipt,
    process_bank_csv,
    process_receipt,
)

# ---------------------------------------------------------------------------
# detect_file_type
# ---------------------------------------------------------------------------

class TestDetectFileType:
    def test_detect_file_type_pdf(self, tmp_path: Path):
        f = tmp_path / "receipt.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        assert detect_file_type(f) == "receipt"

    def test_detect_file_type_jpg(self, tmp_path: Path):
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"\xff\xd8fake")
        assert detect_file_type(f) == "receipt"

    def test_detect_file_type_png(self, tmp_path: Path):
        f = tmp_path / "screenshot.png"
        f.write_bytes(b"\x89PNGfake")
        assert detect_file_type(f) == "receipt"

    def test_detect_file_type_jfif(self, tmp_path: Path):
        f = tmp_path / "image.jfif"
        f.write_bytes(b"\xff\xd8fake")
        assert detect_file_type(f) == "receipt"

    def test_detect_file_type_csv(self, tmp_path: Path):
        f = tmp_path / "bank.csv"
        f.write_text("date,amount\n2026-01-01,100")
        assert detect_file_type(f) == "csv"

    def test_detect_file_type_unsupported(self, tmp_path: Path):
        f = tmp_path / "document.docx"
        f.write_bytes(b"PK\x03\x04fake")
        with pytest.raises(ValueError):
            detect_file_type(f)


# ---------------------------------------------------------------------------
# is_supported_receipt
# ---------------------------------------------------------------------------

class TestIsSupportedReceipt:
    @pytest.mark.parametrize("ext", [".pdf", ".jpg", ".png"])
    def test_is_supported_receipt_true(self, tmp_path: Path, ext: str):
        f = tmp_path / f"file{ext}"
        f.write_bytes(b"fake")
        assert is_supported_receipt(f) is True

    @pytest.mark.parametrize("ext", [".csv", ".txt"])
    def test_is_supported_receipt_false(self, tmp_path: Path, ext: str):
        f = tmp_path / f"file{ext}"
        f.write_bytes(b"fake")
        assert is_supported_receipt(f) is False


# ---------------------------------------------------------------------------
# is_supported_csv
# ---------------------------------------------------------------------------

class TestIsSupportedCsv:
    def test_is_supported_csv_true(self, tmp_path: Path):
        f = tmp_path / "bank.csv"
        f.write_text("a,b,c")
        assert is_supported_csv(f) is True

    def test_is_supported_csv_false(self, tmp_path: Path):
        f = tmp_path / "receipt.pdf"
        f.write_bytes(b"%PDF")
        assert is_supported_csv(f) is False


# ---------------------------------------------------------------------------
# process_receipt — duplicate detection
# ---------------------------------------------------------------------------

class TestProcessReceiptDuplicate:
    @pytest.mark.asyncio
    async def test_process_receipt_duplicate_detection(self, tmp_path: Path):
        receipt = tmp_path / "20260115-Laptop.pdf"
        receipt.write_bytes(b"%PDF-1.4 fake receipt content")
        file_hash = hashlib.sha256(receipt.read_bytes()).hexdigest()

        # Mock database: first call says hash not seen, second call says duplicate.
        mock_db = AsyncMock()
        mock_db.get_document_by_hash = AsyncMock(
            side_effect=[None, {"id": 1, "hash": file_hash}]
        )
        mock_db.store_document = AsyncMock(return_value=1)

        # Mock OpenAI client so no real API call happens.
        mock_openai = MagicMock()

        # Patch extraction so the test needs no real LLM response.
        fake_extraction = {
            "vendor": "Amazon",
            "date": "2026-01-15",
            "total": "999.99",
            "currency": "CAD",
            "items": [],
        }
        with patch(
            "core.ingestion.extract_receipt_data",
            new_callable=AsyncMock,
            return_value=fake_extraction,
        ):
            result1 = await process_receipt(receipt, mock_db, openai_client=mock_openai)
            result2 = await process_receipt(receipt, mock_db, openai_client=mock_openai)

        assert result1["status"] != "duplicate"
        assert result2["status"] == "duplicate"


# ---------------------------------------------------------------------------
# process_bank_csv — return counts
# ---------------------------------------------------------------------------

class TestProcessBankCsv:
    @pytest.mark.asyncio
    async def test_process_bank_csv_returns_counts(self, tmp_path: Path):
        csv_file = tmp_path / "bank_jan2026.csv"
        csv_file.write_text(
            "Date,Description,Amount\n"
            "2026-01-05,Amazon,-49.99\n"
            "2026-01-10,Salary,5000.00\n"
        )

        mock_db = AsyncMock()
        # Simulate: first row is new, second is a duplicate.
        mock_db.transaction_exists = AsyncMock(side_effect=[False, True])
        mock_db.store_transaction = AsyncMock(return_value=1)
        mock_db.attempt_reconciliation = AsyncMock(return_value=1)

        with patch(
            "core.ingestion.parse_bank_csv",
            return_value=[
                {
                    "date": "2026-01-05",
                    "description": "Amazon",
                    "amount": "-49.99",
                },
                {
                    "date": "2026-01-10",
                    "description": "Salary",
                    "amount": "5000.00",
                },
            ],
        ):
            result = await process_bank_csv(csv_file, mock_db)

        assert "new_count" in result
        assert "duplicate_count" in result
        assert "matched_count" in result
        assert result["status"] == "success"

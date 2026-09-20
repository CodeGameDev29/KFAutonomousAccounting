"""core/extraction.py — turning an uploaded file into a validated Document.

Covers: file hashing, extraction validation, JSON parsing, Document building,
prompt construction, the async extraction call, and the provider-failover
rules that decide when a failed call is worth retrying.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.extraction as extraction_module
from config.settings import EXPENSE_CATEGORIES
from core.extraction import (
    build_categorization_prompt,
    build_document_from_extraction,
    build_extraction_prompt,
    compute_file_hash,
    extract_document,
    parse_extraction_response,
    validate_extraction,
)
from models.document import Document, DocumentStatus

# ─── Helpers ────────────────────────────────────────────────────────


def _make_temp_file(content: bytes) -> Path:
    """Write *content* to a NamedTemporaryFile and return its Path (caller deletes)."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    f.write(content)
    f.close()
    return Path(f.name)


def _valid_extraction_data(
    *,
    vendor: str = "Test Vendor",
    subtotal: str = "100.00",
    gst: str | None = "5.00",
    pst: str | None = "7.00",
    hst: str | None = None,
    other_tax: str | None = None,
    total: str = "112.00",
    line_items: list[dict] | None = None,
) -> dict:
    """Return a minimal valid extraction dict in the shape the model returns."""
    return {
        "vendor": vendor,
        "date": "2026-01-15",
        "currency": "CAD",
        "subtotal": subtotal,
        "gst": gst,
        "pst": pst,
        "hst": hst,
        "other_tax": other_tax,
        "total": total,
        "payment_method": "Credit Card",
        "invoice_number": "INV-001",
        "line_items": line_items,
    }


# ═══════════════════════════════════════════════════════════════════
#  FILE HASHING
# ═══════════════════════════════════════════════════════════════════


class TestComputeFileHash:
    def test_compute_file_hash(self, tmp_path: Path):
        """SHA-256 hex digest of known content matches hashlib reference."""
        content = b"hello world"
        file = tmp_path / "receipt.pdf"
        file.write_bytes(content)

        expected = hashlib.sha256(content).hexdigest()
        assert compute_file_hash(file) == expected

    def test_compute_file_hash_same_content(self, tmp_path: Path):
        """Two files with identical content produce the same hash."""
        content = b"identical content"
        file_a = tmp_path / "a.pdf"
        file_b = tmp_path / "b.pdf"
        file_a.write_bytes(content)
        file_b.write_bytes(content)

        assert compute_file_hash(file_a) == compute_file_hash(file_b)

    def test_compute_file_hash_different_content(self, tmp_path: Path):
        """Different content produces different hashes."""
        file_a = tmp_path / "a.pdf"
        file_b = tmp_path / "b.pdf"
        file_a.write_bytes(b"content A")
        file_b.write_bytes(b"content B")

        assert compute_file_hash(file_a) != compute_file_hash(file_b)


# ═══════════════════════════════════════════════════════════════════
#  VALIDATION
# ═══════════════════════════════════════════════════════════════════


class TestValidateExtraction:
    def test_validate_extraction_valid(self):
        """subtotal 100 + gst 5 + pst 7 = total 112 is valid."""
        data = _valid_extraction_data()
        is_valid, errors = validate_extraction(data)

        assert is_valid is True
        assert errors == []

    def test_validate_extraction_tax_mismatch(self):
        """subtotal + taxes != total produces a warning (lenient validation)."""
        data = _valid_extraction_data(total="999.99")
        is_valid, warnings = validate_extraction(data)

        # Lenient validation: mismatch is a warning, not a rejection
        assert is_valid is True
        assert len(warnings) >= 1
        # The warning message should mention the mismatch
        assert any("total" in w.lower() or "mismatch" in w.lower() for w in warnings)

    def test_validate_extraction_within_tolerance(self):
        """Off by $0.01 (within +/-$0.02 tolerance) is still valid."""
        # subtotal 100 + gst 5 + pst 7 = 112, but total says 112.01
        data = _valid_extraction_data(total="112.01")
        is_valid, errors = validate_extraction(data)

        assert is_valid is True
        assert errors == []

    def test_validate_extraction_exceeds_tolerance(self):
        """Off by $0.05 exceeds the $0.02 tolerance — produces a warning (lenient)."""
        data = _valid_extraction_data(total="112.05")
        is_valid, warnings = validate_extraction(data)

        # Lenient validation: tolerance exceedance is a warning, not a rejection
        assert is_valid is True
        assert len(warnings) >= 1

    def test_validate_extraction_line_items_match(self):
        """Line items summing to subtotal is valid."""
        line_items = [
            {"description": "Item A", "quantity": 1, "unit_price": "60.00", "amount": "60.00"},
            {"description": "Item B", "quantity": 2, "unit_price": "20.00", "amount": "40.00"},
        ]
        data = _valid_extraction_data(subtotal="100.00", line_items=line_items)
        is_valid, errors = validate_extraction(data)

        assert is_valid is True
        assert errors == []

    def test_validate_extraction_line_items_mismatch(self):
        """Line items that do not sum to subtotal — lenient validation passes with no warning."""
        line_items = [
            {"description": "Item A", "quantity": 1, "unit_price": "30.00", "amount": "30.00"},
        ]
        # subtotal is 100 but line items total only 30
        # Lenient validation does not check line item consistency
        data = _valid_extraction_data(subtotal="100.00", line_items=line_items)
        is_valid, warnings = validate_extraction(data)

        assert is_valid is True

    def test_validate_extraction_no_taxes(self):
        """All tax fields None, subtotal == total is valid."""
        data = _valid_extraction_data(
            subtotal="50.00",
            gst=None,
            pst=None,
            hst=None,
            other_tax=None,
            total="50.00",
        )
        is_valid, errors = validate_extraction(data)

        assert is_valid is True
        assert errors == []

    def test_validate_extraction_missing_total(self):
        """total is None — invalid."""
        data = _valid_extraction_data()
        data["total"] = None
        is_valid, errors = validate_extraction(data)

        assert is_valid is False
        assert any("amount" in e.lower() for e in errors)


# ═══════════════════════════════════════════════════════════════════
#  JSON PARSING
# ═══════════════════════════════════════════════════════════════════


class TestParseExtractionResponse:
    def test_parse_extraction_response_valid(self):
        """Valid JSON string is parsed into a dict."""
        raw = json.dumps({"vendor": "Acme", "total": "42.00"})
        result = parse_extraction_response(raw)

        assert isinstance(result, dict)
        assert result["vendor"] == "Acme"
        assert result["total"] == "42.00"

    def test_parse_extraction_response_invalid_json(self):
        """Garbage input raises ValueError."""
        with pytest.raises(ValueError):
            parse_extraction_response("this is not json {{{")

    def test_parse_extraction_response_with_markdown_fence(self):
        """Markdown code-fence wrapper (```json ... ```) is stripped before parsing."""
        inner = {"vendor": "Fenced Corp", "total": "99.99"}
        raw = f"```json\n{json.dumps(inner)}\n```"
        result = parse_extraction_response(raw)

        assert result["vendor"] == "Fenced Corp"
        assert result["total"] == "99.99"


# ═══════════════════════════════════════════════════════════════════
#  DOCUMENT BUILDING
# ═══════════════════════════════════════════════════════════════════


class TestBuildDocumentFromExtraction:
    def test_build_document_from_extraction(self):
        """Valid extraction data produces a fully populated Document."""
        data = _valid_extraction_data()
        raw_json = json.dumps(data)
        doc = build_document_from_extraction(
            extraction_data=data,
            original_filename="20260115-TestReceipt.pdf",
            file_hash="abc123hash",
            raw_json=raw_json,
        )

        assert isinstance(doc, Document)
        assert doc.original_filename == "20260115-TestReceipt.pdf"
        assert doc.file_hash == "abc123hash"
        assert doc.vendor == "Test Vendor"
        assert doc.currency == "CAD"
        assert doc.subtotal == Decimal("100.00")
        assert doc.total == Decimal("112.00")
        assert doc.tax_gst == Decimal("5.00")
        assert doc.tax_pst == Decimal("7.00")
        assert doc.status == DocumentStatus.EXTRACTED

    def test_build_document_sets_confidence_from_validation(self):
        """Confidence is 'high' when validation passes, 'low' when it fails."""
        # Valid data → high confidence
        valid_data = _valid_extraction_data()
        doc_good = build_document_from_extraction(
            extraction_data=valid_data,
            original_filename="good.pdf",
            file_hash="hash1",
            raw_json=json.dumps(valid_data),
        )
        assert doc_good.extraction_confidence == "high"

        # Lenient validation: total mismatch is a warning, not a failure,
        # so confidence is still "high"
        warn_data = _valid_extraction_data(total="999.99")
        doc_warn = build_document_from_extraction(
            extraction_data=warn_data,
            original_filename="warn.pdf",
            file_hash="hash2",
            raw_json=json.dumps(warn_data),
        )
        assert doc_warn.extraction_confidence == "high"

        # Missing total entirely (None) → invalid → low confidence
        bad_data = _valid_extraction_data()
        bad_data["total"] = None
        doc_bad = build_document_from_extraction(
            extraction_data=bad_data,
            original_filename="bad.pdf",
            file_hash="hash3",
            raw_json=json.dumps(bad_data),
        )
        assert doc_bad.extraction_confidence == "low"

    def test_build_document_preserves_raw_json(self):
        """The model's raw JSON response is stored unmodified on the Document.

        It is the audit trail for every field the extractor derived, so it must
        survive round-tripping exactly as it arrived.
        """
        data = _valid_extraction_data()
        raw_json = json.dumps(data, indent=2)
        doc = build_document_from_extraction(
            extraction_data=data,
            original_filename="receipt.pdf",
            file_hash="somehash",
            raw_json=raw_json,
        )
        assert doc.extraction_raw_json == raw_json


# ═══════════════════════════════════════════════════════════════════
#  PROMPT BUILDING
# ═══════════════════════════════════════════════════════════════════


class TestBuildExtractionPrompt:
    def test_build_extraction_prompt_contains_schema(self):
        """The extraction prompt should reference the expected JSON schema fields."""
        prompt = build_extraction_prompt()
        for field_name in ("vendor", "date", "subtotal", "total", "currency", "line_items"):
            assert field_name in prompt, f"Expected '{field_name}' in extraction prompt"


class TestBuildCategorizationPrompt:
    def test_build_categorization_prompt_includes_categories(self):
        """Every fixed ISED/GIFI category from settings must appear in the prompt,
        and no retired legacy vocabulary may leak."""
        prompt = build_categorization_prompt(
            description="Laptop purchase",
            vendor="Amazon",
            amount="1299.99",
            currency="CAD",
            date="2026-01-15",
        )
        for category in EXPENSE_CATEGORIES:
            assert category in prompt, f"Expected category '{category}' in categorization prompt"
        for retired in ("Office IT", "Bank Fee", "Misc Expense"):
            assert retired not in prompt, f"Retired legacy category '{retired}' leaked into prompt"

    def test_build_categorization_prompt_includes_transaction_details(self):
        """The prompt should embed the supplied transaction details."""
        prompt = build_categorization_prompt(
            description="Monthly server hosting",
            vendor="AWS",
            amount="250.00",
            currency="USD",
            date="2026-02-01",
        )
        assert "AWS" in prompt
        assert "250.00" in prompt
        assert "USD" in prompt
        assert "2026-02-01" in prompt
        assert "Monthly server hosting" in prompt


# ═══════════════════════════════════════════════════════════════════
#  ASYNC EXTRACTION (mock OpenAI)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestExtractDocument:
    async def test_extract_document_calls_openai(self, tmp_path: Path):
        """The OpenAI client is called with the configured model."""
        # Use .png to skip the fitz PDF-to-image conversion path
        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(_valid_extraction_data())))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        await extract_document(file, openai_client=mock_client)

        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args
        # Verify the model parameter is passed (exact value comes from config)
        assert "model" in call_kwargs.kwargs or (
            call_kwargs.args and len(call_kwargs.args) > 0
        )

    async def test_extract_document_returns_parsed_data(self, tmp_path: Path):
        """When OpenAI returns valid JSON, the parsed dict is returned."""
        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")

        expected_data = _valid_extraction_data(vendor="Parsed Vendor")
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(expected_data)))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await extract_document(file, openai_client=mock_client)

        assert isinstance(result, dict)
        assert result["vendor"] == "Parsed Vendor"

    async def test_extract_document_retries_on_invalid_json(self, tmp_path: Path):
        """First call returns bad JSON, second returns good — overall succeeds."""
        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")

        good_data = _valid_extraction_data()

        bad_response = MagicMock()
        bad_response.choices = [
            MagicMock(message=MagicMock(content="NOT VALID JSON {{{{"))
        ]
        good_response = MagicMock()
        good_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(good_data)))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[bad_response, good_response]
        )

        result = await extract_document(file, openai_client=mock_client)

        assert isinstance(result, dict)
        assert result["vendor"] == good_data["vendor"]
        assert mock_client.chat.completions.create.call_count == 2

    async def test_extract_document_flags_low_confidence_on_repeated_failure(
        self, tmp_path: Path
    ):
        """Both attempts return data that fails validation — confidence is 'low'."""
        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")

        # Data where total mismatches badly (exceeds tolerance)
        bad_data = _valid_extraction_data(total="999.99")

        bad_response = MagicMock()
        bad_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(bad_data)))
        ]

        mock_client = AsyncMock()
        # Return the same bad-validation data on every attempt
        mock_client.chat.completions.create = AsyncMock(return_value=bad_response)

        result = await extract_document(file, openai_client=mock_client)

        # The result should still be returned but flagged as low confidence
        assert isinstance(result, dict)
        assert result.get("_confidence", "low") == "low"


# ═══════════════════════════════════════════════════════════════════
#  EXTRACTION SEMAPHORE (concurrency control)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestExtractionSemaphore:
    async def test_extraction_semaphore_limits_concurrency(self, tmp_path: Path):
        """When 10 concurrent extract_document() calls are made with
        LLM_MAX_CONCURRENT_EXTRACTIONS=3, at most 3 run simultaneously."""

        peak_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        valid_data = _valid_extraction_data()

        async def slow_gemini(*args, **kwargs):
            nonlocal peak_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                peak_concurrent = max(peak_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            result = dict(valid_data)
            result["_provider"] = "gemini"
            return result

        # Reset the module-level semaphore so it re-initializes with the test value
        extraction_module._extraction_semaphore = None

        # Create 10 test files
        files = []
        for i in range(10):
            f = tmp_path / f"receipt_{i}.png"
            f.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
            files.append(f)

        mock_gemini_cfg = MagicMock(
            enabled=True, max_concurrent_extractions=3,
            model="gemini-3.6-flash", model_candidates=["gemini-3.6-flash"],
        )
        with patch("core.extraction._extract_with_gemini", side_effect=slow_gemini), \
             patch("config.settings.gemini_config", mock_gemini_cfg):
            # Reset semaphore inside the patched env so it picks up mock config
            extraction_module._extraction_semaphore = None

            results = await asyncio.gather(
                *[extract_document(f) for f in files]
            )

        # All 10 should complete
        assert len(results) == 10
        # Peak concurrency should not exceed 3
        assert peak_concurrent <= 3, f"Peak concurrency was {peak_concurrent}, expected <= 3"
        # At least 2 should have run concurrently (not just serial)
        assert peak_concurrent >= 2, f"Peak concurrency was {peak_concurrent}, expected >= 2"

        # Clean up: reset semaphore for other tests
        extraction_module._extraction_semaphore = None


@pytest.mark.asyncio
class TestQuotaFailover:
    """A capacity rejection fails over at once instead of sleeping on it.

    A 429 says the provider will not serve this call, not that it failed
    transiently: retrying the same request cannot succeed. Stacked retry
    ladders — a tenacity decorator wrapping an in-function sleep loop — would
    turn an instant rejection into minutes of sleeping before the fallback
    provider is even tried, which is longer than an upload request survives.
    The user sees a failed upload while the receipt lands minutes later.

    The contract these lock in: a capacity rejection fails over immediately.
    """

    async def test_gemini_quota_error_fails_over_without_sleeping(self, tmp_path: Path):
        """Drives the real _extract_with_gemini retry path, not a stand-in.

        Patching _extract_with_gemini out would bypass the retry loop that is
        the subject of the test, so this mocks the genai client underneath it
        and counts both the API calls and the seconds slept.
        """
        import google.genai as genai

        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")

        slept: list[float] = []
        calls: list[str] = []

        async def record_sleep(seconds):
            slept.append(seconds)

        async def raise_quota(*args, **kwargs):
            calls.append("gemini")
            raise Exception(
                "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
                "'You exceeded your current quota', 'status': 'RESOURCE_EXHAUSTED'}}"
            )

        fake_genai_client = MagicMock()
        fake_genai_client.aio.models.generate_content = raise_quota

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(_valid_extraction_data())))
        ]
        mock_openai = AsyncMock()
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)

        extraction_module._extraction_semaphore = None
        mock_gemini_cfg = MagicMock(
            enabled=True, max_concurrent_extractions=3,
            model="gemini-3.6-flash", model_candidates=["gemini-3.6-flash"],
        )
        with patch.object(genai, "Client", return_value=fake_genai_client), \
             patch("config.settings.gemini_config", mock_gemini_cfg), \
             patch("core.extraction.asyncio.sleep", side_effect=record_sleep):
            result = await extract_document(file, openai_client=mock_openai)

        extraction_module._extraction_semaphore = None

        assert result["_provider"] == "openai", "quota rejection must fail over to OpenAI"
        # A retrying failover would show repeated calls and recorded sleeps.
        assert calls == ["gemini"], f"quota error must not be retried; got {calls}"
        assert slept == [], f"failover must not sleep; slept for {slept}"

    async def test_quota_error_detection_covers_provider_shapes(self):
        """_is_quota_error must catch the wordings the providers use.

        The detector matches on substrings ("quota", "rate limit", "resource
        exhausted"), so the exact phrases below are the contract: they are what
        distinguishes a capacity rejection from a fault worth retrying.
        """
        from openai import APIStatusError

        from core.extraction import _is_quota_error

        assert _is_quota_error(Exception("429 RESOURCE_EXHAUSTED"))
        assert _is_quota_error(Exception("You exceeded your current quota"))
        assert _is_quota_error(Exception("Rate limit reached for gpt-4.1"))

        rate_limited = MagicMock(spec=APIStatusError)
        rate_limited.status_code = 429
        rate_limited.__str__ = lambda self: "rate limit exceeded"
        assert _is_quota_error(rate_limited)

        # A genuine extraction fault must NOT be mistaken for a capacity one —
        # that would skip the retry the transient path is there to provide.
        assert not _is_quota_error(ValueError("could not parse JSON response"))
        assert not _is_quota_error(Exception("500 Internal Server Error"))


class TestModelLadder:
    """Laddering across models must not quietly lower the accuracy bar.

    Each model carries its own daily request quota, so the ladder is what keeps
    extraction available once a rung is exhausted. The rungs below the first
    are the less accurate ones — a weaker model can invent a vendor name or
    return a total off by a factor of a hundred — and they report "high"
    confidence while doing it. The ladder buys availability; the trust boundary
    is what stops it buying wrong numbers.
    """

    def test_ladder_is_quality_ordered_and_excludes_hallucinators(self):
        from config.settings import DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_MODEL_LADDER

        assert DEFAULT_GEMINI_MODEL_LADDER[0] == DEFAULT_GEMINI_MODEL
        # Invents vendor names, so it is not a rung.
        assert "gemini-3.1-flash-lite" not in DEFAULT_GEMINI_MODEL_LADDER
        # Carries no request quota, so it can never answer.
        assert "gemini-2.5-pro" not in DEFAULT_GEMINI_MODEL_LADDER
        assert len(DEFAULT_GEMINI_MODEL_LADDER) >= 2, "a single rung is not a ladder"

    def test_pinned_dead_model_does_not_demote_the_maintained_default(self, monkeypatch):
        """A deployment may pin a model whose quota has been withdrawn.

        Trust must not be defined as "candidates[0]" — that would make every
        3.6-flash extraction look like a fallback and send all of them to
        manual review.
        """
        import importlib

        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
        import config.settings as cs
        importlib.reload(cs)
        try:
            g = cs.gemini_config
            assert g.model_candidates[0] == "gemini-2.5-pro"
            assert cs.DEFAULT_GEMINI_MODEL in g.trusted_models
            # The survival rungs are explicitly not trusted.
            assert "gemini-3.5-flash" not in g.trusted_models
            assert "gemini-2.5-flash" not in g.trusted_models
        finally:
            monkeypatch.undo()
            importlib.reload(cs)

    def test_ladder_env_override_can_disable_laddering(self, monkeypatch):
        import importlib

        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")
        monkeypatch.setenv("GEMINI_MODEL_LADDER", "gemini-3.6-flash")
        import config.settings as cs
        importlib.reload(cs)
        try:
            assert cs.gemini_config.model_candidates == ["gemini-3.6-flash"]
        finally:
            monkeypatch.undo()
            importlib.reload(cs)


@pytest.mark.asyncio
class TestLadderAdvanceRules:
    """Which failures advance the ladder, and which concede the provider.

    Advancing costs one extra call; conceding costs accuracy, because the
    fallback provider is the weaker extractor of the two. A timeout says
    nothing about the model — only that the call did not return in time — so
    advancing on one would multiply the wait instead of shortening it.
    """

    async def _run(self, exc_factory, tmp_path):
        import google.genai as genai

        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
        tried: list[str] = []

        async def failing(*args, **kwargs):
            tried.append(kwargs.get("model") or "?")
            raise exc_factory()

        fake_client = MagicMock()
        fake_client.aio.models.generate_content = failing

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(_valid_extraction_data())))
        ]
        mock_openai = AsyncMock()
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)

        extraction_module._extraction_semaphore = None
        cfg = MagicMock(
            enabled=True, max_concurrent_extractions=3, model="m1",
            model_candidates=["m1", "m2", "m3"],
        )
        with patch.object(genai, "Client", return_value=fake_client), \
             patch("config.settings.gemini_config", cfg), \
             patch("core.extraction.asyncio.sleep", side_effect=AsyncMock()):
            await extract_document(file, openai_client=mock_openai)
        extraction_module._extraction_semaphore = None
        # Distinct models in order. A model may legitimately appear twice — the
        # in-function transient retry — but this is about ladder *advancement*.
        return list(dict.fromkeys(tried))

    async def test_malformed_output_advances(self, tmp_path: Path):
        tried = await self._run(lambda: ValueError("Invalid JSON: Expecting ',' delimiter"), tmp_path)
        assert tried == ["m1", "m2", "m3"], f"bad JSON must try the next model; got {tried}"

    async def test_quota_advances(self, tmp_path: Path):
        tried = await self._run(lambda: Exception("429 RESOURCE_EXHAUSTED"), tmp_path)
        assert tried == ["m1", "m2", "m3"]

    async def test_timeout_does_not_advance(self, tmp_path: Path):
        tried = await self._run(asyncio.TimeoutError, tmp_path)
        assert tried == ["m1"], f"a timeout must concede immediately; got {tried}"

    async def test_auth_error_does_not_advance(self, tmp_path: Path):
        tried = await self._run(lambda: Exception("401 API key not valid"), tmp_path)
        assert tried == ["m1"], f"auth failure is not model-specific; got {tried}"

    async def test_model_retired_advances(self, tmp_path: Path):
        """New projects get 404 for older models — try the next rung."""
        tried = await self._run(
            lambda: Exception(
                "404 NOT_FOUND. This model models/gemini-2.5-flash is no longer "
                "available to new projects"
            ),
            tmp_path,
        )
        assert tried == ["m1", "m2", "m3"], f"retired model must advance; got {tried}"


@pytest.mark.asyncio
class TestPerCallKeyOverride:
    """``extract_document(gemini_api_key=...)`` overrides the configured key.

    Request quota is granted per project, so a caller that must draw on a
    different project's quota supplies its own key for that one call. The
    code's job is narrow: use whatever key it is given, and never let that key
    leak into calls that did not ask for it.
    """

    async def _capture_key(self, tmp_path: Path, **kwargs) -> str | None:
        import google.genai as genai

        file = tmp_path / "receipt.png"
        file.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
        seen: dict = {}

        def fake_client_ctor(api_key=None, **_kw):
            seen["key"] = api_key
            client = MagicMock()

            async def gen(*a, **k):
                raise Exception("stop here — only the key matters")

            client.aio.models.generate_content = gen
            return client

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps(_valid_extraction_data())))
        ]
        mock_openai = AsyncMock()
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_response)

        extraction_module._extraction_semaphore = None
        cfg = MagicMock(
            enabled=True, max_concurrent_extractions=3, model="m1",
            model_candidates=["m1"], api_key="CONFIGURED_KEY",
        )
        with patch.object(genai, "Client", side_effect=fake_client_ctor), \
             patch("config.settings.gemini_config", cfg), \
             patch("core.extraction.asyncio.sleep", side_effect=AsyncMock()):
            await extract_document(file, openai_client=mock_openai, **kwargs)
        extraction_module._extraction_semaphore = None
        return seen.get("key")

    async def test_the_supplied_key_is_used_when_given(self, tmp_path: Path):
        key = await self._capture_key(tmp_path, gemini_api_key="OVERRIDE_KEY")
        assert key == "OVERRIDE_KEY", "a per-call key must be the one that is used"

    async def test_main_path_still_uses_the_configured_key(self, tmp_path: Path):
        key = await self._capture_key(tmp_path)
        assert key == "CONFIGURED_KEY", "a call with no override uses the configured key"


class TestRenderMemory:
    """PDF rendering is the memory-heavy step of extraction, so the render
    loop must not retain decoded pixmaps."""

    def test_pdf_render_releases_pixmaps(self):
        """Each iteration must drop its pixmap before rendering the next page.

        Without this the decoded RGB buffer — tens of megabytes for one page —
        stays reachable while the next page renders, so peak memory grows with
        the length of the document instead of staying flat.
        """
        import inspect

        from core.extraction import _prepare_image_data

        src = inspect.getsource(_prepare_image_data)
        pdf_block = src[src.index('if ext == ".pdf"'):src.index('return images, "image/png", None')]
        assert "pix = None" in pdf_block, (
            "the PDF render loop must release each pixmap before the next page"
        )

    def test_statement_dpi_default_is_150(self):
        """Statement pages render at 150 DPI, not higher.

        The model downsamples every image to a fixed tile budget, so a higher
        DPI produces the same input-token count and the same parsed rows while
        roughly doubling peak render memory.
        """
        from config.settings import statement_parsing_config
        from core.llm_statement_parser import DEFAULT_STATEMENT_DPI

        assert DEFAULT_STATEMENT_DPI == 150
        assert statement_parsing_config.dpi == 150

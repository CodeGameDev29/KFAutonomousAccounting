"""Large / complex receipts: page planning, the batch merge rules, budgets.

Sending a 20-page invoice as one 200-DPI vision call is a single enormous
request: it can outlive the per-call timeout and it reports no progress while it
runs. These tests lock in the plan that replaces it:

  * ≤ 4 pages            → one call
  * > 4 pages with text  → the text layer in one call + the page-1 image
  * > 4 pages scanned    → batches of 4, merged by _merge_receipt_batches
  * pages beyond the first four render at 150 DPI
  * pages_total / pages_used are always recorded
  * two batches claiming different grand totals is a flag, never a coin toss

Nothing here touches a network: the AsyncOpenAI client is mocked and page
rendering is stubbed so the assertions are about the DPI and batching decisions
rather than about PyMuPDF.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import config.settings as settings
import core.extraction as ext
from core.extraction import (
    RECEIPT_BATCH_MAX_OUTPUT_TOKENS,
    RECEIPT_BATCH_SIZE,
    RECEIPT_FIRST_PAGES_DPI,
    RECEIPT_MAX_OUTPUT_TOKENS,
    RECEIPT_MAX_VISION_PAGES,
    RECEIPT_OVERFLOW_DPI,
    RECEIPT_SINGLE_CALL_MAX_PAGES,
    ReceiptBatch,
    _build_receipt_plan,
    _merge_receipt_batches,
    build_extraction_prompt,
)

# ---------------------------------------------------------------------------
# Fixtures — real PDFs (cheap), stubbed rendering
# ---------------------------------------------------------------------------


def _blank_pdf(path: Path, pages: int) -> Path:
    """A PDF with `pages` empty pages: no text layer, so density is 0.0."""
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def _text_pdf(path: Path, pages: int) -> Path:
    """A PDF whose every page carries a real text layer (density 1.0)."""
    import fitz

    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page()
        body = (
            f"Northwind Fabrication Ltd. invoice NF-2026-4417 page {number}. "
            "Laser-cut aluminium bracket, powder coat satin black, stainless "
            "fastener set, TIG weld assembly, CNC mill spacer, anodise clear."
        )
        page.insert_text((40, 60), body, fontsize=9)
        page.insert_text((40, 90), body, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def render_recorder(monkeypatch):
    """Replace page rendering with a recorder: (page_numbers, dpi) per call."""
    calls: list[tuple[list[int], int]] = []

    def _fake_render(pdf_path: Path, page_numbers, dpi: int) -> list[bytes]:
        pages = list(page_numbers)
        calls.append((pages, dpi))
        return [f"png-page-{n}".encode() for n in pages]

    monkeypatch.setattr(ext, "_render_pdf_pages", _fake_render)
    return calls


@pytest.fixture()
def local_only(monkeypatch):
    """Local provider configured, both hosted providers keyless."""
    monkeypatch.setattr(
        settings, "local_llm_config",
        replace(settings.local_llm_config, base_url="http://test-local:8000/v1", model="primary"),
    )
    monkeypatch.setattr(
        settings, "gemini_config", replace(settings.gemini_config, api_key="", enabled=False)
    )
    monkeypatch.setattr(
        settings, "openai_config", replace(settings.openai_config, api_key="", enabled=False)
    )


def _mock_client(contents: list[str], finish_reasons: list[str] | None = None):
    """AsyncOpenAI-shaped mock returning `contents` one call at a time."""
    responses = []
    for i, content in enumerate(contents):
        response = MagicMock()
        reason = (finish_reasons or ["stop"] * len(contents))[i]
        response.choices = [MagicMock(message=MagicMock(content=content), finish_reason=reason)]
        response.usage = MagicMock(prompt_tokens=1000, completion_tokens=100)
        responses.append(response)
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=responses)
    return client


def _batch(index: int, first: int, last: int) -> ReceiptBatch:
    return ReceiptBatch(index=index, first_page=first, last_page=last, images=[b"x"])


# ---------------------------------------------------------------------------
# Page planning
# ---------------------------------------------------------------------------

class TestReceiptPlan:
    def test_four_pages_or_fewer_stay_one_call(self, tmp_path):
        plan = _build_receipt_plan(_blank_pdf(tmp_path / "small.pdf", 3))

        assert plan.mode == "single"
        assert plan.batches == []
        assert plan.pages_total == 3
        assert plan.pages_used == 3
        assert len(plan.images) == 3

    def test_the_single_call_boundary_is_four_pages(self, tmp_path, render_recorder):
        assert RECEIPT_SINGLE_CALL_MAX_PAGES == 4
        four = _build_receipt_plan(_blank_pdf(tmp_path / "four.pdf", 4))
        five = _build_receipt_plan(_blank_pdf(tmp_path / "five.pdf", 5))

        assert four.mode == "single"
        assert five.mode == "batched"

    def test_a_long_text_layer_invoice_uses_text_plus_the_first_page_image(self, tmp_path, render_recorder):
        plan = _build_receipt_plan(_text_pdf(tmp_path / "text.pdf", 8))

        assert plan.mode == "text"
        assert plan.pages_total == 8
        assert plan.pages_used == 8, "every page's text must reach the model"
        assert plan.text_density is not None and plan.text_density >= 0.85
        assert "Northwind Fabrication" in (plan.text_content or "")
        assert "PAGE 8 OF 8" in (plan.text_content or "")
        # Exactly one image, and it is page 1 at full DPI — the letterhead.
        assert render_recorder == [([0], RECEIPT_FIRST_PAGES_DPI)]
        assert len(plan.images) == 1

    def test_a_long_scanned_invoice_is_batched_four_pages_at_a_time(self, tmp_path, render_recorder):
        plan = _build_receipt_plan(_blank_pdf(tmp_path / "scanned.pdf", 8))

        assert plan.mode == "batched"
        assert plan.pages_total == 8
        assert plan.pages_used == 8
        assert [(b.first_page, b.last_page) for b in plan.batches] == [(1, 4), (5, 8)]
        assert all(len(b.images) <= RECEIPT_BATCH_SIZE for b in plan.batches)

    def test_pages_beyond_the_first_four_render_at_150_dpi(self, tmp_path, render_recorder):
        _build_receipt_plan(_blank_pdf(tmp_path / "scanned.pdf", 9))

        assert render_recorder == [
            ([0, 1, 2, 3], RECEIPT_FIRST_PAGES_DPI),
            ([4, 5, 6, 7, 8], RECEIPT_OVERFLOW_DPI),
        ]
        assert RECEIPT_OVERFLOW_DPI == 150

    def test_a_ragged_last_batch_keeps_its_real_page_range(self, tmp_path, render_recorder):
        plan = _build_receipt_plan(_blank_pdf(tmp_path / "scanned.pdf", 6))

        assert [(b.first_page, b.last_page) for b in plan.batches] == [(1, 4), (5, 6)]
        assert plan.batches[-1].page_count == 2

    def test_beyond_the_vision_ceiling_pages_are_flagged_not_dropped_silently(
        self, tmp_path, render_recorder, monkeypatch
    ):
        monkeypatch.setattr(ext, "RECEIPT_MAX_VISION_PAGES", 8)
        plan = _build_receipt_plan(_blank_pdf(tmp_path / "huge.pdf", 12))

        assert plan.pages_total == 12
        assert plan.pages_used == 8
        assert plan.truncated is True

        merged = _merge_receipt_batches(
            [(_batch(0, 1, 4), {"vendor": "V", "total": "10.00", "grand_total_page": 4})],
            pages_total=plan.pages_total, pages_used=plan.pages_used,
        )
        assert merged["_confidence"] == "low"
        assert "only the first 8 of 12 pages" in merged["_confidence_reason"]

    def test_a_plain_image_receipt_is_untouched(self, tmp_path):
        path = tmp_path / "receipt.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n not really a png")
        plan = _build_receipt_plan(path)

        assert plan.mode == "single"
        assert plan.pages_total == 1
        assert plan.media_type == "image/png"


# ---------------------------------------------------------------------------
# The batch prompt
# ---------------------------------------------------------------------------

class TestBatchPrompt:
    def test_the_batch_prompt_asks_which_page_carries_the_grand_total(self):
        prompt = build_extraction_prompt(batch=(5, 8), pages_total=8)

        assert "grand_total_page" in prompt
        assert "continuation" in prompt
        assert "PAGES 5-8 OF 8" in prompt
        assert "carried forward" in prompt.lower()
        # Itemising four pages of rows costs more output than a batch call is
        # given, for data nothing downstream reads. See the prompt comment.
        assert "Omit `line_items`" in prompt

    def test_the_plain_prompt_omits_nulls_and_line_items(self):
        prompt = build_extraction_prompt()

        assert "OMIT any key you cannot determine" in prompt
        assert "Omit `line_items` entirely" in prompt
        assert "grand_total_page" not in prompt
        # The fields the rest of the engine reads are still specified.
        for field_name in ("vendor", "date", "subtotal", "total", "currency", "line_items"):
            assert field_name in prompt

    def test_the_text_layer_prompt_explains_the_page_one_image(self):
        prompt = build_extraction_prompt(pages_total=8, text_layer=True)

        assert "text layer" in prompt
        assert "page 1" in prompt
        assert "8 pages" in prompt


# ---------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------

class TestMergeRules:
    def test_identity_comes_from_the_first_non_continuation_batch(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {
                    "vendor": "Northwind Fabrication Ltd.",
                    "date": "2026-08-14",
                    "invoice_number": "NF-2026-4417",
                    "currency": "CAD",
                    "continuation": False,
                }),
                (_batch(1, 5, 8), {
                    "vendor": "Page Five Header Co.",
                    "date": "2026-08-31",
                    "invoice_number": "WRONG-1",
                    "total": "15971.20",
                    "grand_total_page": 8,
                    "continuation": True,
                }),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["vendor"] == "Northwind Fabrication Ltd."
        assert merged["date"] == "2026-08-14"
        assert merged["invoice_number"] == "NF-2026-4417"
        assert merged["total"] == "15971.20"
        assert merged.get("_confidence") != "low"

    def test_a_continuation_first_batch_still_yields_to_a_real_header_later(self):
        """Identity is preferred from a non-continuation batch, whichever it is."""
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"continuation": True, "date": "2026-01-02"}),
                (_batch(1, 5, 8), {
                    "continuation": False,
                    "vendor": "Real Vendor Inc.",
                    "date": "2026-08-14",
                    "total": "50.00",
                    "grand_total_page": 8,
                }),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["vendor"] == "Real Vendor Inc."
        assert merged["date"] == "2026-08-14"

    def test_agreeing_totals_do_not_flag(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "total": "15971.20", "grand_total_page": 4}),
                (_batch(1, 5, 8), {"total": "15971.20", "grand_total_page": 8, "continuation": True}),
            ],
            pages_total=8, pages_used=8,
        )

        assert _safe(merged["total"]) == Decimal("15971.20")
        assert "_confidence" not in merged

    def test_disagreeing_totals_are_flagged_low_and_never_silently_chosen(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "total": "1234.56", "grand_total_page": 4}),
                (_batch(1, 5, 8), {"total": "15971.20", "grand_total_page": 8, "continuation": True}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["_confidence"] == "low"
        reason = merged["_confidence_reason"]
        assert "disagree on the grand total" in reason
        assert "1234.56" in reason and "15971.20" in reason
        assert "pages 1-4" in reason and "pages 5-8" in reason
        # The later page's figure is carried so the document is still usable.
        assert _safe(merged["total"]) == Decimal("15971.20")

    def test_two_pennies_apart_counts_as_agreement(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "total": "100.00", "grand_total_page": 4}),
                (_batch(1, 5, 8), {"total": "100.02", "grand_total_page": 8}),
            ],
            pages_total=8, pages_used=8,
        )

        assert "_confidence" not in merged

    def test_a_page_subtotal_is_not_a_grand_total(self):
        """No batch claims the grand total: usable, but flagged for a human."""
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "date": "2026-08-14", "total": "4000.00"}),
                (_batch(1, 5, 8), {"total": "9000.00", "continuation": True}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["_confidence"] == "low"
        assert "no page batch reported the document's grand total" in merged["_confidence_reason"]
        assert _safe(merged["total"]) == Decimal("9000.00")

    def test_no_amount_anywhere_is_reported_as_such(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V"}),
                (_batch(1, 5, 8), {"continuation": True}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["_confidence"] == "low"
        assert "no page batch reported any amount" in merged["_confidence_reason"]

    def test_taxes_follow_the_batch_that_showed_the_grand_total(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "subtotal": "4000.00"}),
                (_batch(1, 5, 8), {
                    "total": "15971.20", "subtotal": "14260.00",
                    "gst": "713.00", "pst": "998.20",
                    "grand_total_page": 8, "continuation": True,
                }),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["subtotal"] == "14260.00", "not the page-1 running subtotal"
        assert merged["gst"] == "713.00"
        assert merged["pst"] == "998.20"

    def test_line_items_concatenate_in_page_order(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {
                    "vendor": "V",
                    "line_items": [{"description": "a"}, {"description": "b"}],
                }),
                (_batch(1, 5, 8), {
                    "total": "10.00", "grand_total_page": 8, "continuation": True,
                    "line_items": [{"description": "c"}],
                }),
            ],
            pages_total=8, pages_used=8,
        )

        assert [item["description"] for item in merged["line_items"]] == ["a", "b", "c"]

    def test_page_accounting_is_always_recorded(self):
        merged = _merge_receipt_batches(
            [(_batch(0, 1, 4), {"vendor": "V", "total": "10.00", "grand_total_page": 1})],
            pages_total=4, pages_used=4,
        )

        assert merged["pages_total"] == 4
        assert merged["pages_used"] == 4
        assert merged["_receipt_mode"] == "batched"
        assert merged["_page_batches"] == 1

    def test_not_a_financial_document_needs_every_batch_to_say_so(self):
        both = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "NOT_A_FINANCIAL_DOCUMENT", "total": "0"}),
                (_batch(1, 5, 8), {"vendor": "NOT_A_FINANCIAL_DOCUMENT", "total": "0"}),
            ],
            pages_total=8, pages_used=8,
        )
        assert both["vendor"] == "NOT_A_FINANCIAL_DOCUMENT"

        one = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "NOT_A_FINANCIAL_DOCUMENT"}),
                (_batch(1, 5, 8), {"vendor": "Real Co.", "total": "10.00", "grand_total_page": 8}),
            ],
            pages_total=8, pages_used=8,
        )
        assert one["vendor"] == "Real Co."

    def test_a_low_confidence_batch_keeps_the_merged_result_low(self):
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "_confidence": "low"}),
                (_batch(1, 5, 8), {"total": "10.00", "grand_total_page": 8}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["_confidence"] == "low"
        assert "low confidence" in merged["_confidence_reason"]

    def test_telemetry_from_every_batch_survives_the_merge(self):
        attempt_a = ext.ExtractionAttempt(model="primary", confidence=0.9, latency_ms=1000)
        attempt_b = ext.ExtractionAttempt(model="primary", confidence=0.9, latency_ms=2000)
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V", "_telemetry": [attempt_a],
                                   "_provider": "local", "_model": "primary"}),
                (_batch(1, 5, 8), {"total": "10.00", "grand_total_page": 8,
                                   "_telemetry": [attempt_b], "_provider": "local"}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["_telemetry"] == [attempt_a, attempt_b]
        assert merged["_provider"] == "local"
        assert merged["_model"] == "primary"

    def test_a_boolean_grand_total_claim_is_accepted(self):
        """Some answers say has_grand_total instead of naming the page."""
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "V"}),
                (_batch(1, 5, 8), {"total": "77.00", "has_grand_total": True}),
            ],
            pages_total=8, pages_used=8,
        )

        assert _safe(merged["total"]) == Decimal("77.00")
        assert "_confidence" not in merged


def _safe(value) -> Decimal:
    return ext._safe_decimal(value)


# ---------------------------------------------------------------------------
# extract_document end to end (mocked endpoint)
# ---------------------------------------------------------------------------

_BATCH_ONE = json.dumps({
    "vendor": "Northwind Fabrication Ltd.",
    "date": "2026-08-14",
    "invoice_number": "NF-2026-4417",
    "currency": "CAD",
    "continuation": False,
})
_BATCH_TWO = json.dumps({
    "total": "15971.20",
    "subtotal": "14260.00",
    "gst": "713.00",
    "pst": "998.20",
    "grand_total_page": 8,
    "continuation": True,
})
_ONE_CALL = json.dumps({"vendor": "Example Retailer", "date": "2026-01-15", "total": "41.25"})


@pytest.mark.asyncio
class TestExtractDocumentBatching:
    async def test_a_long_scanned_invoice_is_batched_into_one_merged_result(
        self, tmp_path, render_recorder, local_only
    ):
        """Two batches, plus the one re-read that proves the grand total."""
        pdf = _blank_pdf(tmp_path / "scanned.pdf", 8)
        client = _mock_client([_BATCH_ONE, _BATCH_TWO, _BATCH_TWO])
        seen: list[tuple[int, int, str | None]] = []

        result = await ext.extract_document(
            pdf,
            local_client=client,
            progress_cb=lambda done, total, mode=None: seen.append((done, total, mode)),
        )

        # Batch 2 claims the grand total and was rendered at 150 DPI, so it is
        # asked again at 200 before that figure is accepted (rule 1).
        assert client.chat.completions.create.await_count == 3
        assert result["total_verified_at_dpi"] == settings.RECEIPT_RETRY_DPI
        assert result["dpi_retries"] == 1
        assert result["vendor"] == "Northwind Fabrication Ltd."
        assert result["date"] == "2026-08-14"
        assert result["total"] == "15971.20"
        assert result["gst"] == "713.00"
        assert result["pages_total"] == 8
        assert result["pages_used"] == 8
        assert result["_receipt_mode"] == "batched"
        assert result.get("_confidence") != "low"
        # Progress after each batch, never past the end.
        # The first call is the engine announcing *how* it is reading this
        # document, before any page has been sent: batched pages here, so the UI
        # is right to show "page 5 of 8" while it works.
        assert seen == [(0, 8, "paged"), (4, 8, "paged"), (8, 8, "paged")]

    async def test_each_batch_call_carries_the_batch_output_budget(
        self, tmp_path, render_recorder, local_only
    ):
        pdf = _blank_pdf(tmp_path / "scanned.pdf", 8)
        client = _mock_client([_BATCH_ONE, _BATCH_TWO, _BATCH_TWO])

        await ext.extract_document(pdf, local_client=client)

        budgets = [
            call.kwargs["max_tokens"]
            for call in client.chat.completions.create.await_args_list
        ]
        # The grand-total re-read is a batch call like any other, budget included.
        assert budgets == [RECEIPT_BATCH_MAX_OUTPUT_TOKENS] * 3
        assert RECEIPT_BATCH_MAX_OUTPUT_TOKENS == 768

    async def test_a_batch_with_no_total_does_not_burn_a_corrective_retry(
        self, tmp_path, render_recorder, local_only
    ):
        """Pages 1-4 of an invoice legitimately carry no grand total."""
        pdf = _blank_pdf(tmp_path / "scanned.pdf", 8)
        client = _mock_client([_BATCH_ONE, _BATCH_TWO, _BATCH_TWO])

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 3, (
            "a missing total inside one batch must not trigger the schema retry; "
            "the third call is the grand-total re-read, not a corrective one"
        )
        assert result["dpi_retries"] == 1

    async def test_a_small_receipt_is_one_call_with_the_plain_budget(
        self, tmp_path, local_only
    ):
        pdf = _blank_pdf(tmp_path / "small.pdf", 2)
        client = _mock_client([_ONE_CALL])
        seen: list[tuple[int, int, str | None]] = []

        result = await ext.extract_document(
            pdf,
            local_client=client,
            progress_cb=lambda done, total, mode=None: seen.append((done, total, mode)),
        )

        assert client.chat.completions.create.await_count == 1
        call = client.chat.completions.create.await_args
        assert call.kwargs["max_tokens"] == RECEIPT_MAX_OUTPUT_TOKENS == 512
        assert result["vendor"] == "Example Retailer"
        assert result["pages_total"] == 2
        assert result["pages_used"] == 2
        assert result["_receipt_mode"] == "single"
        # One call over both pages: nothing can move a page counter until it
        # answers, and the row says so ("Reading 2 pages in one pass").
        assert seen == [(0, 2, "one_pass"), (2, 2, "one_pass")]

    async def test_the_text_layer_path_sends_the_text_and_the_page_one_image(
        self, tmp_path, render_recorder, local_only
    ):
        pdf = _text_pdf(tmp_path / "text.pdf", 8)
        client = _mock_client([json.dumps({
            "vendor": "Northwind Fabrication Ltd.",
            "date": "2026-08-14",
            "total": "15971.20",
        })])

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 1
        content = client.chat.completions.create.await_args.kwargs["messages"][1]["content"]
        text_part = next(part for part in content if part["type"] == "text")
        image_parts = [part for part in content if part["type"] == "image_url"]
        assert "Northwind Fabrication" in text_part["text"]
        assert len(image_parts) == 1, "page 1 rides along for the letterhead"
        assert result["pages_total"] == 8
        assert result["pages_used"] == 8
        assert result["_receipt_mode"] == "text"

    async def test_a_truncated_answer_is_re_asked_once_with_double_the_budget(
        self, tmp_path, local_only
    ):
        pdf = _blank_pdf(tmp_path / "small.pdf", 1)
        client = _mock_client(
            ['{"vendor": "Example Retailer", "date": "2026-01-15", "tot',  _ONE_CALL],
            finish_reasons=["length", "stop"],
        )

        result = await ext.extract_document(pdf, local_client=client)

        budgets = [
            call.kwargs["max_tokens"]
            for call in client.chat.completions.create.await_args_list
        ]
        assert budgets == [RECEIPT_MAX_OUTPUT_TOKENS, RECEIPT_MAX_OUTPUT_TOKENS * 2]
        assert result["total"] == "41.25"

    async def test_a_pdf_that_renders_no_page_fails_loudly(
        self, tmp_path, monkeypatch, local_only
    ):
        """Better an error than a document row with no vendor and no amount."""
        pdf = _blank_pdf(tmp_path / "corrupt.pdf", 8)
        monkeypatch.setattr(ext, "_render_pdf_pages", lambda *a, **k: [])
        client = _mock_client([_BATCH_ONE])

        with pytest.raises(ValueError, match="no page of this document could be rendered"):
            await ext.extract_document(pdf, local_client=client)
        assert client.chat.completions.create.await_count == 0

    async def test_a_progress_callback_that_raises_cannot_fail_the_extraction(
        self, tmp_path, local_only
    ):
        pdf = _blank_pdf(tmp_path / "small.pdf", 1)
        client = _mock_client([_ONE_CALL])

        def _boom(done, total):
            raise RuntimeError("the queue went away")

        result = await ext.extract_document(pdf, local_client=client, progress_cb=_boom)
        assert result["vendor"] == "Example Retailer"


# ---------------------------------------------------------------------------
# Budgets and concurrency
# ---------------------------------------------------------------------------

class TestBudgetsAndConcurrency:
    def test_receipt_budgets(self):
        assert RECEIPT_MAX_OUTPUT_TOKENS == 512
        assert RECEIPT_BATCH_MAX_OUTPUT_TOKENS == 768

    def test_local_concurrency_default_is_four(self, monkeypatch):
        """Servers that batch concurrent requests gain real throughput at four."""
        import importlib

        monkeypatch.delenv("LOCAL_LLM_MAX_CONCURRENT", raising=False)
        importlib.reload(settings)
        try:
            assert settings.LocalLLMConfig().max_concurrent == 4
        finally:
            monkeypatch.undo()
            importlib.reload(settings)

    def test_env_example_documents_the_concurrency_reasoning(self):
        """The number is useless without the sentence that says why it is 4.

        The phrase is matched over the comment block with its wrapping and its
        leading ``#`` removed, so re-flowing the paragraph does not fail this —
        only deleting the reasoning does.
        """
        import re

        from config.settings import PROJECT_ROOT

        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "LOCAL_LLM_MAX_CONCURRENT=4" in text
        unwrapped = re.sub(r"\s+", " ", re.sub(r"\n#\s*", " ", text))
        assert "batch concurrent requests" in unwrapped
        assert "throughput" in unwrapped

    def test_the_vision_ceiling_is_a_real_number(self):
        assert RECEIPT_MAX_VISION_PAGES >= 20


# ---------------------------------------------------------------------------
# Faint pages: the DPI retry
# ---------------------------------------------------------------------------
#
# Pages 5+ render at 150 DPI because the vision tower downsamples to a fixed
# tile budget. On a faint scan that is not free: pale strokes blend into the
# page white and the batch comes back with no vendor, no amount and no
# continuation marker — it read nothing. These tests lock in that such a batch
# is re-rendered ONCE at RECEIPT_RETRY_DPI and asked again, that a batch which
# read something is never re-rendered, and that the count is honest.


def _scanned_pdf(
    path: Path, pages: int, faint_from: int | None = None, grey: float = 0.86
) -> Path:
    """An image-only PDF (no text layer, so the batched path is chosen).

    Pages from ``faint_from`` on are printed in light grey on white — a
    photocopy whose back pages have faded, which is exactly the document class
    the retry exists for.
    """
    import fitz

    typeset = fitz.open()
    for number in range(1, pages + 1):
        page = typeset.new_page()
        faint = faint_from is not None and number >= faint_from
        colour = (grey, grey, grey) if faint else (0, 0, 0)
        body = (
            f"Northwind Fabrication Ltd. invoice NF-2026-4417 page {number} of "
            f"{pages}. Laser-cut aluminium bracket, powder coat satin black."
        )
        page.insert_text((40, 60), body, fontsize=9, color=colour)
        page.insert_text((40, 90), "TOTAL DUE CAD 15,971.20", fontsize=11, color=colour)

    # Flatten every page to a picture so there is no text layer left at all.
    scan = fitz.open()
    for page in typeset:
        pix = page.get_pixmap(dpi=120)
        out = scan.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, pixmap=pix)
    scan.save(str(path))
    scan.close()
    typeset.close()
    return path


def _png_width(data: bytes) -> int:
    """Pixel width straight out of a PNG's IHDR — no decode, no Pillow."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(data[16:20], "big")


def _content_image_widths(content) -> list[int]:
    """Pixel widths of every image in one chat-completions message body."""
    import base64

    widths: list[int] = []
    for part in content:
        if part.get("type") != "image_url":
            continue
        payload = part["image_url"]["url"].split(",", 1)[1]
        widths.append(_png_width(base64.b64decode(payload)))
    return widths


def _call_image_widths(call) -> list[int]:
    """Pixel widths of the images one recorded chat-completions call carried."""
    return _content_image_widths(call.kwargs["messages"][1]["content"])


_EMPTY_BATCH = json.dumps({})
_FAINT_BATCH_READ = json.dumps({
    "total": "15971.20",
    "subtotal": "14260.00",
    "gst": "713.00",
    "pst": "998.20",
    "grand_total_page": 8,
})


class TestReadNothingDetector:
    def test_a_batch_with_nothing_on_it_reads_as_nothing(self):
        assert ext._batch_read_nothing({}) is True
        assert ext._batch_read_nothing({"vendor": None, "total": "null"}) is True
        assert ext._batch_read_nothing({"currency": "CAD"}) is True

    def test_a_vendor_or_an_amount_is_something(self):
        assert ext._batch_read_nothing({"vendor": "Northwind"}) is False
        assert ext._batch_read_nothing({"total": "12.00"}) is False
        assert ext._batch_read_nothing({"amount": "12.00"}) is False
        assert ext._batch_read_nothing({"subtotal": "12.00"}) is False

    def test_not_a_financial_document_is_a_verdict_not_a_blind_read(self):
        """The model read the page and decided. Re-rendering would change nothing."""
        assert ext._batch_read_nothing({"vendor": "NOT_A_FINANCIAL_DOCUMENT"}) is False

    def test_a_real_mid_table_page_reports_something(self):
        """A page the model can read says more than "this continues"."""
        assert ext._batch_read_nothing({"continuation": True, "subtotal": "7400.00"}) is False
        assert ext._batch_read_nothing({"continuation": True, "vendor": "Northwind"}) is False
        assert ext._batch_read_nothing(
            {"continuation": "true", "invoice_number": "NF-2026-4417"}
        ) is False

    def test_a_bare_continuation_marker_is_not_content(self):
        """A near-white page can come back with nothing but this marker.

        `continuation` is the one question a model can answer without reading a
        word, so a batch whose whole answer is that marker has read nothing —
        and treating the marker as content would put the retry out of reach.
        """
        assert ext._batch_read_nothing({"continuation": True}) is True
        assert ext._batch_read_nothing({"continuation": "true"}) is True

    def test_a_claim_with_no_figure_behind_it_is_not_content(self):
        assert ext._batch_read_nothing({"has_grand_total": True}) is True
        assert ext._batch_read_nothing({"line_items": []}) is True


class TestRetryDpiSettings:
    def test_the_dpi_ladder_lives_in_settings(self):
        assert settings.RECEIPT_BATCH_DPI == 150
        assert settings.RECEIPT_RETRY_DPI == 200
        assert settings.RECEIPT_FIRST_PAGES_DPI == 200
        # The module names the engine uses are the settings values, not copies.
        assert ext.RECEIPT_OVERFLOW_DPI == settings.RECEIPT_BATCH_DPI
        assert ext.RECEIPT_FIRST_PAGES_DPI == settings.RECEIPT_FIRST_PAGES_DPI

    def test_a_plan_records_the_dpi_each_batch_was_rendered_at(self, tmp_path):
        plan = _build_receipt_plan(_scanned_pdf(tmp_path / "scan.pdf", 8, faint_from=5))

        assert [b.dpi for b in plan.batches] == [
            settings.RECEIPT_FIRST_PAGES_DPI,
            settings.RECEIPT_BATCH_DPI,
        ]
        assert all(b.source_path is not None for b in plan.batches)


@pytest.mark.asyncio
class TestFaintPageDpiRetry:
    async def test_a_faint_batch_that_read_nothing_is_re_rendered_sharper(
        self, tmp_path, local_only
    ):
        """The whole point: pages 5-8 vanish at 150 DPI and are asked again at 200."""
        pdf = _scanned_pdf(tmp_path / "faded.pdf", 8, faint_from=5)
        client = _mock_client([_BATCH_ONE, _EMPTY_BATCH, _FAINT_BATCH_READ])

        result = await ext.extract_document(pdf, local_client=client)

        calls = client.chat.completions.create.await_args_list
        assert len(calls) == 3, "two batches plus one re-render of the faint one"
        assert result["dpi_retries"] == 1

        first_read = _call_image_widths(calls[1])
        second_read = _call_image_widths(calls[2])
        assert len(first_read) == len(second_read) == 4, "the same four pages"
        assert all(b > a for a, b in zip(first_read, second_read)), (
            "the retry must send sharper pixels, not the same ones again"
        )
        assert second_read[0] == round(
            first_read[0] * settings.RECEIPT_RETRY_DPI / settings.RECEIPT_BATCH_DPI
        ), "re-rendered at exactly RECEIPT_RETRY_DPI"

        # And the retry's answer is what got merged, so the figures are not lost.
        assert _safe(result["total"]) == Decimal("15971.20")
        assert result["vendor"] == "Northwind Fabrication Ltd."
        assert result["pages_used"] == 8

    async def test_a_batch_that_read_something_is_never_re_rendered(
        self, tmp_path, local_only
    ):
        """A 5-page scan whose total is on page 3 — read at 200 DPI already.

        Nothing here read nothing, and nothing here read a total at reduced
        resolution, so neither re-render rule has anything to do: two batches,
        two calls.
        """
        pdf = _scanned_pdf(tmp_path / "clean.pdf", 5)
        client = _mock_client([
            json.dumps({
                "vendor": "Northwind Fabrication Ltd.",
                "date": "2026-08-14",
                "total": "15971.20",
                "grand_total_page": 3,
            }),
            json.dumps({"continuation": True, "subtotal": "7400.00"}),
        ])

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 2
        assert result["dpi_retries"] == 0
        assert result["total_verified_at_dpi"] == settings.RECEIPT_FIRST_PAGES_DPI
        assert _safe(result["total"]) == Decimal("15971.20")
        assert result.get("_confidence") != "low"

    async def test_a_readable_mid_table_page_is_not_re_rendered(
        self, tmp_path, local_only
    ):
        """No grand total on a page that reports a running subtotal is normal."""
        pdf = _scanned_pdf(tmp_path / "long.pdf", 8)
        client = _mock_client([
            _BATCH_ONE, json.dumps({"continuation": True, "subtotal": "7400.00"}),
        ])

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 2
        assert result["dpi_retries"] == 0

    async def test_pages_already_at_the_retry_dpi_are_not_asked_twice(
        self, tmp_path, local_only
    ):
        """Batch 1 renders at 200 DPI already — re-rendering it changes nothing."""
        pdf = _scanned_pdf(tmp_path / "blank-front.pdf", 5)
        client = _mock_client([_EMPTY_BATCH, _EMPTY_BATCH, _FAINT_BATCH_READ])

        result = await ext.extract_document(pdf, local_client=client)

        calls = client.chat.completions.create.await_args_list
        assert len(calls) == 3, "batch 1 not re-asked; batch 2 (150 DPI) re-asked once"
        assert len(_call_image_widths(calls[0])) == 4
        assert len(_call_image_widths(calls[1])) == 1
        assert _call_image_widths(calls[2]) == [
            round(
                _call_image_widths(calls[1])[0]
                * settings.RECEIPT_RETRY_DPI
                / settings.RECEIPT_BATCH_DPI
            )
        ]
        assert result["dpi_retries"] == 1

    async def test_a_page_that_is_genuinely_blank_costs_one_call_and_says_so(
        self, tmp_path, local_only
    ):
        pdf = _scanned_pdf(tmp_path / "blank-back.pdf", 8)
        client = _mock_client([_BATCH_ONE, _EMPTY_BATCH, _EMPTY_BATCH])

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 3, "re-asked once, not twice"
        assert result["dpi_retries"] == 1
        # Nothing was invented to fill the hole: no amount anywhere is flagged.
        assert result["_confidence"] == "low"
        assert "no page batch reported any amount" in result["_confidence_reason"]


# ---------------------------------------------------------------------------
# The grand total is never trusted at reduced resolution
# ---------------------------------------------------------------------------
#
# Reading *nothing* at 150 DPI is the obvious failure and the retry above covers
# it. Reading the *wrong number* is the dangerous one: a slightly darker grey
# scan comes back with a confident total that is not the total on the page, and
# nothing downstream can tell it from a good read. So a batch that claims the
# document's grand total, rendered below RECEIPT_RETRY_DPI, is re-rendered and
# asked again before its figure is accepted (rule 1), and two batches that
# disagree on the total are both re-read before the disagreement is reported
# (rule 2). Both rules share a budget of RECEIPT_TOTAL_VERIFY_MAX_CALLS calls.
#
# The fixtures here are low-contrast PDFs — the pages are genuinely rendered at
# 150 and at 200 DPI — and the model is mocked to answer differently depending
# on which rendering it was shown, which is what a grey scan provokes.

_TRUE_TOTAL = Decimal("15971.20")
_MISREAD_TOTAL = Decimal("14971.20")


def _grey_total_pdf(
    path: Path,
    pages: int = 8,
    *,
    faint_from: int = 5,
    grey: float = 0.78,
) -> Path:
    """A scanned invoice whose grand total is printed on the LAST page, in grey.

    Pages 1..faint_from-1 are black; from ``faint_from`` on they are printed in
    grey on white — the photocopy-of-a-photocopy whose back pages faded. Every
    page but the last shows a carried-forward figure (which is not the grand
    total); only the last page prints "TOTAL DUE".

    Flattened to images, so there is no text layer and the batched vision path is
    the one under test. ``grey=0.78`` sits in the dangerous band: pale enough to
    lose a digit at 150 DPI, dark enough that the batch still reads something and
    so never reaches the read-nothing retry.
    """
    import fitz

    typeset = fitz.open()
    running = 0.0
    for number in range(1, pages + 1):
        page = typeset.new_page()
        faint = number >= faint_from
        colour = (grey, grey, grey) if faint else (0.0, 0.0, 0.0)
        page.insert_text(
            (40, 60),
            f"Northwind Fabrication Ltd. invoice NF-2026-4417 page {number} of {pages}.",
            fontsize=9, color=colour,
        )
        y = 90
        for row in range(6):
            amount = 37.50 + row * 11.25 + number
            running += amount
            page.insert_text(
                (40, y),
                f"Laser-cut 3mm aluminium bracket (lot {100 + number * 6 + row})"
                f"          {amount:,.2f}",
                fontsize=9, color=colour,
            )
            y += 22
        if number < pages:
            page.insert_text((40, y + 10), f"Carried forward: {running:,.2f}",
                             fontsize=9, color=colour)
            page.insert_text((40, y + 30),
                             "Continued on next page — this is not the invoice total.",
                             fontsize=8, color=colour)
        else:
            page.insert_text((40, y + 10), "Subtotal: 14,260.00", fontsize=10, color=colour)
            page.insert_text((40, y + 28), "GST (5%): 713.00", fontsize=10, color=colour)
            page.insert_text((40, y + 46), "RST (7%): 998.20", fontsize=10, color=colour)
            page.insert_text((40, y + 70), f"TOTAL DUE (CAD): ${_TRUE_TOTAL:,.2f}",
                             fontsize=12, color=colour)

    scan = fitz.open()
    for page in typeset:
        pix = page.get_pixmap(dpi=120)
        out = scan.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, pixmap=pix)
    scan.save(str(path))
    scan.close()
    typeset.close()
    return path


def _rendered_dpi(pdf: Path, width_px: int) -> int:
    """Which rung of the DPI ladder a rendered page of ``pdf`` came from."""
    import fitz

    doc = fitz.open(str(pdf))
    try:
        inches = doc.load_page(0).rect.width / 72.0
    finally:
        doc.close()
    ladder = (settings.RECEIPT_BATCH_DPI, settings.RECEIPT_RETRY_DPI)
    return min(ladder, key=lambda dpi: abs(width_px - inches * dpi))


def _dpi_aware_client(pdf: Path, answers: dict) -> AsyncMock:
    """A model that answers according to the pages AND the DPI it was shown.

    ``answers`` is keyed by ``(first_page, last_page, dpi)``, falling back to
    ``(first_page, last_page)`` when the answer is the same at any resolution.
    An unmapped request raises rather than silently repeating an earlier answer —
    the point of these tests is *which* rendering was asked.
    """
    seen: list[tuple[tuple[int, int] | None, int | None]] = []

    def _dispatch(*_args, **kwargs):
        messages = kwargs["messages"]
        match = re.search(r"YOU ARE READING PAGES (\d+)-(\d+)", messages[0]["content"])
        pages = (int(match.group(1)), int(match.group(2))) if match else None
        widths = _content_image_widths(messages[1]["content"])
        dpi = _rendered_dpi(pdf, widths[0]) if widths else None
        seen.append((pages, dpi))
        if pages is not None and (*pages, dpi) in answers:
            content = answers[(*pages, dpi)]
        else:
            content = answers[pages]
        response = MagicMock()
        response.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason="stop")
        ]
        response.usage = MagicMock(prompt_tokens=1000, completion_tokens=100)
        return response

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=_dispatch)
    client.calls_seen = seen
    return client


_IDENTITY_ONLY = json.dumps({
    "vendor": "Northwind Fabrication Ltd.",
    "date": "2026-08-14",
    "invoice_number": "NF-2026-4417",
    "currency": "CAD",
    "continuation": False,
})


def _total_answer(total: Decimal, page: int, **extra) -> str:
    body = {"total": f"{total}", "grand_total_page": page, "continuation": True}
    body.update(extra)
    return json.dumps(body)


class TestReVerifyBudgetSettings:
    def test_the_re_read_budget_is_two_calls_and_sits_beside_the_dpi_ladder(self):
        assert ext.RECEIPT_TOTAL_VERIFY_MAX_CALLS == 2
        assert ext.RECEIPT_TOTAL_AGREEMENT_TOLERANCE == Decimal("0.02")


@pytest.mark.asyncio
class TestGrandTotalReVerification:
    async def test_a_wrong_total_read_at_150_dpi_is_corrected_at_200(
        self, tmp_path, local_only
    ):
        """Rule 1, the whole point: the sharper reading of page 8 wins."""
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_MISREAD_TOTAL, 8),
            (5, 8, settings.RECEIPT_RETRY_DPI): _total_answer(_TRUE_TOTAL, 8),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert _safe(result["total"]) == _TRUE_TOTAL, "the 200 DPI reading is the one taken"
        assert result["total_verified_at_dpi"] == settings.RECEIPT_RETRY_DPI
        assert result["dpi_retries"] == 1
        calls = client.chat.completions.create.await_args_list
        assert len(calls) == 3
        assert client.calls_seen == [
            ((1, 4), settings.RECEIPT_FIRST_PAGES_DPI),
            ((5, 8), settings.RECEIPT_BATCH_DPI),
            ((5, 8), settings.RECEIPT_RETRY_DPI),
        ]
        assert all(b > a for a, b in zip(
            _call_image_widths(calls[1]), _call_image_widths(calls[2])
        )), "the re-read must send sharper pixels"

    async def test_two_readings_that_differ_name_both_figures_and_both_dpis(
        self, tmp_path, local_only
    ):
        """A human has to see that the pixels changed the number."""
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_MISREAD_TOTAL, 8),
            (5, 8, settings.RECEIPT_RETRY_DPI): _total_answer(_TRUE_TOTAL, 8),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert result["_confidence"] == "low"
        reason = result["_confidence_reason"]
        assert str(_MISREAD_TOTAL) in reason and str(_TRUE_TOTAL) in reason
        assert str(settings.RECEIPT_BATCH_DPI) in reason
        assert str(settings.RECEIPT_RETRY_DPI) in reason
        assert "pages 5-8" in reason

    async def test_a_re_read_that_agrees_is_accepted_with_no_flag(
        self, tmp_path, local_only
    ):
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8): _total_answer(_TRUE_TOTAL, 8),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert _safe(result["total"]) == _TRUE_TOTAL
        assert result["total_verified_at_dpi"] == settings.RECEIPT_RETRY_DPI
        assert result["dpi_retries"] == 1
        assert result.get("_confidence") != "low", result.get("_confidence_reason")

    async def test_two_pennies_apart_is_the_same_reading_twice(self, tmp_path, local_only):
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_TRUE_TOTAL, 8),
            (5, 8, settings.RECEIPT_RETRY_DPI): _total_answer(
                _TRUE_TOTAL - Decimal("0.02"), 8
            ),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert result.get("_confidence") != "low", result.get("_confidence_reason")
        assert _safe(result["total"]) == _TRUE_TOTAL - Decimal("0.02")

    async def test_every_call_a_re_read_spends_stays_in_the_telemetry(
        self, tmp_path, local_only
    ):
        """extraction_log has to show the call that was spent, not just the winner."""
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_MISREAD_TOTAL, 8),
            (5, 8, settings.RECEIPT_RETRY_DPI): _total_answer(_TRUE_TOTAL, 8),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert len(result["_telemetry"]) == client.chat.completions.create.await_count == 3

    async def test_a_document_no_batch_claimed_a_total_for_is_not_called_verified(
        self, tmp_path, local_only
    ):
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8): json.dumps({"continuation": True, "subtotal": "7400.00"}),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert "total_verified_at_dpi" not in result
        assert result["dpi_retries"] == 0
        assert client.chat.completions.create.await_count == 2

    async def test_two_batches_that_disagree_are_re_read_and_then_accepted(
        self, tmp_path, local_only
    ):
        """Rule 2: a disagreement the sharper reading settles is not a flag."""
        pdf = _grey_total_pdf(tmp_path / "grey-12.pdf", 12, faint_from=5)
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            # Pages 5-8 mistake a carried-forward figure for the grand total…
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_MISREAD_TOTAL, 8),
            # …and at 200 DPI read it for what it is, claiming no total.
            (5, 8, settings.RECEIPT_RETRY_DPI): json.dumps(
                {"continuation": True, "subtotal": "7400.00"}
            ),
            (9, 12, settings.RECEIPT_BATCH_DPI): _total_answer(_TRUE_TOTAL, 12),
            (9, 12, settings.RECEIPT_RETRY_DPI): _total_answer(_TRUE_TOTAL, 12),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 5, "3 batches + 2 re-reads"
        assert result["dpi_retries"] == 2
        assert _safe(result["total"]) == _TRUE_TOTAL
        assert result["total_verified_at_dpi"] == settings.RECEIPT_RETRY_DPI
        assert result.get("_confidence") != "low", result.get("_confidence_reason")

    async def test_a_disagreement_that_survives_the_re_read_stays_low(
        self, tmp_path, local_only
    ):
        pdf = _grey_total_pdf(tmp_path / "grey-12.pdf", 12, faint_from=5)
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8): _total_answer(_MISREAD_TOTAL, 8),
            (9, 12): _total_answer(_TRUE_TOTAL, 12),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert result["dpi_retries"] == 2, "both claimants re-read before flagging"
        assert result["_confidence"] == "low"
        reason = result["_confidence_reason"]
        assert "disagree on the grand total" in reason
        assert str(_MISREAD_TOTAL) in reason and str(_TRUE_TOTAL) in reason
        assert f"{settings.RECEIPT_RETRY_DPI} DPI did not resolve" in reason
        # Still usable: the later page's figure is carried, never invented.
        assert _safe(result["total"]) == _TRUE_TOTAL

    async def test_the_re_read_budget_stops_at_two_calls_per_document(
        self, tmp_path, local_only
    ):
        """Three claimants disagreeing cannot spend three extra calls."""
        pdf = _grey_total_pdf(tmp_path / "grey-16.pdf", 16, faint_from=5)
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8): _total_answer(Decimal("17000.00"), 8),
            (9, 12): _total_answer(Decimal("18000.00"), 12),
            (13, 16): _total_answer(_TRUE_TOTAL, 16),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert result["dpi_retries"] == ext.RECEIPT_TOTAL_VERIFY_MAX_CALLS == 2
        assert client.chat.completions.create.await_count == 6, "4 batches + 2 re-reads"
        assert result["_confidence"] == "low"
        assert "budget (2 calls) ran out" in result["_confidence_reason"]

    async def test_a_total_that_could_not_be_re_read_says_so(
        self, tmp_path, monkeypatch, local_only
    ):
        """A re-render PyMuPDF refuses leaves the figure unverified, not silent."""
        pdf = _grey_total_pdf(tmp_path / "grey-total.pdf")
        real_render = ext._render_pdf_pages
        seen: list[int] = []

        def _render(pdf_path, page_numbers, dpi):
            seen.append(dpi)
            if dpi == settings.RECEIPT_RETRY_DPI and seen.count(dpi) > 1:
                raise RuntimeError("mupdf: cannot rasterise")
            return real_render(pdf_path, page_numbers, dpi)

        monkeypatch.setattr(ext, "_render_pdf_pages", _render)
        client = _dpi_aware_client(pdf, {
            (1, 4): _IDENTITY_ONLY,
            (5, 8, settings.RECEIPT_BATCH_DPI): _total_answer(_MISREAD_TOTAL, 8),
        })

        result = await ext.extract_document(pdf, local_client=client)

        assert client.chat.completions.create.await_count == 2
        assert result["dpi_retries"] == 0, "a render that never happened is not a call"
        assert result["total_verified_at_dpi"] == settings.RECEIPT_BATCH_DPI
        assert result["_confidence"] == "low"
        assert "could not be re-read" in result["_confidence_reason"]


# ---------------------------------------------------------------------------
# The currency marker: what the page prints, not what the model concludes
# ---------------------------------------------------------------------------

def _usd_text_pdf(path: Path, pages: int = 6) -> Path:
    """A machine-generated invoice whose text layer prints "USD" beside figures."""
    import fitz

    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((40, 60), (
            f"Norwood Systems Inc. invoice NS-20841 page {number}. "
            "Engineering consulting, machine time, tooling, freight, handling."
        ), fontsize=9)
        page.insert_text((40, 90), (
            "Line total USD 3,000.00   Sales tax USD 150.00   "
            "Amount due USD 3,150.00"
        ), fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


@pytest.mark.asyncio
class TestTheCurrencyMarkerFromTheTextLayer:
    """A long invoice reaches the model as text, and the text is the better witness.

    The model still transcribes; but where the document's own characters are on
    hand, a currency printed beside a figure settles the question without asking
    anyone. See core/document_currency.py.
    """

    async def test_a_usd_text_layer_states_usd_even_when_the_model_says_nothing(
        self, tmp_path, render_recorder, local_only
    ):
        pdf = _usd_text_pdf(tmp_path / "long-usd.pdf")
        client = _mock_client([json.dumps({
            "vendor": "Norwood Systems Inc.", "date": "2026-08-06",
            "subtotal": "3000.00", "other_tax": "150.00", "total": "3150.00",
        })])

        result = await ext.extract_document(pdf, local_client=client)

        assert result["_receipt_mode"] == "text"
        assert result["currency_marker"] == "USD"
        assert result["_currency_marker_from"] == "text_layer"
        from core.document_currency import currency_from_extraction
        assert currency_from_extraction(result) == ("USD", "stated")

    async def test_the_model_quoting_a_marker_is_left_alone(
        self, tmp_path, render_recorder, local_only
    ):
        """What the model read off the page outranks a scan of the text."""
        pdf = _usd_text_pdf(tmp_path / "long-usd.pdf")
        client = _mock_client([json.dumps({
            "vendor": "Norwood Systems Inc.", "date": "2026-08-06",
            "currency_marker": "CA$", "currency": "CAD", "total": "3150.00",
        })])

        result = await ext.extract_document(pdf, local_client=client)

        assert result["currency_marker"] == "CA$"
        assert "_currency_marker_from" not in result

    async def test_a_bare_dollar_text_layer_adopts_nothing(
        self, tmp_path, render_recorder, local_only
    ):
        """"$1,200.00" on every page is not evidence of anything."""
        import fitz

        pdf = tmp_path / "long-bare.pdf"
        doc = fitz.open()
        for number in range(1, 6):
            page = doc.new_page()
            page.insert_text((40, 60), (
                f"Harbour Supply Co. statement page {number}. Freight, pallets, "
                "shrink wrap, strapping, corner board, labels, delivery."
            ), fontsize=9)
            page.insert_text((40, 90), "Amount due $1,200.00", fontsize=9)
        doc.save(str(pdf))
        doc.close()

        client = _mock_client([json.dumps({
            "vendor": "Harbour Supply Co.", "currency_marker": "$",
            "total": "1200.00",
        })])

        result = await ext.extract_document(pdf, local_client=client)

        assert result["currency_marker"] == "$"
        assert "_currency_marker_from" not in result


class TestTheMergeCarriesTheMarker:
    def test_the_merge_carries_the_marker_off_the_page_that_printed_it(self):
        """Page 1 shows no currency text; the total page prints "US$"."""
        merged = _merge_receipt_batches(
            [
                (_batch(0, 1, 4), {"vendor": "Norwood Systems Inc.",
                                   "currency_marker": ""}),
                (_batch(1, 5, 8), {"currency_marker": "US$", "currency": "USD",
                                   "total": "3150.00", "grand_total_page": 8}),
            ],
            pages_total=8, pages_used=8,
        )

        assert merged["currency_marker"] == "US$"
        from core.document_currency import currency_from_extraction
        assert currency_from_extraction(merged) == ("USD", "stated")

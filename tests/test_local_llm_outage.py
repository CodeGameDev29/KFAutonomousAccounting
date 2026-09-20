"""An unreachable local model must say so, everywhere a user can feel it.

Everything here is synthetic: the endpoint is a loopback URL nothing listens on
(``http://127.0.0.1:9999/v1``), the model is called ``primary``, and every
exception is constructed by hand. No socket is opened by any test in this file.

The engine's primary — and, with both hosted keys empty, only — extraction
provider is a local OpenAI-compatible server (``LLM_BASE_URL``). When it is off,
the honest answer is 503 "the local extraction model is not reachable, nothing
was extracted" — not 500 "unexpected", not 504 "your file may be too complex",
not 422 "this statement could not be read", and never a silent fall-through to a
hosted provider.

The three layers that have to agree on that are pinned below: the detector in
``core/llm_errors.py``, the HTTP classifier the upload endpoints call, and the
three engine paths that reach the model (receipt extraction, statement parsing,
the synchronous agent ladder).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

import config.settings as settings
from core.llm_errors import (
    LOCAL_LLM_UNREACHABLE_CODE,
    LOCAL_LLM_UNREACHABLE_MESSAGE,
    LocalLLMUnreachableError,
    is_local_endpoint_down,
)
from server.api._extraction_errors import classify_extraction_error

#: Where the invented configuration points its model. Nothing listens there,
#: and nothing in this file ever tries: every client is a mock.
LOCAL_BASE_URL = "http://127.0.0.1:9999/v1"
LOCAL_MODEL = "primary"


# ---------------------------------------------------------------------------
# Helpers — the exception shapes the SDKs raise
# ---------------------------------------------------------------------------

def _request() -> httpx.Request:
    return httpx.Request("POST", f"{LOCAL_BASE_URL}/chat/completions")


def _api_connection_error() -> APIConnectionError:
    """What the openai client raises when the server refuses the socket."""
    return APIConnectionError(request=_request())


def _api_timeout_error() -> APITimeoutError:
    """openai's timeout — a subclass of APIConnectionError."""
    return APITimeoutError(request=_request())


def _httpx_connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("All connection attempts failed", request=_request())


def _refused() -> ConnectionRefusedError:
    return ConnectionRefusedError(
        10061, "No connection could be made because the target machine actively refused it"
    )


_DOWN_CASES = {
    "openai_api_connection": _api_connection_error,
    "openai_api_timeout": _api_timeout_error,
    "httpx_connect": _httpx_connect_error,
    "connection_refused": _refused,
    "already_labelled": LocalLLMUnreachableError,
}


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

class TestDetector:
    @pytest.mark.parametrize("name", sorted(_DOWN_CASES))
    def test_every_outage_shape_is_recognised(self, name):
        assert is_local_endpoint_down(_DOWN_CASES[name]()) is True

    def test_dns_failure_on_the_endpoint_hostname_counts(self):
        """A host name in LLM_BASE_URL that no longer resolves is an outage."""
        import socket

        assert is_local_endpoint_down(socket.gaierror(11001, "getaddrinfo failed")) is True

    def test_a_wrapped_cause_is_found(self):
        """openai wraps httpx; several call sites re-raise `from exc`."""
        outer = RuntimeError("extraction failed")
        outer.__cause__ = _httpx_connect_error()
        assert is_local_endpoint_down(outer) is True

    def test_a_text_only_connection_failure_counts(self):
        """Layers that re-raise a plain RuntimeError keep only the sentence, so
        the detector has to read it — that is why _CONNECTION_PHRASES exists."""
        assert is_local_endpoint_down(RuntimeError("connection refused")) is True

    def test_things_that_are_not_an_outage(self):
        """A slow model, a bad file and a rate-limit refusal are not outages."""
        assert is_local_endpoint_down(asyncio.TimeoutError()) is False
        assert is_local_endpoint_down(ValueError("File format .docx is not supported")) is False
        assert is_local_endpoint_down(RuntimeError("429 rate limited")) is False
        assert is_local_endpoint_down(RuntimeError("LLM provider unavailable")) is False
        assert is_local_endpoint_down(httpx.ReadTimeout("read timed out", request=_request())) is False
        assert is_local_endpoint_down(None) is False


# ---------------------------------------------------------------------------
# classify_extraction_error
# ---------------------------------------------------------------------------

class TestClassifyExtractionError:
    @pytest.mark.parametrize("name", sorted(_DOWN_CASES))
    def test_outage_is_503_with_the_honest_message(self, name):
        status, code, message = classify_extraction_error(
            _DOWN_CASES[name](), "receipt.pdf"
        )
        assert status == 503
        assert code == LOCAL_LLM_UNREACHABLE_CODE == "LOCAL_MODEL_UNREACHABLE"
        assert message == LOCAL_LLM_UNREACHABLE_MESSAGE
        # It names the thing that is down and says nothing was extracted.
        assert "local extraction model" in message
        assert "Nothing was extracted" in message
        # A server the reader can restart is not something to report to anyone.
        assert "contact support" not in message

    def test_outage_wins_over_the_timeout_rung(self):
        """APITimeoutError says "timed out" but means "never connected"."""
        status, code, _ = classify_extraction_error(_api_timeout_error(), "receipt.pdf")
        assert (status, code) == (503, LOCAL_LLM_UNREACHABLE_CODE)

    # ── Existing classifications must not move ──────────────────────────

    def test_unsupported_format_still_400(self):
        assert classify_extraction_error(
            ValueError("File type .docx is not supported"), "f.docx"
        )[:2] == (400, "UNSUPPORTED_FORMAT")

    def test_not_a_financial_document_still_422(self):
        assert classify_extraction_error(
            ValueError("NOT_A_FINANCIAL_DOCUMENT"), "cat.png"
        )[:2] == (422, "NOT_FINANCIAL_DOCUMENT")

    def test_corrupted_file_still_422(self):
        assert classify_extraction_error(
            RuntimeError("fitz: cannot open broken document"), "f.pdf"
        )[:2] == (422, "FILE_CORRUPTED")

    def test_slow_model_still_504(self):
        assert classify_extraction_error(asyncio.TimeoutError(), "f.pdf")[:2] == (504, "TIMEOUT")

    def test_rate_limit_still_503_rate_limited(self):
        assert classify_extraction_error(
            RuntimeError("429 rate limited"), "f.pdf"
        )[:2] == (503, "RATE_LIMITED")

    def test_unknown_failure_still_500(self):
        status, code, message = classify_extraction_error(
            RuntimeError("LLM provider unavailable"), "f.pdf"
        )
        assert (status, code) == (500, "EXTRACTION_ERROR")
        assert "server log" in message


# ---------------------------------------------------------------------------
# Receipt extraction (core/extraction.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_only(monkeypatch):
    """The common deployment shape: a local model configured, no hosted keys."""
    monkeypatch.setattr(
        settings, "local_llm_config",
        replace(settings.local_llm_config, base_url=LOCAL_BASE_URL, model=LOCAL_MODEL),
    )
    monkeypatch.setattr(
        settings, "gemini_config",
        replace(settings.gemini_config, api_key="", enabled=False),
    )
    monkeypatch.setattr(
        settings, "openai_config",
        replace(settings.openai_config, api_key="", enabled=False),
    )


@pytest.fixture()
def receipt_png(tmp_path: Path) -> Path:
    """A .png so extraction skips the PyMuPDF render path."""
    path = tmp_path / "outage_probe.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
    return path


def _dead_client() -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=_api_connection_error())
    return client


@pytest.mark.asyncio
class TestExtractDocumentReportsTheOutage:
    async def test_raises_the_labelled_error(self, local_only, receipt_png):
        from core.extraction import extract_document

        with pytest.raises(LocalLLMUnreachableError) as excinfo:
            await extract_document(receipt_png, local_client=_dead_client())

        assert str(excinfo.value) == LOCAL_LLM_UNREACHABLE_MESSAGE
        # The transport error is preserved for the log, not for the user.
        assert isinstance(excinfo.value.__cause__, APIConnectionError)

    async def test_that_error_classifies_as_503(self, local_only, receipt_png):
        """End to end on the classification the upload endpoint actually calls."""
        from core.extraction import extract_document

        try:
            await extract_document(receipt_png, local_client=_dead_client())
        except Exception as exc:
            status, code, message = classify_extraction_error(exc, "outage_probe.png")
        assert (status, code, message) == (
            503, LOCAL_LLM_UNREACHABLE_CODE, LOCAL_LLM_UNREACHABLE_MESSAGE,
        )

    async def test_no_paid_provider_is_reached(self, local_only, receipt_png, monkeypatch):
        """Keyless hosted providers stay untouched — an outage is not a licence to spend."""
        import core.extraction as ext

        async def never(*args, **kwargs):
            raise AssertionError("a hosted provider was called during a local outage")

        monkeypatch.setattr(ext, "_try_gemini_candidates", never)
        monkeypatch.setattr(ext, "_extract_with_openai", never)

        with pytest.raises(LocalLLMUnreachableError):
            await ext.extract_document(receipt_png, local_client=_dead_client())

    async def test_a_keyed_fallback_still_answers(self, local_only, receipt_png, monkeypatch):
        """The outage label must not break a fallback that IS configured."""
        import core.extraction as ext

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )

        async def gemini_ok(*args, **kwargs):
            return {
                "vendor": "Cedarview Supplies Ltd",
                "_provider": "gemini",
                "_model": "gemini-3.6-flash",
            }

        monkeypatch.setattr(ext, "_try_gemini_candidates", gemini_ok)

        result = await ext.extract_document(receipt_png, local_client=_dead_client())
        assert result["_provider"] == "gemini"


# ---------------------------------------------------------------------------
# Statement parsing (core/llm_statement_parser.py)
# ---------------------------------------------------------------------------
#
# One invented January page for Example Corp Inc.: opening 1,000.00, a single
# 41.25 debit, closing 958.75. The arithmetic is here so the fixture reads as a
# statement rather than as three unrelated numbers.

_STATEMENT_JSON = json.dumps({
    "metadata": {
        "institution": "BMO",
        "account_type": "CHECKING",
        "account_number": "0000",
        "statement_period_start": "2026-01-01",
        "statement_period_end": "2026-01-31",
        "opening_balance": "1000.00",
        "closing_balance": "958.75",
        "currency": "CAD",
        "multi_account": False,
        "page_count": 1,
    },
    "transactions": [
        {
            "date": "2026-01-05",
            "amount": "41.25",
            "description": "SAMPLE STREET CAFE",
            "type": "debit",
        },
    ],
})


@pytest.mark.asyncio
class TestStatementParseReportsTheOutage:
    @pytest.fixture()
    def statement_pdf(self, tmp_path: Path) -> Path:
        path = tmp_path / "statement.pdf"
        path.write_bytes(b"%PDF-1.4 not really a pdf")
        return path

    @pytest.fixture(autouse=True)
    def _text_mode(self, monkeypatch):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

    async def test_raises_the_statement_flavour_of_the_outage_error(
        self, local_only, statement_pdf, monkeypatch
    ):
        import core.llm_statement_parser as parser

        async def local_down(*args, **kwargs):
            raise _api_connection_error()

        monkeypatch.setattr(parser, "_extract_statement_with_local", local_down)

        with pytest.raises(parser.StatementLocalEndpointDown) as excinfo:
            await parser.parse_statement_with_llm(
                statement_pdf, "user-example", "statement.pdf"
            )

        # Still a StatementParseError, so every existing handler keeps working.
        assert isinstance(excinfo.value, parser.StatementParseError)
        assert isinstance(excinfo.value, LocalLLMUnreachableError)
        assert str(excinfo.value) == LOCAL_LLM_UNREACHABLE_MESSAGE
        assert is_local_endpoint_down(excinfo.value) is True

    async def test_a_readable_statement_error_stays_generic(
        self, local_only, statement_pdf, monkeypatch
    ):
        """A model that answered badly is not an outage."""
        import core.llm_statement_parser as parser

        async def local_garbage(*args, **kwargs):
            raise ValueError("Invalid JSON: expecting value at line 1")

        monkeypatch.setattr(parser, "_extract_statement_with_local", local_garbage)

        with pytest.raises(parser.StatementParseError) as excinfo:
            await parser.parse_statement_with_llm(
                statement_pdf, "user-example", "statement.pdf"
            )

        assert not isinstance(excinfo.value, parser.StatementLocalEndpointDown)
        assert "All LLM paths failed" in str(excinfo.value)

    async def test_a_keyed_fallback_still_parses(self, local_only, statement_pdf, monkeypatch):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )

        async def local_down(*args, **kwargs):
            raise _api_connection_error()

        async def gemini_ok(text, prompt, db, model_name):
            return parser.BankStatementExtraction(**json.loads(_STATEMENT_JSON)), 100, 20, 0.001

        monkeypatch.setattr(parser, "_extract_statement_with_local", local_down)
        monkeypatch.setattr(parser, "_extract_statement_with_gemini_text", gemini_ok)

        result = await parser.parse_statement_with_llm(
            statement_pdf, "user-example", "statement.pdf"
        )
        assert result.parse_method == "gemini_text"


# ---------------------------------------------------------------------------
# The synchronous ladder (core/llm_sync.py) — categorization, matcher, linker
# ---------------------------------------------------------------------------

class TestSyncLadderReportsTheOutage:
    def test_outage_is_raised_with_the_user_facing_sentence(self, local_only, monkeypatch):
        from core import llm_sync

        def local_down(prompt, max_tokens=None):
            raise _api_connection_error()

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)

        with pytest.raises(LocalLLMUnreachableError) as excinfo:
            llm_sync.call_json("hi", label="match_validator")

        assert str(excinfo.value) == LOCAL_LLM_UNREACHABLE_MESSAGE
        assert isinstance(excinfo.value.__cause__, APIConnectionError)

    def test_reconciliation_agents_survive_the_outage(self, local_only, monkeypatch):
        """A reconcile run must degrade to deterministic scoring, not traceback."""
        from core import llm_sync
        from core.reconciliation import _call_llm_for_reconciliation

        def local_down(prompt, max_tokens=None):
            raise _api_connection_error()

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)

        with pytest.raises(LocalLLMUnreachableError):
            _call_llm_for_reconciliation("prompt")

    def test_date_validator_batch_degrades_to_the_heuristic(self, local_only, monkeypatch):
        """The engine's own guard: an LLM outage never fails a reconcile run.

        The invented pair is a 41.25 cafe receipt dated 2026-01-05 against the
        bank debit that cleared 15 days later — a gap the heuristic has to rule
        on by itself once the model cannot be asked.
        """
        from datetime import date

        from core import llm_sync
        from core.date_validator import validate_date_gaps

        def local_down(prompt, max_tokens=None):
            raise _api_connection_error()

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)

        out = validate_date_gaps([{
            "match_index": 0,
            "txn_date": date(2026, 1, 20),
            "txn_amount": "41.25",
            "txn_currency": "CAD",
            "txn_description": "SAMPLE STREET CAFE",
            "txn_account": "CAD",
            "doc_vendor": "Sample Street Cafe",
            "doc_date": date(2026, 1, 5),
            "doc_total": "41.25",
            "doc_currency": "CAD",
            "doc_filename": "20260105-sample-street-cafe.pdf",
            "gap_days": 15,
        }])
        assert len(out) == 1
        assert out[0]["verdict"] in ("ACCEPT", "REJECT")

    def test_match_validator_propagates_the_outage(self, local_only, monkeypatch):
        """The shared entry every sync agent routes through."""
        from core import llm_sync
        from core.match_validator import _call_llm

        def local_down(prompt, max_tokens=None):
            raise _api_connection_error()

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)

        with pytest.raises(LocalLLMUnreachableError):
            _call_llm("prompt")

    def test_a_real_model_error_is_not_relabelled(self, local_only, monkeypatch):
        from core import llm_sync

        def local_garbage(prompt, max_tokens=None):
            raise ValueError("Expecting value: line 1 column 1")

        monkeypatch.setattr(llm_sync, "call_local_json", local_garbage)

        with pytest.raises(ValueError):
            llm_sync.call_json("hi")

    def test_no_provider_configured_still_says_so(self, monkeypatch):
        from core import llm_sync

        monkeypatch.setattr(
            settings, "local_llm_config",
            replace(settings.local_llm_config, base_url=""),
        )
        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="", enabled=False),
        )
        monkeypatch.setattr(
            settings, "openai_config",
            replace(settings.openai_config, api_key="", enabled=False),
        )
        with pytest.raises(RuntimeError, match="no LLM provider configured"):
            llm_sync.call_json("hi")

    def test_a_keyed_fallback_still_answers(self, local_only, monkeypatch):
        from core import llm_sync

        monkeypatch.setattr(
            settings, "openai_config",
            replace(settings.openai_config, api_key="key", enabled=True),
        )

        def local_down(prompt, max_tokens=None):
            raise _api_connection_error()

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)
        monkeypatch.setattr(
            llm_sync, "call_openai_json", lambda prompt, max_tokens=None: {"ok": True}
        )

        assert llm_sync.call_json("hi")["ok"] is True


# ---------------------------------------------------------------------------
# The sentence itself
# ---------------------------------------------------------------------------

def test_the_outage_message_is_written_once():
    """One sentence, one definition — the API, the parser and the ladder share it."""
    assert LOCAL_LLM_UNREACHABLE_MESSAGE == (
        "The local extraction model is not reachable. Nothing was extracted; "
        "try again once it is back."
    )


def test_the_message_names_no_particular_machine():
    """It describes the *role*, not one deployment's hardware.

    Every deployment of this code points ``LLM_BASE_URL`` at its own server,
    under its own name, on its own network — and this sentence is shown to whoever
    uploaded the file. A host name, an address or a nickname in it would be
    meaningless to that reader and would leak where the model happens to run.
    """
    lowered = LOCAL_LLM_UNREACHABLE_MESSAGE.lower()
    assert "http" not in lowered
    assert "192.168" not in lowered
    assert "localhost" not in lowered
    assert "127.0.0.1" not in lowered

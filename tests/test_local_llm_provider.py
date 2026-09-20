"""The local model is the engine's PRIMARY provider.

A deployment may well have no hosted keys at all, so these tests lock in the
case that has to work on its own: with only `LOCAL_LLM_BASE_URL` set, an upload
reaches the local endpoint, nothing tries to construct a hosted client, and a
deployment with nothing configured at all gets a clear error instead of a 401
traceback.

Nothing here touches a network — the AsyncOpenAI client is mocked. The
`_local_llm_disabled_by_default` fixture in conftest.py keeps the local provider
off everywhere else, so each test below turns it on deliberately.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import config.settings as settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extraction_json(vendor: str = "Jordan Avery Consulting") -> str:
    return json.dumps({
        "vendor": vendor,
        "date": "2026-02-01",
        "currency": "USD",
        "subtotal": None,
        "gst": None,
        "hst": None,
        "pst": None,
        "other_tax": None,
        "total": "3000.00",
        "amount": None,
        "payment_method": None,
        "invoice_number": "INV-2026-001",
        "purpose": "consulting",
        "line_items": None,
    })


def _mock_chat_client(content: str, tokens_in: int = 1200, tokens_out: int = 90):
    """An AsyncOpenAI-shaped mock that returns `content` with usage numbers."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    response.usage = MagicMock(prompt_tokens=tokens_in, completion_tokens=tokens_out)
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


@pytest.fixture()
def local_enabled(monkeypatch):
    """Turn the local provider on, pointed at a URL nothing will actually call."""
    cfg = replace(
        settings.local_llm_config,
        base_url="http://127.0.0.1:9999/v1",
        model="primary",
    )
    monkeypatch.setattr(settings, "local_llm_config", cfg)
    return cfg


@pytest.fixture()
def hosted_keys_empty(monkeypatch):
    """Both hosted providers keyless: the local-only configuration."""
    monkeypatch.setattr(
        settings, "gemini_config", replace(settings.gemini_config, api_key="", enabled=False)
    )
    monkeypatch.setattr(
        settings, "openai_config", replace(settings.openai_config, api_key="", enabled=False)
    )


@pytest.fixture()
def receipt_png(tmp_path: Path) -> Path:
    """A .png so extraction skips the PyMuPDF render path."""
    path = tmp_path / "legacy_probe.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
    return path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestLocalLLMConfig:
    def test_enabled_is_just_a_base_url(self):
        cfg = settings.LocalLLMConfig()
        assert replace(cfg, base_url="http://x:1/v1").enabled is True
        assert replace(cfg, base_url="").enabled is False

    def test_api_key_is_never_empty(self, monkeypatch):
        """The openai client refuses to construct without a key."""
        import importlib

        monkeypatch.setenv("LOCAL_LLM_API_KEY", "")
        importlib.reload(settings)
        try:
            assert settings.local_llm_config.api_key == "local"
        finally:
            monkeypatch.undo()
            importlib.reload(settings)

    def test_thinking_disabled_sends_both_vendor_knobs(self):
        """Some servers honour chat_template_kwargs; others honour `think`."""
        cfg = replace(settings.LocalLLMConfig(), disable_thinking=True)
        assert cfg.extra_body == {
            "chat_template_kwargs": {"enable_thinking": False},
            "think": False,
        }
        assert replace(cfg, disable_thinking=False).extra_body == {}

    def test_ladder_puts_local_first_and_skips_keyless_hosted(
        self, local_enabled, hosted_keys_empty
    ):
        assert settings.extraction_provider_ladder() == ["local"]

    def test_ladder_order_when_everything_is_configured(self, local_enabled, monkeypatch):
        monkeypatch.setattr(
            settings, "gemini_config", replace(settings.gemini_config, api_key="k", enabled=True)
        )
        monkeypatch.setattr(
            settings, "openai_config", replace(settings.openai_config, api_key="k", enabled=True)
        )
        assert settings.extraction_provider_ladder() == ["local", "gemini", "openai"]

    def test_local_model_is_trusted_for_auto_accept(self, local_enabled, hosted_keys_empty):
        """Its output is auto-accepted on the same terms as a pinned hosted model."""
        trusted = settings.trusted_extraction_models()
        assert trusted[0] == "primary"
        # The fallback rungs further down the ladder stay untrusted.
        assert "gemini-3.5-flash" not in trusted
        assert "gemini-2.5-flash" not in trusted


# ---------------------------------------------------------------------------
# Receipt extraction routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExtractDocumentRoutesToLocal:
    async def test_local_is_used_when_hosted_keys_are_empty(
        self, local_enabled, hosted_keys_empty, receipt_png
    ):
        from core.extraction import extract_document

        client = _mock_chat_client(_extraction_json())
        result = await extract_document(receipt_png, local_client=client)

        assert result["_provider"] == "local"
        assert result["_model"] == "primary"
        assert result["vendor"] == "Jordan Avery Consulting"
        client.chat.completions.create.assert_called_once()
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "primary"
        assert kwargs["temperature"] == 0
        assert kwargs["response_format"] == {"type": "json_object"}
        # Thinking off for both server flavours.
        assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert kwargs["extra_body"]["think"] is False

    async def test_local_runs_before_gemini(self, local_enabled, monkeypatch, receipt_png):
        """Gemini keeps its key but must not be reached while local answers."""
        import core.extraction as ext

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )

        gemini_calls: list[str] = []

        async def never(*args, **kwargs):
            gemini_calls.append("gemini")
            raise AssertionError("Gemini must not run while local answers")

        monkeypatch.setattr(ext, "_try_gemini_candidates", never)

        client = _mock_chat_client(_extraction_json())
        result = await ext.extract_document(receipt_png, local_client=client)

        assert result["_provider"] == "local"
        assert gemini_calls == []

    async def test_local_failure_falls_through_to_gemini(
        self, local_enabled, monkeypatch, receipt_png
    ):
        import core.extraction as ext

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )

        async def gemini_ok(*args, **kwargs):
            return {"vendor": "From Gemini", "_provider": "gemini", "_model": "gemini-3.6-flash"}

        monkeypatch.setattr(ext, "_try_gemini_candidates", gemini_ok)

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        result = await ext.extract_document(receipt_png, local_client=client)
        assert result["_provider"] == "gemini"

    async def test_zero_cost_usage_is_still_logged(
        self, local_enabled, hosted_keys_empty, receipt_png
    ):
        """Free does not mean invisible — usage lands in the API-call ledger."""
        from core.extraction import extract_document

        logged: list[tuple] = []

        class _Db:
            def log_api_call(self, kind, model, tin, tout, cost):
                logged.append((kind, model, tin, tout, cost))

        client = _mock_chat_client(_extraction_json(), tokens_in=1500, tokens_out=120)
        result = await extract_document(receipt_png, local_client=client, db=_Db())

        assert logged == [("extraction", "primary", 1500, 120, 0.0)]
        assert result["_telemetry"][-1].cost_usd == 0.0
        assert result["_telemetry"][-1].tokens_in == 1500

    async def test_no_provider_configured_raises_a_clear_error(
        self, hosted_keys_empty, monkeypatch, receipt_png
    ):
        from core.extraction import extract_document

        monkeypatch.setattr(
            settings, "local_llm_config",
            replace(settings.local_llm_config, base_url=""),
        )

        with pytest.raises(RuntimeError, match="no extraction provider configured"):
            await extract_document(receipt_png)


# ---------------------------------------------------------------------------
# Statement parsing routing
# ---------------------------------------------------------------------------

_STATEMENT_JSON = json.dumps({
    "metadata": {
        "institution": "BMO",
        "account_type": "CHECKING",
        "account_number": "1234",
        "statement_period_start": "2026-01-01",
        "statement_period_end": "2026-01-31",
        "opening_balance": "1000.00",
        "closing_balance": "900.00",
        "currency": "CAD",
        "multi_account": False,
        "page_count": 1,
    },
    "transactions": [
        {"date": "2026-01-05", "amount": "50.00", "description": "COFFEE SHOP", "type": "debit"},
        {"date": "2026-01-10", "amount": "30.00", "description": "PARKING", "type": "debit"},
        {"date": "2026-01-20", "amount": "20.00", "description": "BOOKSTORE", "type": "debit"},
    ],
})


@pytest.mark.asyncio
class TestStatementParserOrder:
    @pytest.fixture()
    def statement_pdf(self, tmp_path: Path) -> Path:
        path = tmp_path / "statement.pdf"
        path.write_bytes(b"%PDF-1.4 not really a pdf")
        return path

    async def test_local_text_mode_answers_first(
        self, local_enabled, hosted_keys_empty, monkeypatch, statement_pdf
    ):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

        client = _mock_chat_client(_STATEMENT_JSON)
        monkeypatch.setattr(
            parser, "_extract_statement_with_local",
            lambda text, images, prompt, db, client=client, **_kw: parser._statement_via_chat_completions(
                text, images, prompt, client, db,
                model="primary", provider="local", timeout_s=5.0,
                input_cost_per_token=0.0, output_cost_per_token=0.0,
                create_kwargs={"response_format": {"type": "json_object"}, "temperature": 0},
            ),
        )

        result = await parser.parse_statement_with_llm(
            statement_pdf, "user-1", "statement.pdf"
        )
        assert result.parse_method == "local_text"
        assert len(result.transactions) == 3
        assert result.llm_cost_usd == 0.0

    async def test_gemini_is_not_touched_while_local_answers(
        self, local_enabled, monkeypatch, statement_pdf
    ):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )
        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

        async def never(*args, **kwargs):
            raise AssertionError("Gemini must not run while local answers")

        monkeypatch.setattr(parser, "_extract_statement_with_gemini_text", never)

        async def local_ok(text_content, images, prompt, db, client=None, **_kw):
            extraction = parser.BankStatementExtraction(**json.loads(_STATEMENT_JSON))
            return extraction, 100, 20, 0.0

        monkeypatch.setattr(parser, "_extract_statement_with_local", local_ok)

        result = await parser.parse_statement_with_llm(
            statement_pdf, "user-1", "statement.pdf"
        )
        assert result.parse_method == "local_text"

    async def test_local_failure_falls_through_to_gemini(
        self, local_enabled, monkeypatch, statement_pdf
    ):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )
        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

        async def local_down(*args, **kwargs):
            raise RuntimeError("connection refused")

        async def gemini_ok(text, prompt, db, model_name):
            extraction = parser.BankStatementExtraction(**json.loads(_STATEMENT_JSON))
            return extraction, 100, 20, 0.001

        monkeypatch.setattr(parser, "_extract_statement_with_local", local_down)
        monkeypatch.setattr(parser, "_extract_statement_with_gemini_text", gemini_ok)

        result = await parser.parse_statement_with_llm(
            statement_pdf, "user-1", "statement.pdf"
        )
        assert result.parse_method == "gemini_text"

    async def test_no_provider_configured_raises_a_clear_error(
        self, hosted_keys_empty, monkeypatch, statement_pdf
    ):
        import core.llm_statement_parser as parser

        monkeypatch.setattr(
            settings, "local_llm_config",
            replace(settings.local_llm_config, base_url=""),
        )
        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

        with pytest.raises(parser.StatementParseError, match="no statement parsing provider"):
            await parser.parse_statement_with_llm(statement_pdf, "user-1", "statement.pdf")

    async def test_parse_method_accepts_the_local_values(self):
        """The UI and the statements table both persist this string."""
        import core.llm_statement_parser as parser

        extraction = parser.BankStatementExtraction(**json.loads(_STATEMENT_JSON))
        for method in ("local_text", "local_vision"):
            res = parser.StatementParseResult(
                transactions=extraction.transactions,
                metadata=extraction.metadata,
                confidence="HIGH",
                flags=[],
                parse_method=method,
                balance_matches=True,
                balance_difference="0.00",
            )
            assert res.parse_method == method


# ---------------------------------------------------------------------------
# Synchronous agents (matcher, linker, date validator, categorization agent)
# ---------------------------------------------------------------------------

class TestSyncLadder:
    def test_local_first(self, local_enabled, hosted_keys_empty, monkeypatch):
        from core import llm_sync

        calls: list[str] = []

        def fake_local(prompt, max_tokens=None):
            calls.append("local")
            return {"ok": True}

        monkeypatch.setattr(llm_sync, "call_local_json", fake_local)
        assert llm_sync.call_json("hi")["ok"] is True
        assert calls == ["local"]

    def test_falls_through_to_gemini_then_openai(self, local_enabled, monkeypatch):
        from core import llm_sync

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="key", enabled=True),
        )
        monkeypatch.setattr(
            settings, "openai_config",
            replace(settings.openai_config, api_key="key", enabled=True),
        )

        order: list[str] = []

        def local_down(prompt, max_tokens=None):
            order.append("local")
            raise RuntimeError("connection refused")

        def gemini_down(prompt):
            order.append("gemini")
            raise RuntimeError("429 rate limited")

        def openai_ok(prompt, max_tokens=None):
            order.append("openai")
            return {"ok": True}

        monkeypatch.setattr(llm_sync, "call_local_json", local_down)
        monkeypatch.setattr(llm_sync, "call_gemini_json", gemini_down)
        monkeypatch.setattr(llm_sync, "call_openai_json", openai_ok)

        assert llm_sync.call_json("hi")["ok"] is True
        assert order == ["local", "gemini", "openai"]

    def test_no_provider_configured_raises_rather_than_401(self, hosted_keys_empty, monkeypatch):
        from core import llm_sync

        monkeypatch.setattr(
            settings, "local_llm_config",
            replace(settings.local_llm_config, base_url=""),
        )
        with pytest.raises(RuntimeError, match="no LLM provider configured"):
            llm_sync.call_json("hi")

    def test_every_sync_agent_uses_the_shared_ladder(self, local_enabled, hosted_keys_empty, monkeypatch):
        """match_validator, the linker and the date validator share one ladder."""
        from core import llm_sync
        from core.llm_transaction_linker import _call_llm_for_linking
        from core.match_validator import _call_llm

        seen: list[str] = []
        monkeypatch.setattr(
            llm_sync, "call_local_json",
            lambda prompt, max_tokens=None: (seen.append("local"), {"verdict": "ACCEPT"})[1],
        )
        assert _call_llm("p")["verdict"] == "ACCEPT"
        assert _call_llm_for_linking("p")["verdict"] == "ACCEPT"
        assert seen == ["local", "local"]


class TestParseMethodListsAgree:
    """The parse_method vocabulary lives in three places. They must not drift.

    The Pydantic pattern, the database CHECK constraint and the frontend label
    map are three separate copies of one list. A value missing from the
    constraint turns a correctly parsed statement into a failed INSERT on
    `statements_parse_method_check`; one missing from the label map renders to
    the user as a raw identifier.
    """

    _EXPECTED = {
        "local_text", "local_vision",
        "gemini_text", "gemini_vision",
        "openai_fallback", "legacy_pdf_to_csv",
    }

    def _schema_values(self) -> set[str]:
        import re

        from config.settings import PROJECT_ROOT

        sql = (PROJECT_ROOT / "db" / "schema_pg.sql").read_text(encoding="utf-8")
        m = re.search(r"parse_method\s+TEXT CHECK \(parse_method IN \(([^)]*)\)\)", sql)
        assert m, "statements.parse_method CHECK constraint not found in schema_pg.sql"
        return set(re.findall(r"'([^']+)'", m.group(1)))

    def test_pydantic_pattern_matches_the_schema_constraint(self):
        from core.llm_statement_parser import StatementParseResult

        pattern = StatementParseResult.model_fields["parse_method"].metadata[0].pattern
        pydantic_values = set(re.findall(r"[a-z_]+", pattern.replace("^(", "").replace(")$", "")))
        assert pydantic_values == self._EXPECTED
        assert self._schema_values() == self._EXPECTED

    def test_frontend_labels_cover_every_value(self):
        """An unlabelled parse_method renders as a raw identifier to the user."""
        from config.settings import PROJECT_ROOT

        labels = (PROJECT_ROOT / "web" / "src" / "lib" / "display-labels.ts").read_text(
            encoding="utf-8"
        )
        block = labels.split("PARSE_METHOD_LABELS", 1)[1].split("};", 1)[0]
        for value in self._EXPECTED:
            assert f"{value}:" in block, f"PARSE_METHOD_LABELS omits {value}"

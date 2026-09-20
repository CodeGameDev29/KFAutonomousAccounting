"""Getting JSON back out of a local model, and a failure nobody can miss.

Every reply in this file is **invented**. The scenario they all describe is the
same synthetic one: a ``Cedarview Supplies Ltd`` receipt for CAD 104.55 is being
matched against three candidate transactions (ids 41, 57 and 88), and a
shortlist agent is asked which of them could be it.

The failure they cover is a token ceiling against a model that writes a
paragraph of ``"reasoning"`` before it closes the object, so the JSON arrives cut
off — either mid-string (*"Unterminated string starting at ..."*) or right after
a comma (*"Expecting property name enclosed in double quotes ..."*, which is what
a cut at the end of the text looks like to ``json``). A reply that does not parse
takes an agent down to its deterministic fallback, and the whole point of the
tally at the bottom of this module is that such a downgrade can never be silent.

The shapes below are those two truncations, plus the other things a local model
wraps its JSON in: code fences, a ``<think>`` block, a lead-in sentence, a
one-element list. Every test asserts the recovery, and the last groups assert
that a run can *report* a fallback instead of only logging one.

Nothing here touches a network: the OpenAI client is mocked, and conftest blanks
the local endpoint for every test that does not turn it on itself.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import config.settings as settings
from core.llm_sync import (
    LLMJsonError,
    call_local_json,
    extract_json_object,
    llm_call_stats,
    llm_call_stats_snapshot,
    parse_json_response,
    reset_llm_call_stats,
)

# ── The invented replies ──────────────────────────────────────────────────

# A single-line object whose "reasoning" value never ends because the budget ran
# out mid-sentence — json reports it as "Unterminated string starting at".
TRUNCATED_IN_STRING = (
    '{"shortlist": [41, 57], "reasoning": "The proof total is CAD 104.55. '
    "Candidate 41 is CAD 104.55 on the same day, which is an exact match. "
    "Candidate 57 is CAD 105.40, within a dollar of it, so it is plausible "
    "too. Candidate 88 is CAD 1234.56, which cannot"
)

# A pretty-printed object cut off just after a comma, before the next property
# name — json reports it as "Expecting property name enclosed in double quotes".
TRUNCATED_AFTER_COMMA = (
    '{\n  "verdict": "ACCEPT",\n  "max_acceptable_days": 21,\n'
    '  "reasoning": "Telecom invoices post after the debit clears",\n  '
)

FENCED = '```json\n{"shortlist": [41], "reasoning": "exact amount, same day"}\n```'
THINKING_THEN_JSON = (
    "<think>The receipt total is CAD 104.55, so candidate 41 is the only one "
    'that could match.</think>\n{"shortlist": [41], "reasoning": "exact"}'
)
PROSE_AROUND_JSON = (
    'Here is the result:\n{"shortlist": [41], "reasoning": "exact"}\n'
    "Let me know if you would like the other candidates ruled out."
)
LIST_WRAPPED = '[{"shortlist": [41], "reasoning": "exact"}]'
NESTED_BRACES_IN_STRING = (
    '{"reasoning": "the model wrote {braces} and \\"quotes\\" inside", '
    '"shortlist": [23]}'
)


class TestExtractJsonObject:
    def test_plain_object(self):
        assert json.loads(extract_json_object('{"a": 1}')) == {"a": 1}

    def test_code_fences_are_stripped(self):
        assert json.loads(extract_json_object(FENCED))["shortlist"] == [41]

    def test_thinking_block_is_dropped(self):
        assert json.loads(extract_json_object(THINKING_THEN_JSON))["shortlist"] == [41]

    def test_prose_before_and_after_is_dropped(self):
        assert json.loads(extract_json_object(PROSE_AROUND_JSON))["shortlist"] == [41]

    def test_braces_and_quotes_inside_a_string_do_not_end_the_object(self):
        """The brace counter tracks quoting, or prose about braces truncates the
        object one character into the reasoning."""
        data = json.loads(extract_json_object(NESTED_BRACES_IN_STRING))
        assert data["shortlist"] == [23]
        assert "{braces}" in data["reasoning"]

    def test_truncated_object_is_reported_as_truncated(self):
        """A cut after a comma is recoverable (retry with more room), so it has to
        be told apart from a reply that was simply not JSON."""
        with pytest.raises(LLMJsonError) as exc:
            extract_json_object(TRUNCATED_AFTER_COMMA)
        assert exc.value.truncated is True
        assert "never closed" in str(exc.value)

    def test_truncated_string_is_reported_as_truncated(self):
        with pytest.raises(LLMJsonError) as exc:
            extract_json_object(TRUNCATED_IN_STRING)
        assert exc.value.truncated is True

    def test_no_json_at_all(self):
        with pytest.raises(LLMJsonError):
            extract_json_object("I cannot answer that.")

    def test_empty_reply(self):
        with pytest.raises(LLMJsonError):
            extract_json_object("")


class TestParseJsonResponse:
    def test_single_element_list_is_unwrapped(self):
        assert parse_json_response(LIST_WRAPPED)["shortlist"] == [41]

    def test_multi_element_list_is_a_shape_error(self):
        with pytest.raises(LLMJsonError) as exc:
            parse_json_response('[{"a": 1}, {"b": 2}]')
        assert "expected a JSON object" in str(exc.value)

    def test_finish_reason_length_marks_it_truncated(self):
        """The provider already knows why it stopped; the parser should say so."""
        with pytest.raises(LLMJsonError) as exc:
            parse_json_response('{"a": 1', finish_reason="length")
        assert exc.value.truncated is True

    def test_raw_text_is_carried_on_the_error_for_the_log(self):
        with pytest.raises(LLMJsonError) as exc:
            parse_json_response(TRUNCATED_IN_STRING, finish_reason="length")
        assert "The proof total is CAD 104.55" in exc.value.raw


# ── The retry, against a mocked local endpoint ────────────────────────────

@pytest.fixture()
def local_enabled(monkeypatch):
    """Point the local provider at a loopback URL nothing will actually call."""
    cfg = replace(
        settings.local_llm_config,
        base_url="http://127.0.0.1:9999/v1",
        model="primary",
    )
    monkeypatch.setattr(settings, "local_llm_config", cfg)
    return cfg


def _client_returning(*replies: tuple[str, str]):
    """A sync OpenAI-shaped mock yielding (content, finish_reason) in order."""
    calls: list[dict] = []

    def _create(**kwargs):
        calls.append(kwargs)
        content, finish_reason = replies[min(len(calls) - 1, len(replies) - 1)]
        choice = MagicMock(finish_reason=finish_reason)
        choice.message = MagicMock(content=content)
        return MagicMock(choices=[choice])

    client = MagicMock()
    client.chat.completions.create = _create
    return client, calls


class TestLocalRetry:
    def test_truncated_reply_is_retried_with_a_bigger_budget(
        self, local_enabled, monkeypatch,
    ):
        """The instruction alone cannot make room — the budget has to grow too."""
        good = '{"shortlist": [41], "reasoning": "exact amount on the same day"}'
        client, calls = _client_returning(
            (TRUNCATED_IN_STRING, "length"), (good, "stop"),
        )
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)
        reset_llm_call_stats()

        result = call_local_json("shortlist these", max_tokens=300)

        assert result["shortlist"] == [41]
        assert len(calls) == 2
        assert calls[1]["max_tokens"] > calls[0]["max_tokens"]
        assert "not valid JSON" in calls[1]["messages"][0]["content"]
        assert llm_call_stats_snapshot()["retried"] == 1

    def test_json_object_response_format_and_temperature_zero(
        self, local_enabled, monkeypatch,
    ):
        """Classification and scoring, not prose: ask for an object, decide at 0."""
        client, calls = _client_returning(('{"ok": true}', "stop"))
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)

        call_local_json("go")

        assert calls[0]["response_format"] == {"type": "json_object"}
        assert calls[0]["temperature"] == 0

    def test_second_failure_raises_and_logs_the_raw_text(
        self, local_enabled, monkeypatch, caplog,
    ):
        """One corrective retry, then give up — with the reply in the log, so a
        failure can be diagnosed without reproducing the run that caused it."""
        client, calls = _client_returning(
            (TRUNCATED_IN_STRING, "length"), (TRUNCATED_AFTER_COMMA, "length"),
        )
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)

        with caplog.at_level("WARNING"):
            with pytest.raises(LLMJsonError):
                call_local_json("shortlist these", max_tokens=300)

        assert len(calls) == 2
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "still not JSON after retry" in logged
        assert "max_acceptable_days" in logged  # the raw reply is in the log

    def test_fenced_reply_needs_no_retry(self, local_enabled, monkeypatch):
        """Fences are a formatting habit, not a failure — no second round trip."""
        client, calls = _client_returning((FENCED, "stop"))
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)

        assert call_local_json("go")["shortlist"] == [41]
        assert len(calls) == 1


# ── The tally the run summary reports ─────────────────────────────────────

class TestFallbackTally:
    def test_no_provider_configured_counts_as_a_fallback(self, monkeypatch):
        from core.llm_sync import call_json

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
        reset_llm_call_stats()

        with pytest.raises(RuntimeError):
            call_json("anything", label="amount_agent")

        stats = llm_call_stats_snapshot()
        assert stats["calls"] == 1
        assert stats["fallbacks"] == 1
        assert stats["by_label"] == {"amount_agent": 1}

    def test_a_parse_failure_is_tallied_under_the_calling_agent(
        self, local_enabled, monkeypatch,
    ):
        """The label names the calling agent, not the plumbing underneath it, or
        every failure is reported against whichever module made the request."""
        from core.llm_sync import call_json

        monkeypatch.setattr(
            settings, "gemini_config",
            replace(settings.gemini_config, api_key="", enabled=False),
        )
        monkeypatch.setattr(
            settings, "openai_config",
            replace(settings.openai_config, api_key="", enabled=False),
        )
        client, _ = _client_returning(
            (TRUNCATED_IN_STRING, "length"), (TRUNCATED_IN_STRING, "length"),
        )
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)
        reset_llm_call_stats()

        with pytest.raises(LLMJsonError):
            call_json("shortlist", max_tokens=300, label="date_agent")

        stats = llm_call_stats_snapshot()
        assert stats["fallbacks"] == 1
        assert stats["by_label"] == {"date_agent": 1}
        assert stats["ok"] == 0

    def test_a_good_call_is_tallied_as_ok(self, local_enabled, monkeypatch):
        from core.llm_sync import call_json

        client, _ = _client_returning(('{"ok": true}', "stop"))
        monkeypatch.setattr("openai.OpenAI", lambda **kw: client)
        reset_llm_call_stats()

        assert call_json("go", label="final_agent") == {"ok": True}
        stats = llm_call_stats_snapshot()
        assert (stats["ok"], stats["fallbacks"]) == (1, 0)

    def test_reset_clears_the_tally_between_runs(self):
        """A run reports its own numbers, not the ones before it."""
        llm_call_stats.record_call()
        llm_call_stats.record_fallback("amount_agent", "boom")
        reset_llm_call_stats()
        assert llm_call_stats_snapshot() == {
            "calls": 0, "ok": 0, "retried": 0, "fallbacks": 0,
            "by_label": {}, "reasons": {},
        }


class TestEngineBudget:
    def test_engine_calls_do_not_use_a_paragraph_sized_ceiling(self):
        """A shortlist reply that explains itself is far longer than a bare
        verdict; the ceiling has to clear that with room, or the truncation
        retry above becomes the normal path rather than the exception."""
        from core.match_validator import ENGINE_JSON_MAX_TOKENS

        assert ENGINE_JSON_MAX_TOKENS >= 1024

    def test_reconciliation_run_carries_the_tally_out(self):
        """The tally has to leave the engine, or the API cannot report it."""
        from core.reconciliation import MultiPassResult

        assert MultiPassResult().llm_stats == {}

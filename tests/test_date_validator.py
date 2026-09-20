"""core/date_validator.py — the LLM path, and the heuristic behind it.

When a transaction and its document are further apart in time than the engine
is comfortable with, a model is asked whether that gap is plausible for this
kind of payment. Three properties have to hold together, and the third can mask
the first two: a template that cannot be formatted raises before the call is
ever made, and the module then answers from `_heuristic_fallback` while logging
only "Date validation failed… using heuristic" — a silent downgrade that looks
exactly like a healthy fallback.

So these tests pin all three separately: the prompt formats in both gap
directions with no placeholder left behind, a reachable model's verdict is the
answer that is used, and an unreachable or unusable model still yields a
verdict instead of an exception — with an unformattable template logged at
ERROR so it cannot hide inside the fallback path.

Nothing here opens a socket — `core.llm_sync.call_json` is replaced per test.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest
from openai import APIConnectionError

from core import date_validator
from core.date_validator import (
    DATE_GAP_VALIDATION_THRESHOLD,
    build_validation_prompt,
    validate_date_gaps,
)


def _ctx(**overrides) -> dict:
    """A match context: a $50 coffee receipt cleared 15 days later."""
    ctx = {
        "match_index": 0,
        "txn_date": date(2026, 1, 20),
        "txn_amount": "50.00",
        "txn_currency": "CAD",
        "txn_description": "COFFEE SHOP ANYTOWN MB",
        "txn_account": "CAD",
        "doc_vendor": "Coffee Shop",
        "doc_date": date(2026, 1, 5),
        "doc_total": "50.00",
        "doc_currency": "CAD",
        "doc_filename": "receipt.pdf",
        "gap_days": 15,
    }
    ctx.update(overrides)
    return ctx


def _connection_error() -> APIConnectionError:
    """What the openai client raises when the local LLM server refuses the socket."""
    return APIConnectionError(
        request=httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    )


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

class TestThePromptFormats:
    def test_transaction_after_the_document(self):
        prompt = build_validation_prompt(_ctx())

        assert "transaction is after the document" in prompt
        assert "DATE GAP: 15 days" in prompt
        assert "COFFEE SHOP ANYTOWN MB" in prompt
        assert "2026-01-05" in prompt and "2026-01-20" in prompt

    def test_transaction_before_the_document(self):
        prompt = build_validation_prompt(
            _ctx(txn_date=date(2026, 1, 5), doc_date=date(2026, 1, 20))
        )

        assert "transaction is before the document" in prompt

    def test_no_placeholder_survives(self):
        """Every `{field}` is filled; the JSON example's `{{ }}` braces are not."""
        prompt = build_validation_prompt(_ctx())

        assert "{txn_date}" not in prompt
        assert "{direction}" not in prompt
        assert "txn_after" not in prompt
        assert '{"max_acceptable_days"' in prompt  # escaped braces, still literal JSON

    def test_threshold_is_unchanged(self):
        assert DATE_GAP_VALIDATION_THRESHOLD == 7


# ---------------------------------------------------------------------------
# The LLM path
# ---------------------------------------------------------------------------

class TestTheModelDecides:
    def test_a_plausible_gap_verdict_comes_from_the_model(self, monkeypatch):
        """The model's answer, not the heuristic's 14-day default, is returned."""
        seen: list[str] = []

        def fake_call_json(prompt, max_tokens=None, label=None):
            seen.append(prompt)
            return {
                "max_acceptable_days": 30,
                "verdict": "ACCEPT",
                "reasoning": "Card charge from a café posted after a weekend batch",
            }

        monkeypatch.setattr("core.llm_sync.call_json", fake_call_json)

        [result] = validate_date_gaps([_ctx()])

        assert result["verdict"] == "ACCEPT"
        assert result["max_acceptable_days"] == 30
        assert result["reasoning"].startswith("Card charge")
        assert "Heuristic" not in result["reasoning"]
        assert len(seen) == 1
        assert "transaction is after the document" in seen[0]

    def test_the_model_rejects_an_implausible_gap(self, monkeypatch):
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda prompt, max_tokens=None, label=None: {
                "max_acceptable_days": 5,
                "verdict": "REJECT",
                "reasoning": "A café charge does not clear four months later",
            },
        )

        [result] = validate_date_gaps([_ctx(gap_days=120)])

        assert result["verdict"] == "REJECT"
        assert result["max_acceptable_days"] == 5

    def test_an_explicit_model_reject_overrides_its_own_window(self, monkeypatch):
        """Documented threshold behaviour: `verdict: REJECT` wins over the gap test."""
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda prompt, max_tokens=None, label=None: {
                "max_acceptable_days": 60,
                "verdict": "REJECT",
                "reasoning": "Vendor does not match the transaction at all",
            },
        )

        [result] = validate_date_gaps([_ctx(gap_days=15)])

        assert result["verdict"] == "REJECT"

    def test_a_gap_inside_the_model_window_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda prompt, max_tokens=None, label=None: {
                "max_acceptable_days": 3,
                "reasoning": "Debit card, posts next business day",
            },
        )

        [result] = validate_date_gaps([_ctx(gap_days=2)])

        assert result["verdict"] == "ACCEPT"
        assert result["max_acceptable_days"] == 3

    def test_every_match_in_the_batch_is_validated(self, monkeypatch):
        calls: list[int] = []

        def fake_call_json(prompt, max_tokens=None, label=None):
            calls.append(1)
            return {"max_acceptable_days": 30, "verdict": "ACCEPT", "reasoning": "ok"}

        monkeypatch.setattr("core.llm_sync.call_json", fake_call_json)

        results = validate_date_gaps([_ctx(match_index=0), _ctx(match_index=1)])

        assert [r["match_index"] for r in results] == [0, 1]
        assert len(calls) == 2

    def test_an_empty_batch_calls_nothing(self, monkeypatch):
        def explode(*_a, **_kw):
            raise AssertionError("no LLM call should be made for an empty batch")

        monkeypatch.setattr("core.llm_sync.call_json", explode)

        assert validate_date_gaps([]) == []


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------

class TestTheHeuristicStillCatchesTheFall:
    def test_an_unreachable_model_returns_the_heuristic_verdict(self, monkeypatch, caplog):
        def local_down(prompt, max_tokens=None, label=None):
            raise _connection_error()

        monkeypatch.setattr("core.llm_sync.call_json", local_down)

        with caplog.at_level(logging.WARNING, logger="core.date_validator"):
            [result] = validate_date_gaps([_ctx(gap_days=15)])

        # Heuristic: no subscription/intermediary keyword, CAD account -> 14 days,
        # so a 15-day gap is rejected.
        assert result["verdict"] == "REJECT"
        assert result["max_acceptable_days"] == 14
        assert result["reasoning"] == "Heuristic: max 14 days for this transaction type"
        assert any(
            "using heuristic" in record.message or "using heuristic" in record.getMessage()
            for record in caplog.records
        ), caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_a_subscription_gap_still_gets_the_45_day_window(self, monkeypatch):
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda *_a, **_kw: (_ for _ in ()).throw(_connection_error()),
        )

        [result] = validate_date_gaps([
            _ctx(
                txn_description="BLUEPEAK SOFTWARE SUBSCRIPTION",
                doc_vendor="Bluepeak Software Inc",
                gap_days=40,
            )
        ])

        assert result["verdict"] == "ACCEPT"
        assert result["max_acceptable_days"] == 45

    def test_unusable_model_json_falls_back_rather_than_crashing(self, monkeypatch, caplog):
        """A model that answers with junk is an outage too, as far as a run cares."""
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda *_a, **_kw: {"max_acceptable_days": "no idea", "verdict": "MAYBE"},
        )

        with caplog.at_level(logging.WARNING, logger="core.date_validator"):
            [result] = validate_date_gaps([_ctx(gap_days=15)])

        assert result["reasoning"].startswith("Heuristic")
        assert result["max_acceptable_days"] == 14
        assert "using heuristic" in caplog.text

    def test_a_list_instead_of_an_object_falls_back(self, monkeypatch):
        monkeypatch.setattr("core.llm_sync.call_json", lambda *_a, **_kw: ["ACCEPT"])

        [result] = validate_date_gaps([_ctx()])

        assert result["reasoning"].startswith("Heuristic")

    def test_a_broken_template_is_logged_as_an_error_not_an_outage(self, monkeypatch, caplog):
        """An unformattable prompt template logs at ERROR, not as a quiet outage.

        A template holding an expression ``str.format`` cannot evaluate raises
        before any call is made, and the heuristic then answers. That path must
        be distinguishable in the log from a genuinely unreachable model, or a
        broken template degrades every verdict without anything looking wrong.
        """
        monkeypatch.setattr(
            date_validator,
            "VALIDATION_PROMPT",
            'gap {gap_days}, transaction is {"after" if txn_after else "before"}',
        )
        monkeypatch.setattr(
            "core.llm_sync.call_json",
            lambda *_a, **_kw: pytest.fail("the LLM must not be called with a broken prompt"),
        )

        with caplog.at_level(logging.ERROR, logger="core.date_validator"):
            [result] = validate_date_gaps([_ctx(gap_days=15)])

        assert result["reasoning"].startswith("Heuristic")
        assert "failed to format" in caplog.text
        assert any(record.levelno == logging.ERROR for record in caplog.records)

"""Per-attempt extraction telemetry — one record per provider attempt.

No network is touched: `_extract_with_gemini` and `_extract_with_openai` are
monkeypatched so the outer `extract_document` runs against scripted outcomes.

What is pinned here is that every attempt reaches the telemetry list — the ones
that failed as well as the one that answered — carrying its model, latency and
error text, and that a fallback's record names the model it fell back from.
Without that trail nobody can tell afterwards whether a document was read by
the primary model or by a fallback, or what it cost to get there. The writer
also has to survive a database that has no telemetry table yet, because losing
the extraction over its own bookkeeping would be the worse failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_file(tmp_path) -> Path:
    """A harmless text file is enough — the extractors are stubbed."""
    path = tmp_path / "fake.txt"
    path.write_text("test", encoding="utf-8")
    return path


def test_extraction_attempt_dataclass_defaults():
    from core.extraction import ExtractionAttempt
    att = ExtractionAttempt(model="gemini-2.5-pro", confidence=0.9, latency_ms=100)
    assert att.fallback_from is None
    assert att.error is None
    assert att.tokens_in is None


def test_string_to_numeric_confidence_known_strings():
    from core.extraction import _string_to_numeric_confidence
    assert _string_to_numeric_confidence("high") == 0.9
    assert _string_to_numeric_confidence("medium") == 0.6
    assert _string_to_numeric_confidence("low") == 0.3
    assert _string_to_numeric_confidence("mystery") == 0.9  # defaults to high
    assert _string_to_numeric_confidence(0.42) == 0.42
    assert _string_to_numeric_confidence(2.0) == 1.0  # clamp
    assert _string_to_numeric_confidence(-1) == 0.0   # clamp


@pytest.mark.asyncio
async def test_extract_document_attaches_telemetry_gemini_success(monkeypatch, fake_file):
    import core.extraction as ext

    async def fake_gemini(*args, **kwargs):
        return {
            "vendor": "Acme",
            "_provider": "gemini",
            "_model": "gemini-2.5-pro",
            "_telemetry": [ext.ExtractionAttempt(
                model="gemini-2.5-pro",
                confidence=0.92,
                latency_ms=842,
            )],
        }

    monkeypatch.setattr(ext, "_extract_with_gemini", fake_gemini)

    # Force gemini_config.enabled=True via a frozen-dataclass swap. The local
    # provider is disabled by the conftest fixture, so Gemini leads the chain.
    from dataclasses import replace

    import config.settings as settings
    from config.settings import gemini_config
    monkeypatch.setattr(settings, "gemini_config", replace(gemini_config, enabled=True))

    result = await ext.extract_document(fake_file)
    tel = result["_telemetry"]
    assert len(tel) == 1
    assert tel[0].model == "gemini-2.5-pro"
    assert tel[0].fallback_from is None


@pytest.mark.asyncio
async def test_extract_document_merges_fallback_telemetry(monkeypatch, fake_file):
    import core.extraction as ext

    async def failing_gemini(*args, **kwargs):
        raise RuntimeError("gemini rate limit")

    async def fake_openai(*args, **kwargs):
        return {
            "vendor": "Acme",
            "_provider": "openai",
            "_model": "gpt-4.1",
            "_telemetry": [ext.ExtractionAttempt(
                model="gpt-4.1",
                confidence=0.87,
                latency_ms=1200,
                fallback_from=None,  # the gemini-fallback_from is patched in extract_document
            )],
        }

    monkeypatch.setattr(ext, "_extract_with_gemini", failing_gemini)
    monkeypatch.setattr(ext, "_extract_with_openai", fake_openai)

    from dataclasses import replace

    import config.settings as settings
    from config.settings import gemini_config, openai_config
    monkeypatch.setattr(settings, "gemini_config", replace(gemini_config, enabled=True))
    # Both hosted providers are opt-in by key, and the local provider is off
    # by default in tests (conftest), so this test names the two rungs it
    # exercises: Gemini fails, OpenAI answers.
    monkeypatch.setattr(
        settings, "openai_config", replace(openai_config, api_key="test-key", enabled=True)
    )

    result = await ext.extract_document(fake_file)
    tel = result["_telemetry"]
    # One attempt per Gemini candidate that was tried, then the OpenAI success.
    # "gemini rate limit" reads as a capacity error, so the ladder walks every
    # candidate before conceding the provider.
    candidates = gemini_config.model_candidates
    assert len(tel) == len(candidates) + 1
    for attempt, expected_model in zip(tel, candidates):
        assert attempt.model == expected_model
        assert attempt.error is not None
        assert "gemini rate limit" in attempt.error
    # The openai attempt is marked as a fallback_from the primary model
    assert tel[-1].model == "gpt-4.1"
    assert tel[-1].fallback_from == gemini_config.model
    assert tel[-1].confidence == 0.87


@pytest.mark.asyncio
async def test_record_extraction_attempts_swallows_missing_table(monkeypatch):
    import psycopg2

    from core.extraction import ExtractionAttempt, record_extraction_attempts

    class _Cursor:
        def execute(self, sql, params=None):
            raise psycopg2.errors.UndefinedTable("extraction_log does not exist")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Db:
        def _conn(self):
            return _Conn()

    await record_extraction_attempts(
        _Db(), "user-1", 42,
        [ExtractionAttempt(model="gemini-2.5-pro", confidence=0.9, latency_ms=100)],
        final_model="gemini-2.5-pro",
        final_confidence=0.9,
    )
    # Must not raise — assertion is the absence of an exception.


@pytest.mark.asyncio
async def test_record_extraction_attempts_writes_rows(monkeypatch):
    from core.extraction import ExtractionAttempt, record_extraction_attempts

    calls: list[tuple] = []

    class _Cursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Db:
        def _conn(self):
            return _Conn()

    telemetry = [
        ExtractionAttempt(model="gemini-2.5-pro", confidence=0.0, latency_ms=300,
                          error="rate limit"),
        ExtractionAttempt(model="gpt-4.1", confidence=0.88, latency_ms=1100,
                          tokens_in=1234, tokens_out=321, cost_usd=0.01,
                          fallback_from="gemini-2.5-pro"),
    ]
    await record_extraction_attempts(_Db(), "user-1", 42, telemetry,
                                     final_model="gpt-4.1", final_confidence=0.88)
    # 2 INSERTs + 1 UPDATE = 3 queries
    assert len(calls) == 3
    inserts = [c for c in calls if "INSERT INTO extraction_log" in c[0]]
    update = next(c for c in calls if "UPDATE documents" in c[0])
    assert len(inserts) == 2
    assert inserts[0][1][2] == "gemini-2.5-pro"
    assert inserts[0][1][9] == "rate limit"
    assert inserts[1][1][8] == "gemini-2.5-pro"  # fallback_from
    assert update[1] == ("gpt-4.1", 0.88, 42, "user-1")

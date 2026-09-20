"""Unit tests for server/safe_exec.py — the exception-swallowing helper.

``safe_call`` and ``safe`` exist so that best-effort work (telemetry, cache
warming, a cosmetic side effect) cannot take down the caller. What these tests
prove is that the swallowing is never silent: every caught exception is logged
with the label the caller gave it, a default is returned in its place, and
``KeyboardInterrupt``/``SystemExit`` still propagate so the process can be shut
down.
"""

from __future__ import annotations

import logging

import pytest

from server.safe_exec import safe, safe_call


def test_safe_call_returns_value_on_success() -> None:
    assert safe_call("demo", lambda x: x * 2, 21) == 42


def test_safe_call_returns_default_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    def boom():
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.WARNING, logger="server.safe_exec"):
        result = safe_call("demo.boom", boom, default="fallback")
    assert result == "fallback"
    # The exception must hit the log — silent swallowing is the antipattern
    # this helper exists to replace.
    assert any("demo.boom" in r.getMessage() for r in caplog.records)
    assert any("kaboom" in r.getMessage() or "RuntimeError" in r.getMessage() for r in caplog.records)


def test_safe_call_default_is_none_if_not_given() -> None:
    assert safe_call("demo", lambda: (_ for _ in ()).throw(ValueError())) is None


def test_safe_context_manager_swallows_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="server.safe_exec"):
        with safe("demo.ctx"):
            raise ValueError("ctx-fail")
    assert any("demo.ctx" in r.getMessage() for r in caplog.records)


def test_safe_call_does_not_catch_keyboardinterrupt() -> None:
    def interrupt():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        safe_call("demo.kbint", interrupt)


def test_safe_call_does_not_catch_systemexit() -> None:
    def exiting():
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        safe_call("demo.sysexit", exiting)

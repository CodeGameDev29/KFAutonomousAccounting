"""Unit tests for server.deps.is_enabled.

Authoritative source: business_profile.feature_flags -> flag_name.
Fallback: BETA_FLAGS_DEFAULT_ON env var when the user has no
business_profile row or the flag key is absent/null.
"""
from __future__ import annotations

import importlib


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def _conn(self):
        return _FakeConn(self._row)


class _BrokenDB:
    def _conn(self):
        raise RuntimeError("connection pool exhausted")


def _reload_deps(monkeypatch, env_value: str | None):
    if env_value is None:
        monkeypatch.delenv("BETA_FLAGS_DEFAULT_ON", raising=False)
    else:
        monkeypatch.setenv("BETA_FLAGS_DEFAULT_ON", env_value)
    import server.deps as deps
    return importlib.reload(deps)


def test_db_flag_true(monkeypatch):
    deps = _reload_deps(monkeypatch, "[]")
    db = _FakeDB(row=(True,))
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is True


def test_db_flag_false_does_not_fall_through(monkeypatch):
    # Even if env lists the flag, an explicit DB False must win.
    deps = _reload_deps(monkeypatch, '["quickbooks_export"]')
    db = _FakeDB(row=(False,))
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is False


def test_db_key_absent_env_lists_flag(monkeypatch):
    # Row exists but the flag key is NULL (JSONB `->` on missing key) — env applies.
    deps = _reload_deps(monkeypatch, '["quickbooks_export"]')
    db = _FakeDB(row=(None,))
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is True


def test_db_key_absent_env_silent(monkeypatch):
    deps = _reload_deps(monkeypatch, "[]")
    db = _FakeDB(row=(None,))
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is False


def test_no_business_profile_row_env_lists_flag(monkeypatch):
    # fetchone returns None — user has no business_profile row yet.
    deps = _reload_deps(monkeypatch, '["quickbooks_export"]')
    db = _FakeDB(row=None)
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is True


def test_db_error_fails_closed(monkeypatch):
    # Even if env lists the flag, a DB exception must return False (fail-closed).
    deps = _reload_deps(monkeypatch, '["quickbooks_export"]')
    assert deps.is_enabled(_BrokenDB(), "user-1", "quickbooks_export") is False


def test_invalid_env_json_treated_empty(monkeypatch):
    deps = _reload_deps(monkeypatch, "not-valid-json")
    # Invalid env parses to empty frozenset → missing-row fallback returns False.
    db = _FakeDB(row=None)
    assert deps.is_enabled(db, "user-1", "quickbooks_export") is False

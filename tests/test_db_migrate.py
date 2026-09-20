"""Unit tests for db.migrate — exercise the runner against a fake cursor."""

from __future__ import annotations

import pathlib

import pytest

from db import migrate


class _FakeCursor:
    """Minimal psycopg2-cursor stand-in that records executed SQL."""

    def __init__(self, applied_rows: list[str] | None = None) -> None:
        self._applied_rows = list(applied_rows or [])
        self.executed: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))
        if "INSERT INTO schema_migrations" in sql and params:
            self._applied_rows.append(params[0])

    def fetchall(self):
        return [(name,) for name in self._applied_rows]


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(migrate.psycopg2, "connect", lambda *_a, **_k: conn)


def _patch_files(monkeypatch, files: list[str]) -> None:
    paths = [pathlib.Path(name) for name in files]
    monkeypatch.setattr(migrate, "_discover", lambda: paths)


def test_no_pending_migrations_is_noop(monkeypatch):
    cur = _FakeCursor(applied_rows=["001_a.sql"])
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)
    _patch_files(monkeypatch, ["001_a.sql"])

    rc = migrate.run(dsn="postgres://fake")

    assert rc == 0
    inserts = [sql for sql, _ in cur.executed if "INSERT INTO schema_migrations" in sql]
    assert inserts == []
    assert conn.closed is True


def test_baseline_records_without_executing(monkeypatch, tmp_path):
    # Create two fake SQL files — they must NOT be executed in baseline mode.
    a = tmp_path / "001_a.sql"
    a.write_text("SELECT bogus_symbol_that_would_fail;")
    b = tmp_path / "002_b.sql"
    b.write_text("SELECT another_bogus_symbol;")

    cur = _FakeCursor()
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)
    _patch_files(monkeypatch, [str(a), str(b)])

    rc = migrate.run(dsn="postgres://fake", baseline=True)

    assert rc == 0
    # Both filenames got inserted into schema_migrations.
    inserted = [params[0] for sql, params in cur.executed
                if "INSERT INTO schema_migrations" in sql]
    assert inserted == ["001_a.sql", "002_b.sql"]
    # Neither SQL file body got executed — nothing containing "bogus_symbol".
    assert not any("bogus_symbol" in sql for sql, _ in cur.executed)


def test_pending_migrations_execute_in_order(monkeypatch, tmp_path):
    a = tmp_path / "001_a.sql"
    a.write_text("CREATE TABLE a_marker (x INT);")
    b = tmp_path / "002_b.sql"
    b.write_text("CREATE TABLE b_marker (y INT);")

    cur = _FakeCursor()
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)
    _patch_files(monkeypatch, [str(a), str(b)])

    rc = migrate.run(dsn="postgres://fake")

    assert rc == 0
    # Expect: CREATE schema_migrations, SELECT applied, then for each file:
    # execute body, INSERT filename, commit.
    body_execs = [sql for sql, _ in cur.executed
                  if "a_marker" in sql or "b_marker" in sql]
    assert body_execs == [
        "CREATE TABLE a_marker (x INT);",
        "CREATE TABLE b_marker (y INT);",
    ]
    inserted = [params[0] for sql, params in cur.executed
                if "INSERT INTO schema_migrations" in sql]
    assert inserted == ["001_a.sql", "002_b.sql"]


def test_failing_migration_rolls_back_and_aborts(monkeypatch, tmp_path):
    good = tmp_path / "001_good.sql"
    good.write_text("CREATE TABLE good_marker (x INT);")
    bad = tmp_path / "002_bad.sql"
    bad.write_text("THIS IS BROKEN SQL;")
    unreached = tmp_path / "003_unreached.sql"
    unreached.write_text("CREATE TABLE unreached (x INT);")

    class _FailingCursor(_FakeCursor):
        def execute(self, sql: str, params: tuple | None = None) -> None:
            if "THIS IS BROKEN SQL" in sql:
                raise RuntimeError("syntax error")
            super().execute(sql, params)

    cur = _FailingCursor()
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)
    _patch_files(monkeypatch, [str(good), str(bad), str(unreached)])

    with pytest.raises(RuntimeError, match="syntax error"):
        migrate.run(dsn="postgres://fake")

    # Good migration committed, bad rolled back, third never attempted.
    inserted = [params[0] for sql, params in cur.executed
                if "INSERT INTO schema_migrations" in sql]
    assert inserted == ["001_good.sql"]
    assert conn.rollbacks >= 1
    assert not any("unreached" in sql for sql, _ in cur.executed)


def test_rollback_files_are_ignored(tmp_path, monkeypatch):
    # Real _discover() reads from MIGRATIONS_DIR — point it at tmp_path.
    (tmp_path / "001_real.sql").write_text("")
    (tmp_path / "001_real.rollback.sql").write_text("")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    found = [p.name for p in migrate._discover()]
    assert found == ["001_real.sql"]

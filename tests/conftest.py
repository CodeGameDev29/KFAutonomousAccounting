"""Shared fixtures for the Autonomous Accounting test suite.

Everything here is synthetic. The company is ``Example Corp Inc.``, the people
and merchants are invented, the amounts are made up, and no real book, bank
account or document is represented — see ``tests/fixtures/README.md``.

Three things this file guarantees before a single test runs:

1. **No ``.env`` is ever loaded into the test process.** ``config/settings``
   calls ``load_dotenv(override=True)``, which walks *up* from its own directory
   until it finds a file — on a contributor's machine that can reach a real,
   unrelated ``.env`` several directories above the checkout, and with
   ``override=True`` it beats anything a test sets. So ``load_dotenv`` is made a
   no-op for the whole session and a test run depends on nothing but this file.
   The repository's own ``.env`` is still *read* (parsed, never applied) for one
   value: ``DATABASE_URL``, so that ``pytest`` works on a Quickstart install
   without being told anything.
2. **The database is a throwaway, and the suite creates it.** The DSN is
   resolved below, always ending in ``_test``, and the database is created if it
   does not exist. A name that does not end in ``_test`` is refused loudly
   rather than written to. Tests that need PostgreSQL skip themselves when it is
   unreachable.
3. **No network.** The local LLM endpoint is blanked for every test; the tests
   that mean to exercise a provider re-enable it explicitly.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 1. No .env is applied to the test process
# ---------------------------------------------------------------------------

def _dotenv_values() -> dict:
    """Parse ``<repo>/.env`` without touching ``os.environ``. ``{}`` if absent."""
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(str(path)).items() if v}
    except Exception:  # pragma: no cover - a malformed .env is not a test failure
        return {}


def _install_dotenv_guard() -> None:
    """Make ``load_dotenv`` a no-op for the whole test session.

    Patched on the ``dotenv`` module itself, before any project module has done
    ``from dotenv import load_dotenv`` — conftest is imported first, so the
    ``from`` binding every project module takes is already the guarded one.

    A no-op rather than a confinement: ``config/settings`` loads ``.env`` with
    ``override=True`` on purpose, so an operator's own file would otherwise beat
    ``monkeypatch.setenv`` inside a test and make the result depend on whatever
    that install happens to be configured for. Everything the suite needs is set
    in this file; ``.env`` is parsed separately, for ``DATABASE_URL`` only.
    """
    try:
        import dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dependency
        return

    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.find_dotenv = lambda *a, **k: ""


_DOTENV = _dotenv_values()
_install_dotenv_guard()


# ---------------------------------------------------------------------------
# 2. The throwaway database, and the guard that keeps it throwaway
# ---------------------------------------------------------------------------

#: Where the suite looks for PostgreSQL when nothing else says. This is the
#: Quickstart's own database name and port, plus ``_test``.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql://postgres@127.0.0.1:5432/autonomous_accounting_test"
)


def _database_name(dsn: str) -> str:
    """The database name out of a libpq URL, ignoring any query string."""
    parsed = urlparse(dsn)
    return (parsed.path or "").lstrip("/").split("?")[0]


def _with_database(dsn: str, name: str) -> str:
    """The same DSN pointing at a different database name."""
    parsed = urlparse(dsn)
    query = f"?{parsed.query}" if parsed.query else ""
    return urlunparse(parsed._replace(path=f"/{name}", query="")) + query


def _derive_test_dsn(dsn: str) -> str:
    """``<db>`` becomes ``<db>_test``; a name already ending in ``_test`` stays.

    The suite creates users, inserts rows and truncates tables. It must never
    be able to reach the database the application itself uses, and the cheapest
    way to guarantee that is never to use the name it was given: it always runs
    against a ``_test`` sibling, on the same server, with the same credentials.
    """
    name = _database_name(dsn)
    if not name:
        return dsn
    return dsn if name.endswith("_test") else _with_database(dsn, f"{name}_test")


def _resolve_test_dsn() -> str:
    """Where the tests connect, in order of precedence.

    1. ``TEST_DATABASE_URL`` — used verbatim, for a database that is not a
       sibling of the application's own (CI, a second server, another port).
    2. ``DATABASE_URL`` exported in the shell — a ``_test`` sibling of it.
    3. ``DATABASE_URL`` in ``<repo>/.env`` — a ``_test`` sibling of it. This is
       what makes a bare ``pytest`` work on a Quickstart install, where the DSN
       exists only in that file.
    4. ``DEFAULT_TEST_DATABASE_URL``.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    for candidate in (os.environ.get("DATABASE_URL"), _DOTENV.get("DATABASE_URL")):
        if candidate:
            return _derive_test_dsn(candidate)
    return DEFAULT_TEST_DATABASE_URL


def _assert_throwaway_database(dsn: str) -> None:
    """Refuse to run against anything that is not obviously disposable.

    These tests create users, insert transactions and truncate tables. Pointing
    them at real books would destroy them. Fail loudly, at collection time,
    before any connection is opened.
    """
    name = _database_name(dsn)
    if name.endswith("_test"):
        return
    raise RuntimeError(
        f"Refusing to run the test suite against {dsn!r}: the database is named "
        f"{name!r}, which does not end in '_test'.\n"
        "The suite creates users, inserts rows and truncates tables; it must "
        "never be pointed at real books. Unset TEST_DATABASE_URL to let the "
        "suite derive a '_test' sibling of your DATABASE_URL, or point "
        "TEST_DATABASE_URL at a database whose name ends in '_test'."
    )


os.environ["DATABASE_URL"] = _resolve_test_dsn()
_assert_throwaway_database(os.environ["DATABASE_URL"])


# ---------------------------------------------------------------------------
# 3. Settings that must be in place before the first project import
# ---------------------------------------------------------------------------

# Dummy secrets, so modules that assert an env var at import time don't crash.
# Nothing here reaches a network: every external call is mocked per test.
os.environ.setdefault("AUTH_JWT_SECRET", "test-jwt-secret-not-a-real-key")
os.environ.setdefault("STORAGE_URL_SECRET", "test-storage-secret-not-a-real-key")

# A hosted provider's key exported in the contributor's shell must not turn into
# a real API call from a test run. `_local_llm_disabled_by_default` below blanks
# the *primary* provider; these two env vars are what the hosted fallbacks read
# at class-definition time, so they have to be gone before `config.settings` is
# first imported — after that, `OpenAIConfig().enabled` has already been decided.
# A test that means to exercise a hosted path sets its own key with
# `monkeypatch.setenv` and reloads, which still works.
for _hosted_key in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(_hosted_key, None)

# The real peer-benchmark table is 41 MB and gitignored (see
# scripts/ingest_ised_benchmarks.py). Tests read the small synthetic slice.
os.environ.setdefault(
    "BENCHMARK_DATA_PATH", "tests/fixtures/ised_benchmarks_sample.json"
)

from db.database import Database  # noqa: E402
from models.document import Document, DocumentStatus  # noqa: E402
from models.transaction import AccountType, Transaction, TransactionStatus  # noqa: E402

# ---------------------------------------------------------------------------
# PostgreSQL: reachability, and a schema for a database that has none
# ---------------------------------------------------------------------------

#: The same order README.md gives, and for the same reasons: ``auth.uid()``
#: before the policies that call it, the grants after the tables they grant on.
#: ``db/schema_pg.sql`` is the whole schema; anything added after that baseline
#: is applied by ``db.migrate`` between these two steps.
_BOOTSTRAP_SQL = ("db/local_auth_schema.sql", "db/schema_pg.sql")
_GRANTS_SQL = "db/local_grants.sql"


def _postgres_connection(dsn: str):
    """Open one short-lived connection, or ``None`` if there is nothing there."""
    try:
        import psycopg2
    except ImportError:  # pragma: no cover - psycopg2 is a hard dependency
        return None
    try:
        return psycopg2.connect(dsn, connect_timeout=3)
    except Exception:
        return None


def _create_test_database(dsn: str) -> None:
    """``CREATE DATABASE`` on the same server, with the same credentials.

    Connects to the ``postgres`` maintenance database, which every server has.
    Silent on every failure: the server may be down, or the role may not be
    allowed to create databases, and either way the caller falls through to
    "PostgreSQL is unreachable" and those tests skip themselves.
    """
    name = _database_name(dsn)
    if not name.endswith("_test"):  # pragma: no cover - guarded above already
        return
    admin = _postgres_connection(_with_database(dsn, "postgres"))
    if admin is None:
        return
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            from psycopg2 import sql

            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        print(f"conftest: created test database {name!r}", file=sys.stderr)
    except Exception:
        pass  # already exists, or not permitted — either way, carry on
    finally:
        admin.close()


@pytest.fixture(scope="session", autouse=True)
def _test_database_schema() -> None:
    """Create and bootstrap the throwaway database, once per session.

    Typing ``pytest`` on a Quickstart install should work with nothing else
    done first: the database is created if missing and given the schema, any
    migrations and the grants — the README's steps, in the README's order. If
    PostgreSQL is not running at all, this does nothing and the tests that need
    it skip themselves with a reason.
    """
    dsn = os.environ["DATABASE_URL"]
    _assert_throwaway_database(dsn)
    conn = _postgres_connection(dsn)
    if conn is None:
        _create_test_database(dsn)
        conn = _postgres_connection(dsn)
    if conn is None:
        return
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.transactions')")
            if cur.fetchone()[0] is not None:
                return  # already bootstrapped
            for relative in _BOOTSTRAP_SQL:
                cur.execute((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"conftest: could not bootstrap the test schema: {exc}", file=sys.stderr)
        return
    finally:
        conn.close()

    # Anything added after the baseline, through the same entry point
    # `python -m db.migrate` uses. A no-op while db/migrations/ holds nothing
    # but its README, which is what a fresh checkout looks like.
    try:
        from db import migrate

        migrate.run()
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"conftest: migrations did not apply: {exc}", file=sys.stderr)
        return

    # Grants last, on the tables the migrations just created. Without this the
    # suite bootstraps a database its own application role cannot write to, and
    # every test that goes through `SET LOCAL ROLE authenticated` fails with
    # "permission denied".
    grants = _postgres_connection(os.environ["DATABASE_URL"])
    if grants is None:  # pragma: no cover - environment dependent
        return
    try:
        grants.autocommit = True
        with grants.cursor() as cur:
            cur.execute((PROJECT_ROOT / _GRANTS_SQL).read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"conftest: grants did not apply: {exc}", file=sys.stderr)
    finally:
        grants.close()


# ---------------------------------------------------------------------------
# No network: the LLM endpoint is off unless a test asks for it
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _local_llm_disabled_by_default(monkeypatch):
    """Keep the local LLM endpoint out of the default test environment.

    ``LLM_BASE_URL`` is the engine's *primary* provider, so without this every
    test that reaches ``extract_document`` or ``parse_statement_with_llm`` would
    open a real socket to whatever inference server the contributor happens to
    be running. Nothing in this suite reaches a network; that is the rule this
    fixture keeps true.

    Tests that mean to exercise the local provider re-enable it explicitly — see
    ``tests/test_local_llm_provider.py``.
    """
    from dataclasses import replace

    import config.settings as settings

    monkeypatch.setattr(
        settings,
        "local_llm_config",
        replace(settings.local_llm_config, base_url=""),
    )
    yield


# ---------------------------------------------------------------------------
# SQLite database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """A temporary SQLite database initialised with the project schema.

    Yields the path to the database file; the file is removed on teardown.
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())
    conn.close()
    yield db_path
    # Close any singleton Database instance still holding the file open.
    canonical = str(db_path.resolve())
    if canonical in Database._instances:
        Database._instances[canonical].close()
    if db_path.exists():
        db_path.unlink()


@pytest.fixture()
def db(tmp_db: Path) -> Database:
    """A ``Database`` connected to the temporary SQLite database."""
    return Database(str(tmp_db))


# ---------------------------------------------------------------------------
# Model fixtures — one invented month of one invented company
# ---------------------------------------------------------------------------
#
# The arithmetic is deliberate so a test can re-derive what it asserts: each
# document's subtotal plus its taxes equals its total, and each total equals the
# amount of the transaction it proves.

@pytest.fixture()
def sample_transactions() -> list[Transaction]:
    """Five invented transactions spanning four accounts and two currencies."""
    return [
        Transaction(
            account=AccountType.CAD,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 15),
            amount=Decimal("67.20"),
            currency="CAD",
            description="ORCHID TELECOM PREAUTH PMT",
            source_file="test_bank_cad.csv",
            source_row=3,
            status=TransactionStatus.UNMATCHED,
        ),
        Transaction(
            account=AccountType.CAD,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 22),
            amount=Decimal("300.00"),
            currency="CAD",
            description="BLUEPEAK SOFTWARE INC",
            source_file="test_bank_cad.csv",
            source_row=7,
            status=TransactionStatus.UNMATCHED,
        ),
        Transaction(
            account=AccountType.USD,
            transaction_type="CREDIT",
            date_posted=date(2026, 1, 10),
            amount=Decimal("9600.00"),
            currency="USD",
            description="INCOMING WIRE HARBOURLINE LOGISTICS",
            source_file="test_bank_usd.csv",
            source_row=1,
            status=TransactionStatus.UNMATCHED,
        ),
        Transaction(
            account=AccountType.CREDIT_CARD,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 18),
            amount=Decimal("40.32"),
            currency="CAD",
            description="EXAMPLE RETAILER MARKETPLACE",
            source_file="test_card.csv",
            source_row=12,
            status=TransactionStatus.UNMATCHED,
        ),
        Transaction(
            account=AccountType.WISE,
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 12),
            amount=Decimal("1800.00"),
            currency="USD",
            description="Contractor payment Jordan Avery",
            source_file="test_wise.csv",
            source_row=2,
            status=TransactionStatus.UNMATCHED,
        ),
    ]


@pytest.fixture()
def sample_documents() -> list[Document]:
    """Three invented extracted documents, proving three of the transactions."""
    return [
        Document(
            original_filename="20260112-orchid-telecom.pdf",
            stored_path="books-2026/202601 Jan/20260112-orchid-telecom.pdf",
            file_hash="1111111111111111111111111111111a",
            vendor="Orchid Telecom",
            document_date=date(2026, 1, 12),
            currency="CAD",
            subtotal=Decimal("60.00"),
            tax_gst=Decimal("3.00"),
            tax_pst=Decimal("4.20"),
            total=Decimal("67.20"),
            payment_method="Direct Debit",
            extraction_confidence="high",
            status=DocumentStatus.EXTRACTED,
        ),
        Document(
            original_filename="20260120-bluepeak-software.pdf",
            stored_path="books-2026/202601 Jan/20260120-bluepeak-software.pdf",
            file_hash="2222222222222222222222222222222b",
            vendor="Bluepeak Software Inc",
            document_date=date(2026, 1, 20),
            currency="CAD",
            subtotal=Decimal("300.00"),
            tax_gst=None,
            total=Decimal("300.00"),
            invoice_number="BP-2026-00412",
            extraction_confidence="high",
            status=DocumentStatus.EXTRACTED,
        ),
        Document(
            original_filename="20260118-example-retailer.pdf",
            stored_path="books-2026/202601 Jan/20260118-example-retailer.pdf",
            file_hash="3333333333333333333333333333333c",
            vendor="Example Retailer",
            document_date=date(2026, 1, 17),
            currency="CAD",
            subtotal=Decimal("36.00"),
            tax_gst=Decimal("1.80"),
            tax_pst=Decimal("2.52"),
            total=Decimal("40.32"),
            payment_method="Credit card",
            extraction_confidence="medium",
            status=DocumentStatus.EXTRACTED,
        ),
    ]


# ---------------------------------------------------------------------------
# CSV fixtures — the checked-in synthetic files, copied where a test can use them
# ---------------------------------------------------------------------------

def _copy_fixture(tmp_path: Path, name: str, as_name: str | None = None) -> Path:
    """Copy a generated fixture into ``tmp_path`` so a test may consume it.

    The name matters on the way in: ``core/bank_parser.py`` infers which account
    a chequing CSV belongs to from the filename stem.
    """
    target = tmp_path / (as_name or name)
    target.write_bytes((FIXTURES_DIR / name).read_bytes())
    return target


@pytest.fixture()
def sample_csv_cad(tmp_path: Path) -> Path:
    """The four-row chequing CSV, under a name that resolves to the CAD account."""
    return _copy_fixture(tmp_path, "test_bank_cad.csv", "statement_jan2026_cad.csv")


@pytest.fixture()
def sample_csv_transfers(tmp_path: Path) -> Path:
    """The transfer-platform activity export (three COMPLETED rows of five)."""
    return _copy_fixture(tmp_path, "test_wise.csv", "wise_jan2026.csv")


# ---------------------------------------------------------------------------
# Directory-structure fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """A month-folder tree in the layout ``config.settings.get_month_dir`` builds.

    ::

        books-2026/
            202601 Jan/
            202602 Feb/
            202603 Mar/
    """
    base = tmp_path / "books-2026"
    for month_dir in ("202601 Jan", "202602 Feb", "202603 Mar"):
        (base / month_dir).mkdir(parents=True)
    (base / "202601 Jan" / "20260112-orchid-telecom.pdf").write_bytes(
        (FIXTURES_DIR / "test_receipt_retailer.pdf").read_bytes()
    )
    return base


# ---------------------------------------------------------------------------
# Access guards, for the suites that mock the database
# ---------------------------------------------------------------------------
#
# Many suites replace ``get_db`` with a MagicMock, which makes the real
# email-verification lookup return an unserializable mock. Overriding the
# dependency lets those tests reach the actual handler. Verified email is the
# only access guard there is: a signed-in user reaches their own data and
# nothing else.

@pytest.fixture(autouse=True)
def _bypass_access_guards():
    """Override the verified-email dependency while ``server.app`` is importable.

    Tests that want to assert the guard itself override it again in their own
    setup, which wins because this fixture restores rather than clears.
    """
    try:
        from server.access import require_verified_email
        from server.app import app
        from server.auth import AuthUser
    except Exception:
        yield
        return

    async def _verified(user: AuthUser | None = None) -> AuthUser:
        return user or AuthUser(
            id="stub-user",
            email="demo@example.com",
            role="authenticated",
            email_verified=True,
        )

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[require_verified_email] = _verified
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

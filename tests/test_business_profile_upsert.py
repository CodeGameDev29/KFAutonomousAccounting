"""The business profile is upserted field by field, and an empty field is not a value.

``business_profile.province`` selects the GST/HST rate the reports use and
``industry`` selects the peer-benchmark cohort, so a write that blanks either of
them changes numbers rather than labels. Any caller that submits a form before
it has the stored profile in hand sends empty strings, and the guard that has to
survive a refactor is that an empty string reaches the row as "not supplied".
"""

from __future__ import annotations

import os
import uuid

import pytest

from db.database_pg import DatabasePg


@pytest.fixture()
def profile_db():
    """A DatabasePg bound to one throwaway account in the test database."""
    psycopg2 = pytest.importorskip("psycopg2")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not set — no PostgreSQL to exercise")
    try:
        owner = psycopg2.connect(dsn, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unreachable: {exc}")

    user_id = str(uuid.uuid4())
    owner.autocommit = True
    with owner.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email, encrypted_password, "
            "email_confirmed_at, created_at, updated_at) "
            "VALUES (%s, %s, 'x', NOW(), NOW(), NOW())",
            (user_id, f"profile-{user_id[:8]}@example.com"),
        )

    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=dsn)
    try:
        yield DatabasePg(pool, user_id=user_id)
    finally:
        pool.closeall()
        with owner.cursor() as cur:
            cur.execute("DELETE FROM business_profile WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))
        owner.close()


STORED = {
    "company_name": "Example Corp Inc.",
    "fiscal_year": 2026,
    "base_currency": "CAD",
    "tax_jurisdiction": "CA",
    "province": "Manitoba",
    "business_number": "123456789 RC0001",
    "industry": "Food & Hospitality",
    "naics_code": "722511",
}


def test_empty_strings_do_not_blank_a_stored_profile(profile_db: DatabasePg) -> None:
    """A save that carries "" for a field leaves that field as it was."""
    profile_db.upsert_business_profile(dict(STORED))

    profile_db.upsert_business_profile({
        "company_name": "Example Corp Inc.",
        "fiscal_year": 2026,
        "base_currency": "CAD",
        "tax_jurisdiction": "CA",
        "province": "",
        "business_number": "",
        "industry": "",
        "naics_code": "",
    })

    after = profile_db.get_business_profile()
    assert after is not None
    assert after["province"] == "Manitoba"
    assert after["industry"] == "Food & Hospitality"
    assert after["business_number"] == "123456789 RC0001"
    assert after["naics_code"] == "722511"


def test_missing_keys_do_not_blank_a_stored_profile(profile_db: DatabasePg) -> None:
    """The same for a payload that leaves the fields out entirely."""
    profile_db.upsert_business_profile(dict(STORED))

    profile_db.upsert_business_profile({"company_name": "Renamed Corp Inc."})

    after = profile_db.get_business_profile()
    assert after is not None
    assert after["company_name"] == "Renamed Corp Inc."
    assert after["province"] == "Manitoba"
    assert after["industry"] == "Food & Hospitality"


def test_a_real_value_still_replaces_the_stored_one(profile_db: DatabasePg) -> None:
    """The guard must not make the fields read-only."""
    profile_db.upsert_business_profile(dict(STORED))

    profile_db.upsert_business_profile({"province": "Ontario", "industry": "Real Estate"})

    after = profile_db.get_business_profile()
    assert after is not None
    assert after["province"] == "Ontario"
    assert after["industry"] == "Real Estate"


def test_an_empty_string_still_inserts_the_first_row(profile_db: DatabasePg) -> None:
    """With no row to preserve, an empty field is stored as it arrived."""
    profile_db.upsert_business_profile({
        "company_name": "Example Corp Inc.",
        "province": "",
        "industry": "",
    })

    after = profile_db.get_business_profile()
    assert after is not None
    assert after["company_name"] == "Example Corp Inc."
    assert after["province"] == ""
    assert after["industry"] == ""

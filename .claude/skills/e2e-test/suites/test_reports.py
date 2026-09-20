"""Reports, exports, and sharing tests."""

from __future__ import annotations

from lib.api_client import APIClient
from lib.test_framework import (
    NotExercised,
    assert_eq,
    assert_key,
    assert_status,
    assert_status_in,
    not_applicable_to_persona,
)
from profiles import UserProfile


def test_list_report_years(client: APIClient, profile: UserProfile):
    """List available report years."""
    resp = client.get("/api/reports/years")
    assert_status(resp, 200, "Report years")
    data = resp.json()
    assert_key(data, "years", "Years response")


def test_get_monthly_report(client: APIClient, profile: UserProfile):
    """Get a specific month's report data.

    January of the fiscal year is where the fixture statement lands, so this
    month exists whenever ingestion worked.
    """
    resp = client.get(f"/api/reports/{profile.fiscal_year}/1")
    assert_status_in(resp, (200, 404), "Monthly report")
    if resp.status_code == 404:
        raise NotExercised(
            f"no report exists for {profile.fiscal_year}-01 on this account, so there was "
            "nothing to read back; the generated statement's rows are dated 2026-01"
        )
    data = resp.json()
    assert_key(data, "year", "Report data")
    assert_key(data, "month", "Report data")


def test_share_report(client: APIClient, profile: UserProfile):
    """Create a shareable link for a month's report."""
    if not profile.shares_report:
        not_applicable_to_persona(profile, "shares_report", "share reports with a link")

    resp = client.post(f"/api/reports/{profile.fiscal_year}/1/share")
    if resp.status_code == 404:
        raise NotExercised(
            f"no {profile.fiscal_year}-01 report data to share on this account"
        )
    assert_status(resp, 200, "Share report")
    data = resp.json()
    assert_key(data, "token", "Share response")
    assert_key(data, "expires_at", "Share response")


def test_access_shared_report(client: APIClient, profile: UserProfile):
    """Access a shared report via token (no auth)."""
    if not profile.shares_report:
        not_applicable_to_persona(profile, "shares_report", "share reports with a link")

    share_resp = client.post(f"/api/reports/{profile.fiscal_year}/1/share")
    assert_status(share_resp, 200, "Share report (for the anonymous read)")
    token = share_resp.json().get("token")
    assert token, f"Share response carried no token: {share_resp.text[:200]}"

    # Access without auth
    unauthed = APIClient(client.base_url)
    try:
        resp = unauthed.get(f"/api/shared/{token}")
        assert_status(resp, 200, "Shared report access")
        data = resp.json()
        assert_key(data, "company_name", "Shared report")
        assert_eq(data.get("shared"), True, "Shared flag")
    finally:
        unauthed.close()


def test_shared_report_invalid_token(client: APIClient, profile: UserProfile):
    """An unknown or expired share token yields nothing.

    The assertions are what make it able to fail: a wrong status, *or* a 200 that
    leaks report fields.
    """
    unauthed = APIClient(client.base_url)
    try:
        resp = unauthed.get("/api/shared/invalid-token-that-does-not-exist-abc123")
        assert_status_in(resp, (401, 403, 404, 410), "Invalid share token")
        body = resp.text.lower()
        for leak in ("company_name", "total_income", "transactions"):
            assert leak not in body, (
                f"Rejected share token still returned report field '{leak}': {resp.text[:200]}"
            )
    finally:
        unauthed.close()


def test_download_pdf_report(client: APIClient, profile: UserProfile):
    """Download PDF report for a month."""
    if not profile.downloads_report:
        not_applicable_to_persona(profile, "downloads_report", "download reports")

    resp = client.get(f"/api/reports/{profile.fiscal_year}/1/pdf")
    # 404 if no data for that month, 200 if the PDF generated.
    # 500 = server crash during generation — a real bug, not an acceptable state.
    assert_status_in(resp, (200, 404), "PDF report (500 = server error, not acceptable)")
    if resp.status_code == 200:
        assert resp.content[:4] == b"%PDF", (
            f"/pdf returned 200 but the body is not a PDF: {resp.content[:40]!r}"
        )


def test_download_audit_binder(client: APIClient, profile: UserProfile):
    """Download ZIP audit binder for a month."""
    if not profile.downloads_report:
        not_applicable_to_persona(profile, "downloads_report", "download reports")

    resp = client.get(f"/api/reports/{profile.fiscal_year}/1/binder")
    # 500 = server crash during binder generation — a real bug, not acceptable.
    assert_status_in(resp, (200, 404), "Binder (500 = server error, not acceptable)")
    if resp.status_code == 200:
        assert resp.content[:2] == b"PK", (
            f"/binder returned 200 but the body is not a ZIP: {resp.content[:40]!r}"
        )


def test_close_month(client: APIClient, profile: UserProfile):
    """Close a month to prevent further edits."""
    if not profile.closes_month:
        not_applicable_to_persona(profile, "closes_month", "close months")

    resp = client.post(f"/api/reports/{profile.fiscal_year}/1/close")
    if resp.status_code == 404:
        raise NotExercised(
            f"no {profile.fiscal_year}-01 report data exists to close on this account"
        )
    # 422 "Cannot close month with 0 transactions" is the endpoint refusing to
    # close an empty month, which is correct behaviour and not this test's
    # subject. It is NOT-EXERCISED only for a persona that never uploads a
    # statement; for one that does, an empty January means ingestion lost its
    # rows, and that stays a FAIL.
    if (resp.status_code == 422 and "0 transactions" in resp.text
            and not profile.uploads_bank_csv):
        raise NotExercised(
            f"{profile.fiscal_year}-01 holds no transactions for persona '{profile.name}', which "
            "uploads no statement, so there is no month to close. The endpoint refused with "
            "422 'Cannot close month with 0 transactions', which is the correct refusal."
        )
    assert_status_in(resp, (200, 409), "Close month")


def test_reopen_month(client: APIClient, profile: UserProfile):
    """Reopen the month `close_month` just closed.

    Strict on 200: this test only runs when close_month passed, so the month is
    closed and reopening it is the documented operation. Accepting 400 here was
    another verdict that could not fail.
    """
    if not profile.closes_month:
        not_applicable_to_persona(profile, "closes_month", "close and reopen months")

    resp = client.post(f"/api/reports/{profile.fiscal_year}/1/reopen")
    assert_status(resp, 200, "Reopen month")


def test_missing_receipts(client: APIClient, profile: UserProfile):
    """List transactions missing receipts."""
    data = client.missing_receipts(year=profile.fiscal_year, month=1)
    assert_key(data, "missing", "Missing receipts")
    assert_key(data, "count", "Missing receipts")


def test_system_status(client: APIClient, profile: UserProfile):
    """System status returns aggregate counts."""
    data = client.system_status()
    assert_key(data, "total_transactions", "System status")
    assert_key(data, "total_documents", "System status")
    assert_key(data, "unmatched_transactions", "System status")

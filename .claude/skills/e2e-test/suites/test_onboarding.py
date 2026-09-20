"""Onboarding wizard tests.

A session is a declared precondition (see the runner's registry), so nothing
here re-checks `client.token`. Optional integrations (Gmail, Wise, PayPal) need
credentials a test install does not have; the one integration check here is the
part that needs none — the endpoint refusing an empty token before it calls
anybody.
"""

from __future__ import annotations

from lib.api_client import APIClient
from lib.test_framework import NotExercised, assert_eq, assert_key, assert_status
from profiles import UserProfile


def test_onboarding_status_initial(client: APIClient, profile: UserProfile):
    """The onboarding status endpoint answers for this account."""
    data = client.onboarding_status()
    assert_key(data, "complete", "Onboarding status")
    # Not asserted false: personas share one account, which has been onboarded.


def test_save_profile(client: APIClient, profile: UserProfile):
    """Save business profile during onboarding."""
    data = client.save_profile(
        company_name=profile.company_name,
        fiscal_year=profile.fiscal_year,
        currency=profile.base_currency,
    )
    assert_eq(data.get("status"), "ok", "Save profile")


def test_save_profile_validation(client: APIClient, profile: UserProfile):
    """An empty company name is either rejected or accepted — and then restored.

    This records which behaviour exists and, crucially, puts the real company
    name back so later report assertions are not comparing against a blank
    profile.
    """
    resp = client.post("/api/onboarding/profile", {
        "company_name": "",
        "fiscal_year": profile.fiscal_year,
        "base_currency": profile.base_currency,
        "tax_jurisdiction": "CA",
    })
    assert resp.status_code in (200, 400, 422), (
        f"Empty company name returned HTTP {resp.status_code}: {resp.text[:200]}"
    )
    if resp.status_code == 200:
        restored = client.save_profile(
            profile.company_name, profile.fiscal_year, profile.base_currency)
        assert_eq(restored.get("status"), "ok", "Restore company name after the empty-name probe")


def test_onboarding_config(client: APIClient, profile: UserProfile):
    """Onboarding config returns profile and source status."""
    resp = client.get("/api/onboarding/config")
    assert_status(resp, 200, "Onboarding config")
    data = resp.json()
    assert_key(data, "profile", "Config response")
    assert_key(data, "sources", "Config response")


def test_wise_rejects_blank_token(client: APIClient, profile: UserProfile):
    """`POST /api/onboarding/wise/test` refuses a blank token without calling out.

    A blank token is rejected before any request leaves the machine, so this
    needs no Wise credential and no network. What it guards is the silent-success
    bug: an integration card that reports "Connected" for a token nobody gave it.
    """
    resp = client.post("/api/onboarding/wise/test", {"token": "   "})
    if resp.status_code == 429:
        raise NotExercised("the Wise test endpoint is rate-limited (5/minute) right now")
    assert_status(resp, 400, "Blank Wise token")
    config = client.get("/api/onboarding/config")
    assert_status(config, 200, "Onboarding config after the blank-token probe")


def test_complete_onboarding(client: APIClient, profile: UserProfile):
    """Mark onboarding as complete."""
    data = client.complete_onboarding()
    assert_eq(data.get("status"), "ok", "Complete onboarding")


def test_onboarding_status_after_completion(client: APIClient, profile: UserProfile):
    """After completion, status shows complete=true."""
    data = client.onboarding_status()
    assert_eq(data.get("complete"), True, "Onboarding complete flag")


def test_dashboard_accessible_after_onboarding(client: APIClient, profile: UserProfile):
    """Dashboard endpoint accessible after onboarding completion."""
    resp = client.get("/api/dashboard/summary")
    assert_status(resp, 200, "Dashboard after onboarding")

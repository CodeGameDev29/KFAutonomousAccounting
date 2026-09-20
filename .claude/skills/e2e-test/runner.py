#!/usr/bin/env python3
"""E2E API suite runner — drives every backend suite for each user persona.

This is the API half of the `e2e-test` skill; the browser journeys under
`journeys/` are the other half and prove the experience. A full run owes both.

It talks to a *running* app over HTTP and never imports the application, so it
is not part of the project's pytest suite and must not be collected by it.

Configuration comes from the environment (or the project `.env`):

    AA_E2E_BASE_URL   where the app is             (default http://localhost:8080)
    AA_E2E_EMAIL      the throwaway test account   (required)
    AA_E2E_PASSWORD   its password                 (required)

**The account must be a throwaway holding only synthetic data.** Unless
`--no-reset` is given, this runner calls `POST /api/actions/reset` before the
run, between personas and after it — that deletes every transaction, document,
match and ledger entry the account owns. Never point it at real books.

Usage (from the repo root, with the project's virtualenv active):
    python .claude/skills/e2e-test/runner.py                   # all personas
    python .claude/skills/e2e-test/runner.py --profile basic   # one persona
    python .claude/skills/e2e-test/runner.py --no-reset        # leave the account's data alone
    python .claude/skills/e2e-test/runner.py --url http://localhost:9000
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SKILL_DIR = Path(__file__).resolve().parent
# Only the skill directory goes on the path: the suites import `lib`, `profiles`
# and `suites` from here and nothing from the application.
sys.path.insert(0, str(SKILL_DIR))

# How long one reconciliation run may take before the client gives up on it.
# 90 minutes, because on a large account with a slow local model a run can
# genuinely take over an hour. The ceiling has to fit the worst case, not the
# median — the run continues server-side either way, so a short ceiling reports
# a slow machine as a broken engine. `setdefault`, so an explicit
# AA_E2E_RECONCILE_TIMEOUT still wins, and set before lib.api_client is
# imported: it reads this at import time.
os.environ.setdefault("AA_E2E_RECONCILE_TIMEOUT", "5400")

from lib.api_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    APIClient,
    default_base_url,
    signup_open,
    test_account,
)
from lib.test_framework import PROGRESS_FILE, TestRunner  # noqa: E402
from profiles import ALL_PROFILES, PROFILE_MAP, UserProfile  # noqa: E402
from suites import (  # noqa: E402
    test_auth,
    test_data_ingestion,
    test_onboarding,
    test_reconciliation,
    test_reports,
    test_security,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("e2e-api")

EXIT_PRECONDITION = 2


@dataclass(frozen=True)
class Case:
    """One test, and the tests that must have passed for it to mean anything.

    `deps` names earlier cases in this profile. When one of them failed or was
    not exercised, this case is recorded NOT-EXERCISED carrying that cause — the
    alternative is a receipt upload that "passes" on an HTTP 400 followed by
    twenty reconciliation FAILs with no data behind them.
    """

    name: str
    fn: Callable
    deps: tuple[str, ...] = field(default_factory=tuple)


# Test suites ordered by dependency: auth first, then onboarding, then data, etc.
# Order inside a suite is load-bearing wherever a case mutates state another
# reads — the review actions run *after* the idempotency check, because
# unmatching a pair deliberately makes it matchable again.
SUITE_REGISTRY: list[tuple[str, list[Case]]] = [
    ("auth", [
        Case("health_check", test_auth.test_health_check),
        Case("signup_when_open", test_auth.test_signup_when_open),
        # `login` is deliberately dependency-free: it establishes the session the
        # rest of the run depends on, so it has to be able to report its own verdict.
        Case("login", test_auth.test_login),
        Case("login_wrong_password", test_auth.test_login_wrong_password),
        Case("unauthenticated_access_blocked", test_auth.test_unauthenticated_access_blocked),
        Case("session_persistence", test_auth.test_session_persistence, ("login",)),
        Case("token_refresh", test_auth.test_token_refresh, ("login",)),
        Case("invalid_token_rejected", test_auth.test_invalid_token_rejected),
    ]),
    ("onboarding", [
        Case("onboarding_status_initial", test_onboarding.test_onboarding_status_initial, ("login",)),
        Case("save_profile", test_onboarding.test_save_profile, ("login",)),
        Case("save_profile_validation", test_onboarding.test_save_profile_validation, ("login",)),
        Case("onboarding_config", test_onboarding.test_onboarding_config, ("login",)),
        Case("wise_rejects_blank_token", test_onboarding.test_wise_rejects_blank_token, ("login",)),
        Case("complete_onboarding", test_onboarding.test_complete_onboarding, ("login",)),
        Case("onboarding_status_after_completion",
             test_onboarding.test_onboarding_status_after_completion, ("complete_onboarding",)),
        Case("dashboard_accessible_after_onboarding",
             test_onboarding.test_dashboard_accessible_after_onboarding, ("complete_onboarding",)),
    ]),
    ("data_ingestion", [
        Case("upload_bank_csv", test_data_ingestion.test_upload_bank_csv, ("login",)),
        Case("upload_bank_pdf", test_data_ingestion.test_upload_bank_pdf, ("login",)),
        Case("upload_receipt", test_data_ingestion.test_upload_receipt, ("login",)),
        Case("upload_duplicate_receipt", test_data_ingestion.test_upload_duplicate_receipt, ("login",)),
        Case("upload_invalid_file_type", test_data_ingestion.test_upload_invalid_file_type, ("login",)),
        Case("list_transactions", test_data_ingestion.test_list_transactions, ("login",)),
        Case("list_documents", test_data_ingestion.test_list_documents, ("login",)),
        Case("transaction_count", test_data_ingestion.test_transaction_count, ("login",)),
    ]),
    ("reconciliation", [
        Case("pre_reconciliation_baseline", test_reconciliation.test_pre_reconciliation_baseline,
             ("upload_bank_csv", "upload_receipt")),
        Case("run_reconciliation", test_reconciliation.test_run_reconciliation,
             ("pre_reconciliation_baseline",)),
        Case("transaction_statuses_changed", test_reconciliation.test_transaction_statuses_changed,
             ("run_reconciliation",)),
        Case("matched_transaction_has_receipt", test_reconciliation.test_matched_transaction_has_receipt,
             ("transaction_statuses_changed",)),
        Case("match_has_confidence_and_explanation",
             test_reconciliation.test_match_has_confidence_and_explanation, ("run_reconciliation",)),
        Case("reconciliation_summary", test_reconciliation.test_reconciliation_summary,
             ("run_reconciliation",)),
        Case("list_matches", test_reconciliation.test_list_matches, ("run_reconciliation",)),
        Case("list_pending_matches", test_reconciliation.test_list_pending_matches,
             ("run_reconciliation",)),
        # Before the review actions: reject and unmatch free their pairs, which a
        # correct engine then matches again on the next run.
        Case("reconciliation_idempotent", test_reconciliation.test_reconciliation_idempotent,
             ("run_reconciliation",)),
        Case("approve_match", test_reconciliation.test_approve_match, ("list_matches",)),
        Case("reject_match", test_reconciliation.test_reject_match, ("list_matches",)),
        Case("bulk_approve_all", test_reconciliation.test_bulk_approve_all, ("run_reconciliation",)),
        Case("unmatch_approved_match", test_reconciliation.test_unmatch_approved_match,
             ("run_reconciliation",)),
        Case("audit_log_records_actions", test_reconciliation.test_audit_log_records_actions,
             ("login",)),
    ]),
    ("reports", [
        Case("list_report_years", test_reports.test_list_report_years, ("login",)),
        Case("get_monthly_report", test_reports.test_get_monthly_report, ("login",)),
        Case("share_report", test_reports.test_share_report, ("login",)),
        Case("access_shared_report", test_reports.test_access_shared_report, ("share_report",)),
        Case("shared_report_invalid_token", test_reports.test_shared_report_invalid_token),
        Case("download_pdf_report", test_reports.test_download_pdf_report, ("login",)),
        Case("download_audit_binder", test_reports.test_download_audit_binder, ("login",)),
        Case("close_month", test_reports.test_close_month, ("login",)),
        Case("reopen_month", test_reports.test_reopen_month, ("close_month",)),
        Case("missing_receipts", test_reports.test_missing_receipts, ("login",)),
        Case("system_status", test_reports.test_system_status, ("login",)),
    ]),
    ("security", [
        Case("security_headers_present", test_security.test_security_headers_present),
        Case("referrer_policy_header", test_security.test_referrer_policy_header),
        Case("cors_not_wildcard", test_security.test_cors_not_wildcard),
        Case("docs_not_exposed", test_security.test_docs_not_exposed),
        Case("data_isolation_between_users", test_security.test_data_isolation_between_users, ("login",)),
        Case("idor_document_access", test_security.test_idor_document_access, ("login",)),
        Case("idor_transaction_access", test_security.test_idor_transaction_access, ("login",)),
        Case("reset_requires_confirmation", test_security.test_reset_requires_confirmation, ("login",)),
        Case("error_messages_not_leaking", test_security.test_error_messages_not_leaking, ("login",)),
        Case("rate_limiting_exists", test_security.test_rate_limiting_exists),
    ]),
]


def wait_for_server(url: str, timeout: int = 30) -> bool:
    """Wait for the backend server to be healthy."""
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def run_profile(runner: TestRunner, profile: UserProfile, base_url: str):
    """Execute all test suites for a single user profile.

    Each test is called with `(client)` or `(client, profile)` according to its
    own signature. Matching test *names* against hand-maintained lists instead
    is how a check ends up recording a TypeError as its verdict for months.
    """
    logger.info("=" * 60)
    logger.info("PROFILE: %s — %s", profile.name, profile.description)
    logger.info("  Pain points: %s", "; ".join(profile.pain_points[:2]))
    logger.info("=" * 60)

    runner.set_profile(profile.name, profile.description)
    client = APIClient(base_url)

    try:
        for suite_name, cases in SUITE_REGISTRY:
            runner.set_suite(suite_name)
            logger.info("── Suite: %s ──", suite_name)

            for case in cases:
                wants_profile = len(inspect.signature(case.fn).parameters) > 1
                args = (client, profile) if wants_profile else (client,)
                runner.run_test(case.name, case.fn, *args, depends_on=case.deps)
    finally:
        client.close()


def reset_account(base_url: str, email: str, password: str):
    """Wipe every transaction, document, match and ledger entry the account owns.

    Personas share one account, so this runs between them as well as before and
    after: without it persona 2 inherits persona 1's ledger and every "expect N
    transactions" assertion drifts. Uploads are content-hash deduped, so
    leftovers also swallow the next persona's fixtures silently.
    """
    client = APIClient(base_url)
    try:
        result = client.login(email, password)
        if "error" in result or not client.token:
            logger.warning("  Could not sign in to reset the test account: %s", result.get("error"))
            return
        deleted = (client.reset_data() or {}).get("deleted") or {}
        logger.info("  Reset test account data: %s",
                    ", ".join(f"{t}={n}" for t, n in sorted(deleted.items()) if n) or "nothing")
        # Proof, not trust: a reset that answered "ok" while a DELETE was rolled
        # back leaves the next persona reconciling rows it never uploaded. This
        # check is what shows the account the run is about to assert counts
        # against is actually empty.
        left = client.system_status()
        if left.get("total_transactions") or left.get("total_documents"):
            logger.warning(
                "  The test account is STILL not empty — %s transaction(s) and %s document(s) "
                "remain. Every count assertion after this reads against data this run did "
                "not upload.", left.get("total_transactions"), left.get("total_documents"))
        else:
            logger.info("  The test account is empty: 0 transactions, 0 documents")
    except Exception as e:
        logger.warning("  Reset failed: %s", e)
    finally:
        client.close()


def _stop(message: str, code: int):
    logger.error(message)
    try:
        PROGRESS_FILE.write_text('{"status": "error", "error": %s}' % _json_str(message))
    except Exception:
        pass
    sys.exit(code)


def _json_str(text: str) -> str:
    import json
    return json.dumps(text)


def main():
    parser = argparse.ArgumentParser(
        description="E2E API suite runner. Needs AA_E2E_EMAIL and AA_E2E_PASSWORD for a "
                    "THROWAWAY account holding synthetic data only: unless --no-reset is given, "
                    "the run deletes everything that account owns.")
    parser.add_argument("--profile", choices=list(PROFILE_MAP.keys()),
                        help="Run a single profile instead of all")
    parser.add_argument("--api-only", action="store_true",
                        help="No-op: this runner is the API half. Accepted so a skill-level "
                             "flag can be forwarded without argparse exiting.")
    parser.add_argument("--url", default=None,
                        help=f"Backend base URL (default: AA_E2E_BASE_URL, else {DEFAULT_BASE_URL})")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Skip the final reset after the last profile, leaving the last "
                             "persona's data in place for the browser journeys")
    parser.add_argument("--no-reset", dest="fresh", action="store_false", default=True,
                        help="Never call POST /api/actions/reset: no pre-run reset, none between "
                             "personas, no cleanup. Use it when the account holds data worth "
                             "keeping (a browser journey's corpus, or another run in progress). "
                             "The cost is that personas then see each other's rows.")
    args = parser.parse_args()
    base_url = (args.url or default_base_url()).rstrip("/")

    # Preconditions, before anything can be claimed to have been tested: a
    # missing credential is one setup problem, not sixty product failures.
    e2e_email, e2e_password = test_account()
    missing = [name for name, value in (("AA_E2E_EMAIL", e2e_email),
                                        ("AA_E2E_PASSWORD", e2e_password)) if not value]
    if missing:
        _stop(
            f"{' and '.join(missing)} not set. Export AA_E2E_EMAIL and AA_E2E_PASSWORD (or put "
            "them in the project .env) for a THROWAWAY account that holds only synthetic data — "
            "this suite uploads, mutates and deletes everything in it. To create one: set "
            "AUTH_ALLOW_SIGNUP=1, restart the server, sign up in the browser, set it back to 0. "
            "Refusing to run: a suite that cannot sign in proves nothing.",
            EXIT_PRECONDITION,
        )

    logger.info("Checking server at %s ...", base_url)
    if not wait_for_server(base_url):
        _stop(f"Server at {base_url} not healthy after 30s — start it with "
              "`python -m uvicorn server.app:app --port 8080`, or set AA_E2E_BASE_URL.", 1)
    logger.info("Server healthy!")

    profiles = [PROFILE_MAP[args.profile]] if args.profile else ALL_PROFILES

    if signup_open(base_url):
        logger.info("Registration is open — the signup and cross-account isolation checks will "
                    "create throwaway accounts at a reserved documentation domain.")
    else:
        logger.info("Registration is closed — the signup and cross-account isolation checks "
                    "will be NOT-EXERCISED.")

    runner = TestRunner()
    runner.start()

    try:
        if args.fresh:
            # Fresh state before running: prevents "duplicate" rejections and
            # stale data from prior runs from being read as product behaviour.
            logger.info("── Fresh start: resetting the test account's data ──")
            reset_account(base_url, e2e_email, e2e_password)
        else:
            logger.warning(
                "── --no-reset: the account keeps whatever data it already holds ──")
            logger.warning(
                "    Personas share one account, so later personas see earlier ones' rows; "
                "count assertions are read against that. Per-run fixtures stay unique so "
                "nothing is swallowed by dedup.")

        for i, profile in enumerate(profiles):
            if i and args.fresh:
                # Personas share the account: hand the next one an empty ledger.
                reset_account(base_url, e2e_email, e2e_password)
            run_profile(runner, profile, base_url)

        if args.fresh and not args.no_cleanup:
            reset_account(base_url, e2e_email, e2e_password)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception("Runner crashed: %s", e)
    finally:
        runner.finish()

    # Exit code: 0 if nothing failed, 1 if anything did. NOT-EXERCISED results
    # are neither credit nor failure - they are listed in the report by name.
    failed = sum(1 for r in runner._all_results if r.status == "failed")
    not_exercised = sum(1 for r in runner._all_results if r.status == "not-exercised")
    if not_exercised:
        logger.info("%d test(s) NOT-EXERCISED - see the report for each reason", not_exercised)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()

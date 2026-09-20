"""User profile definitions for E2E testing.

Each profile is a distinct persona — pain points, how they get data in, how they
review, which edge cases they hit. A persona is a *behaviour*, not an account:
registration is closed by default, so all of them drive the one throwaway test
account (`AA_E2E_EMAIL` / `AA_E2E_PASSWORD`) one after another, which is why the
runner clears that account's data between profiles.

Every company name here is invented, in the style of `tests/fixtures/`.
"""

from dataclasses import dataclass, field

from lib.api_client import test_account


@dataclass
class UserProfile:
    name: str
    description: str
    company_name: str
    fiscal_year: int
    base_currency: str

    # Data ingestion
    uploads_bank_csv: bool = False
    uploads_bank_pdf: bool = False
    uploads_receipts: bool = False

    # Workflow
    runs_reconciliation: bool = True
    reviews_matches: bool = True
    approves_all: bool = False  # bulk approve vs individual
    downloads_report: bool = True
    shares_report: bool = False
    closes_month: bool = False

    # Edge cases to test
    test_duplicate_upload: bool = False
    test_invalid_file: bool = False

    # Pain points (documented for report context)
    pain_points: list[str] = field(default_factory=list)

    @property
    def email(self) -> str:
        """The address to sign in with — always the shared test account."""
        return test_account()[0] or ""

    @property
    def password(self) -> str:
        return test_account()[1] or ""


# ── Profile A: Basic Small Business Owner ──────────────────────────────
# Pain: "I just want my books done. Don't make me think."
# Flow: Sign in → quick onboarding → upload CSV → reconcile → export
PROFILE_BASIC = UserProfile(
    name="basic",
    description="Basic small biz owner — CSV upload, quick reconcile, minimal config",
    company_name="Example Consulting Inc.",
    fiscal_year=2026,
    base_currency="CAD",
    uploads_bank_csv=True,
    uploads_receipts=True,
    runs_reconciliation=True,
    reviews_matches=True,
    approves_all=True,
    downloads_report=True,
    test_duplicate_upload=True,
    pain_points=[
        "Wants zero complexity — upload CSV, click match, download report",
        "Gets confused by integration jargon such as 'OAuth' or 'API token'",
        "Needs reassurance that data stays on the machine running the app",
        "Monthly time budget: 5 minutes max",
    ],
)

# ── Profile B: Power User ──────────────────────────────────────────────
# Pain: "I review every match myself and I share the result."
# Flow: Sign in → upload statement + receipts → reconcile → review one by one → share report
PROFILE_FULL = UserProfile(
    name="full",
    description="Power user — individual review, corrections, shared reports, month close",
    company_name="Example Digital Services Inc.",
    fiscal_year=2026,
    base_currency="CAD",
    uploads_bank_csv=True,
    uploads_receipts=True,
    runs_reconciliation=True,
    reviews_matches=True,
    approves_all=False,  # reviews individually
    downloads_report=True,
    shares_report=True,
    closes_month=True,
    pain_points=[
        "Multi-currency (CAD + USD) complicates everything",
        "Transfers show as both a debit and a credit across accounts",
        "Wants to share a monthly report with an accountant via link",
        "Wants every optional integration to say plainly when it is not configured",
    ],
)

# ── Profile C: Manual-Only Skeptic ─────────────────────────────────────
# Pain: "I don't trust connecting anything. I'll upload everything myself."
# Flow: Sign in → skip everything → manual CSV + PDF upload → manual receipt upload → reconcile
PROFILE_MANUAL = UserProfile(
    name="manual",
    description="Privacy-first skeptic — no integrations, manual uploads only, error paths",
    company_name="Example Privacy Consulting",
    fiscal_year=2026,
    base_currency="CAD",
    uploads_bank_csv=True,
    uploads_bank_pdf=True,
    uploads_receipts=True,
    runs_reconciliation=True,
    reviews_matches=True,
    approves_all=False,
    downloads_report=True,
    test_invalid_file=True,
    test_duplicate_upload=True,
    pain_points=[
        "Doesn't trust third-party API connections — wants full local control",
        "Uploads both CSV and PDF bank statements (different months)",
        "Tests error handling with corrupt/invalid files",
        "Wants to verify no data leaks to external services",
    ],
)

# ── Profile D: Bookkeeper ──────────────────────────────────────────────
# Pain: "Each client needs CRA-ready exports and a closed month."
# Flow: Sign in → profile → upload → reconcile → PDF + ZIP export → close month
PROFILE_BOOKKEEPER = UserProfile(
    name="bookkeeper",
    description="Bookkeeper — CRA record-keeping focus, PDF/ZIP exports, month close workflow",
    company_name="Example Bookkeeping Professional Corp",
    fiscal_year=2026,
    base_currency="CAD",
    uploads_bank_csv=True,
    uploads_receipts=True,
    runs_reconciliation=True,
    reviews_matches=True,
    approves_all=False,
    downloads_report=True,
    shares_report=True,
    closes_month=True,
    pain_points=[
        "Needs an audit binder (unencrypted ZIP with organized receipts)",
        "Must close months to prevent accidental edits",
        "Shares reports with business owners via time-limited links",
        "Needs a detailed audit log of all changes for accountability",
    ],
)

ALL_PROFILES = [
    PROFILE_BASIC,
    PROFILE_FULL,
    PROFILE_MANUAL,
    PROFILE_BOOKKEEPER,
]

PROFILE_MAP = {p.name: p for p in ALL_PROFILES}


def personas_with(flag: str) -> list[str]:
    """Names of the personas whose profile sets `flag` True.

    Used when a test records NOT-EXERCISED for a persona that deliberately
    skips a surface: the reason then names who *does* cover it, so a reader can
    tell a persona choice from a coverage hole.
    """
    return [p.name for p in ALL_PROFILES if getattr(p, flag, False)]

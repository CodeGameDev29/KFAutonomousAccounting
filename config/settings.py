"""Centralized configuration — all API keys, tokens, and settings in one place."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from config.ised_categories import CATEGORY_NAMES

# override=True on purpose: .env is the file that is edited, so it wins over
# whatever happens to be exported in the shell that started the process.
# Without it an ambient OPENAI_API_KEY would beat an explicitly empty
# OPENAI_API_KEY= here and documents would go to a hosted provider that .env
# switched off. An empty value in .env means off.
load_dotenv(override=True)

# ─── Base Paths ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / os.getenv("DATA_DIR", "data")
DB_PATH = PROJECT_ROOT / os.getenv("DB_PATH", "autonomous_accounting.db")

# Peer-benchmark engine static data (in-repo, read-only,
# lru_cache'd once per process — never fetched from a network at request time).
BENCHMARK_DATA_PATH = PROJECT_ROOT / os.getenv(
    "BENCHMARK_DATA_PATH", "config/ised_benchmarks.json"
)
GIFI_MAPPING_PATH = PROJECT_ROOT / os.getenv(
    "GIFI_MAPPING_PATH", "config/aa_category_gifi_map.yaml"
)


def _env_flag(name: str, default: str = "0") -> bool:
    """Read a boolean env var. "1", "true", "yes" and "on" are true, case-free."""
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _llm_env(name: str, legacy: str, default: str) -> str:
    """Read an LLM endpoint setting, accepting the older ``LOCAL_LLM_*`` spelling.

    The canonical names are ``LLM_BASE_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY``;
    ``LOCAL_LLM_*`` is still honoured so an existing .env keeps working.
    """
    return os.getenv(name, "") or os.getenv(legacy, "") or default


@dataclass(frozen=True)
class LocalLLMConfig:
    """The locally hosted model that is the engine's PRIMARY extraction provider.

    Speaks the OpenAI chat-completions protocol and runs on hardware you
    already have, which is why it leads the ladder and Gemini/OpenAI are
    optional fallbacks that only engage when their key is non-empty.

    Any OpenAI-compatible server will do (vLLM, llama.cpp, Ollama's compat
    endpoint, LM Studio): point ``LLM_BASE_URL`` at it and set ``LLM_MODEL`` to
    the name it serves. A vision-capable model is required for receipt and
    statement extraction. This project hosts no model of its own — it is only a
    client of that endpoint, and the only requirement is that ``base_url``
    speaks /v1/chat/completions. When it does not answer, uploads fail with a
    503 that says so (see core/llm_errors.py); nothing silently falls through.

    `api_key` is a dummy by default: the openai client refuses to construct
    without a non-empty key, and most local servers do not check it.
    """

    base_url: str = _llm_env("LLM_BASE_URL", "LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    model: str = _llm_env("LLM_MODEL", "LOCAL_LLM_MODEL", "primary")
    api_key: str = _llm_env("LLM_API_KEY", "LOCAL_LLM_API_KEY", "local")
    # Generous next to the hosted providers' 45s: a locally hosted model is
    # slower per token than a hosted one. It can exceed a request timeout in
    # front of the app, in which case the work still completes server-side
    # after the browser has given up.
    timeout_s: float = float(os.getenv("LOCAL_LLM_TIMEOUT_S", "180"))
    # Servers that batch concurrent requests into one forward pass (vLLM and
    # friends) gain throughput from several extraction calls in flight: a
    # page-batched receipt or a multi-page statement is several independent
    # calls, and serialising them wastes the batcher. Beyond a small number of
    # concurrent vision calls, per-request latency grows faster than throughput
    # does, so the default is modest rather than "as many as the client can
    # open". Raise it to suit the endpoint.
    max_concurrent: int = int(os.getenv("LOCAL_LLM_MAX_CONCURRENT", "4"))
    # The engine's *reconciliation* calls are a different shape of work from
    # document extraction and saturate an endpoint later. An extraction call is
    # a vision prompt of several thousand tokens that decodes a whole receipt;
    # a matcher call is a short JSON classification, so more of them fit in one
    # batch before throughput stops improving. Hence a separate, higher ceiling
    # instead of a reuse of ``max_concurrent``.
    engine_max_concurrent: int = int(os.getenv("LOCAL_LLM_ENGINE_MAX_CONCURRENT", "10"))
    # Some reasoning models emit <think> blocks by default. They cost seconds
    # and tokens and break strict JSON parsing, so extraction turns them off.
    disable_thinking: bool = _env_flag("LOCAL_LLM_DISABLE_THINKING", "1")

    @property
    def enabled(self) -> bool:
        """A base URL is the whole configuration — there is no key to check."""
        return bool(self.base_url)

    @property
    def extra_body(self) -> dict:
        """Vendor-specific knobs for disabling chain-of-thought.

        vLLM honours `chat_template_kwargs.enable_thinking`; other
        OpenAI-compatible servers honour a top-level `think`. Both ignore keys
        they don't know, so sending both is safe and keeps the code
        host-agnostic.
        """
        if not self.disable_thinking:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": False}, "think": False}


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    # Explicit override for fallback scenarios (e.g. statement parser secondary path)
    fallback_model: str = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1")
    # Both hosted providers are optional now. An empty key means "skip me"
    # rather than "401 on every upload".
    enabled: bool = bool(os.getenv("OPENAI_API_KEY", ""))


# The Gemini model this project is written against, and the rung any pinned
# model falls back to when it is unavailable. See model_candidates.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# Fallback ladder, most capable rung first.
#
# Gemini's daily request quota is granted per project per model, so each model
# has its own independent bucket and walking a ladder of N models makes N
# buckets available instead of one.
#
# Only the first rung is treated as trustworthy (see ``trusted_models``): the
# rungs below it are there to keep extraction working when the first one is out
# of quota, and they are more prone to a misread vendor, date or total.
# ``extract_document`` records which rung answered and the upload path forces
# those documents to human review rather than auto-accepting them, because a
# silently wrong number in a tax filing is worse than a blank one.
DEFAULT_GEMINI_MODEL_LADDER = [
    DEFAULT_GEMINI_MODEL,
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


def _parse_model_ladder(raw: str) -> list[str]:
    """Parse a comma-separated GEMINI_MODEL_LADDER override into a clean list."""
    return [m.strip() for m in raw.split(",") if m.strip()]


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str = os.getenv("GEMINI_API_KEY", "")
    # The default is the model this project is written against. A provider can
    # withdraw a model's quota without notice — when that happens every call is
    # rejected with a 429 and, without the ladder below, extraction would fall
    # straight through to the next provider. `model_candidates` exists for
    # exactly that case.
    model: str = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    enabled: bool = bool(os.getenv("GEMINI_API_KEY", ""))
    max_concurrent_extractions: int = int(os.getenv("LLM_MAX_CONCURRENT_EXTRACTIONS", "5"))
    @property
    def model_candidates(self) -> list[str]:
        """Gemini models to try, in order, before giving up on the provider.

        Two jobs. First, a pinned GEMINI_MODEL can have its quota withdrawn,
        and falling straight through to the next provider costs accuracy.
        Second, each model has its own daily quota, so walking the ladder
        extends how many requests are available in a day.

        Env stays authoritative for *preference*: GEMINI_MODEL goes first, then
        the ladder. Set GEMINI_MODEL_LADDER (comma-separated) to override the
        rest, or to a single value to disable laddering entirely.
        """
        ladder = _parse_model_ladder(os.getenv("GEMINI_MODEL_LADDER", "")) or DEFAULT_GEMINI_MODEL_LADDER
        seen: list[str] = []
        for name in (self.model, *ladder):
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def trusted_models(self) -> list[str]:
        """Models whose output may be auto-accepted without human review.

        The configured model plus the model this project is written against.
        Everything else in ``model_candidates`` is a fallback rung — present
        only to keep extraction working when the preferred models are out of
        quota — and is more prone to a misread field, so its output is held for
        review instead.

        Deliberately NOT ``model_candidates[0]``: a pinned model whose quota has
        since been withdrawn must not demote the default to a fallback rung.
        """
        seen: list[str] = []
        for name in (self.model, DEFAULT_GEMINI_MODEL):
            if name and name not in seen:
                seen.append(name)
        return seen


@dataclass(frozen=True)
class ReconciliationConfig:
    # Hard maximum number of days between a proof of transaction's date and a
    # bank transaction's date before the pair is rejected outright, whatever
    # the amount says. This is the fallback the API uses when an account has
    # not set its own ``max_date_variance_days`` in its business profile, and
    # it is the value the matcher's ``max_date_days`` argument carries.
    date_window_days: int = int(os.getenv("RECONCILIATION_DATE_WINDOW_DAYS", "45"))
    auto_approve_threshold: float = float(
        os.getenv("RECONCILIATION_AUTO_APPROVE_THRESHOLD", "0.90")
    )
    review_threshold: float = float(
        os.getenv("RECONCILIATION_REVIEW_THRESHOLD", "0.60")
    )
    # Floor for POST /api/transactions/reviews/approve-all. A bulk approval is
    # a human accepting a batch sight unseen, so it may only settle matches the
    # matcher was already confident about; anything below this stays in the
    # review queue for a pair of eyes. A request may raise this bar but never
    # lower it.
    bulk_approve_min_confidence: float = float(
        os.getenv("RECONCILIATION_BULK_APPROVE_MIN_CONFIDENCE", "0.85")
    )
    amount_weight: float = float(os.getenv("RECONCILIATION_AMOUNT_WEIGHT", "0.5"))
    date_weight: float = float(os.getenv("RECONCILIATION_DATE_WEIGHT", "0.3"))
    vendor_weight: float = float(os.getenv("RECONCILIATION_VENDOR_WEIGHT", "0.2"))
    # ── The amount agent's model call, off by default ──────────────────
    #
    # Pass 1 of the matcher is a union: the deterministic amount shortlist
    # (exact figure, wire-fee window, converted amount on the receipt, USD
    # amount inside a credit-card description, CAD/USD band) merged with
    # whatever the model shortlists. The model call is the most expensive step
    # of the matching phase.
    #
    # Because the pass is a union, switching it off can only remove candidates
    # the model alone proposed — every deterministic candidate is still
    # shortlisted — so the default keeps the accuracy the deterministic rules
    # deliver. The code path stays: set RECONCILIATION_AMOUNT_AGENT_LLM=1 and
    # pass 1 asks the model again and merges its answer in.
    amount_agent_llm: bool = _env_flag("RECONCILIATION_AMOUNT_AGENT_LLM", "0")
    fx_cad_usd_min: float = float(os.getenv("FX_CAD_USD_MIN", "1.20"))
    fx_cad_usd_max: float = float(os.getenv("FX_CAD_USD_MAX", "1.55"))
    wire_fee_tolerance: float = float(os.getenv("WIRE_FEE_TOLERANCE", "25.00"))


def _scrutiny_multipliers() -> dict[str, float]:
    """GIFI buckets a tax authority looks at closely get a higher weight even
    at moderate deviation. A frozen dataclass cannot hold a mutable default, so
    this factory seeds it."""
    return {
        "travel": 2.5,
        "meals_entertainment": 2.5,
        "motor_vehicle": 2.0,
        "cca_depreciation": 1.8,
        "subcontracts": 1.5,
        "professional_fees": 1.2,
        "distribution": 1.5,
    }


@dataclass(frozen=True)
class ReasonablenessConfig:
    """Tunables for the deterministic peer-benchmark engine (core/reasonableness.py).

    All env-overridable. Ratios are fractions of operating revenue (0..1),
    not percentages.
    """

    # Revenue floor / period guardrails
    revenue_floor: float = float(os.getenv("REASONABLENESS_REVENUE_FLOOR", "30000"))
    partial_year_min_days: int = int(
        os.getenv("REASONABLENESS_PARTIAL_YEAR_MIN_DAYS", "335")
    )
    # Dollar/percent materiality — suppress tiny lines with big % deviations
    materiality_floor_pct: float = float(
        os.getenv("REASONABLENESS_MATERIALITY_FLOOR_PCT", "0.02")
    )
    materiality_floor_abs: float = float(
        os.getenv("REASONABLENESS_MATERIALITY_FLOOR_ABS", "500")
    )
    materiality_ref_pct: float = float(
        os.getenv("REASONABLENESS_MATERIALITY_REF_PCT", "0.10")
    )
    materiality_floor_frac: float = float(
        os.getenv("REASONABLENESS_MATERIALITY_FLOOR_FRAC", "0.25")
    )
    # Deviation classification (standard-deviations-ish, in spread units)
    elevated_cutoff: float = float(os.getenv("REASONABLENESS_ELEVATED_CUTOFF", "1.0"))
    stands_out_cutoff: float = float(
        os.getenv("REASONABLENESS_STANDS_OUT_CUTOFF", "2.0")
    )
    # NO-OP: the engine NEVER fabricates a peer spread — absent bounds mean
    # the line is suppressed. Retained ONLY so an existing env key does not
    # error; referenced nowhere in the evaluation path.
    default_spread_pct: float = float(
        os.getenv("REASONABLENESS_DEFAULT_SPREAD_PCT", "0.5")
    )
    # NO-OP for band fabrication; retained solely as a divide-by-zero floor in
    # the deterministic weight math (the published IQR/2 half-spread).
    min_spread: float = float(os.getenv("REASONABLENESS_MIN_SPREAD", "0.01"))
    # ── Data-honesty / density gating ──────────────────────────────────
    # StatCan's own quartile-suppression bar: cohorts with fewer filing
    # businesses are suppressed at READ time (never fabricated). Env-tunable
    # without a re-ingest (lru_cache is process-lifetime → restart to apply).
    min_business_count: int = int(
        os.getenv("REASONABLENESS_MIN_BUSINESS_COUNT", "20")
    )
    # Status classification off published bounds (no fabricated spread):
    #   r > q3 + k·IQR → well_above ;  r < q1 − k·IQR → well_below.
    well_beyond_iqr_mult: float = float(
        os.getenv("REASONABLENESS_WELL_BEYOND_IQR_MULT", "1.0")
    )
    # Median-only (no q1/q3 band) relative tolerance for above/below.
    median_only_tolerance_pct: float = float(
        os.getenv("REASONABLENESS_MEDIAN_ONLY_TOLERANCE_PCT", "0.15")
    )
    # Always-on profitability cohort key — the "most-profitable quarter" peer.
    profitability_cohort: str = os.getenv(
        "REASONABLENESS_PROFITABILITY_COHORT", "pm_q4"
    )
    # Output shaping
    top_n_flags: int = int(os.getenv("REASONABLENESS_TOP_N_FLAGS", "5"))
    several_stands_out: int = int(
        os.getenv("REASONABLENESS_SEVERAL_STANDS_OUT", "2")
    )
    several_total_flags: int = int(
        os.getenv("REASONABLENESS_SEVERAL_TOTAL_FLAGS", "4")
    )
    # Layer-B named-pattern thresholds
    travel_abs: float = float(os.getenv("REASONABLENESS_TRAVEL_ABS", "0.05"))
    meals_abs: float = float(os.getenv("REASONABLENESS_MEALS_ABS", "0.05"))
    vehicle_abs: float = float(os.getenv("REASONABLENESS_VEHICLE_ABS", "0.05"))
    contractor_skew_frac: float = float(
        os.getenv("REASONABLENESS_CONTRACTOR_SKEW_FRAC", "0.60")
    )
    contractor_skew_min_rev: float = float(
        os.getenv("REASONABLENESS_CONTRACTOR_SKEW_MIN_REV", "0.25")
    )
    generic_share_frac: float = float(
        os.getenv("REASONABLENESS_GENERIC_SHARE_FRAC", "0.50")
    )
    # FX fallback used when a currency has no BoC rate (never silently sum)
    fx_fallback: float = float(os.getenv("REASONABLENESS_FX_FALLBACK", "1.35"))
    scrutiny_multipliers: dict[str, float] = field(default_factory=_scrutiny_multipliers)


@dataclass(frozen=True)
class AppConfig:
    fiscal_year: int = int(os.getenv("FISCAL_YEAR", "2026"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    db_path: Path = field(default_factory=lambda: DB_PATH)


# ─── Server Ports (single source of truth — reads from .env PORT) ─────
# Single source of truth for the port. Every script, the dev-server proxy and
# the OAuth redirect default derive from it, so a checkout with no .env still
# points every tool at the same place.
SERVER_PORT = int(os.getenv("PORT", "8080"))


# ─── Expense Categories ─────────────────────────────────────────────
# Fixed Canada ISED/GIFI taxonomy is the single source of truth. See
# config/ised_categories.py. EXPENSE_CATEGORIES is kept as an alias so legacy
# import sites (server/api/actions.py, core/extraction.py build prompt, the
# transactions PATCH fallback) keep working with canonical names.
EXPENSE_CATEGORIES = list(CATEGORY_NAMES)

# ─── Supported File Types ───────────────────────────────────────────
SUPPORTED_RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp"}
SUPPORTED_CSV_EXTENSIONS = {".csv"}


# ─── Upload size cap (single source of truth) ───────────────────────
# Every endpoint that accepts a file reads its limit from here: receipts,
# bank statements, note attachments and the generic file-upload route. One
# number so the API, the error messages and the /health contract the frontend
# reads can never drift apart. Raise it if the extraction pipeline can afford
# the memory — the cost is the page-batched render of a very long PDF, not the
# transfer.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# ─── Month Mapping ──────────────────────────────────────────────────
MONTH_ABBREVS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def get_month_dir(year: int, month: int) -> Path:
    """Return the canonical month directory path, e.g. '202601 Jan'."""
    abbrev = MONTH_ABBREVS[month]
    dir_name = f"{year}{month:02d} {abbrev}"
    return DATA_DIR / dir_name


# ─── Receipt page rendering ─────────────────────────────────────────
# How many pixels a *scanned* receipt page is rendered at before it goes to the
# vision model (core/extraction.py's batched path).
#
# A long scanned invoice is read four pages at a time. The first four render at
# RECEIPT_FIRST_PAGES_DPI and everything after them at RECEIPT_BATCH_DPI,
# because the vision tower downsamples to a fixed tile budget and the extra
# pixels normally only cost render memory.
#
# "Normally" is the catch, and it is why RECEIPT_RETRY_DPI exists. A faint
# scan — grey toner, a photocopy of a photocopy, a fading thermal receipt —
# can survive the higher DPI and vanish at the lower one: the downsample blends
# pale strokes into the page white and the batch comes back with no vendor, no
# amount and no continuation marker, i.e. it read nothing at all. That batch is
# re-rendered once at RECEIPT_RETRY_DPI before its empty answer is accepted.
# One extra call on a page that would otherwise be lost is a trade the engine
# always takes: a missing figure is a hole in a tax filing.
#
# RECEIPT_RETRY_DPI carries a second, stricter rule. Reading *nothing* is the
# obvious failure at 150 DPI; reading the *wrong number* is the dangerous one.
# So a batch that claims to see the document's grand total, rendered at
# RECEIPT_BATCH_DPI, is re-rendered at RECEIPT_RETRY_DPI and asked again before
# its figure is accepted, and two batches that disagree on the total are both
# re-read before the disagreement is reported. Those two rules share a budget of
# core.extraction.RECEIPT_TOTAL_VERIFY_MAX_CALLS extra calls per document, and
# the result records `total_verified_at_dpi`.
RECEIPT_FIRST_PAGES_DPI = int(os.getenv("RECEIPT_FIRST_PAGES_DPI", "200"))
RECEIPT_BATCH_DPI = int(os.getenv("RECEIPT_BATCH_DPI", "150"))
RECEIPT_RETRY_DPI = int(os.getenv("RECEIPT_RETRY_DPI", "200"))


@dataclass(frozen=True)
class StatementParsingConfig:
    """Configuration for the LLM-based generic bank statement parser."""

    # A vision model downsamples to a fixed tile budget, so rendering a
    # statement page above this adds no detail the model can use and costs
    # peak render memory.
    dpi: int = int(os.getenv("LLM_STATEMENT_DPI", "150"))
    # Statements stay on the faster flash model deliberately. A statement is a
    # machine-generated PDF with ruled columns, so a model's perception
    # advantage on a crumpled thermal receipt buys nothing here, while its
    # extra latency is charged to a user waiting on an upload.
    gemini_model: str = os.getenv("LLM_STATEMENT_GEMINI_MODEL", "gemini-2.5-flash")
    openai_model: str = os.getenv("LLM_STATEMENT_OPENAI_MODEL", "gpt-4.1")
    thinking_budget: int = int(os.getenv("LLM_STATEMENT_THINKING_BUDGET", "512"))
    balance_tolerance_cents: int = int(
        os.getenv("LLM_STATEMENT_BALANCE_TOLERANCE_CENTS", "2")
    )

    @property
    def gemini_model_candidates(self) -> list[str]:
        """Statement models to try before conceding the provider.

        Same reasoning as GeminiConfig.model_candidates, and it bites harder
        here: a multi-page statement spends one call per page batch, so a
        single upload can use up a day's quota on its own.
        """
        seen: list[str] = []
        for name in (self.gemini_model, DEFAULT_GEMINI_MODEL):
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def balance_tolerance(self) -> Decimal:
        """Return balance tolerance as a Decimal dollar amount (default $0.02)."""
        return Decimal(self.balance_tolerance_cents) / Decimal(100)


# ─── Singletons ─────────────────────────────────────────────────────
local_llm_config = LocalLLMConfig()
openai_config = OpenAIConfig()
gemini_config = GeminiConfig()
reconciliation_config = ReconciliationConfig()
reasonableness_config = ReasonablenessConfig()
app_config = AppConfig()
statement_parsing_config = StatementParsingConfig()


def extraction_provider_ladder() -> list[str]:
    """Providers the extraction engine will try, in order.

    Local first because it is free, private and always available; the two
    hosted providers only appear when they actually have a key. An empty list
    means extraction cannot run at all — callers should say so plainly rather
    than let a client 401.
    """
    ladder: list[str] = []
    if local_llm_config.enabled:
        ladder.append("local")
    if gemini_config.enabled:
        ladder.append("gemini")
    if openai_config.enabled:
        ladder.append("openai")
    return ladder


def trusted_extraction_models() -> list[str]:
    """Models whose output may be auto-accepted without human review.

    Provider-agnostic superset of ``GeminiConfig.trusted_models``: the local
    model is the primary provider, so its output is auto-accepted on the same
    terms as the configured Gemini model. Everything else — the fallback rungs
    — is held for a human.
    """
    models: list[str] = []
    if local_llm_config.enabled and local_llm_config.model:
        models.append(local_llm_config.model)
    for name in gemini_config.trusted_models:
        if name not in models:
            models.append(name)
    return models

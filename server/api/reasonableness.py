"""CRA Reasonableness / Peer-Benchmark API.

Serves the Analytics "How you compare to similar businesses" card. Given the
user's category totals + their NAICS code, the deterministic engine
(``core.reasonableness``) compares expense/income ratios against ISED FPD peer
benchmarks and returns a coarse 3-band verdict + a ranked, dismissible list of
"worth double-checking" prompts.

**This is a bookkeeping sanity-check / peer comparison, never an audit-risk
score**, and every string it returns is worded that way. Ships dark behind the
``reasonableness_engine`` feature flag — OFF returns the empty ``no_data`` shape
(never 403) so the card hides cleanly.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from datetime import date
from typing import List, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from config.settings import BENCHMARK_DATA_PATH, GIFI_MAPPING_PATH
from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db, is_enabled

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reasonableness", tags=["reasonableness"])

_FLAG_ID_RE = re.compile(r"^[a-z0-9_]{1,128}$")
_FEATURE_FLAG = "reasonableness_engine"
_ATTRIBUTION_FALLBACK = (
    "Contains information licensed under the Open Government Licence – Canada. "
    "Source: ISED/StatCan Financial Performance Data."
)


# ── Pydantic models (field names match the frontend TypeScript 1:1) ──────────

class ReasonablenessFlag(BaseModel):
    id: str
    category: str
    severity: Literal["stands_out", "elevated", "info"]
    direction: Literal["high", "low", "n/a"]
    title: str
    prompt: str
    your_pct: Optional[float]
    peer_pct: Optional[float]
    peer_band: Optional[List[float]]
    amount: Optional[float]
    currency: str
    dismissed: bool
    # No percentile fields — peer_pct + peer_band carry the comparison.
    business_count: Optional[int] = None
    recommendation: Optional[str] = None


class ReasonablenessComparison(BaseModel):
    label: str
    category: str
    gifi_bucket: str
    your_pct: float
    peer_pct: Optional[float]           # median-first central value; null ⇒ suppressed / no line
    peer_band: Optional[List[float]]    # honest [q1, q3] size-cohort range; null ⇒ no band
    amount: float
    currency: str
    direction: Literal["high", "low", "n/a"]
    status: Literal[
        "in_line", "above", "below", "well_above", "well_below",
        "no_benchmark", "watch",
    ]
    cra_scrutinized: bool
    flag_key: Optional[str]
    dismissed: bool
    # No percentile fields — peer_pct + peer_band carry the comparison.
    business_count: Optional[int] = None
    quality: Optional[str] = None          # "A".."E"
    recommendation: Optional[str] = None
    unavailable_reason: Optional[str] = None  # "grouped_other" | "insufficient_filings" | null
    weight: float = 0.0                    # client-side "worth a look" ranking key


class ReasonablenessProfitabilityLine(BaseModel):
    """Always-on, PEER-DESCRIPTIVE per-line comparison vs the most-profitable
    quarter of similar businesses. M-PROFIT: DELIBERATELY no status/direction/
    rank/user-margin field."""
    label: str
    gifi_bucket: str
    your_pct: float
    peer_pct: float
    business_count: int
    note: str


class ReasonablenessCohortTrail(BaseModel):
    naics_depth: int
    band: Optional[str] = None
    geo: Optional[Literal["provincial", "national"]] = None
    cohort: Optional[str] = None           # "all" | "rev_q1..q4"


class ReasonablenessMeta(BaseModel):
    naics_code: Optional[str]
    naics_source: Optional[Literal["profile", "industry_map", "data_inferred"]]
    naics_match_depth: Optional[int]
    revenue_band: Optional[str]
    quartile: Optional[str]
    benchmark_year: Optional[int]
    operating_revenue: float
    currency: str
    period_months: float
    partial_year: bool
    below_floor: bool
    industry_label: Optional[str]
    attribution: str
    dismissed_flag_keys: List[str] = []
    # Source-data disclosure — what the user is being compared against.
    cohort_label: Optional[str] = None          # NAICS industry name
    revenue_band_label: Optional[str] = None     # "$30K–$5M in annual revenue"
    geography: Optional[str] = None              # "Canada (national)"
    province: Optional[str] = None               # the user's province, for the locality note
    cohort_province: Optional[str] = None        # province name if a provincial cohort was used
    sample_size: Optional[int] = None            # businesses behind the benchmark
    pct_profitable: Optional[float] = None
    gross_margin: Optional[float] = None
    net_profit_margin: Optional[float] = None
    annualized_revenue: Optional[float] = None   # the user's, drives the size band
    # Verifiable source links.
    source_url: Optional[str] = None             # exact ISED page for this cohort
    source_tool_url: Optional[str] = None        # the ISED FPD tool
    source_dataset_url: Optional[str] = None     # the open-data bulk dataset
    licence_url: Optional[str] = None            # OGL-Canada
    # Size cohort and explicit data vintage, both rendered by the UI.
    size_cohort: Optional[str] = None            # "all" | "rev_q1..q4"
    size_cohort_label: Optional[str] = None      # plain-language $ range
    cohort_quality: Optional[str] = None         # "A".."E" of the selected cell
    business_count: Optional[int] = None         # REAL N (aligns with sample_size)
    cohort_trail: Optional[ReasonablenessCohortTrail] = None
    data_vintage: Optional[int] = None           # == benchmark_year, for staleness


class ReasonablenessResponse(BaseModel):
    status: Literal["ok", "missing_naics", "below_floor", "no_data"]
    band: Optional[Literal["typical", "few", "several"]]
    band_label: Optional[str]
    flags: List[ReasonablenessFlag]
    comparisons: List[ReasonablenessComparison] = []
    reassurances: List[str]
    meta: ReasonablenessMeta
    start: str
    end: str
    # Always-on profitability section ([] ⇒ the section hides).
    profitability: List[ReasonablenessProfitabilityLine] = []
    profitability_cohort_label: Optional[str] = None


class DismissFlagRequest(BaseModel):
    dismissed: bool


class DismissFlagResponse(BaseModel):
    id: str
    dismissed: bool


_REVENUE_BAND_LABELS = {
    "30K-5M": "$30K–$5M in annual revenue",
    "5M-20M": "$5M–$20M in annual revenue",
}


def _geography() -> str:
    try:
        return (_load_benchmarks().get("meta") or {}).get("geography") or "Canada (national)"
    except Exception:
        return "Canada (national)"


def _bench_meta() -> dict:
    try:
        return _load_benchmarks().get("meta") or {}
    except Exception:
        return {}


# engine.suppressed_reason → response.status
_SUPPRESS_TO_STATUS = {
    None: "ok",
    "below_revenue_floor": "below_floor",
    "missing_naics": "missing_naics",
    "no_benchmark_cell": "no_data",
}
# engine internal band "a_few" → wire "few"; "typical"/"several" pass through.
_BAND_TO_WIRE = {"typical": "typical", "a_few": "few", "several": "several"}


# ── Cached static-data loaders (read once per process) ───────────────────────

@functools.lru_cache(maxsize=1)
def _load_benchmarks() -> dict:
    with open(BENCHMARK_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@functools.lru_cache(maxsize=1)
def _load_gifi_mapping() -> dict:
    with open(GIFI_MAPPING_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _attribution() -> str:
    try:
        return (_load_benchmarks().get("meta") or {}).get("attribution") or _ATTRIBUTION_FALLBACK
    except Exception:
        return _ATTRIBUTION_FALLBACK


def _empty_meta(currency: str = "CAD") -> ReasonablenessMeta:
    return ReasonablenessMeta(
        naics_code=None, naics_source=None, naics_match_depth=None,
        revenue_band=None, quartile=None, benchmark_year=None,
        operating_revenue=0.0, currency=currency, period_months=0.0,
        partial_year=False, below_floor=False, industry_label=None,
        attribution=_attribution(), dismissed_flag_keys=[],
    )


def _no_data_response(start: str, end: str, currency: str = "CAD") -> ReasonablenessResponse:
    return ReasonablenessResponse(
        status="no_data", band=None, band_label=None, flags=[], reassurances=[],
        meta=_empty_meta(currency), start=start, end=end,
    )


# ── Range resolution (mirrors analytics.get_category_breakdown) ──────────────

def _resolve_range(db: DatabasePg, start: Optional[str], end: Optional[str]) -> tuple[date, date]:
    from server.api.analytics import _month_bounds, _parse_iso_date

    if (start is None) != (end is None):
        raise HTTPException(status_code=400, detail="Both start and end must be provided together")
    if start is None and end is None:
        latest = db.get_latest_transaction_month()
        if latest is None:
            today = date.today()
            return _month_bounds(today.year, today.month)
        return _month_bounds(latest[0], latest[1])
    s = _parse_iso_date(start, "start")
    e = _parse_iso_date(end, "end")
    if s > e:
        raise HTTPException(status_code=400, detail="start must be on or before end")
    if (e - s).days > 5 * 366:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 5 years")
    return s, e


# ── GET /api/reasonableness/flags ────────────────────────────────────────────

@router.get("/flags", response_model=ReasonablenessResponse)
def get_reasonableness_flags(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
) -> ReasonablenessResponse:
    """Peer-benchmark comparison for the Analytics card.

    Missing-NAICS, below-floor, and empty-data are 200 responses with a
    ``status`` discriminator (not errors). Feature-flag OFF short-circuits to
    the empty ``no_data`` shape.
    """
    s, e = _resolve_range(db, start, end)
    start_iso, end_iso = s.isoformat(), e.isoformat()

    # 1. Feature gate — ships dark; OFF hides the card (never 403).
    if not is_enabled(db, user.id, _FEATURE_FLAG):
        return _no_data_response(start_iso, end_iso)

    # 2. Profile
    profile = db.get_business_profile() or {}
    naics_code = profile.get("naics_code")
    base_currency = (profile.get("base_currency") or "CAD").upper()
    industry_label = profile.get("industry")
    province = profile.get("province")

    # 3. Category rows
    try:
        rows = db.get_category_breakdown(start_iso, end_iso)
    except Exception:
        logger.exception("reasonableness: category breakdown failed")
        raise HTTPException(status_code=500, detail="Failed to compute peer comparison")

    if not rows:
        return _no_data_response(start_iso, end_iso, base_currency)

    # 4. FX rates for non-base currencies (BoC, cached daily). Unconvertible
    #    currencies are omitted → the engine applies its deterministic fallback.
    fx_rates: dict[str, float] = {}
    try:
        from server.api.analytics import _fetch_boc_rate

        for cur in {(r.get("currency") or base_currency).upper() for r in rows}:
            if cur == base_currency:
                continue
            rate = _fetch_boc_rate(cur)
            if rate is not None:
                fx_rates[cur] = rate.rate
    except Exception:
        logger.warning("reasonableness: FX lookup failed; using fallback", exc_info=True)

    # 5. Dismissed suppression set
    try:
        dismissed = {d["flag_key"] for d in db.get_dismissed_reasonableness_flags()}
    except Exception:
        logger.warning("reasonableness: dismissed-flag read failed; treating as none", exc_info=True)
        dismissed = set()

    # 5b. Resolve NAICS without asking the user for a code: stored code →
    #     coarse industry label → spending signature. None → card hides.
    # The crosswalk has to be the SSOT projection, not the raw YAML. The YAML
    # carries only `fallback_rules` and `default` now — its `categories:` block
    # moved into config/ised_categories.py — so handing the engine the bare dict
    # leaves the exact-name table empty and every canonical category falls
    # through the fuzzy substring rules. That silently mis-buckets four of them:
    # "All other revenues" lands in operating revenue and inflates the ratio
    # denominator, "Labour and commissions" stops rolling up to salaries_wages
    # so payroll never benchmarks, the GST/HST remittance is dropped as
    # non-operating, and the owner's draw is read as a distribution and excluded
    # from expenses. load_category_gifi_map() merges the SSOT with the same
    # fallback rules, which is what core.reasonableness uses on its own.
    from core.reasonableness import load_category_gifi_map

    mapping = load_category_gifi_map()
    try:
        from core.reasonableness import resolve_naics

        naics_code, naics_source = resolve_naics(
            naics_code, industry_label, rows, mapping=mapping,
        )
    except Exception:
        logger.warning("reasonableness: NAICS resolution failed", exc_info=True)
        naics_source = "profile" if naics_code else None

    # 6. Engine (deterministic, no I/O on the hot path)
    try:
        from core.reasonableness import evaluate_reasonableness

        result = evaluate_reasonableness(
            rows, naics_code, s, e, dismissed,
            naics_source=naics_source, province=province,
            base_currency=base_currency, fx_rates=fx_rates,
            benchmarks=_load_benchmarks(), mapping=mapping,
        )
    except Exception:
        logger.exception("reasonableness: engine failed")
        raise HTTPException(status_code=500, detail="Failed to compute peer comparison")

    # 7. Map engine result → wire response
    status = _SUPPRESS_TO_STATUS.get(result.suppressed_reason, "no_data")
    band = _BAND_TO_WIRE.get(result.band) if status == "ok" else None
    band_label = result.band_label if status == "ok" else None

    flags = [
        ReasonablenessFlag(
            id=f.flag_key, category=f.category, severity=f.severity,
            direction=f.direction, title=f.title, prompt=f.prompt,
            your_pct=f.your_pct, peer_pct=f.peer_pct, peer_band=f.peer_band,
            amount=f.amount, currency=result.currency,
            dismissed=f.flag_key in dismissed,
            business_count=f.business_count, recommendation=f.recommendation,
        )
        for f in result.flags
    ]

    comparisons = [
        ReasonablenessComparison(
            label=c.label, category=c.category, gifi_bucket=c.gifi_bucket,
            your_pct=c.your_pct, peer_pct=c.peer_pct, peer_band=c.peer_band,
            amount=c.amount, currency=result.currency, direction=c.direction,
            status=c.status, cra_scrutinized=c.cra_scrutinized,
            flag_key=c.flag_key,
            dismissed=bool(c.flag_key and c.flag_key in dismissed),
            business_count=c.business_count, quality=c.quality,
            recommendation=c.recommendation, unavailable_reason=c.unavailable_reason,
            weight=c.weight,
        )
        for c in result.comparisons
    ]

    profitability = [
        ReasonablenessProfitabilityLine(
            label=p.label, gifi_bucket=p.gifi_bucket, your_pct=p.your_pct,
            peer_pct=p.peer_pct, business_count=p.business_count, note=p.note,
        )
        for p in (result.profitability or [])
    ]

    meta = ReasonablenessMeta(
        naics_code=result.naics_used,
        naics_source=result.naics_source,
        naics_match_depth=result.naics_fallback_level or None,
        revenue_band=result.revenue_band,
        quartile=result.quartile,
        benchmark_year=result.benchmark_year or None,
        operating_revenue=result.operating_revenue,
        currency=result.currency,
        period_months=result.period_months,
        partial_year=result.is_partial_year,
        below_floor=(result.suppressed_reason == "below_revenue_floor"),
        industry_label=industry_label,
        attribution=result.attribution,
        dismissed_flag_keys=sorted(dismissed),
        cohort_label=result.cohort_label,
        revenue_band_label=(
            _REVENUE_BAND_LABELS.get(result.revenue_band) if result.revenue_band else None
        ),
        geography=(
            f"{result.cohort_province} (provincial)" if result.cohort_province
            else _geography()
        ),
        province=province,
        cohort_province=result.cohort_province,
        sample_size=result.cohort_sample_size,
        pct_profitable=result.cohort_pct_profitable,
        gross_margin=result.cohort_gross_margin,
        net_profit_margin=result.cohort_net_margin,
        annualized_revenue=result.annualized_revenue,
        source_url=result.cohort_source_url,
        source_tool_url=_bench_meta().get("source_tool_url"),
        source_dataset_url=_bench_meta().get("source_dataset_url"),
        licence_url=_bench_meta().get("attribution_url"),
        size_cohort=result.size_cohort,
        size_cohort_label=result.size_cohort_label,
        cohort_quality=result.cohort_quality,
        business_count=result.cohort_business_count,
        cohort_trail=(
            ReasonablenessCohortTrail(**result.cohort_trail)
            if result.cohort_trail else None
        ),
        data_vintage=result.benchmark_year or None,
    )

    return ReasonablenessResponse(
        status=status, band=band, band_label=band_label, flags=flags,
        comparisons=comparisons, reassurances=list(result.reassurance), meta=meta,
        start=start_iso, end=end_iso,
        profitability=profitability,
        profitability_cohort_label=result.profitability_cohort_label,
    )


# ── POST /api/reasonableness/flags/{id}/dismiss ──────────────────────────────

@router.post("/flags/{id}/dismiss", response_model=DismissFlagResponse)
def toggle_dismiss_flag(
    req: DismissFlagRequest,
    id: str = Path(...),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
) -> DismissFlagResponse:
    """Toggle a flag's dismissed state. Not feature-gated (harmless write; the
    card never renders when the read gate is off). Idempotent."""
    if not _FLAG_ID_RE.match(id):
        raise HTTPException(status_code=400, detail="Invalid flag id")
    try:
        if req.dismissed:
            db.upsert_dismissed_reasonableness_flag(id)
        else:
            db.delete_dismissed_reasonableness_flag(id)
    except Exception:
        logger.exception("reasonableness: dismiss toggle failed")
        raise HTTPException(status_code=500, detail="Failed to update flag")
    return DismissFlagResponse(id=id, dismissed=req.dismissed)

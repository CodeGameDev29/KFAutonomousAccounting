"""Analytics API — category profit/loss breakdown.

Serves the Analytics tab and the Dashboard "Top 5 Profit/Loss Categories"
widget. Aggregates per-category signed net within a user-selected date
range, excluding internal transfer-like categories.
"""

from __future__ import annotations

import logging
import re
import threading
from calendar import monthrange
from datetime import date, datetime
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class CategoryBreakdown(BaseModel):
    category: str
    currency: str
    income: float
    expenses: float
    net: float
    transaction_count: int
    # Fixed ISED/GIFI enrichment (additive; derived from config/ised_categories.py).
    section: str | None = None       # revenue|cost_of_sales|operating|structural|uncategorized
    ised_bucket: str | None = None   # benchmark rollup slug (None for Uncategorized/Refund)
    kind: str | None = None          # income|cogs|expense|transfer|equity|tax|contra|none


class DefaultPeriod(BaseModel):
    year: int
    month: int


class CategoryBreakdownResponse(BaseModel):
    categories: List[CategoryBreakdown]
    default_period: Optional[DefaultPeriod]
    start: str
    end: str


class MonthlyTrendEntry(BaseModel):
    month: int
    currency: str
    income: float
    expenses: float


class MonthlyTrendResponse(BaseModel):
    year: int
    entries: List[MonthlyTrendEntry]


@router.get("/monthly-trend", response_model=MonthlyTrendResponse)
def get_monthly_trend(
    year: int = Query(..., ge=2000, le=2100),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
) -> MonthlyTrendResponse:
    """Per-month, per-currency income/expense for the Analytics trend chart.

    Currencies are kept separate so the frontend can FX-convert instead of
    numerically mixing CAD and USD.
    """
    try:
        rows = db.get_monthly_trend(year)
    except Exception:
        logger.exception("Failed to compute monthly trend")
        raise HTTPException(status_code=500, detail="Failed to compute monthly trend")
    return MonthlyTrendResponse(
        year=year,
        entries=[MonthlyTrendEntry(**r) for r in rows],
    )


class FxRate(BaseModel):
    rate: float
    as_of: str  # observation date of the Bank of Canada quote


class FxRatesResponse(BaseModel):
    base: str
    rates: Dict[str, FxRate]
    missing: List[str]


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Bank of Canada Valet daily noon rates, cached per (currency, fetch day) so
# the API is called at most once per currency per day per process.
_FX_CACHE: Dict[str, tuple[date, FxRate]] = {}
_FX_CACHE_LOCK = threading.Lock()
_BOC_VALET_URL = "https://www.bankofcanada.ca/valet/observations/FX{cur}CAD/json?recent=1"


def _fetch_boc_rate(currency: str) -> Optional[FxRate]:
    today = date.today()
    with _FX_CACHE_LOCK:
        cached = _FX_CACHE.get(currency)
        if cached and cached[0] == today:
            return cached[1]
    try:
        resp = httpx.get(_BOC_VALET_URL.format(cur=currency), timeout=6.0)
        resp.raise_for_status()
        obs = resp.json().get("observations") or []
        if not obs:
            return None
        latest = obs[-1]
        rate = float(latest[f"FX{currency}CAD"]["v"])
        result = FxRate(rate=rate, as_of=str(latest["d"]))
    except Exception:
        logger.warning("BoC FX lookup failed for %s", currency, exc_info=True)
        return None
    with _FX_CACHE_LOCK:
        _FX_CACHE[currency] = (today, result)
    return result


@router.get("/fx-rates", response_model=FxRatesResponse)
def get_fx_rates(
    currencies: str = Query(..., description="Comma-separated ISO codes, e.g. USD,EUR"),
    user: AuthUser = Depends(get_current_user),
) -> FxRatesResponse:
    """Daily CAD conversion rates (Bank of Canada) for the given currencies.

    Used by the Analytics summary to show an approximate all-accounts total.
    Currencies the BoC does not quote come back in ``missing`` so the
    frontend can fall back to per-currency display instead of a wrong total.
    """
    requested = [c.strip().upper() for c in currencies.split(",") if c.strip()]
    requested = [c for c in requested if c != "CAD"]
    if not requested or len(requested) > 10:
        raise HTTPException(status_code=400, detail="Provide 1-10 non-CAD currency codes")
    for c in requested:
        if not _CURRENCY_RE.match(c):
            raise HTTPException(status_code=400, detail=f"Invalid currency code: {c}")

    rates: Dict[str, FxRate] = {}
    missing: List[str] = []
    for c in dict.fromkeys(requested):  # dedupe, keep order
        r = _fetch_boc_rate(c)
        if r is None:
            missing.append(c)
        else:
            rates[c] = r
    return FxRatesResponse(base="CAD", rates=rates, missing=missing)


def _parse_iso_date(s: str, field: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} format; expected YYYY-MM-DD",
        )


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


@router.get("/category-breakdown", response_model=CategoryBreakdownResponse)
def get_category_breakdown(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
) -> CategoryBreakdownResponse:
    """Return per-category signed net for a date range.

    If ``start`` and ``end`` are both omitted, the backend falls back to
    the calendar month of the user's most recent transaction and returns
    that month in ``default_period`` so the frontend can backfill the URL.
    """
    if (start is None) != (end is None):
        raise HTTPException(
            status_code=400,
            detail="Both start and end must be provided together",
        )

    default_period: Optional[DefaultPeriod] = None

    if start is None and end is None:
        latest = db.get_latest_transaction_month()
        if latest is None:
            today = date.today()
            s, e = _month_bounds(today.year, today.month)
            return CategoryBreakdownResponse(
                categories=[],
                default_period=None,
                start=s.isoformat(),
                end=e.isoformat(),
            )
        year, month = latest
        s, e = _month_bounds(year, month)
        default_period = DefaultPeriod(year=year, month=month)
    else:
        s = _parse_iso_date(start, "start")
        e = _parse_iso_date(end, "end")
        if s > e:
            raise HTTPException(
                status_code=400,
                detail="start must be on or before end",
            )
        if (e - s).days > 5 * 366:
            raise HTTPException(
                status_code=400,
                detail="Date range cannot exceed 5 years",
            )

    try:
        rows = db.get_category_breakdown(s.isoformat(), e.isoformat())
    except Exception:
        logger.exception("Failed to compute category breakdown")
        raise HTTPException(
            status_code=500,
            detail="Failed to compute category breakdown",
        )

    # Enrich each row with its fixed ISED/GIFI section/bucket/kind. Uncategorized
    # (NULL -> "Uncategorized") resolves to the "uncategorized" pseudo-section with
    # ised_bucket=None so it is surfaced separately, never folded into the peer
    # "other_operating" line.
    from config.ised_categories import CATEGORY_BY_NAME
    out = []
    for r in rows:
        c = CATEGORY_BY_NAME.get(r.get("category"))
        out.append(CategoryBreakdown(
            **r,
            section=(c.section if c else "uncategorized"),
            ised_bucket=(c.ised_bucket if c else None),
            kind=(c.kind if c else "none"),
        ))

    return CategoryBreakdownResponse(
        categories=out,
        default_period=default_period,
        start=s.isoformat(),
        end=e.isoformat(),
    )

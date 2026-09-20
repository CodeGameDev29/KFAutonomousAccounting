"""CRA Reasonableness / Peer-Benchmark engine — deterministic core.

Pure ratio math: no LLM, no network, no I/O on the hot path (the benchmark
table + category map are loaded once via ``lru_cache``). Given a user's
category totals and a NAICS code, it compares expense/income ratios against
ISED Financial Performance Data peer benchmarks and surfaces a coarse 3-band
verdict plus a ranked list of "worth double-checking" prompts.

**This is a bookkeeping sanity-check / peer comparison, never an audit-risk
score.** Every user-facing string produced here must obey the legal-framing
copy contract (see ``_reject_forbidden_language``); the band labels + disclaimer
are hardcoded constants, never assembled from user data.

Entry point: :func:`evaluate_reasonableness`.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from config.settings import (
    BENCHMARK_DATA_PATH,
    GIFI_MAPPING_PATH,
    ReasonablenessConfig,
    reasonableness_config,
)

try:  # PyYAML is a hard dep, but keep the import defensive for odd envs.
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


# ── Legal-framing guard (shared source of truth with the API/frontend) ───────

BAND_LABELS: dict[str, str] = {
    "typical": "Looks typical",
    "a_few": "A few things worth a look",
    "several": "Several things stand out — worth reviewing with your bookkeeper",
}

DISCLAIMER = (
    "Peer comparison, not tax advice or an audit prediction — "
    "compares to public averages only."
)

# Forbidden substrings (case-insensitive) — must never appear in any
# user-facing string. Mirrors the API + frontend guard so a drift in one place
# is caught by the unit test in either.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "audit risk score",
    "audit risk",
    "risk score",
    "cra-proof",
    "cra proof",
    "audit shield",
    "won't get audited",
    "will not get audited",
    "won't be audited",
    "will be audited",
    "safe from cra",
    "audit probability",
    "chance of audit",
    "odds of audit",
    "likelihood of audit",
    # Outlier / alarm framing.
    "red flag",
    "flagged",
    "the cra will notice",
    "cra will notice",
    "suspicious",
    "at risk",
    "overspending",
    "too high",
    "too low",
    # User-directed profitability verdicts (the profitability
    # comparison is always-on and must stay peer-descriptive; never rank the user)
    "you're less profitable",
    "you are less profitable",
    "less profitable than",
    "underperforming",
    "below-average business",
    "below average business",
    # Banned stats vocabulary in USER-FACING prose (modeled internally as cohort
    # keys / code identifiers, never surfaced in rendered copy).
    "quartile",
    "percentile",
)
_FORBIDDEN_AUDIT_PCT_RE = re.compile(
    r"\b\d{1,3}\s*%?\s*(chance|probability|risk)\s+of\s+audit\b", re.IGNORECASE
)
# "guarantee" is only forbidden in a tax/audit context.
_FORBIDDEN_GUARANTEE_RE = re.compile(
    r"guarantee[a-z]*\b.{0,40}\b(audit|cra|tax)", re.IGNORECASE
)


def _reject_forbidden_language(text: str | None) -> bool:
    """Return True if ``text`` contains any forbidden framing.

    Used as a fail-safe on any optional generated prose (the deterministic
    templates below are authored to pass it) and asserted by the unit tests.
    """
    if not text:
        return False
    low = text.lower()
    if any(sub in low for sub in _FORBIDDEN_SUBSTRINGS):
        return True
    if _FORBIDDEN_AUDIT_PCT_RE.search(text):
        return True
    if _FORBIDDEN_GUARANTEE_RE.search(text):
        return True
    return False


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Flag:
    flag_key: str
    layer: str  # "A" (ratio) | "B" (named pattern)
    severity: str  # "info" | "elevated" | "stands_out"
    category: str  # app category name ("" for cross-cutting)
    direction: str  # "high" | "low" | "n/a"
    gifi_bucket: str | None
    title: str
    prompt: str
    your_pct: float | None
    peer_pct: float | None
    peer_band: list[float] | None
    amount: float | None
    weight: float
    cra_scrutinized: bool
    # Defaulted so constructors that omit them stay valid.
    business_count: int | None = None       # count of businesses behind the peer cell
    recommendation: str | None = None        # deterministic framing-safe prose (None if none)


@dataclass(frozen=True)
class Comparison:
    """One line of the always-visible per-category comparison table: the user's
    spend on a GIFI bucket vs the peer industry median, with a plain status."""
    label: str
    category: str
    gifi_bucket: str
    your_pct: float
    peer_pct: float | None  # median-first central value; None when suppressed / no ISED line
    peer_band: list[float] | None  # honest size-cohort range [q1, q3]; None → no band (never fabricated)
    amount: float
    direction: str  # "high" | "low" | "n/a"
    status: str     # in_line|above|below|well_above|well_below|no_benchmark|watch
    cra_scrutinized: bool
    flag_key: str | None  # the ratio/pattern flag key when this line is a concern
    # Rank vocabulary stays out of the payload: peer_pct + peer_band carry the
    # whole comparison.
    business_count: int | None = None     # count of businesses behind the peer cell
    quality: str | None = None            # "A".."E" StatCan data-quality letter
    recommendation: str | None = None     # deterministic framing-safe prose (None if in-line)
    unavailable_reason: str | None = None  # "grouped_other" | "insufficient_filings" | None
    weight: float = 0.0                   # client-side ranking key for "worth a look"


@dataclass(frozen=True)
class ProfitabilityComparison:
    """Always-on, PEER-DESCRIPTIVE per-line comparison vs the most-profitable
    quarter (pm_q4) of similar Canadian businesses. This is a fact
    about peers + the user's own share; it carries NO user-margin rank field."""
    label: str
    gifi_bucket: str
    your_pct: float
    peer_pct: float          # the pm_q4 cohort's %-of-revenue for this line
    business_count: int      # N behind the pm_q4 cohort cell (≥ min_business_count)
    note: str                # peer-descriptive + mandatory defuser


@dataclass(frozen=True)
class ReassuranceNote:
    key: str
    text: str


@dataclass(frozen=True)
class ReasonablenessResult:
    band: str  # "typical" | "a_few" | "several"  (route maps a_few→few)
    band_label: str
    naics_used: str | None
    naics_source: str | None
    naics_fallback_level: int
    revenue_band: str | None
    quartile: str | None
    operating_revenue: float
    currency: str
    period_months: float
    is_partial_year: bool
    annualized: bool
    flags: list[Flag]
    comparisons: list[Comparison]  # every benchmarkable category, always shown
    total_flags_before_cap: int
    reassurance: list[str]
    suppressed_reason: str | None  # below_revenue_floor|missing_naics|no_benchmark_cell|None
    dismissed_applied: list[str]
    attribution: str
    benchmark_year: int
    # Source-data disclosure — exactly which cohort the user is compared against.
    cohort_label: str | None          # NAICS industry name, e.g. "Specialized design services…"
    cohort_sample_size: int | None    # # of businesses' filings behind the cell
    cohort_pct_profitable: float | None
    cohort_gross_margin: float | None
    cohort_net_margin: float | None
    cohort_source_url: str | None     # deep link to the exact ISED source page
    cohort_province: str | None       # province name if a province cohort was used, else None
    annualized_revenue: float         # the user's revenue annualized (drives the size band)
    # Size + profitability cohorts. Defaulted so
    # the two keyword constructors (`_suppressed`, final return) stay explicit.
    size_cohort: str | None = None            # "all" | "rev_q1..q4" — selected size cohort
    size_cohort_label: str | None = None      # plain-language $ range (None when "all")
    cohort_quality: str | None = None         # "A".."E" of the selected cell
    cohort_business_count: int | None = None  # count of businesses in the selected cell
    cohort_trail: dict | None = None          # {"naics_depth","band","geo","cohort"}
    profitability: list = field(default_factory=list)   # list[ProfitabilityComparison]
    profitability_cohort_label: str | None = None       # None/empty ⇒ section hidden


# ── Benchmark table + category map wrappers ─────────────────────────────────

class BenchmarkCell:
    def __init__(self, data: dict[str, Any]):
        self._d = data
        self.naics: str = data.get("naics", "")
        self.naics_label: str = data.get("naics_label", "")
        self.revenue_band: str | None = data.get("revenue_band")
        self.quartile: str = data.get("quartile", "all")
        self.sample_size: int | None = data.get("sample_size")
        self.pct_profitable: float | None = data.get("pct_profitable")
        self.source_url: str | None = data.get("source_url")
        self.ratios: dict[str, float] = data.get("ratios") or {}
        self.lines: dict[str, Any] = data.get("lines") or {}
        self.province: str | None = data.get("province")
        self.province_name: str | None = data.get("province_name")
        # Density + quality + cohort signals. Fall back to the bare
        # ``sample_size``/``quartile`` keys so a table written without those
        # fields still loads (it degrades to honest suppression, never a
        # fabricated band).
        self._business_count = data.get("business_count", data.get("sample_size"))
        self.quality: str | None = data.get("quality")            # "A".."E" | None
        self.cohort: str = data.get("cohort", data.get("quartile", "all"))
        self.cohort_revenue_low: float | None = data.get("cohort_revenue_low")
        self.cohort_revenue_high: float | None = data.get("cohort_revenue_high")

    @property
    def business_count(self) -> int | None:
        try:
            return int(self._business_count) if self._business_count is not None else None
        except (TypeError, ValueError):
            return None

    def get_line(self, bucket: str) -> dict[str, Any] | None:
        """Return the peer line for a GIFI bucket, or None if absent.

        Return shape (keys always present; bounds may be None):
            {"pct_of_revenue": float,   # median-first central value
             "q1": float | None,        # honest size-cohort lower bound (real, gated)
             "median": float | None,    # median breakpoint (== the cohort average for the
                                         #   'all' cell; None on rev_q*/pm_q* point cells)
             "q3": float | None}        # honest size-cohort upper bound (real, gated)

        NEVER synthesizes a spread. A bare float (average only) or a
        {'pct_of_revenue','low','high'} dict yields q1/median/q3 = None so
        the engine treats the line as un-benchmarkable-with-a-band and (under the
        min-N gate) suppresses rather than fabricating a ±spread. Defense-in-depth:
        a non-monotonic / inverted band (firm-cohort quartiles CAN invert) is
        dropped here even though ingest is the primary monotonicity gate.
        """
        v = self.lines.get(bucket)
        if v is None:
            return None
        if isinstance(v, dict):
            q1 = v.get("q1")
            median = v.get("median")
            q3 = v.get("q3")
            # median-first surfaced value; fall back to the stored average/point.
            central = median if median is not None else v.get("pct_of_revenue")
            # A real band requires q1 ≤ q3 (and q1 ≤ median ≤ q3 when a median is
            # present). Anything else → no usable band (never a fabricated one).
            band_ok = (
                q1 is not None and q3 is not None and q1 <= q3
                and (median is None or q1 <= median <= q3)
            )
            if not band_ok:
                q1 = q3 = None
            return {"pct_of_revenue": central, "q1": q1, "median": median, "q3": q3}
        # A bare float is the cohort AVERAGE, carrying no bounds → no band.
        return {"pct_of_revenue": v, "q1": None, "median": None, "q3": None}


class BenchmarkTable:
    def __init__(self, data: dict[str, Any]):
        self._d = data or {}
        self.meta: dict[str, Any] = self._d.get("meta") or {}
        self._cells: dict[str, Any] = self._d.get("cells") or {}

    @property
    def attribution(self) -> str:
        return self.meta.get(
            "attribution",
            "Contains information licensed under the Open Government Licence – Canada. "
            "Source: ISED/StatCan Financial Performance Data.",
        )

    @property
    def benchmark_year(self) -> int:
        return int(self.meta.get("source_year", 0) or 0)

    def lookup(
        self, naics: str | None, revenue_band: str | None, quartile: str = "all",
        province: str | None = None,
    ) -> tuple[BenchmarkCell | None, int]:
        """Find the benchmark cell, walking the NAICS tree 6→5→4 digits.

        At each NAICS level, prefer the user's province cohort
        (``<naics>|<band>|<q>|<province>``) then fall back to the national cell
        (``<naics>|<band>|<q>``), so a finer NAICS national cell beats a coarser
        province cell. Returns (cell, fallback_level).
        """
        if not naics or not revenue_band:
            return None, 0
        digits = re.sub(r"\D", "", str(naics))
        if not digits:
            return None, 0
        start = min(len(digits), 6)
        for length in range(start, 3, -1):  # 6, 5, 4 (never coarser than 4)
            prefix = digits[:length]
            if province:
                pcell = self._cells.get(f"{prefix}|{revenue_band}|{quartile}|{province}")
                if pcell is not None:
                    return BenchmarkCell(pcell), length
            cell = self._cells.get(f"{prefix}|{revenue_band}|{quartile}")
            if cell is not None:
                return BenchmarkCell(cell), length
        return None, 0

    # ── Cohort selection with density gate ──────────────────────────────────

    def _cell(self, key: str) -> "BenchmarkCell | None":
        raw = self._cells.get(key)
        return BenchmarkCell(raw) if raw is not None else None

    def sufficient(self, cell: "BenchmarkCell | None", config: ReasonablenessConfig) -> bool:
        """Density gate — StatCan's own quartile-suppression bar
        (business_count ≥ min_business_count). A null count fails (suppress,
        never fabricate)."""
        return cell is not None and (cell.business_count or 0) >= config.min_business_count

    def select_cell(
        self,
        naics: str | None,
        revenue_band: str | None,
        annualized_revenue: float,
        province: str | None,
        config: ReasonablenessConfig,
    ) -> tuple["BenchmarkCell | None", dict]:
        """Pick the FINEST cohort that still passes the density gate.

        Precedence (finest → coarsest), first ``sufficient()`` hit wins:
          for naics_len in 6, 5, 4:                # finest NAICS first
            for geo in (province, national):       # province preferred
              1. the rev_q* cohort whose [low, high] brackets annualized_revenue
              2. the "all" cohort (whole revenue band)
        Returns ``(cell, trail)``; ``(None, trail)`` ⇒ caller SUPPRESSES.
        ``trail = {"naics_depth","band","geo","cohort"}``.
        """
        trail = {"naics_depth": 0, "band": revenue_band, "geo": None, "cohort": None}
        if not naics or not revenue_band:
            return None, trail
        digits = re.sub(r"\D", "", str(naics))
        if not digits:
            return None, trail
        start = min(len(digits), 6)

        def _bracketing_rev(prefix: str, suffix: str) -> "BenchmarkCell | None":
            for q in ("rev_q1", "rev_q2", "rev_q3", "rev_q4"):
                c = self._cell(f"{prefix}|{revenue_band}|{q}{suffix}")
                if c is None:
                    continue
                lo, hi = c.cohort_revenue_low, c.cohort_revenue_high
                if lo is not None and hi is not None and lo <= annualized_revenue <= hi:
                    return c
                if lo is not None and hi is None and annualized_revenue >= lo:
                    return c
            return None

        for length in range(start, 3, -1):  # 6, 5, 4 (4 = floor)
            prefix = digits[:length]
            geos = ([("provincial", f"|{province}")] if province else []) + [("national", "")]
            for geo_name, suffix in geos:
                size_cell = _bracketing_rev(prefix, suffix)
                if self.sufficient(size_cell, config):
                    return size_cell, {
                        "naics_depth": length, "band": revenue_band,
                        "geo": geo_name, "cohort": size_cell.cohort,
                    }
                all_cell = self._cell(f"{prefix}|{revenue_band}|all{suffix}")
                if self.sufficient(all_cell, config):
                    return all_cell, {
                        "naics_depth": length, "band": revenue_band,
                        "geo": geo_name, "cohort": "all",
                    }
        return None, trail

    def select_profit_cohort(
        self,
        naics: str | None,
        revenue_band: str | None,
        cohort_key: str,
        province: str | None,
        config: ReasonablenessConfig,
    ) -> tuple["BenchmarkCell | None", dict]:
        """``select_cell`` with the size-bracket step replaced by a single fixed
        cohort key (e.g. ``pm_q4``); same density gate + trail shape. pm_q* cells
        are national-only, so the provincial suffix simply misses."""
        trail = {"naics_depth": 0, "band": revenue_band, "geo": None, "cohort": None}
        if not naics or not revenue_band:
            return None, trail
        digits = re.sub(r"\D", "", str(naics))
        if not digits:
            return None, trail
        start = min(len(digits), 6)
        for length in range(start, 3, -1):
            prefix = digits[:length]
            geos = ([("provincial", f"|{province}")] if province else []) + [("national", "")]
            for geo_name, suffix in geos:
                c = self._cell(f"{prefix}|{revenue_band}|{cohort_key}{suffix}")
                if self.sufficient(c, config):
                    return c, {
                        "naics_depth": length, "band": revenue_band,
                        "geo": geo_name, "cohort": cohort_key,
                    }
        return None, trail


class CategoryMapping:
    __slots__ = (
        "gifi_bucket",
        "is_operating_revenue",
        "is_distribution",
        "cra_scrutiny_weight",
        "is_generic",
    )

    def __init__(self, d: dict[str, Any]):
        self.gifi_bucket: str = d.get("gifi_bucket", "other_operating")
        self.is_operating_revenue: bool = bool(d.get("is_operating_revenue", False))
        self.is_distribution: bool = bool(d.get("is_distribution", False))
        self.cra_scrutiny_weight: float = float(d.get("cra_scrutiny_weight", 1.0))
        self.is_generic: bool = bool(d.get("is_generic", False))


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class CategoryGifiMap:
    def __init__(self, data: dict[str, Any]):
        self._d = data or {}
        self._categories: dict[str, dict[str, Any]] = self._d.get("categories") or {}
        # Pre-normalize exact category names for whitespace/case tolerance.
        self._by_norm: dict[str, dict[str, Any]] = {
            _normalize_name(k): v for k, v in self._categories.items()
        }
        self._fallback_rules: list[dict[str, Any]] = self._d.get("fallback_rules") or []
        self._default: dict[str, Any] = self._d.get("default") or {
            "gifi_bucket": "other_operating",
            "is_generic": True,
        }

    def resolve(self, category: str | None) -> CategoryMapping:
        name = category or "Uncategorized"
        # 1. exact (case/whitespace tolerant)
        entry = self._categories.get(name) or self._by_norm.get(_normalize_name(name))
        if entry is not None:
            return CategoryMapping(entry)
        # 2. fallback substring rules (first hit wins, in file order)
        norm = _normalize_name(name)
        for rule in self._fallback_rules:
            for needle in rule.get("contains", []):
                if _normalize_name(needle) in norm:
                    merged = {k: v for k, v in rule.items() if k != "contains"}
                    return CategoryMapping(merged)
        # 3. default
        return CategoryMapping(self._default)


# ── Loaders (lru_cache; used when the caller doesn't pass data in) ──────────

@lru_cache(maxsize=8)
def load_benchmarks(path: Path | None = None) -> BenchmarkTable:
    p = Path(path) if path else BENCHMARK_DATA_PATH
    # The cohort table (rev_q*/pm_q* cells) is large and may be shipped gzipped.
    # Prefer the plain .json; fall back to a sibling .json.gz. Either way it is
    # one process-lifetime read (lru_cache'd).
    gz = p.with_suffix(p.suffix + ".gz")
    if not p.exists() and gz.exists():
        p = gz
    if str(p).endswith(".gz"):
        import gzip
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return BenchmarkTable(json.load(f))
    with open(p, "r", encoding="utf-8") as f:
        return BenchmarkTable(json.load(f))


def _load_legacy_gifi_yaml(path: Path | None = None) -> dict[str, Any]:
    """Load ``aa_category_gifi_map.yaml`` — read only for ``fallback_rules`` +
    ``default`` (the free-text safety net for category names outside the fixed
    taxonomy)."""
    p = Path(path) if path else GIFI_MAPPING_PATH
    if yaml is None:  # pragma: no cover
        raise RuntimeError("PyYAML is required to load the GIFI category map")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=8)
def load_category_gifi_map(path: Path | None = None) -> CategoryGifiMap:
    """Build the category→GIFI-bucket crosswalk.

    The ``categories`` block is the SSOT identity projection from
    ``config/ised_categories.py`` — each fixed name maps to its own ``ised_bucket``,
    an exact near-identity rather than a fuzzy substring match.
    ``fallback_rules`` + ``default`` come from the YAML and are the free-text
    safety net for category names outside the fixed taxonomy.
    """
    from config import ised_categories

    data = ised_categories.as_gifi_map_dict()  # {"categories": {name: {...}}}
    legacy = _load_legacy_gifi_yaml(path)
    data["fallback_rules"] = legacy.get("fallback_rules", [])
    data["default"] = legacy.get("default", {"gifi_bucket": "other_operating", "is_generic": True})
    return CategoryGifiMap(data)


def revenue_band_for(
    revenue: float | None, config: ReasonablenessConfig = reasonableness_config
) -> str | None:
    """Map an (annualized) operating-revenue $ amount to an FPD band slug."""
    if revenue is None or revenue < config.revenue_floor:
        return None
    if revenue < 5_000_000:
        return "30K-5M"
    if revenue < 20_000_000:
        return "5M-20M"
    return None  # above the FPD ceiling — no comparable cohort


# ── NAICS inference (no manual code entry — derive from the saved industry
#    label, else from the spending signature; if nothing clearly fits one of
#    the seeded cohorts, return None so the card simply hides). ────────────────

# Coarse onboarding industry label → representative NAICS. ONLY the labels that
# clearly map to a seeded benchmark cohort are listed; everything else stays
# unmapped (card hidden) rather than compared against a poor-fit cohort.
# Maps to the 4-digit NAICS industry groups ISED actually publishes financial
# performance data at (and that ship as benchmark cells).
INDUSTRY_TO_NAICS: dict[str, str] = {
    "Software & Technology": "5415",
    "Consulting & Professional Services": "5416",
    "Food & Hospitality": "7225",
    "Construction & Trades": "2361",
}

# business_profile.province → StatCan SGC code (keys the province benchmark cells).
PROVINCE_TO_SGC: dict[str, str] = {
    "Newfoundland and Labrador": "10", "Prince Edward Island": "11",
    "Nova Scotia": "12", "New Brunswick": "13", "Quebec": "24", "Ontario": "35",
    "Manitoba": "46", "Saskatchewan": "47", "Alberta": "48",
    "British Columbia": "59", "Yukon": "60", "Northwest Territories": "61",
    "Nunavut": "62",
}


def _infer_naics_from_data(
    category_rows: Iterable[Any], cmap: CategoryGifiMap
) -> str | None:
    """Best-effort NAICS from the spending signature — only returns a code when
    the pattern clearly matches a seeded cohort, else None (card hides)."""
    buckets: dict[str, float] = defaultdict(float)
    for row in category_rows:
        m = cmap.resolve(_row_get(row, "category") or "")
        if m.is_operating_revenue or m.is_distribution:
            continue
        if m.gifi_bucket in ("transfer", "non_operating"):
            continue
        buckets[m.gifi_bucket] += float(_row_get(row, "expenses", 0.0) or 0.0)
    total = sum(buckets.values())
    if total <= 0:
        return None
    cos = buckets.get("cost_of_sales", 0.0) / total
    sub = buckets.get("subcontracts", 0.0) / total
    it = buckets.get("it_software", 0.0) / total
    rent = buckets.get("rent", 0.0) / total
    # Restaurant: high cost-of-sales + rent.
    if cos > 0.30 and rent > 0.03:
        return "7225"
    # Construction: cost-of-sales + heavy subcontracting.
    if cos > 0.25 and sub > 0.15:
        return "2361"
    # Software / IT services: meaningful IT or contractor spend, no COGS.
    if cos < 0.15 and (it > 0.04 or sub > 0.15):
        return "5415"
    return None  # no clear fit → don't show


def resolve_naics(
    naics_code: str | None,
    industry_label: str | None,
    category_rows: Iterable[Any],
    *,
    mapping: CategoryGifiMap | dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a NAICS code + its source without asking the user for a code.

    Priority: explicit stored code → coarse industry label map → data
    signature. Returns (None, None) when nothing clearly fits a seeded cohort,
    so the card hides rather than comparing against a poor-fit industry.
    """
    if naics_code:
        return naics_code, "profile"
    if industry_label:
        mapped = INDUSTRY_TO_NAICS.get(industry_label.strip())
        if mapped:
            return mapped, "industry_map"
    cmap = mapping if isinstance(mapping, CategoryGifiMap) else (
        CategoryGifiMap(mapping) if isinstance(mapping, dict) else load_category_gifi_map()
    )
    inferred = _infer_naics_from_data(category_rows, cmap)
    if inferred:
        return inferred, "data_inferred"
    return None, None


# ── Display helpers + deterministic prose templates (framing-compliant) ─────

_BUCKET_LABELS: dict[str, str] = {
    "operating_revenue": "Revenue",
    "cost_of_sales": "Cost of sales",
    "subcontracts": "Contractors",
    "professional_fees": "Professional fees",
    "salaries_wages": "Wages",
    "employee_benefits": "Employee benefits",
    "travel": "Travel",
    "meals_entertainment": "Meals & entertainment",
    "motor_vehicle": "Vehicle",
    "advertising": "Advertising",
    "rent": "Rent",
    "insurance": "Insurance",
    "interest_bank": "Interest & bank charges",
    "office_supplies": "Office supplies",
    "it_software": "Software & IT",
    "repairs_maintenance": "Repairs & maintenance",
    "cca_depreciation": "Depreciation / CCA",
    "utilities": "Utilities & phone",
    "delivery": "Delivery & shipping",
    "other_operating": "Other operating costs",
    # Locally reclassified operating costs (no ISED peer benchmark line).
    "income_tax": "Income tax / GST-HST",
    "owner_draw": "Owner's draw",
}


def _bucket_label(bucket: str | None) -> str:
    return _BUCKET_LABELS.get(bucket or "", (bucket or "This line").replace("_", " ").title())


def _fmt_money(amount: float, currency: str) -> str:
    formatted = f"{abs(amount):,.0f}"
    if currency in ("CAD", "USD", ""):
        return f"${formatted}"
    return f"{formatted} {currency}"


def _ratio_title(label: str, direction: str) -> str:
    if direction == "high":
        return f"{label} is high vs similar businesses"
    return f"{label} is low vs similar businesses"


def _ratio_prompt(
    label: str, r_i: float, amount: float, b_i: float, direction: str,
    severity: str, currency: str,
) -> str:
    yours = f"{r_i * 100:.1f}%"
    peer = f"{b_i * 100:.1f}%"
    money = _fmt_money(amount, currency)
    if direction == "high":
        lead = "This stands out" if severity == "stands_out" else "This is a bit higher than usual"
        return (
            f"{label} is about {yours} of your revenue ({money}). "
            f"Similar businesses typically report around {peer}. "
            f"{lead} — worth checking it's categorized correctly and business-related."
        )
    return (
        f"{label} is about {yours} of your revenue ({money}), lower than the "
        f"typical {peer} for similar businesses. Usually nothing to worry about, "
        f"but worth a quick sanity-check."
    )


def _status_from_bounds(
    r: float,
    q1: float | None,
    q3: float | None,
    central: float | None,
    config: ReasonablenessConfig,
) -> tuple[str, str]:
    """Return (status, comparison_direction) using REAL bounds only — NEVER a
    fabricated spread.

    Full band present → distance in true IQR units:
        r > q3 + k·IQR → well_above ; r > q3 → above
        r < q1 − k·IQR → well_below ; r < q1 → below ; else in_line.
    Band absent (rev_q* point cell, non-monotonic 'all' line, or median-only) →
    relative tolerance around the central value, no band shown.
    """
    if q1 is not None and q3 is not None and q3 >= q1:
        iqr = q3 - q1
        k = config.well_beyond_iqr_mult
        if r > q3 + k * iqr:
            return "well_above", "high"
        if r > q3:
            return "above", "high"
        if r < q1 - k * iqr:
            return "well_below", "low"
        if r < q1:
            return "below", "low"
        return "in_line", "n/a"
    m = central if central is not None else 0.0
    t = config.median_only_tolerance_pct
    if m > 0 and r > m * (1 + t):
        return "above", "high"
    if m > 0 and r < m * (1 - t):
        return "below", "low"
    return "in_line", "n/a"


def _recommendation(
    label: str,
    status: str,
    peer_pct: float | None,
    your_pct: float,
    business_count: int | None,
    currency: str,
) -> str | None:
    """Deterministic, framing-safe recommendation string. Returns
    None for in-line lines. Never a verdict, never business-strategy advice,
    never introduces the CRA as an actor; authored to pass
    ``_reject_forbidden_language`` (asserted in tests)."""
    yours = f"{your_pct * 100:.1f}%"
    peer = f"{peer_pct * 100:.1f}%" if peer_pct is not None else None
    if status == "no_benchmark":
        return (
            f"Statistics Canada groups {label} into \"other expenses\", so there's no "
            f"separate peer number to compare against — nothing to do here."
        )
    if status in ("above", "well_above"):
        mid = f" (the middle sits near {peer})" if peer else ""
        return (
            f"{label} is around {yours} of your revenue — higher than most similar "
            f"businesses{mid}. That's often perfectly normal; it can be worth a quick "
            f"check that everything here is categorized correctly and business-related, "
            f"and a good thing to review with your bookkeeper."
        )
    if status in ("below", "well_below"):
        mid = f" (the middle sits near {peer})" if peer else ""
        return (
            f"{label} is around {yours} of your revenue — lower than most similar "
            f"businesses{mid}. Usually nothing to worry about; some businesses simply "
            f"record this differently, and that's typically fine."
        )
    return None  # in_line → no recommendation


def _profitability_note(label: str, your_pct: float, peer_pct: float) -> str:
    """Peer-DESCRIPTIVE note with a mandatory defuser. NEVER ranks
    the user's own margin."""
    return (
        f"Among the most-profitable quarter of similar Canadian businesses, {label} runs "
        f"about {peer_pct * 100:.1f}% of revenue; yours is about {your_pct * 100:.1f}%. Many "
        f"healthy businesses differ here — it's context for a conversation with your "
        f"accountant, not a target."
    )


def _size_cohort_label(cell: "BenchmarkCell | None") -> str | None:
    """Plain-language $ range for the selected size cohort (None when 'all').
    Never uses the token 'quartile'/'percentile' (banned stats vocabulary)."""
    if cell is None or cell.cohort == "all":
        return None
    lo, hi = cell.cohort_revenue_low, cell.cohort_revenue_high
    if lo is None:
        return None
    if hi is None:
        return f"Businesses like yours by revenue ({_fmt_money(lo, 'CAD')}+)"
    return (
        f"Businesses like yours by revenue "
        f"({_fmt_money(lo, 'CAD')}–{_fmt_money(hi, 'CAD')})"
    )


# ── Row accessor ─────────────────────────────────────────────────────────────

def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


# ── Engine entry ─────────────────────────────────────────────────────────────

def evaluate_reasonableness(
    category_rows: Iterable[Any],
    naics_code: str | None,
    period_start: date,
    period_end: date,
    dismissed_flag_keys: Iterable[str] | None = None,
    *,
    naics_source: str | None = None,
    province: str | None = None,
    base_currency: str = "CAD",
    fx_rates: dict[str, float] | None = None,
    benchmarks: BenchmarkTable | dict[str, Any] | None = None,
    mapping: CategoryGifiMap | dict[str, Any] | None = None,
    config: ReasonablenessConfig = reasonableness_config,
) -> ReasonablenessResult:
    """Evaluate peer-benchmark reasonableness. Deterministic, no I/O on the
    hot path."""
    dismissed = set(dismissed_flag_keys or [])
    fx = fx_rates or {}
    bench = benchmarks if isinstance(benchmarks, BenchmarkTable) else (
        BenchmarkTable(benchmarks) if isinstance(benchmarks, dict) else load_benchmarks()
    )
    cmap = mapping if isinstance(mapping, CategoryGifiMap) else (
        CategoryGifiMap(mapping) if isinstance(mapping, dict) else load_category_gifi_map()
    )
    attribution = bench.attribution
    benchmark_year = bench.benchmark_year
    base_currency = (base_currency or "CAD").upper()

    # 1. Period
    days = (period_end - period_start).days + 1
    days = max(days, 1)
    period_months = round(days / 30.4, 1)
    is_partial = days < config.partial_year_min_days

    # 2. FX-collapse + classify
    operating_revenue = 0.0
    generic_revenue = 0.0
    distribution_total = 0.0
    bucket_amount: dict[str, float] = defaultdict(float)
    bucket_scrutiny: dict[str, float] = defaultdict(lambda: 1.0)
    bucket_sources: dict[str, set[str]] = defaultdict(set)
    bucket_primary_cat: dict[str, str] = {}
    bucket_primary_amt: dict[str, float] = defaultdict(float)
    generic_primary_cat = "Income"
    generic_primary_amt = 0.0

    for row in category_rows:
        category = _row_get(row, "category") or "Uncategorized"
        currency = (_row_get(row, "currency") or base_currency).upper()
        income = float(_row_get(row, "income", 0.0) or 0.0)
        expenses = float(_row_get(row, "expenses", 0.0) or 0.0)
        net_raw = _row_get(row, "net", None)
        net = float(net_raw) if net_raw is not None else (income - expenses)
        rate = 1.0 if currency == base_currency else float(fx.get(currency, config.fx_fallback))

        m = cmap.resolve(category)
        if m.gifi_bucket in ("transfer", "non_operating"):
            continue
        if m.is_distribution:
            distribution_total += abs(net * rate)
            continue
        if m.is_operating_revenue:
            amt = income * rate
            operating_revenue += amt
            if amt >= config.materiality_floor_abs:
                bucket_sources["operating_revenue"].add(category)
            if m.is_generic:
                generic_revenue += amt
                if amt > generic_primary_amt:
                    generic_primary_amt = amt
                    generic_primary_cat = category
            continue
        # expense line
        amt = expenses * rate
        if amt <= 0:
            continue
        bucket_amount[m.gifi_bucket] += amt
        bucket_scrutiny[m.gifi_bucket] = max(
            bucket_scrutiny[m.gifi_bucket], m.cra_scrutiny_weight
        )
        if amt >= config.materiality_floor_abs:
            bucket_sources[m.gifi_bucket].add(category)
        if amt > bucket_primary_amt[m.gifi_bucket]:
            bucket_primary_amt[m.gifi_bucket] = amt
            bucket_primary_cat[m.gifi_bucket] = category

    total_expenses = sum(bucket_amount.values())
    # naics_source is resolved by the caller (profile | industry_map |
    # data_inferred); default to "profile" when a code is present.
    if naics_source is None:
        naics_source = "profile" if naics_code else None

    # 3. Guardrails
    def _suppressed(reason: str, *, revenue_band: str | None = None) -> ReasonablenessResult:
        return ReasonablenessResult(
            band="typical",
            band_label="",
            naics_used=naics_code if reason != "missing_naics" else None,
            naics_source=naics_source if reason != "missing_naics" else None,
            naics_fallback_level=0,
            revenue_band=revenue_band,
            quartile=None,
            operating_revenue=round(operating_revenue, 2),
            currency=base_currency,
            period_months=period_months,
            is_partial_year=is_partial,
            annualized=is_partial,
            flags=[],
            comparisons=[],
            total_flags_before_cap=0,
            reassurance=[],
            suppressed_reason=reason,
            dismissed_applied=[],
            attribution=attribution,
            benchmark_year=benchmark_year,
            cohort_label=None,
            cohort_sample_size=None,
            cohort_pct_profitable=None,
            cohort_gross_margin=None,
            cohort_net_margin=None,
            cohort_source_url=None,
            cohort_province=None,
            annualized_revenue=round(operating_revenue, 2),
        )

    eff_revenue = operating_revenue * 365.0 / days if is_partial else operating_revenue
    if operating_revenue <= 0 or eff_revenue < config.revenue_floor:
        return _suppressed("below_revenue_floor")
    if not naics_code:
        return _suppressed("missing_naics")

    # Band derives from ANNUALIZED operating revenue. FPD bands are ANNUAL
    # figures ($30K-$5M / $5M-$20M), so a monthly or partial-year view must be
    # annualized first — otherwise a perfectly healthy business whose in-period
    # revenue is under $30K (i.e. almost any single month) gets no band and a
    # blank card. The shipped table seeds only 30K-5M cohorts, so anything that
    # lands in a larger band (or above the FPD ceiling) falls back to 30K-5M
    # rather than showing nothing.
    province_code = PROVINCE_TO_SGC.get((province or "").strip()) if province else None
    rev_band = revenue_band_for(eff_revenue, config)
    # Density-gated cohort selection: match the user's annualized revenue to the
    # size sub-cohort whose real $ bounds bracket it, else the whole band — the
    # FINEST cohort (NAICS 6→5→4 × province→national) that clears the min-N gate.
    cell, trail = bench.select_cell(naics_code, rev_band, eff_revenue, province_code, config)
    if cell is None and rev_band != "30K-5M":
        cell, trail = bench.select_cell(naics_code, "30K-5M", eff_revenue, province_code, config)
        if cell is not None:
            rev_band = "30K-5M"
    if cell is None:
        return _suppressed("no_benchmark_cell", revenue_band=rev_band)
    fb_level = trail["naics_depth"]

    # 4. Layer A — per-line ratio deviation. Builds BOTH the always-visible
    #    per-category comparison table (every material benchmarkable bucket,
    #    including in-line ones) AND the ranked concern flags (deviating only).
    flags: list[Flag] = []
    comparisons: list[Comparison] = []
    ratio_flag_buckets: set[str] = set()
    for bucket, amt in bucket_amount.items():
        r_i = amt / operating_revenue
        # materiality floor: skip tiny lines (both % and $ below threshold)
        if r_i < config.materiality_floor_pct and amt < config.materiality_floor_abs:
            continue
        scrutiny = bucket_scrutiny[bucket]
        label = _bucket_label(bucket)
        line = cell.get_line(bucket)
        peer_pct = line["pct_of_revenue"] if line is not None else None
        q1 = line["q1"] if line is not None else None
        q3 = line["q3"] if line is not None else None
        bcount = cell.business_count

        if line is None or peer_pct is None:
            # ISED folds this bucket into "other operating expenses" (travel,
            # software/IT, office, meals, vehicle). No peer figure exists — show
            # the category honestly (nothing looks missing) and flag a
            # CRA-scrutinized line over the absolute threshold as a concern.
            thresh = {
                "travel": config.travel_abs,
                "meals_entertainment": config.meals_abs,
                "motor_vehicle": config.vehicle_abs,
            }.get(bucket)
            is_watch = scrutiny > 1.0 and thresh is not None and r_i > thresh
            comparisons.append(
                Comparison(
                    label=label,
                    category=bucket_primary_cat.get(bucket, label),
                    gifi_bucket=bucket,
                    your_pct=r_i,
                    peer_pct=None,
                    peer_band=None,
                    amount=round(amt, 2),
                    direction="n/a",
                    status="watch" if is_watch else "no_benchmark",
                    cra_scrutinized=scrutiny > 1.0,
                    flag_key=(f"pattern_{bucket}_over_threshold" if is_watch else None),
                    business_count=None,
                    quality=None,
                    recommendation=(
                        None if is_watch
                        else _recommendation(label, "no_benchmark", None, r_i, None, base_currency)
                    ),
                    unavailable_reason="grouped_other",
                    weight=0.0,
                )
            )
            continue

        # ── min-N suppression ──
        # Null/absent real bounds or a thin cohort → SUPPRESS the peer comparison
        # for this line. NEVER fabricate a ±default_spread_pct band.
        if bcount is None or bcount < config.min_business_count:
            comparisons.append(
                Comparison(
                    label=label,
                    category=bucket_primary_cat.get(bucket, label),
                    gifi_bucket=bucket,
                    your_pct=r_i,
                    peer_pct=None,
                    peer_band=None,
                    amount=round(amt, 2),
                    direction="n/a",
                    status="no_benchmark",
                    cra_scrutinized=scrutiny > 1.0,
                    flag_key=None,
                    business_count=bcount,
                    quality=cell.quality,
                    recommendation=None,
                    unavailable_reason="insufficient_filings",
                    weight=0.0,
                )
            )
            continue

        # ── real, dense-enough peer point (median-first) ──
        band_pair = [q1, q3] if (q1 is not None and q3 is not None) else None
        status, comparison_direction = _status_from_bounds(r_i, q1, q3, peer_pct, config)
        is_concern = status != "in_line"
        direction = comparison_direction if comparison_direction != "n/a" else (
            "high" if r_i >= peer_pct else "low"
        )
        flag_key = f"ratio_{bucket}_{direction}" if is_concern else None
        rec = (
            _recommendation(label, status, peer_pct, r_i, bcount, base_currency)
            if is_concern else None
        )
        # Weight uses the REAL IQR/2 half-spread, never a fabricated default spread.
        half = (
            (q3 - q1) / 2.0 if (band_pair is not None and q3 > q1)
            else max(abs(r_i - peer_pct), config.min_spread)
        )
        dist = abs(r_i - peer_pct) / max(half, config.min_spread)
        materiality_factor = config.materiality_floor_frac + min(
            r_i / config.materiality_ref_pct, 1.0
        )
        weight = dist * scrutiny * materiality_factor

        comparisons.append(
            Comparison(
                label=label,
                category=bucket_primary_cat.get(bucket, label),
                gifi_bucket=bucket,
                your_pct=r_i,
                peer_pct=peer_pct,               # median-first central value
                peer_band=band_pair,             # real [q1, q3] or None
                amount=round(amt, 2),
                direction=comparison_direction,
                status=status,
                cra_scrutinized=scrutiny > 1.0,
                flag_key=flag_key,
                business_count=bcount,
                quality=cell.quality,
                recommendation=rec,
                unavailable_reason=None,
                weight=(weight if is_concern else 0.0),
            )
        )

        if not is_concern:
            continue  # in interquartile range — table only, no concern flag

        severity = "stands_out" if status in ("well_above", "well_below") else "elevated"
        flags.append(
            Flag(
                flag_key=flag_key,
                layer="A",
                severity=severity,
                category=bucket_primary_cat.get(bucket, label),
                direction=direction,
                gifi_bucket=bucket,
                title=_ratio_title(label, direction),
                prompt=rec,                      # deterministic recommendation IS the prompt
                your_pct=r_i,
                peer_pct=peer_pct,
                peer_band=band_pair,
                amount=round(amt, 2),
                weight=weight,
                cra_scrutinized=scrutiny > 1.0,
                business_count=bcount,
                recommendation=rec,
            )
        )
        ratio_flag_buckets.add(bucket)

    # 5. Layer B — named CRA-scrutiny patterns
    labour = bucket_amount.get("salaries_wages", 0.0) + bucket_amount.get("employee_benefits", 0.0)
    contractors = bucket_amount.get("subcontracts", 0.0) + bucket_amount.get("professional_fees", 0.0)

    # 5a. contractor skew (vs payroll) — subsumes the per-line subcontracts/wages flags
    if (
        (labour + contractors) > 0
        and contractors / (labour + contractors) > config.contractor_skew_frac
        and contractors / operating_revenue > config.contractor_skew_min_rev
    ):
        c_pct = contractors / operating_revenue
        l_pct = labour / operating_revenue
        flags.append(
            Flag(
                flag_key="pattern_contractor_heavy",
                layer="B",
                severity="elevated",
                category=bucket_primary_cat.get("subcontracts", "Purchases, materials and sub-contracts"),
                direction="high",
                gifi_bucket="subcontracts",
                title="Contractor spend is much larger than payroll",
                prompt=(
                    f"Contractors are {c_pct * 100:.1f}% of revenue "
                    f"({_fmt_money(contractors, base_currency)}) while payroll is "
                    f"{l_pct * 100:.1f}% ({_fmt_money(labour, base_currency)}). "
                    f"Worth confirming worker classification and any "
                    f"international-contractor withholding."
                ),
                your_pct=c_pct,
                peer_pct=None,
                peer_band=None,
                amount=round(contractors, 2),
                weight=c_pct * config.scrutiny_multipliers.get("subcontracts", 1.5) * 10.0,
                cra_scrutinized=True,
            )
        )
        suppress = {"subcontracts", "professional_fees", "salaries_wages"}
        flags = [f for f in flags if not (f.layer == "A" and f.gifi_bucket in suppress)]
        ratio_flag_buckets -= suppress

    # 5b. generic revenue bucket share
    if operating_revenue > 0 and generic_revenue / operating_revenue > config.generic_share_frac:
        g_pct = generic_revenue / operating_revenue
        flags.append(
            Flag(
                flag_key="pattern_generic_income_bucket",
                layer="B",
                severity="elevated",
                category=generic_primary_cat,
                direction="n/a",
                gifi_bucket="operating_revenue",
                title='Most revenue is in a generic "Income" bucket',
                prompt=(
                    f"About {g_pct * 100:.0f}% of revenue is in a generic "
                    f"'{generic_primary_cat}' category. Worth confirming these are "
                    f"all operating sales (not a loan, capital injection, or transfer)."
                ),
                your_pct=g_pct,
                peer_pct=None,
                peer_band=None,
                amount=round(generic_revenue, 2),
                weight=g_pct * 5.0,
                cra_scrutinized=False,
            )
        )

    # 5c. travel / meals / vehicle over an absolute threshold — universal
    #     fallback heuristic; only emit when Layer A did NOT already flag the
    #     bucket (the ratio flag carries richer peer context).
    for bucket, key, thresh in (
        ("travel", "pattern_travel_over_threshold", config.travel_abs),
        ("meals_entertainment", "pattern_meals_over_threshold", config.meals_abs),
        ("motor_vehicle", "pattern_vehicle_over_threshold", config.vehicle_abs),
    ):
        amt = bucket_amount.get(bucket, 0.0)
        if amt <= 0 or bucket in ratio_flag_buckets:
            continue
        r = amt / operating_revenue
        if r > thresh:
            label = _bucket_label(bucket)
            flags.append(
                Flag(
                    flag_key=key,
                    layer="B",
                    severity="elevated",
                    category=bucket_primary_cat.get(bucket, label),
                    direction="high",
                    gifi_bucket=bucket,
                    title=f"{label} looks high",
                    prompt=(
                        f"{label} is about {r * 100:.1f}% of your revenue "
                        f"({_fmt_money(amt, base_currency)}). This is one of the lines "
                        f"CRA looks at closely — worth confirming each item has a "
                        f"business purpose and nothing personal slipped in."
                    ),
                    your_pct=r,
                    peer_pct=None,
                    peer_band=None,
                    amount=round(amt, 2),
                    weight=r * config.scrutiny_multipliers.get(bucket, 2.0) * 10.0,
                    cra_scrutinized=True,
                )
            )

    # 5d. expenses exceed revenue
    if total_expenses > operating_revenue:
        flags.append(
            Flag(
                flag_key="pattern_expenses_exceed_revenue",
                layer="B",
                severity="stands_out",
                category="",
                direction="n/a",
                gifi_bucket=None,
                title="Expenses are higher than revenue this period",
                prompt=(
                    "Total expenses exceed operating revenue for this period. "
                    "If that's expected (start-up costs, a slow month), no action "
                    "needed — otherwise worth checking for miscategorized income "
                    "or one-off costs."
                ),
                your_pct=None,
                peer_pct=None,
                peer_band=None,
                amount=round(total_expenses, 2),
                weight=12.0,
                cra_scrutinized=False,
            )
        )

    # 5e. category fragmentation (≥2 distinct material source categories → one bucket)
    if any(len(cats) >= 2 for cats in bucket_sources.values()):
        flags.append(
            Flag(
                flag_key="pattern_category_fragmentation",
                layer="B",
                severity="elevated",
                category="",
                direction="n/a",
                gifi_bucket=None,
                title="Some categories look fragmented",
                prompt=(
                    "A few near-duplicate categories map to the same kind of expense. "
                    "Consolidating them makes your books easier to review and your "
                    "peer comparison more accurate."
                ),
                your_pct=None,
                peer_pct=None,
                peer_band=None,
                amount=None,
                weight=3.0,
                cra_scrutinized=False,
            )
        )

    # 5f. owner's draw present (positive info signal)
    if distribution_total > 0:
        flags.append(
            Flag(
                flag_key="info_owners_draw_present",
                layer="B",
                severity="info",
                category="Owner's Draw",
                direction="n/a",
                gifi_bucket="distribution",
                title="Owner's Draw is separated correctly",
                prompt=(
                    "Owner's Draw is present and correctly excluded from expenses — "
                    "nothing to change. If it's a shareholder-loan repayment or "
                    "dividend, just confirm it's recorded that way."
                ),
                your_pct=None,
                peer_pct=None,
                peer_band=None,
                amount=None,
                weight=0.0,
                cra_scrutinized=False,
            )
        )

    # 5g. Always-on profitability comparison vs the most-profitable quarter
    #     (pm_q4). PEER-DESCRIPTIVE only: never ranks the user's own
    #     margin. Suppressed entirely when the pm cohort is below min-N.
    profitability: list[ProfitabilityComparison] = []
    profitability_cohort_label: str | None = None
    pm_cell, _pm_trail = bench.select_profit_cohort(
        naics_code, rev_band, config.profitability_cohort, province_code, config
    )
    if bench.sufficient(pm_cell, config):
        for bucket, amt in bucket_amount.items():
            r_i = amt / operating_revenue
            if r_i < config.materiality_floor_pct and amt < config.materiality_floor_abs:
                continue
            pl = pm_cell.get_line(bucket)
            peer = pl["pct_of_revenue"] if pl is not None else None
            if peer is None:
                continue  # no pm_q4 line for this bucket → suppress (never fabricate)
            plabel = _bucket_label(bucket)
            profitability.append(
                ProfitabilityComparison(
                    label=plabel,
                    gifi_bucket=bucket,
                    your_pct=r_i,
                    peer_pct=peer,
                    business_count=pm_cell.business_count or 0,
                    note=_profitability_note(plabel, r_i, peer),
                )
            )
        profitability.sort(key=lambda p: (-p.your_pct, p.gifi_bucket))
        if profitability:
            profitability_cohort_label = (
                "the most-profitable quarter of similar Canadian businesses"
            )

    # 6. Finalize — dismiss filter → rank → cap → band → reassurance
    dismissed_applied = [f.flag_key for f in flags if f.flag_key in dismissed]
    flags = [f for f in flags if f.flag_key not in dismissed]
    total_flags_before_cap = len(flags)
    flags.sort(key=lambda f: (-f.weight, f.flag_key))
    flags = flags[: config.top_n_flags]

    # Comparison table: biggest categories first (always shown in full).
    comparisons.sort(key=lambda c: (-c.your_pct, c.gifi_bucket))

    band = _aggregate_band(flags, config)

    reassurance: list[str] = []
    net = operating_revenue - total_expenses
    net_margin = net / operating_revenue if operating_revenue > 0 else 0.0
    peer_margin = cell.ratios.get("net_profit_margin")
    if peer_margin is not None and net_margin > peer_margin:
        reassurance.append(
            f"Your net margin (~{net_margin * 100:.1f}%) is above the industry "
            f"average (~{peer_margin * 100:.1f}%) — a strong sign, not a concern."
        )

    return ReasonablenessResult(
        band=band,
        band_label=BAND_LABELS[band],
        naics_used=naics_code,
        naics_source=naics_source,
        naics_fallback_level=fb_level,
        comparisons=comparisons,
        revenue_band=rev_band,
        quartile=trail["cohort"],
        operating_revenue=round(operating_revenue, 2),
        currency=base_currency,
        period_months=period_months,
        is_partial_year=is_partial,
        annualized=is_partial,
        flags=flags,
        total_flags_before_cap=total_flags_before_cap,
        reassurance=reassurance,
        suppressed_reason=None,
        dismissed_applied=dismissed_applied,
        attribution=attribution,
        benchmark_year=benchmark_year,
        cohort_label=cell.naics_label or None,
        cohort_sample_size=cell.business_count,        # businesses behind the selected cell
        cohort_pct_profitable=cell.pct_profitable,
        cohort_gross_margin=cell.ratios.get("gross_margin"),
        cohort_net_margin=cell.ratios.get("net_profit_margin"),
        cohort_source_url=cell.source_url,
        cohort_province=cell.province_name,
        annualized_revenue=round(eff_revenue, 2),
        size_cohort=trail["cohort"],
        size_cohort_label=_size_cohort_label(cell),
        cohort_quality=cell.quality,
        cohort_business_count=cell.business_count,
        cohort_trail=trail,
        profitability=profitability,
        profitability_cohort_label=profitability_cohort_label,
    )


def _aggregate_band(flags: list[Flag], config: ReasonablenessConfig) -> str:
    real = [f for f in flags if f.severity != "info"]
    if not real:
        return "typical"
    n_stands_out = sum(1 for f in real if f.severity == "stands_out")
    n_total = len(real)
    if n_stands_out >= config.several_stands_out or n_total >= config.several_total_flags:
        return "several"
    return "a_few"

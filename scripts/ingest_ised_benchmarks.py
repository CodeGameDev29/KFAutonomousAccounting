#!/usr/bin/env python3
"""Offline ISED Financial Performance Data (FPD) → benchmark JSON transform.

**Never imported by the app, never called at request time.** Run manually to
(re)generate ``config/ised_benchmarks.json`` — the static peer-benchmark table
the reasonableness engine reads.

Two modes:

  --seed-only   Emit a small illustrative table: a few industries with round,
                invented figures, plus one ``rev_q2`` and one ``pm_q4`` cohort
                cell so the cohort code paths have something to read. It makes
                the engine and its tests runnable without a download. The
                numbers are examples, not industry data, and the file says so
                in ``meta``. Deterministic — re-running produces a
                byte-identical file.

  (default)     Download the FPD CSVs from the Open Government Portal
                (CKAN dataset ``3f9e1080-…`` for 2024), parse, normalize,
                validate fail-loud, and emit. Emits up to nine cohort cells per
                (naics, geo): ``all`` (national + provinces) plus
                ``rev_q1..rev_q4`` and ``pm_q1..pm_q4`` (national-only).
                Raw CSVs are NOT committed; only the parsed JSON.

CLI:
  python scripts/ingest_ised_benchmarks.py --seed-only \
      --out config/ised_benchmarks.json
  python scripts/ingest_ised_benchmarks.py --year 2024 \
      --out config/ised_benchmarks.json [--raw-dir DIR] [--source-url URL] \
      [--anchor anchor.json] [--dry-run]

Every run validates structure and plausibility. ``--anchor`` adds an optional
check that one named cell still carries the figures you expect — see
:func:`load_anchor`. Any validation failure exits non-zero and does NOT
overwrite ``--out``.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("ingest_ised_benchmarks")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "config" / "ised_benchmarks.json"
CKAN_DATASET_ID = "3f9e1080-4a08-4c0f-ace7-1b94a1afd61d"  # FPD 2024
CKAN_PACKAGE_SHOW = (
    "https://open.canada.ca/data/api/3/action/package_show?id=" + CKAN_DATASET_ID
)

# ── The optional anchor check ───────────────────────────────────────────────
#
# Structure and plausibility are checked on every run, for everyone. An anchor
# is an extra, opt-in check: a small JSON file naming ONE cell and the figures
# it is expected to carry, so that a refresh which quietly changes what a cell
# means aborts instead of overwriting ``--out``. Which cell is worth watching
# depends on which table is being read, so the anchor is supplied per run
# rather than built in; with none supplied, the check does not run.
#
# Anchor file shape (only ``key`` is required; give it ``lines``, ``ratios``,
# or both):
#
#   {
#     "key": "<naics>|<revenue band>|<cohort>",
#     "tolerance": 0.02,
#     "lines":  {"subcontracts": 0.11, "salaries_wages": 0.29},
#     "ratios": {"net_profit_margin": 0.16, "gross_margin": 0.72}
#   }
#
# Supply it with ``--anchor PATH`` or by setting ``ISED_BENCHMARK_ANCHOR``.
ANCHOR_ENV_VAR = "ISED_BENCHMARK_ANCHOR"
#: Absolute fraction-of-revenue tolerance used when the anchor file omits one.
DEFAULT_ANCHOR_TOLERANCE = 0.02

# StatCan's own quartile-suppression bar. Ingest NEVER filters on it — thin
# cells are kept so the engine can *explain* the suppression at read time. Only
# used for the meta hint + an advisory validate log.
MIN_SAMPLE_SIZE = 20

# Plausibility bounds. Generous enough to keep every normal industry (commodity
# cohorts legitimately exceed 100% on some lines), tight enough that a genuine
# mis-scaled/mis-column read is caught. Pathological small-sample cohorts that
# breach these are dropped from the table with a logged count.
_PER_LINE_MAX = 8.0            # a single line's share of revenue (800%)
_LINE_SUM_MAX = 20.0          # sum of expense lines
_NET_MARGIN_RANGE = (-15.0, 1.5)
_GROSS_MARGIN_RANGE = (-0.5, 1.0)

# FPD "total expenses (percentage)" line label (English, first header line,
# normalized lower-case) → GIFI bucket slug, matched against the published
# CSV column headers. Lines not listed (opening/closing inventory, indirect
# operating-expenses aggregate, total expenses) are intentionally skipped;
# "net profit/loss" is handled specially as the net margin.
_FPD_LINE_TO_BUCKET: dict[str, str] = {
    "cost of sales (direct expenses)": "cost_of_sales",
    "wages and benefits": "employee_benefits",
    "purchases, materials and sub-contracts": "subcontracts",
    "labour and commissions": "salaries_wages",
    "amortization and depletion": "cca_depreciation",
    "repairs and maintenance": "repairs_maintenance",
    "utilities and telephone/telecommunication": "utilities",
    "rent": "rent",
    "interest and bank charges": "interest_bank",
    "professional and business fees": "professional_fees",
    "advertising and promotion": "advertising",
    "delivery, shipping and warehouse": "delivery",
    "insurance": "insurance",
    "other expenses": "other_operating",
}
_FPD_NET_MARGIN_LABEL = "net profit/loss"

# ISED suppression sentinels. The percentage parser (_num) treats any of these
# — and anything >= 9999 — as suppressed. Integer/dollar parsers (_int / _revk)
# use ONLY the explicit large sentinels, because a business count or a $-range
# is legitimately >= 9999 (a cohort of tens of thousands of firms would be
# wrongly nulled by the >=9999 percentage catch-all).
_SUPPRESSED_SENTINELS = (9999.0, 99999.9, 999999.0)
_BIG_SENTINELS = (99999.9, 999999.0)  # for counts / dollar ranges

_CANADA_CODES = {"00", "0"}         # province code for national (varies by band file)
_INCORP_PREFERENCE = ["3", "2", "1"]  # all-businesses > incorporated > unincorporated

# StatCan Standard Geographical Classification province/territory code → name.
# (Regional aggregates like 15/49/63 are intentionally excluded — the table keys on the
# 10 provinces + 3 territories that match business_profile.province.)
_SGC_PROVINCES: dict[str, str] = {
    "10": "Newfoundland and Labrador", "11": "Prince Edward Island",
    "12": "Nova Scotia", "13": "New Brunswick", "24": "Quebec", "35": "Ontario",
    "46": "Manitoba", "47": "Saskatchewan", "48": "Alberta",
    "59": "British Columbia", "60": "Yukon", "61": "Northwest Territories",
    "62": "Nunavut",
}

# Revenue-band slug → (file token, band label used in resource filenames).
_BANDS = [("30K-5M", "30k_5m"), ("5M-20M", "5m_20m")]

# Cohort keys the engine reads (key slot 3). "all" carries the honest per-line
# size-cohort band; rev_q* / pm_q* are national-only point cells.
_REV_COHORTS = ("rev_q1", "rev_q2", "rev_q3", "rev_q4")
_PM_COHORTS = ("pm_q1", "pm_q2", "pm_q3", "pm_q4")
_ALL_COHORTS = ["all", *_REV_COHORTS, *_PM_COHORTS]

# Known dataset ids by year (falls back to a CKAN search if absent).
_YEAR_DATASETS: dict[int, str] = {
    2024: "3f9e1080-4a08-4c0f-ace7-1b94a1afd61d",
    2023: "16b7eeb0-9a97-46c0-af3d-7f2114dedf5e",
    2022: "ee04cfb2-9473-47cb-a0e1-64823da7ab82",
}

_ATTRIBUTION = (
    "Contains information licensed under the Open Government "
    "Licence – Canada. Source: ISED/StatCan Financial "
    "Performance Data."
)
_QUALITY_NOTE = (
    "A–E is Statistics Canada's data-quality rating (A best). Figures cover "
    "filing/surviving firms only (survivorship caveat)."
)
_MIN_N_NOTE = (
    "Cohorts with fewer than 20 filing businesses are shown as 'not enough "
    "Canadian filings to show a reliable range.'"
)


# ── Seed data ────────────────────────────────────────────────────────────────

def _seed_benchmarks() -> dict[str, Any]:
    """A small illustrative table, so the engine runs without a download.

    Every figure below is a round invented example — not industry data, and
    not a transcription of any published release. The industries are the ones
    the engine's coarse industry labels map to, so each mapped label has a cell
    to read, plus one more that carries a ``rev_q2`` and a ``pm_q4`` cell so the
    cohort code paths have something to select. Which industry carries those is
    arbitrary: no code path is keyed to it.
    """
    def _cell(naics, label, *, cohort, business_count, quality, profitable, gross,
              net, lines, cohort_rev_low=None, cohort_rev_high=None):
        return {
            "naics": naics,
            "naics_label": label,
            "revenue_band": "30K-5M",
            "cohort": cohort,
            "business_count": business_count,
            "sample_size": business_count,  # deprecated alias (one release)
            "quality": quality,
            "cohort_revenue_low": cohort_rev_low,
            "cohort_revenue_high": cohort_rev_high,
            "pct_profitable": profitable,
            "ratios": {"gross_margin": gross, "net_profit_margin": net},
            # No per-cell source: these figures are invented, so there is no
            # page to cite. The downloaded table links each cell to its own.
            "source_url": None,
            "lines": {"operating_revenue": 1.0, **lines},
        }

    return {
        "meta": _seed_meta(),
        "cells": {
            "2361|30K-5M|all": _cell(
                "2361", "Residential building construction",
                cohort="all", business_count=50000, quality="A",
                profitable=0.72, gross=0.35, net=0.08,
                lines={
                    "cost_of_sales": 0.65,
                    "subcontracts": 0.45,
                    "salaries_wages": 0.12,
                    "employee_benefits": 0.07,
                    "rent": 0.02,
                },
            ),
            "5414|30K-5M|all": _cell(
                "5414", "Specialized design services",
                cohort="all", business_count=40000, quality="A",
                profitable=0.75, gross=0.70, net=0.15,
                lines={
                    # A couple of lines carry a size-cohort band to exercise the
                    # {q1,median,q3} display path; the rest are bare floats (no
                    # reliable band -> the engine shows a median tick only).
                    "salaries_wages": {"pct_of_revenue": 0.30, "q1": 0.20, "median": 0.30, "q3": 0.40},
                    "subcontracts": {"pct_of_revenue": 0.12, "q1": 0.05, "median": 0.12, "q3": 0.20},
                    "employee_benefits": 0.035,
                    "professional_fees": 0.05,
                    "advertising": 0.04,
                    "rent": 0.04,
                    "cca_depreciation": 0.025,
                },
            ),
            "5414|30K-5M|rev_q2": _cell(
                "5414", "Specialized design services",
                cohort="rev_q2", business_count=10000, quality="A",
                profitable=0.75, gross=0.72, net=0.16,
                cohort_rev_low=150000, cohort_rev_high=600000,
                lines={
                    "salaries_wages": 0.28,
                    "subcontracts": 0.14,
                    "employee_benefits": 0.03,
                    "professional_fees": 0.045,
                    "rent": 0.035,
                },
            ),
            "5414|30K-5M|pm_q4": _cell(
                "5414", "Specialized design services",
                cohort="pm_q4", business_count=10000, quality="A",
                profitable=1.0, gross=0.75, net=0.30,
                lines={
                    # the top profit-margin quarter spends less on most lines
                    "salaries_wages": 0.24,
                    "subcontracts": 0.09,
                    "employee_benefits": 0.025,
                    "professional_fees": 0.04,
                    "rent": 0.025,
                },
            ),
            "5415|30K-5M|all": _cell(
                "5415", "Computer systems design and related services",
                cohort="all", business_count=50000, quality="A",
                profitable=0.80, gross=0.78, net=0.18,
                lines={
                    "salaries_wages": 0.34,
                    "subcontracts": 0.14,
                    "employee_benefits": 0.045,
                    "professional_fees": 0.05,
                    "advertising": 0.025,
                    "rent": 0.03,
                },
            ),
            "5416|30K-5M|all": _cell(
                "5416", "Management, scientific and technical consulting services",
                cohort="all", business_count=50000, quality="A",
                profitable=0.82, gross=0.82, net=0.30,
                lines={
                    "salaries_wages": 0.26,
                    "employee_benefits": 0.03,
                    "subcontracts": 0.11,
                    "professional_fees": 0.055,
                    "advertising": 0.02,
                    "rent": 0.025,
                },
            ),
            "7225|30K-5M|all": _cell(
                "7225", "Restaurants and other eating places",
                cohort="all", business_count=50000, quality="A",
                profitable=0.58, gross=0.52, net=0.04,
                lines={
                    "cost_of_sales": 0.48,
                    "salaries_wages": 0.27,
                    "employee_benefits": 0.075,
                    "rent": 0.09,
                    "advertising": 0.025,
                },
            ),
        },
    }


def _seed_meta() -> dict[str, Any]:
    """Meta for the illustrative table: it says outright that it is invented.

    No licensed data is in this file, so it carries no attribution — claiming
    one would misattribute made-up numbers to a public release. The download
    mode's meta (:func:`_download_meta`) carries the real attribution.
    """
    return {
        "source": "SYNTHETIC — illustrative figures, not a published release",
        "synthetic": True,
        "source_year": 2024,
        "notice": (
            "Illustrative example figures, invented for this file so the "
            "engine runs without a download. They are NOT industry data and "
            "must not be read as one: no business was surveyed and no Open "
            "Government Licence data file was used to build them. Re-run "
            "scripts/ingest_ised_benchmarks.py without --seed-only to download "
            "and transform the real Financial Performance Data release."
        ),
        "data_note": (
            "Shaped like the downloaded table: per-line values are shares of "
            "revenue, a line may carry a {q1, median, q3} band or a bare "
            "central value, rev_q* cohorts carry a revenue range and pm_q4 is "
            "the top profit-margin cohort."
        ),
        "geography": "Canada (national)",
        "source_tool_url": "https://ised-isde.canada.ca/site/financial-performance-data/en",
        "source_dataset_url": "https://open.canada.ca/data/en/dataset/" + CKAN_DATASET_ID,
        "revenue_bands": ["30K-5M", "5M-20M"],
        "cohorts": list(_ALL_COHORTS),
        "quartiles": ["all"],  # deprecated alias, kept one release
        "min_business_count": MIN_SAMPLE_SIZE,
        "quality_note": _QUALITY_NOTE,
        "min_n_note": _MIN_N_NOTE,
        "revenue_floor_cad": 30000,
    }


# ── Real download pipeline ──────────────────────────────────────────────────

_UA = {"User-Agent": "AutonomousAccounting-FPD-ingest/1.0"}


def _http_get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed gov host
        return r.read()


def _download(url: str, raw_dir: Path) -> bytes:
    """Download to the raw cache (raw CSVs are NOT committed), reusing if present."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / url.rsplit("/", 1)[-1]
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    logger.info("downloading %s", url)
    data = _http_get(url)
    dest.write_bytes(data)
    return data


def _read_csv(raw: bytes) -> list[list[str]]:
    # FPD CSVs are Windows-1252 with bilingual headers; cp1252 decodes cleanly.
    text = raw.decode("cp1252", "replace")
    return list(csv.reader(io.StringIO(text)))


def _is_sentinel(v: float) -> bool:
    """A percentage-cell sentinel: an explicit ISED nines-marker, or anything
    >= 9999 (no real *percentage* reaches that)."""
    return any(abs(v - s) < 0.5 for s in _SUPPRESSED_SENTINELS) or v >= 9999.0


def _num(cell: str | None) -> float | None:
    """Parse a percentage cell → fraction, or None if blank/suppressed."""
    if cell is None:
        return None
    s = cell.strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if _is_sentinel(v):
        return None
    return round(v / 100.0, 4)


def _int(cell: str | None) -> int | None:
    """Parse a business-count cell → int (NO /100). Blank / non-numeric /
    explicit large sentinel → None. Deliberately does NOT reject values >= 9999
    (a real cohort count is legitimately larger than that)."""
    if cell is None:
        return None
    s = cell.strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v < 0 or any(abs(v - s2) < 0.5 for s2 in _BIG_SENTINELS):
        return None
    return int(round(v))


def _revk(cell: str | None) -> float | None:
    """Parse a revenue-range cell (published in $THOUSANDS) → dollars (× 1000).
    Blank / non-positive / explicit large sentinel → None. Does NOT apply the
    >= 9999 catch-all (a $-range in the 5M-20M band reaches 20000 thousand)."""
    if cell is None:
        return None
    s = cell.strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 0 or any(abs(v - s2) < 0.5 for s2 in _BIG_SENTINELS):
        return None
    return round(v * 1000.0, 2)


def _quality(cell: str | None) -> str | None:
    """Return the A–E data-quality letter, else None."""
    if cell is None:
        return None
    s = cell.strip().upper()
    return s if s in {"A", "B", "C", "D", "E"} else None


def _monotone(values: list[float]) -> bool:
    """True if the series is non-decreasing OR non-increasing (in given order)."""
    if len(values) < 2:
        return True
    nondec = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
    noninc = all(values[i] >= values[i + 1] for i in range(len(values) - 1))
    return nondec or noninc


def _first_label(header_cell: str) -> str:
    """First non-empty line of a header cell, normalized (English label)."""
    for line in (header_cell or "").split("\n"):
        s = " ".join(line.strip().lower().split())
        if s:
            return s
    return ""


def _rq_indices(header: list[str], avg_i: int) -> list[int] | None:
    """The 4 revenue-/margin-quartile sub-column indices for the 7-col block
    that starts at ``avg_i`` — verified by their header labels so silent layout
    drift is caught (returns None if any sub-column label is absent)."""
    idxs = [avg_i + 2, avg_i + 3, avg_i + 4, avg_i + 5]
    pats = ["bottom quartile", "lower middle", "upper middle", "top quartile"]
    for j, pat in zip(idxs, pats):
        if j >= len(header) or pat not in _first_label(header[j]):
            return None
    return idxs


def _line_columns(
    header: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Map bucket → block descriptor, and locate the net-margin block.

    Each expense LINE is a 7-column block:
      [average %, "Quality Indicator", "Bottom Quartile <line>",
       "Lower Middle <line>", "Upper Middle <line>", "Top Quartile <line>",
       "% of Businesses Reporting"].

    Returns:
      cols[bucket] = {"avg": i, "quality": i+1|None, "rq": [i1..i4]|None}
        rq is the 4 by-revenue (or by-profit-margin, in the PM file) sub-columns.
      net_block = {"avg": i, "rq": [...]|None} | None
    """
    cols: dict[str, dict[str, Any]] = {}
    net_block: dict[str, Any] | None = None
    for i, cell in enumerate(header):
        label = _first_label(cell)
        if label == _FPD_NET_MARGIN_LABEL and net_block is None:
            net_block = {"avg": i, "rq": _rq_indices(header, i)}
            continue
        bucket = _FPD_LINE_TO_BUCKET.get(label)
        if bucket and bucket not in cols:
            q_i = (i + 1) if (i + 1 < len(header)
                              and "quality" in _first_label(header[i + 1])) else None
            cols[bucket] = {"avg": i, "quality": q_i, "rq": _rq_indices(header, i)}
    return cols, net_block


def _revenue_columns(header: list[str]) -> dict[str, Any]:
    """Locate business_count, whole-industry quality, and the per-quartile
    revenue-range low/high columns in a ``total_revenue`` file, by header label."""
    def find(*must: str) -> int | None:
        for i, c in enumerate(header):
            lab = _first_label(c)
            if all(m in lab for m in must):
                return i
        return None

    count = find("whole industry", "reliability")
    if count is None:
        count = 3  # verified fixed position
    return {
        "count": count,
        "quality": find("quality indicator"),  # first == whole-industry quality
        "ranges": {
            "rev_q1": (find("bottom quartile", "low revenue range"),
                       find("bottom quartile", "high revenue range")),
            "rev_q2": (find("lower middle", "low revenue range"),
                       find("lower middle", "high revenue range")),
            "rev_q3": (find("upper middle", "low revenue range"),
                       find("upper middle", "high revenue range")),
            "rev_q4": (find("top quartile", "low revenue range"),
                       find("top quartile", "high revenue range")),
        },
    }


def _geo_rows(rows: list[list[str]]) -> dict[tuple[str, str], list[str]]:
    """{(naics, geo): best row}. geo = "CA" for national, else an SGC province
    code. Prefer incorporation "3" (all businesses) → "2" → "1"."""
    by: dict[tuple[str, str], dict[str, list[str]]] = {}
    for r in rows[1:]:
        if len(r) <= 2:
            continue
        prov = r[2].strip()
        if prov in _CANADA_CODES:
            geo = "CA"
        elif prov in _SGC_PROVINCES:
            geo = prov
        else:
            continue  # skip regional aggregates
        by.setdefault((r[1].strip(), geo), {}).setdefault(r[0].strip(), r)
    out: dict[tuple[str, str], list[str]] = {}
    for key, by_inc in by.items():
        for pref in _INCORP_PREFERENCE:
            if pref in by_inc:
                out[key] = by_inc[pref]
                break
    return out


def _parse_revenue(rows: list[list[str]], rev_cols: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """{(naics, geo): {business_count, quality, ranges}} from a total_revenue file.
    ranges = {rev_qN: (low_$, high_$)} in actual dollars (published thousands × 1000)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (naics, geo), row in _geo_rows(rows).items():
        ci = rev_cols["count"]
        qi = rev_cols["quality"]
        bc = _int(row[ci]) if (ci is not None and ci < len(row)) else None
        ql = _quality(row[qi]) if (qi is not None and qi < len(row)) else None
        ranges: dict[str, tuple[float | None, float | None]] = {}
        for qn, (li, hi) in rev_cols["ranges"].items():
            lo = _revk(row[li]) if (li is not None and li < len(row)) else None
            h = _revk(row[hi]) if (hi is not None and hi < len(row)) else None
            ranges[qn] = (lo, h)
        out[(naics, geo)] = {"business_count": bc, "quality": ql, "ranges": ranges}
    return out


def _line_band(avg: float, rq_values: list[float | None] | None) -> tuple[Any, bool]:
    """Derive a line's on-disk representation + whether its band was dropped.

    median == the whole-band average; q1/q3 = min/max of the four
    revenue-quartile figures — but ONLY when the series is monotonic in cohort
    order AND q1 <= median <= q3. On pass → a {pct_of_revenue,q1,median,q3} dict.
    Otherwise → a bare float (== the average; the engine reads it as a no-band
    line). NEVER sort the values to fabricate a clean band.

    Returns (representation, band_dropped) where band_dropped is True only for a
    genuine inversion (not the not-enough-data case).
    """
    if rq_values is None:
        return avg, False  # file has no sub-columns for this line → no band
    present = [v for v in rq_values if v is not None]
    if len(present) < 3:
        return avg, False  # too few cohort figures to form a band
    q1, q3 = min(present), max(present)
    median = avg
    if _monotone(present) and q1 <= median <= q3:
        return {"pct_of_revenue": avg, "q1": q1, "median": median, "q3": q3}, False
    return avg, True  # inverted / average-outside-band → suppress the band


def _line_pct(line: Any) -> float | None:
    """A benchmark line may be a bare float or a {'pct_of_revenue': ...} dict."""
    if isinstance(line, (int, float)):
        return float(line)
    if isinstance(line, dict):
        return line.get("pct_of_revenue")
    return None


def _plausible(lines: dict[str, Any], net: float | None,
               ratios: dict[str, float], pct_profitable: float | None) -> bool:
    """Drop pathological (real but unusable) cohorts — covers every bound
    _validate enforces, so the refresh never aborts on one bad cohort."""
    expense_sum = 0.0
    for b, v in lines.items():
        pct = _line_pct(v)
        if pct is None:
            continue
        if pct > _PER_LINE_MAX:
            return False
        if b != "operating_revenue":
            expense_sum += pct
    if expense_sum > _LINE_SUM_MAX:
        return False
    if net is not None and not (_NET_MARGIN_RANGE[0] <= net <= _NET_MARGIN_RANGE[1]):
        return False
    gm = ratios.get("gross_margin")
    if gm is not None and not (_GROSS_MARGIN_RANGE[0] <= gm <= _GROSS_MARGIN_RANGE[1]):
        return False
    if pct_profitable is not None and pct_profitable > 1.0:
        return False
    return True


def _point_cell_lines(row: list[str], cols: dict[str, dict[str, Any]],
                      qi: int) -> tuple[dict[str, Any], float | None]:
    """Build the per-line map for a cohort (rev_q*/pm_q*) POINT cell: each line's
    value is that line's quartile sub-column (qi ∈ 0..3), ÷100. Returns (lines, cogs)."""
    lines: dict[str, Any] = {"operating_revenue": 1.0}
    cogs = None
    for bucket, info in cols.items():
        rq = info.get("rq")
        if not rq:
            continue
        j = rq[qi]
        val = _num(row[j]) if j < len(row) else None
        if val is None:
            continue
        if bucket == "cost_of_sales":
            cogs = val
        lines[bucket] = val  # bare float (point cell — no band)
    return lines, cogs


def _cohort_ratios(cogs: float | None, net: float | None) -> dict[str, float]:
    ratios: dict[str, float] = {}
    if net is not None:
        ratios["net_profit_margin"] = net
    if cogs is not None and cogs < 1.0:
        ratios["gross_margin"] = round(1.0 - cogs, 4)
    return ratios


def _download_and_parse(year: int, raw_dir: Path, source_url: str | None) -> dict[str, Any]:
    """Download the ISED FPD CSVs for ``year`` and parse → benchmark dict.

    Emits up to nine cohort cells per (naics, geo): ``all`` (national +
    provinces, with the honest per-line size-cohort band) plus ``rev_q1..q4``
    and ``pm_q1..q4`` (national-only)."""
    dataset_id = _resolve_dataset_id(year, source_url)
    raw_dir = raw_dir / str(year)  # year-scope the raw cache
    pkg = json.loads(_http_get(
        "https://open.canada.ca/data/api/3/action/package_show?id=" + dataset_id
    ).decode("utf-8", "replace"))
    resources = pkg["result"]["resources"]

    def _find_csv(*needles: str, exclude: tuple[str, ...] = ()) -> str | None:
        for res in resources:
            low = (res.get("url") or "").lower()
            if not low.endswith(".csv"):
                continue
            if any(x in low for x in exclude):
                continue
            if all(n in low for n in needles):
                return res["url"]
        return None

    def _require(*needles: str, exclude: tuple[str, ...] = ()) -> str:
        url = _find_csv(*needles, exclude=exclude)
        if url is None:
            raise RuntimeError(f"FPD resource not found for {needles!r} in dataset {dataset_id}")
        return url

    # NAICS → English label.
    labels: dict[str, str] = {}
    for r in _read_csv(_download(_require("naics_descriptors"), raw_dir))[1:]:
        if r and r[0].strip():
            labels[r[0].strip()] = (r[1].strip() if len(r) > 1 else "")

    cells: dict[str, Any] = {}
    for band_slug, token in _BANDS:
        # ── revenue-cohort expenses file (whole-band + RQ sub-cols) ──
        exp = _read_csv(_download(
            _require("total_expenses_percentage", token, exclude=("profit_margin",)), raw_dir))
        _guard_header(exp[0], f"expenses-{band_slug}")
        cols, net_block = _line_columns(exp[0])
        _guard_line_columns(cols, net_block, f"expenses-{band_slug}")

        # ── total_revenue join (business_count, quality, revenue $ ranges) ──
        rev_rows = _read_csv(_download(
            _require("total_revenue", token, exclude=("profit_margin",)), raw_dir))
        _guard_header(rev_rows[0], f"revenue-{band_slug}")
        rev_cols = _revenue_columns(rev_rows[0])
        _guard_revenue_columns(rev_cols, f"revenue-{band_slug}")
        rev_by_geo = _parse_revenue(rev_rows, rev_cols)

        # ── % profitable (published only for the 30K-5M band) ──
        prof_url = _find_csv("profit_non_profit", token, exclude=("profit_margin",))
        prof_by_geo = _geo_rows(_read_csv(_download(prof_url, raw_dir))) if prof_url else {}

        # ── profit-margin expenses + its revenue join (national-only cohorts) ──
        pm_cols: dict[str, dict[str, Any]] = {}
        pm_net_block: dict[str, Any] | None = None
        pm_geo: dict[tuple[str, str], list[str]] = {}
        pm_rev_by_geo: dict[tuple[str, str], dict[str, Any]] = {}
        pm_url = _find_csv("profit_margin", "total_expenses_percentage", token)
        if pm_url:
            pm = _read_csv(_download(pm_url, raw_dir))
            _guard_header(pm[0], f"pm-expenses-{band_slug}")
            pm_cols, pm_net_block = _line_columns(pm[0])
            pm_geo = _geo_rows(pm)
            pm_rev_url = _find_csv("profit_margin", "total_revenue", token)
            if pm_rev_url:
                pm_rev_rows = _read_csv(_download(pm_rev_url, raw_dir))
                pm_rev_cols = _revenue_columns(pm_rev_rows[0])
                pm_rev_by_geo = _parse_revenue(pm_rev_rows, pm_rev_cols)

        n_all = n_prov = n_cohort = dropped = dropped_bounds = 0

        for (naics, geo), row in _geo_rows(exp).items():
            rev_info = rev_by_geo.get((naics, geo))
            whole_count = rev_info["business_count"] if rev_info else None
            whole_quality = rev_info["quality"] if rev_info else None

            # ---- "all" cohort cell (national + provincial) ----
            lines: dict[str, Any] = {"operating_revenue": 1.0}
            cogs = None
            for bucket, info in cols.items():
                avg_i = info["avg"]
                avg = _num(row[avg_i]) if avg_i < len(row) else None
                if avg is None:
                    continue
                if bucket == "cost_of_sales":
                    cogs = avg
                rq_vals = None
                if info.get("rq"):
                    rq_vals = [_num(row[j]) if j < len(row) else None for j in info["rq"]]
                rep, band_dropped = _line_band(avg, rq_vals)
                if band_dropped:
                    dropped_bounds += 1
                lines[bucket] = rep
            net = _num(row[net_block["avg"]]) if (net_block and net_block["avg"] < len(row)) else None
            if net is None and len(lines) <= 1:
                continue  # nothing usable
            ratios = _cohort_ratios(cogs, net)
            prof_row = prof_by_geo.get((naics, geo))
            pct_profitable = _num(prof_row[3]) if (prof_row and len(prof_row) > 3) else None
            if not _plausible(lines, net, ratios, pct_profitable):
                dropped += 1
                continue
            cell: dict[str, Any] = {
                "naics": naics,
                "naics_label": labels.get(naics, ""),
                "revenue_band": band_slug,
                "cohort": "all",
                "business_count": whole_count,
                "sample_size": whole_count,  # deprecated alias
                "quality": whole_quality,
                "cohort_revenue_low": None,
                "cohort_revenue_high": None,
                "pct_profitable": pct_profitable,
                "ratios": ratios,
                "source_url": f"https://ised-isde.canada.ca/app/ixb/cis/performance/{naics}",
                "lines": lines,
            }
            if geo == "CA":
                cells[f"{naics}|{band_slug}|all"] = cell
                n_all += 1
            else:
                cell["province"] = geo
                cell["province_name"] = _SGC_PROVINCES[geo]
                cells[f"{naics}|{band_slug}|all|{geo}"] = cell
                n_prov += 1
                continue  # cohort cells are NATIONAL-only

            # ---- rev_q1..rev_q4 cohort cells (national only) ----
            ranges = rev_info["ranges"] if rev_info else {}
            per_q_count = round(whole_count / 4) if whole_count is not None else None
            for qi, qname in enumerate(_REV_COHORTS):
                qlines, qcogs = _point_cell_lines(row, cols, qi)
                if len([b for b in qlines if b != "operating_revenue"]) < 3:
                    continue  # too few usable lines for a point cell
                qnet = None
                if net_block and net_block.get("rq"):
                    jn = net_block["rq"][qi]
                    qnet = _num(row[jn]) if jn < len(row) else None
                qratios = _cohort_ratios(qcogs, qnet)
                if not _plausible(qlines, qnet, qratios, None):
                    dropped += 1
                    continue
                lo, hi = ranges.get(qname, (None, None))
                cells[f"{naics}|{band_slug}|{qname}"] = {
                    "naics": naics,
                    "naics_label": labels.get(naics, ""),
                    "revenue_band": band_slug,
                    "cohort": qname,
                    "business_count": per_q_count,
                    "sample_size": per_q_count,
                    "quality": whole_quality,
                    "cohort_revenue_low": lo,
                    "cohort_revenue_high": hi,
                    "pct_profitable": None,
                    "ratios": qratios,
                    "source_url": f"https://ised-isde.canada.ca/app/ixb/cis/performance/{naics}",
                    "lines": qlines,
                }
                n_cohort += 1

            # ---- pm_q1..pm_q4 cohort cells (national only) ----
            pm_row = pm_geo.get((naics, "CA"))
            if pm_row and pm_cols:
                pm_info = pm_rev_by_geo.get((naics, "CA"))
                pm_whole = pm_info["business_count"] if pm_info else None
                pm_quality = pm_info["quality"] if pm_info else None
                per_pm_count = round(pm_whole / 4) if pm_whole is not None else None
                for qi, qname in enumerate(_PM_COHORTS):
                    plines, pcogs = _point_cell_lines(pm_row, pm_cols, qi)
                    if len([b for b in plines if b != "operating_revenue"]) < 3:
                        continue
                    pnet = None
                    if pm_net_block and pm_net_block.get("rq"):
                        jn = pm_net_block["rq"][qi]
                        pnet = _num(pm_row[jn]) if jn < len(pm_row) else None
                    pratios = _cohort_ratios(pcogs, pnet)
                    if not _plausible(plines, pnet, pratios, None):
                        dropped += 1
                        continue
                    cells[f"{naics}|{band_slug}|{qname}"] = {
                        "naics": naics,
                        "naics_label": labels.get(naics, ""),
                        "revenue_band": band_slug,
                        "cohort": qname,
                        "business_count": per_pm_count,
                        "sample_size": per_pm_count,
                        "quality": pm_quality,
                        "cohort_revenue_low": None,
                        "cohort_revenue_high": None,
                        "pct_profitable": None,
                        "ratios": pratios,
                        "source_url": f"https://ised-isde.canada.ca/app/ixb/cis/performance/{naics}",
                        "lines": plines,
                    }
                    n_cohort += 1

        logger.info(
            "band %s: %d national-all + %d provincial-all + %d cohort (rev/pm) cells "
            "(%d implausible dropped, %d inverted line-bands nulled)",
            band_slug, n_all, n_prov, n_cohort, dropped, dropped_bounds)

    return {"meta": _download_meta(year, dataset_id), "cells": cells}


def _download_meta(year: int, dataset_id: str) -> dict[str, Any]:
    return {
        "source": "ISED / Statistics Canada — Financial Performance Data",
        "source_year": year,
        "synthetic": False,   # real licensed data — see meta.attribution
        "data_note": (
            f"Auto-ingested from the ISED Financial Performance Data {year} open "
            "dataset. Line percentages are shares of total revenue. The 'all' "
            "cohort carries the honest per-line size-cohort range (q1/median/q3), "
            "suppressed on inversion; rev_q* cohorts carry real revenue $ ranges; "
            "pm_q4 is the top profit-margin quartile. Business counts come from the "
            "total_revenue file. rev_q*/pm_q* cohorts are national-only."
        ),
        "geography": "Canada (national; provinces for the 'all' cohort)",
        "licence": "Open Government Licence – Canada",
        "attribution": _ATTRIBUTION,
        "attribution_url": "https://open.canada.ca/en/open-government-licence-canada",
        "source_tool_url": "https://ised-isde.canada.ca/site/financial-performance-data/en",
        "source_dataset_url": "https://open.canada.ca/data/en/dataset/" + dataset_id,
        "revenue_bands": ["30K-5M", "5M-20M"],
        "cohorts": list(_ALL_COHORTS),
        "quartiles": ["all"],  # deprecated alias, kept one release
        "min_business_count": MIN_SAMPLE_SIZE,
        "quality_note": _QUALITY_NOTE,
        "min_n_note": _MIN_N_NOTE,
        "revenue_floor_cad": 30000,
    }


def _guard_header(header: list[str], what: str) -> None:
    """Fail loud if the fixed leading column positions ever drift."""
    if not (len(header) >= 3
            and "incorporation" in header[0].lower()
            and "naics" in header[1].lower()
            and ("province" in header[2].lower() or "canada" in header[2].lower())):
        raise RuntimeError(f"unexpected {what} column layout: {header[:3]!r}")


def _guard_line_columns(cols: dict[str, dict[str, Any]],
                        net_block: dict[str, Any] | None, what: str) -> None:
    """Fail loud if the expenses file is missing the anchor lines or their
    revenue-quartile sub-columns (so silent drift can't emit fabricated data)."""
    for required in ("subcontracts", "salaries_wages", "cost_of_sales"):
        if required not in cols:
            raise RuntimeError(f"{what}: could not locate expected line column {required!r}")
    if net_block is None:
        raise RuntimeError(f"{what}: could not locate the net-profit/loss column")
    for required in ("subcontracts", "salaries_wages"):
        if not cols[required].get("rq"):
            raise RuntimeError(
                f"{what}: quartile sub-columns absent for {required!r} — "
                "layout drift; refusing to emit possibly-fabricated bounds")


def _guard_revenue_columns(rev_cols: dict[str, Any], what: str) -> None:
    """Fail loud if the total_revenue file is missing business_count, quality,
    or any of the per-quartile revenue-range columns."""
    if rev_cols.get("count") is None:
        raise RuntimeError(f"{what}: business_count column absent")
    if rev_cols.get("quality") is None:
        raise RuntimeError(f"{what}: whole-industry quality column absent")
    for qn, (li, hi) in rev_cols["ranges"].items():
        if li is None or hi is None:
            raise RuntimeError(f"{what}: revenue-range columns absent for {qn}")


def _resolve_dataset_id(year: int, source_url: str | None) -> str:
    """Known id for the year, else search CKAN for 'Financial Performance Data (year)'."""
    if source_url and "/dataset/" in source_url:
        return source_url.rstrip("/").rsplit("/", 1)[-1]
    if year in _YEAR_DATASETS:
        return _YEAR_DATASETS[year]
    data = json.loads(_http_get(
        "https://open.canada.ca/data/api/3/action/package_search?q="
        f"Financial+Performance+Data+{year}&rows=30"
    ).decode("utf-8", "replace"))
    for p in data["result"]["results"]:
        if str(year) in (p.get("title") or ""):
            return p["id"]
    raise RuntimeError(f"Could not resolve FPD dataset id for year {year}")


# ── Validation + deterministic emit ─────────────────────────────────────────

def _round_floats(obj: Any) -> Any:
    """Recursively round floats to 4dp for a deterministic, clean git diff."""
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v) for v in obj]
    return obj


def load_anchor(path: Path | str | None) -> dict[str, Any] | None:
    """Read an optional anchor file → a normalized anchor, or ``None``.

    ``None`` in, ``None`` out: with no anchor supplied, :func:`_validate`
    checks structure and plausibility only. An anchor that WAS asked for and
    cannot be read, or that checks nothing, raises — silently skipping a check
    that was asked for is worse than not offering one.
    """
    if path is None:
        return None
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read anchor file {p}: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"anchor file {p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw.get("key"):
        raise RuntimeError(f"anchor file {p}: no 'key' naming the cell to check")
    lines = raw.get("lines") or {}
    ratios = raw.get("ratios") or {}
    if not isinstance(lines, dict) or not isinstance(ratios, dict):
        raise RuntimeError(f"anchor file {p}: 'lines' and 'ratios' must be objects")
    if not lines and not ratios:
        raise RuntimeError(
            f"anchor file {p} checks nothing — give it 'lines', 'ratios', or both")
    try:
        tolerance = float(raw.get("tolerance", DEFAULT_ANCHOR_TOLERANCE))
        anchor = {
            "key": str(raw["key"]),
            "tolerance": tolerance,
            "lines": {str(k): float(v) for k, v in lines.items()},
            "ratios": {str(k): float(v) for k, v in ratios.items()},
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"anchor file {p}: expected numbers, got {exc}") from exc
    if tolerance <= 0:
        raise RuntimeError(f"anchor file {p}: tolerance must be positive")
    return anchor


def _anchor_errors(cells: dict[str, Any], anchor: dict[str, Any]) -> list[str]:
    """Compare one named cell against the figures the anchor expects."""
    key, tol = anchor["key"], anchor["tolerance"]
    cell = cells.get(key)
    if cell is None:
        return [f"anchor cell {key!r} absent"]
    errors: list[str] = []
    lines = cell.get("lines") or {}
    for bucket, expected in anchor["lines"].items():
        got = _line_pct(lines.get(bucket))
        if got is None or abs(got - expected) > tol:
            errors.append(f"anchor {key} line {bucket}={got} outside {expected}±{tol}")
    ratios = cell.get("ratios") or {}
    for name, expected in anchor["ratios"].items():
        got = ratios.get(name)
        if got is None or abs(got - expected) > tol:
            errors.append(f"anchor {key} ratio {name}={got} outside {expected}±{tol}")
    return errors


def _validate(
    data: dict[str, Any], out_path: Path, *, anchor: dict[str, Any] | None = None
) -> list[str]:
    """Fail-loud checks. Returns a list of error strings (empty = OK).

    Structure, provenance and plausibility are checked always. ``anchor`` is
    the optional per-run expectation from :func:`load_anchor`; when it is
    ``None`` no cell-specific figures are demanded.
    """
    errors: list[str] = []
    cells = data.get("cells") or {}
    meta = data.get("meta") or {}

    # Provenance. A table built from the licensed release must carry the
    # licence's attribution; a table of invented figures must say so instead,
    # and must NOT carry an attribution that would misdescribe where it came
    # from. Exactly one of the two is required.
    if meta.get("synthetic"):
        if not str(meta.get("notice") or "").strip():
            errors.append(
                "meta.synthetic is set but meta.notice does not say the figures "
                "are invented")
        if meta.get("attribution"):
            errors.append(
                "meta.synthetic is set but meta.attribution claims a data source")
    elif not meta.get("attribution") or "Open Government Licence" not in meta["attribution"]:
        errors.append("meta.attribution missing the OGL-Canada string")
    if not cells:
        errors.append("no cells emitted")

    if anchor is not None:
        errors.extend(_anchor_errors(cells, anchor))

    # Per-cell sanity (belt-and-suspenders — the parse step already drops
    # implausible cohorts). min-N is now a READ-time concern, so a low
    # business_count is an advisory count, never a hard error.
    low_n = 0
    for key, cell in cells.items():
        expense_sum = 0.0
        for bucket, line in (cell.get("lines") or {}).items():
            pct = _line_pct(line)
            if pct is None or not (0.0 <= pct <= _PER_LINE_MAX):
                errors.append(f"cell {key} line {bucket} pct_of_revenue={pct} implausible")
            elif bucket != "operating_revenue":
                expense_sum += pct
            # Honest-band invariant: any populated band must be ordered.
            if isinstance(line, dict):
                q1, med, q3 = line.get("q1"), line.get("median"), line.get("q3")
                if q1 is not None and q3 is not None:
                    if q1 > q3 or (med is not None and not (q1 <= med <= q3)):
                        errors.append(
                            f"cell {key} line {bucket} band not ordered: "
                            f"q1={q1} median={med} q3={q3}")
        if expense_sum > _LINE_SUM_MAX:
            errors.append(
                f"cell {key} expense line-% sum={expense_sum:.2f} exceeds "
                f"_LINE_SUM_MAX={_LINE_SUM_MAX} (likely a duplicated/mis-scaled column)")
        ratios = cell.get("ratios") or {}
        nm = ratios.get("net_profit_margin")
        if nm is not None and not (_NET_MARGIN_RANGE[0] <= nm <= _NET_MARGIN_RANGE[1]):
            errors.append(f"cell {key} net_profit_margin={nm} implausible")
        gm = ratios.get("gross_margin")
        if gm is not None and not (_GROSS_MARGIN_RANGE[0] <= gm <= _GROSS_MARGIN_RANGE[1]):
            errors.append(f"cell {key} gross_margin={gm} implausible")
        pp = cell.get("pct_profitable")
        if pp is not None and not (0.0 <= pp <= 1.0):
            errors.append(f"cell {key} pct_profitable={pp} implausible")
        q = cell.get("quality")
        if q is not None and q not in {"A", "B", "C", "D", "E"}:
            errors.append(f"cell {key} quality={q!r} not in A..E")
        bc = cell.get("business_count")
        if bc is not None and bc < MIN_SAMPLE_SIZE:
            low_n += 1
    if low_n:
        logger.info("advisory: %d cell(s) have business_count < %d "
                    "(kept — engine suppresses at read time)", low_n, MIN_SAMPLE_SIZE)

    # Regression guard: don't silently shrink the table on a bad refresh.
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text(encoding="utf-8"))
            prior_n = len(prior.get("cells") or {})
            if prior_n and len(cells) < 0.95 * prior_n:
                errors.append(
                    f"cell count regressed: {len(cells)} < 95% of prior {prior_n}")
        except Exception as exc:  # noqa: BLE001 — advisory only
            logger.warning("could not read prior %s for regression guard: %s", out_path, exc)

    return errors


def _strip_none_cells(data: dict[str, Any]) -> dict[str, Any]:
    """Drop top-level ``None`` cell fields (a size trim). The engine reads
    every cell field via ``dict.get`` so an absent key is identical to an
    explicit ``null`` — e.g. ``cohort_revenue_low`` (null on ``all``/``pm_q*``
    cells), a suppressed ``quality``, or ``pct_profitable`` on a cohort cell."""
    for cell in (data.get("cells") or {}).values():
        for k in [k for k, v in cell.items() if v is None]:
            del cell[k]
    return data


def _serialize(data: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, 4dp floats, UTF-8 en-dash kept."""
    return json.dumps(_round_floats(data), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _emit(data: dict[str, Any], out_path: Path, *, dry_run: bool,
          anchor: dict[str, Any] | None = None) -> int:
    _strip_none_cells(data)
    errors = _validate(data, out_path, anchor=anchor)
    n_cells = len(data.get("cells") or {})
    payload = _serialize(data)
    size_mb = len(payload.encode("utf-8")) / (1024 * 1024)
    logger.info("parsed %d cells, %.1f MB (anchor: %s)", n_cells, size_mb,
                anchor["key"] if anchor else "none supplied")
    if size_mb > 40.0:
        logger.warning("serialized size %.1f MB exceeds the ~40 MB cap — "
                       "consider gzip-committing with a load_benchmarks() fallback",
                       size_mb)
    if errors:
        for e in errors:
            logger.error("VALIDATION: %s", e)
        logger.error("aborting — %d validation error(s); %s NOT written", len(errors), out_path)
        return 1
    if dry_run:
        logger.info("--dry-run: %d cells validated OK; %s NOT written", n_cells, out_path)
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding="utf-8")
    logger.info("wrote %d cells (%.1f MB) → %s", n_cells, size_mb, out_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / "data" / "fpd_raw")
    parser.add_argument("--source-url", type=str, default=None)
    parser.add_argument("--seed-only", action="store_true",
                        help="Emit the small illustrative table instead of downloading.")
    parser.add_argument("--anchor", type=Path, default=None,
                        help="Optional JSON file naming one cell and the figures it "
                             "is expected to carry; a miss aborts without writing "
                             f"--out. Defaults to ${ANCHOR_ENV_VAR} if set, else no "
                             "anchor check.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + print stats but do not write --out.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    anchor = load_anchor(args.anchor or os.environ.get(ANCHOR_ENV_VAR) or None)

    if args.seed_only:
        data = _seed_benchmarks()
    else:
        data = _download_and_parse(args.year, args.raw_dir, args.source_url)

    return _emit(data, args.out, dry_run=args.dry_run, anchor=anchor)


if __name__ == "__main__":
    sys.exit(main())

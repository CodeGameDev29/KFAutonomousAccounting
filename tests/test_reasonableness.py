"""Deterministic engine tests for the CRA Reasonableness / Peer-Benchmark engine.

All figures here are synthetic: an invented six-month profit-and-loss for
``Example Corp Inc.``, a made-up Canadian design studio (NAICS 5414,
specialized design services). No real book, client, contractor or remittance is
represented. The peer numbers come from
``tests/fixtures/ised_benchmarks_cohorts_sample.json``, which
``scripts/gen_fixtures.py`` writes: the benchmark table's shape carried across
every cohort the engine selects between, with every figure invented. It is not
a sample of any published release, so every expectation below is re-derived
from that file rather than from industry knowledge.

The worked example in :func:`_golden_rows` is the headline regression: it is
shaped so the engine has exactly one headline finding (contractor spend far
larger than payroll) plus one CRA-threshold finding (travel over 5% of
revenue), and nothing else. Every guardrail test below seeds an input that
would produce a wrong flag/band if the guardrail were removed — a test that
passes while the engine is broken is worse than none.

Band assertions here use the engine's INTERNAL band values
(``typical``/``a_few``/``several``); the wire mapping (``a_few``→``few``) is
asserted in ``tests/test_reasonableness_api.py``.
"""
from __future__ import annotations

import copy
import inspect
import json
from datetime import date
from pathlib import Path

import pytest

import core.reasonableness as _rmod
from config.settings import ReasonablenessConfig
from core.reasonableness import (
    _FORBIDDEN_SUBSTRINGS,
    BAND_LABELS,
    DISCLAIMER,
    INDUSTRY_TO_NAICS,
    BenchmarkCell,
    _profitability_note,
    _recommendation,
    _reject_forbidden_language,
    _status_from_bounds,
    evaluate_reasonableness,
    load_category_gifi_map,
    resolve_naics,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ised_benchmarks_cohorts_sample.json"

#: The NAICS code the invented company files under, and the one the generated
#: cohort sample is keyed on. Nothing in the engine is pinned to it.
GOLDEN_NAICS = "5414"

# The invented period: six months, Apr–Sep 2026. 183 days is under the engine's
# 335-day full-year bar, so every golden run is a partial year and the revenue
# band is derived from the ANNUALIZED figure.
GOLDEN_START = date(2026, 4, 1)
GOLDEN_END = date(2026, 9, 30)
FULL_YEAR_START = date(2026, 1, 1)
FULL_YEAR_END = date(2026, 12, 31)

# The invented P&L, stated once so every expectation below can be re-derived
# from it rather than pasted. Sum of the two sales lines.
GOLDEN_REVENUE = 196400.00
GOLDEN_CONTRACTORS = 61900.00          # 31.5% of revenue
GOLDEN_WAGES = 20600.00                # 10.5% of revenue
GOLDEN_TRAVEL = 13400.00               # 6.8% of revenue


def _benchmarks() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _cell(cohort: str, band: str = "30K-5M") -> dict:
    return _benchmarks()["cells"][f"{GOLDEN_NAICS}|{band}|{cohort}"]


def _peer_line(bucket: str, cohort: str = "all"):
    """The raw peer line for a bucket, straight out of the generated sample.

    A whole-band line is a ``{pct_of_revenue, q1, median, q3}`` dict; a cohort
    cell is a point cell, so each of its lines is a bare float.
    """
    return _cell(cohort)["lines"][bucket]


def _peer_pct(bucket: str, cohort: str = "all") -> float:
    """The peer central value for a bucket, whichever shape its line is in."""
    line = _peer_line(bucket, cohort)
    return line["median"] if isinstance(line, dict) else line


def _rev_cohort_bounds(cohort: str) -> tuple[float, float]:
    c = _cell(cohort)
    return c["cohort_revenue_low"], c["cohort_revenue_high"]


def _mapping():
    # Exercise the real shipped category map (validates config too).
    return load_category_gifi_map()


def _row(category, income=0.0, expenses=0.0, currency="CAD"):
    return {
        "category": category,
        "currency": currency,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "transaction_count": 1,
    }


def _golden_rows():
    """Six invented months of Example Corp Inc., in the FIXED Canada ISED/GIFI
    vocabulary. The fixed taxonomy gives every line one canonical name, so the
    category-fragmentation and generic-income safety nets stay quiet — that
    noise reduction is the point of the fixed taxonomy.

    The shape that matters: $196,400 of operating sales, $61,900 paid to
    contractors against only $20,600 of payroll (the headline), $13,400 of
    travel (6.8% of revenue, over the CRA-scrutiny threshold), a CRA remittance
    and an owner's draw that must both stay out of the peer ratios' numerator
    games, and an immaterial insurance line that must be dropped entirely.
    """
    return [
        _row("Sales of goods and services", income=128750.00),
        _row("Sales of goods and services", income=67650.00),
        # non-operating — must be excluded from the revenue base
        _row("Refund/Credit", income=2240.00),
        _row("All other revenues", income=168.00),  # interest earned
        # contractors → one canonical cost-of-sales bucket (subcontracts)
        _row("Purchases, materials and sub-contracts", expenses=27400.00),
        _row("Purchases, materials and sub-contracts", expenses=21300.00),
        _row("Purchases, materials and sub-contracts", expenses=13200.00),
        # payroll
        _row("Labour and commissions", expenses=20600.00),
        # CRA remittance — a locally reclassified operating cost with no ISED line
        _row("Income tax and GST/HST remittance", expenses=6450.00),
        # travel — the second finding
        _row("Travel", expenses=13400.00),
        # design tooling → one canonical it_software bucket
        _row("Software and IT", expenses=2150.00),
        _row("Software and IT", expenses=1380.00),
        # immaterial line: under 2% of revenue AND under the $500 floor
        _row("Insurance", expenses=385.00),
        # owner's draw is counted as an operating cost, never a distribution
        _row("Owner's draw", expenses=16200.00),
    ]


def _run(rows, naics=GOLDEN_NAICS, start=GOLDEN_START, end=GOLDEN_END, **kw):
    kw.setdefault("benchmarks", _benchmarks())
    kw.setdefault("mapping", _mapping())
    return evaluate_reasonableness(rows, naics, start, end, **kw)


# ── The golden worked example ────────────────────────────────────────────────

class TestGolden:
    def test_golden_end_to_end(self):
        r = _run(_golden_rows())
        assert r.suppressed_reason is None
        # 128,750 + 67,650 operating sales; the refund and the interest line are
        # non-operating and never reach the denominator.
        assert r.operating_revenue == pytest.approx(GOLDEN_REVENUE, abs=0.01)
        # Two elevated findings, none that "stands out" → a_few, not several.
        assert r.band == "a_few"
        assert r.is_partial_year is True
        keys = [f.flag_key for f in r.flags]
        # contractor-vs-payroll is the headline (subcontracts IS an ISED line);
        # travel has no ISED benchmark so it surfaces via the CRA threshold.
        assert keys[0] == "pattern_contractor_heavy"
        assert "pattern_travel_over_threshold" in keys
        # Owner's draw is an operating cost, so the distribution info flag stays off.
        assert "info_owners_draw_present" not in keys
        # The fixed taxonomy removes the fragmentation + generic-bucket flags.
        assert "pattern_category_fragmentation" not in keys
        assert "pattern_generic_income_bucket" not in keys
        assert len(r.flags) <= 5
        # contractor-heavy subsumes the per-line subcontracts/wages ratio flags
        assert "ratio_subcontracts_high" not in keys
        assert "ratio_salaries_wages_low" not in keys
        # travel is never a ratio flag (no ISED peer line for it)
        assert "ratio_travel_high" not in keys

    def test_golden_contractor_headline_details(self):
        r = _run(_golden_rows())
        cf = r.flags[0]
        assert cf.flag_key == "pattern_contractor_heavy"
        # $61,900 of the $196,400 revenue base = 31.5%.
        assert cf.your_pct == pytest.approx(GOLDEN_CONTRACTORS / GOLDEN_REVENUE, abs=0.0005)
        assert cf.amount == pytest.approx(GOLDEN_CONTRACTORS, abs=0.01)

    def test_golden_contractor_skew_is_why_it_fires(self):
        """The pattern needs BOTH halves of its definition to be true here, so a
        future threshold change cannot quietly turn the headline into luck."""
        cfg = ReasonablenessConfig()
        skew = GOLDEN_CONTRACTORS / (GOLDEN_CONTRACTORS + GOLDEN_WAGES)
        assert skew > cfg.contractor_skew_frac          # 75.0% of labour spend
        assert GOLDEN_CONTRACTORS / GOLDEN_REVENUE > cfg.contractor_skew_min_rev


# ── Legal framing ────────────────────────────────────────────────────────────

class TestLegalFraming:
    def test_no_banned_language_on_any_surface(self):
        r = _run(_golden_rows())
        blobs = [r.band_label, r.attribution, *r.reassurance]
        for f in r.flags:
            blobs.extend([f.title, f.prompt, f.category])
        for text in blobs:
            assert not _reject_forbidden_language(text), f"forbidden framing in: {text!r}"
            low = (text or "").lower()
            for sub in _FORBIDDEN_SUBSTRINGS:
                assert sub not in low, f"forbidden substring {sub!r} in {text!r}"

    def test_approved_attribution_and_labels(self):
        r = _run(_golden_rows())
        assert "Open Government Licence" in r.attribution
        assert "ISED" in r.attribution or "Statistics Canada" in r.attribution
        assert r.band_label == BAND_LABELS["a_few"]
        assert r.band_label == "A few things worth a look"

    def test_disclaimer_constant_is_clean(self):
        assert not _reject_forbidden_language(DISCLAIMER)
        assert "not tax advice" in DISCLAIMER

    def test_guard_catches_known_bad_phrases(self):
        for bad in [
            "Your audit risk score is 71%",
            "This makes you CRA-proof",
            "You won't get audited",
            "20% chance of audit",
            "We guarantee no CRA audit",
        ]:
            assert _reject_forbidden_language(bad), bad


# ── Operating-revenue denominator isolation ──────────────────────────────────

class TestDenominator:
    def test_denominator_excludes_non_operating(self):
        r = _run(_golden_rows())
        # Operating sales only. The $2,240 refund and the $168 of interest earned
        # are non-operating; adding them would inflate the base to $198,808 and
        # understate every expense ratio.
        assert r.operating_revenue == pytest.approx(GOLDEN_REVENUE, abs=0.01)
        assert r.operating_revenue < 196400.00 + 2240.00 + 168.00

    def test_owners_draw_is_counted_as_an_operating_cost(self):
        # Owner's draw is an operating cost (owner_draw bucket), NOT skipped as a
        # distribution. It surfaces as a comparison row and is never treated as
        # revenue or as a distribution.
        r = _run(_golden_rows())
        buckets = {c.gifi_bucket for c in r.comparisons}
        assert "owner_draw" in buckets
        assert "distribution" not in buckets
        # It has no ISED peer line, so it carries no peer average and no ratio flag.
        od = next(c for c in r.comparisons if c.gifi_bucket == "owner_draw")
        assert od.peer_pct is None
        assert od.amount == pytest.approx(16200.00, abs=0.01)
        assert not any(f.gifi_bucket == "distribution" for f in r.flags)

    def test_pure_transfers_change_nothing(self):
        base = _run(_golden_rows())
        with_transfers = _golden_rows() + [
            _row("Credit card payment", expenses=5200.0),
            _row("Internal transfer", income=8100.0),
        ]
        r = _run(with_transfers)
        assert r.operating_revenue == pytest.approx(base.operating_revenue, abs=0.01)
        assert [f.flag_key for f in r.flags] == [f.flag_key for f in base.flags]


# ── The revenue floor and its boundary ───────────────────────────────────────

class TestRevenueFloor:
    def test_below_the_floor_is_suppressed(self):
        # A full year at $23,400 is under the $30,000 floor: no peer cohort is
        # meaningful at that size, so the card suppresses rather than compares.
        rows = [
            _row("Sales of goods and services", income=23400.0),
            _row("Travel", expenses=6800.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.suppressed_reason == "below_revenue_floor"
        assert r.flags == []

    def test_just_above_the_floor_benchmarks_normally(self):
        # $32,800 clears the floor by $2,800 and must benchmark normally.
        rows = [
            _row("Sales of goods and services", income=32800.0),
            _row("Travel", expenses=5200.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.suppressed_reason is None

    def test_revenue_band_for_helper(self):
        from core.reasonableness import revenue_band_for

        assert revenue_band_for(29_500) is None          # under the floor
        assert revenue_band_for(30_500) == "30K-5M"
        assert revenue_band_for(4_900_000) == "30K-5M"
        assert revenue_band_for(7_250_000) == "5M-20M"
        assert revenue_band_for(21_000_000) is None      # above the FPD ceiling


# ── Partial year vs full year ────────────────────────────────────────────────

class TestPeriod:
    def test_partial_year_detected(self):
        r = _run(_golden_rows(), start=GOLDEN_START, end=GOLDEN_END)
        assert r.is_partial_year is True
        # 183 days ≈ 6.0 months on the engine's 30.4-day month.
        assert r.period_months == pytest.approx(6.0, abs=0.1)

    def test_full_year_is_not_partial(self):
        r = _run(_golden_rows(), start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.is_partial_year is False


# ── A missing NAICS code ─────────────────────────────────────────────────────

class TestMissingNaics:
    def test_a_missing_code_suppresses_before_any_lookup(self):
        r = _run(_golden_rows(), naics=None)
        assert r.suppressed_reason == "missing_naics"
        assert r.flags == []
        assert r.naics_used is None
        assert r.naics_source is None
        # revenue is computed BEFORE the NAICS gate
        assert r.operating_revenue == pytest.approx(GOLDEN_REVENUE, abs=0.01)


# ── Materiality ──────────────────────────────────────────────────────────────

class TestMateriality:
    def test_a_tiny_line_is_dropped_however_far_it_deviates(self):
        # $320 of advertising on $138,000 of revenue is 0.23% — a large
        # deviation from the peer line, and completely meaningless. It is under
        # BOTH the 2%-of-revenue and the $500 floors, so the line is dropped.
        rows = [
            _row("Sales of goods and services", income=138000.0),
            _row("Advertising and promotion", expenses=320.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert not any(f.gifi_bucket == "advertising" for f in r.flags)
        assert not any(c.gifi_bucket == "advertising" for c in r.comparisons)

    def test_a_material_line_is_kept_and_flagged(self):
        # Same peer line, same direction, but $13,800 = 10% of revenue.
        # $138,000 is bracketed by the rev_q1 cohort, a point cell with no
        # published spread, so the comparison is the relative tolerance around
        # its central value — and 10% is several times that. It must flag.
        rows = [
            _row("Sales of goods and services", income=138000.0),
            _row("Advertising and promotion", expenses=13800.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        lo, hi = _rev_cohort_bounds("rev_q1")
        assert lo <= 138000.0 <= hi
        tolerance = ReasonablenessConfig().median_only_tolerance_pct
        assert 13800.0 / 138000.0 > _peer_pct("advertising", "rev_q1") * (1 + tolerance)
        assert any(f.flag_key == "ratio_advertising_high" for f in r.flags)


# ── NAICS fallback: 6 to 5 to 4, exact, none ─────────────────────────────────

class TestNaicsFallback:
    #: A 6-digit code inside the sample's 4-digit industry group.
    SIX_DIGIT = "541430"

    def test_a_six_digit_code_falls_back_to_the_four_digit_cell(self):
        # the sample carries the 4-digit group only, so the lookup widens to it
        r = _run(_golden_rows(), naics=self.SIX_DIGIT)
        assert r.suppressed_reason is None
        assert r.naics_fallback_level == 4

    def test_an_exact_six_digit_cell_is_preferred(self):
        bench = _benchmarks()
        key = f"{GOLDEN_NAICS}|30K-5M|all"
        bench["cells"][f"{self.SIX_DIGIT}|30K-5M|all"] = dict(bench["cells"][key])
        bench["cells"][f"{self.SIX_DIGIT}|30K-5M|all"]["naics"] = self.SIX_DIGIT
        r = _run(_golden_rows(), naics=self.SIX_DIGIT, benchmarks=bench)
        assert r.naics_fallback_level == 6

    def test_a_code_with_no_cell_at_any_depth_suppresses(self):
        r = _run(_golden_rows(), naics="999999")
        assert r.suppressed_reason == "no_benchmark_cell"
        assert r.flags == []


# ── The dismiss filter ───────────────────────────────────────────────────────

class TestDismiss:
    def test_dismissing_a_flag_removes_it_and_reranks(self):
        base = _run(_golden_rows())
        assert base.flags[0].flag_key == "pattern_contractor_heavy"
        r = _run(_golden_rows(), dismissed_flag_keys={"pattern_contractor_heavy"})
        keys = [f.flag_key for f in r.flags]
        assert "pattern_contractor_heavy" not in keys
        assert keys[0] == "pattern_travel_over_threshold"
        assert "pattern_contractor_heavy" in r.dismissed_applied

    def test_dismiss_recomputes_band(self):
        # Dismissing the headline leaves only the travel threshold flag, so the
        # band is recomputed from what survives rather than frozen at first pass.
        r = _run(
            _golden_rows(),
            dismissed_flag_keys={
                "pattern_contractor_heavy",
                "pattern_generic_income_bucket",
                "pattern_category_fragmentation",
            },
        )
        assert r.band in ("a_few", "typical")


# ── Multi-currency consolidation ─────────────────────────────────────────────

class TestFx:
    def test_one_bucket_in_two_currencies_consolidates(self):
        # Split the $13,400 of travel into CAD 7,850 + USD 4,111.11 @ 1.35
        # (= CAD 5,549.99). One bucket, one flag, same dollar total.
        rows = [r for r in _golden_rows() if r["category"] != "Travel"]
        rows.append(_row("Travel", expenses=7850.00, currency="CAD"))
        rows.append(_row("Travel", expenses=4111.11, currency="USD"))
        r = _run(rows, fx_rates={"USD": 1.35})
        travel = [f for f in r.flags if f.flag_key == "pattern_travel_over_threshold"]
        assert len(travel) == 1  # consolidated, not two rows
        assert travel[0].amount == pytest.approx(GOLDEN_TRAVEL, abs=2.0)

    def test_a_currency_with_no_rate_is_still_deterministic(self):
        rows = [
            _row("Sales of goods and services", income=138000.0, currency="CAD"),
            _row("Travel", expenses=6900.0, currency="EUR"),  # no rate provided
        ]
        r1 = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END, fx_rates={})
        r2 = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END, fx_rates={})
        # deterministic (fx_fallback applied), never a naive cross-currency sum
        assert [f.flag_key for f in r1.flags] == [f.flag_key for f in r2.flags]
        assert "pattern_travel_over_threshold" in [f.flag_key for f in r1.flags]


# ── Verdict bands and the flag cap ───────────────────────────────────────────

class TestBands:
    def _typical_rows(self):
        """A second invented company whose ratios sit inside every peer band in
        the sample: $214,000 of sales, 29% payroll, 12.1% contractors and 3.3%
        travel (under the 5% threshold). That the first two really are inside
        the published band is re-derived from the sample in the test below."""
        return [
            _row("Sales of goods and services", income=214000.0),
            _row("Labour and commissions", expenses=62000.0),
            _row("Purchases, materials and sub-contracts", expenses=26000.0),
            _row("Travel", expenses=7000.0),
        ]

    def test_everything_inside_the_peer_band_is_typical(self):
        r = _run(self._typical_rows(), start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.band == "typical"
        assert r.flags == []
        assert r.band_label == BAND_LABELS["typical"]
        # Every ratio really is inside the published band — the reason there is
        # nothing to flag, not an accident of the thresholds.
        for bucket, amount in (("salaries_wages", 62000.0), ("subcontracts", 26000.0)):
            line = _peer_line(bucket)
            assert line["q1"] <= amount / 214000.0 <= line["q3"]
        # high margin surfaces as reassurance, never a flag
        assert any("margin" in n for n in r.reassurance)

    def test_one_finding_is_a_few(self):
        rows = [r for r in self._typical_rows() if r["category"] != "Travel"]
        rows.append(_row("Travel", expenses=12900.0))  # 6% > the 5% CRA threshold
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        real = [f for f in r.flags if f.severity != "info"]
        assert 1 <= len(real) <= 3
        assert r.band == "a_few"

    def test_four_findings_are_several(self):
        # Golden + two more material lines past their peer figures: advertising,
        # whose whole-band line carries no reliable spread (so the relative
        # tolerance around its central value decides), and rent, which is more
        # than one IQR past q3 → stands_out. Four real flags ≥ the
        # several_total_flags bar.
        rows = _golden_rows() + [
            _row("Advertising and promotion", expenses=12200.0),
            _row("Rent", expenses=22700.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        tolerance = ReasonablenessConfig().median_only_tolerance_pct
        assert 12200.0 / GOLDEN_REVENUE > _peer_pct("advertising") * (1 + tolerance)
        rent = _peer_line("rent")
        assert 22700.0 / GOLDEN_REVENUE > rent["q3"] + (rent["q3"] - rent["q1"])
        assert r.total_flags_before_cap >= ReasonablenessConfig().several_total_flags
        assert r.band == "several"

    def test_only_the_top_five_flags_survive(self):
        # Six candidate flags across benchmarked lines + named patterns; only the
        # top five survive, ordered by weight.
        rows = [
            _row("Sales of goods and services", income=265000.0),
            _row("Advertising and promotion", expenses=15900.0),       # above
            _row("Rent", expenses=33000.0),                            # well_above
            _row("Professional and business fees", expenses=26500.0),  # above
            _row("Travel", expenses=21200.0),                          # pattern_travel
            _row("Motor vehicle expenses", expenses=23850.0),          # pattern_vehicle
            _row("Meals and entertainment", expenses=15900.0),         # pattern_meals
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert len(r.flags) == ReasonablenessConfig().top_n_flags == 5
        assert r.total_flags_before_cap > 5
        weights = [f.weight for f in r.flags]
        assert weights == sorted(weights, reverse=True)


# ── Scrutiny weighting ───────────────────────────────────────────────────────

class TestScrutinyWeight:
    def test_a_scrutinized_line_outranks_a_larger_unscrutinized_one(self):
        # Both lines share one peer line (2.1% of revenue, band 0.9–3.4%), so the
        # only difference is the bucket. Advertising (scrutiny 1.0) deviates
        # further in IQR units — 14% vs travel's 8% — yet travel (scrutiny 2.5)
        # must still rank first, because CRA-scrutinized lines are what the user
        # most needs to look at.
        peer = {"pct_of_revenue": 0.021, "q1": 0.009, "median": 0.021, "q3": 0.034}
        bench = {
            "meta": {"source_year": 2024, "attribution": _OGL, "min_business_count": 20},
            "cells": {
                f"{GOLDEN_NAICS}|30K-5M|all": {
                    "naics": GOLDEN_NAICS, "revenue_band": "30K-5M", "cohort": "all",
                    "business_count": 3480, "quality": "A",
                    "ratios": {"net_profit_margin": 0.16},
                    "lines": {"travel": dict(peer), "advertising": dict(peer)},
                }
            },
        }
        rows = [
            _row("Sales of goods and services", income=175000.0),
            _row("Travel", expenses=14000.0),                     # r = 0.08
            _row("Advertising and promotion", expenses=24500.0),  # r = 0.14
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END, benchmarks=bench)
        travel = next(f for f in r.flags if f.gifi_bucket == "travel")
        adv = next(f for f in r.flags if f.gifi_bucket == "advertising")
        assert abs(adv.your_pct - peer["median"]) > abs(travel.your_pct - peer["median"])
        assert travel.weight > adv.weight
        assert r.flags.index(travel) < r.flags.index(adv)


# ── Named patterns ───────────────────────────────────────────────────────────

class TestNamedPatterns:
    def test_contractor_heavy_is_an_elevated_finding(self):
        r = _run(_golden_rows())
        cf = next(f for f in r.flags if f.flag_key == "pattern_contractor_heavy")
        assert cf.severity == "elevated"
        assert cf.your_pct == pytest.approx(GOLDEN_CONTRACTORS / GOLDEN_REVENUE, abs=0.0005)

    def test_generic_income_cannot_fire_under_the_fixed_taxonomy(self):
        # The fixed taxonomy has NO generic REVENUE category (Sales / All other
        # revenues are both specific), so the generic-income-bucket flag can never
        # fire — eliminating that noise is a core goal of the taxonomy swap.
        rows = [
            _row("Sales of goods and services", income=158000.0),
            _row("Travel", expenses=12400.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert not any(f.flag_key == "pattern_generic_income_bucket" for f in r.flags)

    def test_free_text_labels_that_share_a_bucket_still_fragment(self):
        # Fixed canonical names cannot fragment a bucket, but the engine's
        # fragmentation safety net still fires for un-migrated FREE-TEXT strings
        # that resolve (via the YAML fallback) to the same GIFI bucket.
        rows = [
            _row("Client Revenue", income=158000.0),
            _row("Cloud hosting fees", expenses=12400.0),   # -> it_software (fallback)
            _row("Software licenses", expenses=9800.0),     # -> it_software (fallback)
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert any(f.flag_key == "pattern_category_fragmentation" for f in r.flags)

    def test_expenses_larger_than_revenue_are_named(self):
        rows = [
            _row("Sales of goods and services", income=64000.0),
            _row("Purchases, materials and sub-contracts", expenses=82000.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert any(f.flag_key == "pattern_expenses_exceed_revenue" for f in r.flags)


# ── Determinism ──────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_two_runs_of_one_input_are_identical(self):
        r1 = _run(_golden_rows())
        r2 = _run(_golden_rows())
        assert r1.band == r2.band
        assert [f.flag_key for f in r1.flags] == [f.flag_key for f in r2.flags]
        assert [f.your_pct for f in r1.flags] == [f.your_pct for f in r2.flags]
        assert [f.weight for f in r1.flags] == [f.weight for f in r2.flags]


# ── Category mapping: legacy labels, unknown names ───────────────────────────

class TestCategoryMap:
    def test_a_legacy_free_text_label_is_tolerated(self):
        rows = _golden_rows() + [_row("Bank Fee/Interest", expenses=410.0)]
        r = _run(rows)  # must not raise
        assert r.suppressed_reason is None

    def test_an_unknown_category_lands_in_the_generic_bucket(self):
        rows = [
            _row("Client Revenue", income=152000.0),
            _row("Some Brand New Category", expenses=9700.0),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)  # defaults, no KeyError
        assert r.suppressed_reason is None
        assert any(c.gifi_bucket == "other_operating" for c in r.comparisons)


# ── Regression: fallback-mapping fixes ───────────────────────────────────────

class TestMappingRegressions:
    def _m(self):
        return load_category_gifi_map()

    def test_cost_of_sales_is_expense_not_revenue(self):
        m = self._m().resolve("Cost of Sales")
        assert m.gifi_bucket == "cost_of_sales"
        assert m.is_operating_revenue is False

    def test_sales_tax_excluded_not_revenue(self):
        m = self._m().resolve("Sales Tax")
        assert m.is_operating_revenue is False
        assert m.gifi_bucket == "non_operating"

    def test_revenue_canada_remittance_excluded(self):
        m = self._m().resolve("Revenue Canada")
        assert m.is_operating_revenue is False

    def test_legit_revenue_still_maps_to_revenue(self):
        m = self._m().resolve("Service Revenue")
        assert m.gifi_bucket == "operating_revenue"
        assert m.is_operating_revenue is True

    def test_airfare_is_travel_with_scrutiny(self):
        m = self._m().resolve("Airfare")
        assert m.gifi_bucket == "travel"
        assert m.cra_scrutiny_weight > 1.0

    def test_air_travel_is_travel(self):
        assert self._m().resolve("Air Travel").gifi_bucket == "travel"

    def test_automation_software_still_it(self):
        assert self._m().resolve("Automation Software").gifi_bucket == "it_software"

    def test_withdrawal_not_distribution(self):
        m = self._m().resolve("Cash Withdrawal")
        assert m.is_distribution is False

    def test_owner_withdrawal_is_distribution(self):
        assert self._m().resolve("Owner Withdrawal").is_distribution is True

    def test_repairs_bucket(self):
        assert self._m().resolve("Repairs and Maintenance").gifi_bucket == "repairs_maintenance"

    def test_golden_still_maps_correctly(self):
        m = self._m()
        assert m.resolve("Client Revenue").is_operating_revenue is True
        assert m.resolve("Contractor Fees").gifi_bucket == "subcontracts"
        # Canonical "Owner's draw" is an operating cost (owner_draw), not a
        # distribution. Free-text withdrawals still map to distribution via the
        # YAML fallback (test_owner_withdrawal_is_distribution).
        assert m.resolve("Owner's Draw").gifi_bucket == "owner_draw"
        assert m.resolve("Owner's Draw").is_distribution is False


class TestPartialYearBand:
    def test_monthly_view_below_period_floor_still_shows(self):
        # One month of a healthy business ($28,400 in April, ~$346K annualized)
        # is under the $30K in-period floor, but the band is derived from the
        # ANNUALIZED figure, so it maps to 30K-5M and the card renders rather
        # than going blank on every monthly view.
        rows = [
            _row("Sales of goods and services", income=28400.0),
            _row("Travel", expenses=3400.0),
            _row("Purchases, materials and sub-contracts", expenses=8900.0),
        ]
        r = _run(rows, start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert r.suppressed_reason is None
        assert r.revenue_band == "30K-5M"
        assert r.annualized_revenue > 30000.0 > r.operating_revenue
        assert r.is_partial_year is True
        assert r.comparisons

    def test_high_revenue_short_window_falls_back_to_seeded_band(self):
        # $2.6M over ~3 months annualizes past $10M (the 5M-20M band). The only
        # 5M-20M cell in the sample is below the density gate, so the engine
        # falls back to the dense 30K-5M cohort rather than showing a blank card.
        rows = [
            _row("Sales of goods and services", income=2_600_000.0),
            _row("Travel", expenses=325000.0),
        ]
        r = _run(rows, start=date(2026, 3, 1), end=date(2026, 5, 31))
        assert r.suppressed_reason is None
        assert r.annualized_revenue > 5_000_000.0
        assert r.revenue_band == "30K-5M"
        assert r.is_partial_year is True


# ── Always-visible per-category comparisons ──────────────────────────────────

class TestComparisons:
    def test_golden_has_full_comparison_table(self):
        r = _run(_golden_rows())
        assert r.comparisons, "every material category should be present"
        by_bucket = {c.gifi_bucket: c for c in r.comparisons}
        # Contractors at 31.5% are more than one IQR past the peer q3; wages at
        # 10.5% are under the peer q1. Each carries a real ISED peer figure.
        sub_line = _peer_line("subcontracts")
        assert GOLDEN_CONTRACTORS / GOLDEN_REVENUE > sub_line["q3"] + (
            sub_line["q3"] - sub_line["q1"]
        )
        assert by_bucket["subcontracts"].status == "well_above"
        assert by_bucket["subcontracts"].peer_pct is not None
        assert GOLDEN_WAGES / GOLDEN_REVENUE < _peer_line("salaries_wages")["q1"]
        assert by_bucket["salaries_wages"].status in ("below", "well_below")
        # travel has no ISED line → still shown, as a CRA-watch row with no peer avg
        assert "travel" in by_bucket
        assert by_bucket["travel"].status == "watch"
        assert by_bucket["travel"].peer_pct is None
        assert by_bucket["travel"].cra_scrutinized is True
        # benchmarked rows carry a peer avg; no-benchmark rows carry None
        for c in r.comparisons:
            if c.status in ("no_benchmark", "watch"):
                assert c.peer_pct is None
            else:
                assert c.peer_pct is not None and c.peer_pct > 0

    def test_in_line_category_present(self):
        # A canonical expense sitting AT its ISED peer ratio renders as an
        # "in_line" comparison row with no concern flag.
        peer = _peer_line("salaries_wages")["median"]
        revenue = 214000.0
        rows = [
            _row("Sales of goods and services", income=revenue),
            _row("Labour and commissions", expenses=revenue * peer),
        ]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        sw = next((c for c in r.comparisons if c.gifi_bucket == "salaries_wages"), None)
        assert sw is not None
        assert sw.status == "in_line"
        assert sw.flag_key is None  # in-line rows are not concerns

    def test_concern_rows_carry_flag_key(self):
        r = _run(_golden_rows())
        sub = next(c for c in r.comparisons if c.gifi_bucket == "subcontracts")
        assert sub.flag_key == "ratio_subcontracts_high"

    def test_comparisons_sorted_by_share_desc(self):
        r = _run(_golden_rows())
        shares = [c.your_pct for c in r.comparisons]
        assert shares == sorted(shares, reverse=True)


class TestSourceDisclosure:
    def test_golden_exposes_cohort_source_data(self):
        """Everything the card claims about the peer cohort must be the cell's
        own published values, not a restated or rounded copy."""
        r = _run(_golden_rows())
        cell = _cell("all")
        assert r.cohort_label == cell["naics_label"]
        assert r.cohort_sample_size == cell["business_count"]
        assert r.cohort_pct_profitable == pytest.approx(cell["pct_profitable"])
        assert r.cohort_gross_margin == pytest.approx(cell["ratios"]["gross_margin"])
        assert r.cohort_net_margin == pytest.approx(cell["ratios"]["net_profit_margin"])
        assert r.cohort_source_url == cell["source_url"]
        # partial year → annualized revenue exceeds the in-period revenue
        assert r.annualized_revenue > r.operating_revenue

    def test_suppressed_has_no_cohort_data(self):
        r = _run(_golden_rows(), naics="999999")  # no cohort
        assert r.suppressed_reason == "no_benchmark_cell"
        assert r.cohort_label is None
        assert r.cohort_sample_size is None


class TestProvinceCohorts:
    #: Invented provincial margin, deliberately unlike the national one so the
    #: test can tell which cell was used.
    PROVINCE_NET_MARGIN = 0.194

    def _bench_with_province(self, prov_net=PROVINCE_NET_MARGIN):
        b = _benchmarks()
        nat = b["cells"][f"{GOLDEN_NAICS}|30K-5M|all"]
        prov = copy.deepcopy(nat)
        prov["province"] = "46"          # Manitoba's StatCan SGC code
        prov["province_name"] = "Manitoba"
        prov["ratios"] = {"gross_margin": nat["ratios"]["gross_margin"],
                        "net_profit_margin": prov_net}
        b["cells"][f"{GOLDEN_NAICS}|30K-5M|all|46"] = prov
        return b

    def test_uses_province_cohort_when_available(self):
        r = _run(_golden_rows(), province="Manitoba", benchmarks=self._bench_with_province())
        assert r.cohort_province == "Manitoba"
        assert r.cohort_net_margin == pytest.approx(self.PROVINCE_NET_MARGIN)

    def test_falls_back_to_national_when_province_missing(self):
        # no Ontario cell → national cohort used
        r = _run(_golden_rows(), province="Ontario", benchmarks=self._bench_with_province())
        assert r.cohort_province is None
        assert r.cohort_net_margin == pytest.approx(
            _cell("all")["ratios"]["net_profit_margin"]
        )

    def test_no_province_uses_national(self):
        r = _run(_golden_rows(), benchmarks=self._bench_with_province())
        assert r.cohort_province is None

    def test_unknown_province_name_ignored(self):
        r = _run(_golden_rows(), province="Atlantis", benchmarks=self._bench_with_province())
        assert r.cohort_province is None


class TestResolveNaics:
    def test_explicit_code_wins(self):
        # A stored code is used as-is, whatever the coarse label says.
        assert resolve_naics("541430", "Food & Hospitality", []) == ("541430", "profile")

    def test_every_mapped_industry_label_resolves(self):
        for label, code in INDUSTRY_TO_NAICS.items():
            assert resolve_naics(None, label, []) == (code, "industry_map")

    def test_unmapped_industry_no_data_falls_through(self):
        # Unmapped label + no spending signature → no cohort → hide.
        naics, source = resolve_naics(None, "Nonprofit", [
            _row("Client Revenue", income=152000.0),
            _row("Others", expenses=46000.0),
        ])
        assert naics is None and source is None

    def test_contractor_heavy_services_signature_is_inferred(self):
        # No cost of sales at all, and contractors are over half of spend: the
        # professional-services signature the engine seeds a cohort for.
        rows = [
            _row("Sales of goods and services", income=184000.0),
            _row("Purchases, materials and sub-contracts", expenses=62000.0),
            _row("Labour and commissions", expenses=21000.0),
            _row("Software and IT", expenses=3500.0),
        ]
        naics, source = resolve_naics(None, None, rows)
        assert naics in INDUSTRY_TO_NAICS.values()
        assert source == "data_inferred"

    def test_data_inference_restaurant(self):
        # Heavy cost of sales plus real rent: the food-service signature.
        rows = [
            _row("Client Revenue", income=286000.0),
            _row("Cost of Sales", expenses=114400.0),
            _row("Rent", expenses=25700.0),
        ]
        naics, source = resolve_naics(None, None, rows)
        assert naics == "7225"
        assert source == "data_inferred"

    def test_industry_map_only_covers_seeded(self):
        # Only clear-fit labels are mapped, each to a 4-digit industry group;
        # ambiguous ones are intentionally absent, so the card hides rather
        # than comparing against a poor-fit cohort.
        assert INDUSTRY_TO_NAICS
        assert all(len(code) == 4 and code.isdigit() for code in INDUSTRY_TO_NAICS.values())
        for unmapped in ("Real Estate", "Nonprofit", "Other"):
            assert unmapped not in INDUSTRY_TO_NAICS


def test_reasonableness_engine_on_by_default():
    from server.deps import _DEFAULT_ON_FLAGS, is_enabled

    assert "reasonableness_engine" in _DEFAULT_ON_FLAGS

    class _DB:
        class _Cur:
            def execute(self, *a):
                pass

            def fetchone(self):
                return None  # user has no business_profile / flag row

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _conn(self):
            cur = self._Cur()

            class _Conn:
                def cursor(_s):
                    return cur

                def __enter__(_s):
                    return _s

                def __exit__(_s, *a):
                    return False

            return _Conn()

    db = _DB()
    assert is_enabled(db, "uid", "reasonableness_engine") is True
    assert is_enabled(db, "uid", "some_unknown_flag") is False


def test_config_is_frozen_dataclass():
    cfg = ReasonablenessConfig()
    assert cfg.revenue_floor == 30000
    assert cfg.top_n_flags == 5
    with pytest.raises(Exception):
        cfg.revenue_floor = 1  # frozen


# ═══════════════════════════════════════════════════════════════════════════
# Industry-benchmark data honesty. The engine has NO
# percentile/percentile_of/peer_lower/peer_median/peer_upper — comparisons carry
# median-first `peer_pct` + an honest `peer_band` [q1, q3].
# ═══════════════════════════════════════════════════════════════════════════

_OGL = (
    "Contains information licensed under the Open Government Licence – Canada. "
    "Source: ISED/StatCan Financial Performance Data."
)


def _inline_all_bench(lines, *, business_count=3960, quality="B"):
    """A minimal single-`all`-cell bench (no rev_q* cohorts) so full-year inputs
    select this cell deterministically — it isolates the null-bounds and
    absent-line suppression paths from cohort selection. Invented throughout."""
    return {
        "meta": {"source_year": 2024, "attribution": _OGL, "min_business_count": 20},
        "cells": {
            f"{GOLDEN_NAICS}|30K-5M|all": {
                "naics": GOLDEN_NAICS, "naics_label": "Specialized design services",
                "revenue_band": "30K-5M", "cohort": "all",
                "business_count": business_count, "quality": quality,
                "ratios": {"net_profit_margin": 0.16},
                "lines": lines,
            }
        },
    }


class TestRealSpreadVsSuppression:
    """Real q1/median/q3 bounds emit a REAL peer_band; null bounds NEVER
    fabricate one (peer_band stays None — the honest central value is still
    surfaced). The density gate and grouped-into-other suppression are the ONLY
    paths to a null peer figure."""

    def test_real_bounds_emit_real_peer_band(self):
        r = _run(_golden_rows())
        line = _peer_line("subcontracts")
        sub = next(c for c in r.comparisons if c.gifi_bucket == "subcontracts")
        # the published [q1, q3] surfaced verbatim, not fabricated
        assert sub.peer_band == [line["q1"], line["q3"]]
        assert sub.peer_pct == pytest.approx(line["median"])   # median-first central
        # honest ordering: q1 <= median (peer_pct) <= q3
        assert sub.peer_band[0] <= sub.peer_pct <= sub.peer_band[1]

    def test_null_bounds_line_not_fabricated(self):
        # A line with null q1/q3 but a real central average must NEVER get a
        # fabricated ±spread band. peer_band is None; the honest central is kept.
        bench = _inline_all_bench({
            "advertising": {"pct_of_revenue": 0.034, "q1": None, "median": None, "q3": None},
            "subcontracts": {"pct_of_revenue": 0.12, "q1": 0.08, "median": 0.12, "q3": 0.19},
        })
        rows = [_row("Sales of goods and services", income=172000.0),
                _row("Advertising and promotion", expenses=22360.0)]  # 13% vs mean 3.4%
        r = evaluate_reasonableness(rows, GOLDEN_NAICS, FULL_YEAR_START, FULL_YEAR_END,
                                    benchmarks=bench, mapping=_mapping())
        adv = next(c for c in r.comparisons if c.gifi_bucket == "advertising")
        assert adv.peer_band is None                  # NO fabricated spread
        assert adv.peer_pct == pytest.approx(0.034)   # honest central still surfaced
        # If a concern flag fires it is a median-only classification off the honest
        # central, never a synthetic-band comparison — verify no band leaked in.
        for f in r.flags:
            if f.gifi_bucket == "advertising":
                assert f.peer_band is None

    def test_absent_line_is_suppressed_no_ratio_flag(self):
        # A benchmarkable bucket with NO ISED line at all → honest suppression:
        # status no_benchmark, peer_pct/peer_band None, unavailable_reason set,
        # and CRUCIALLY no ratio_ concern flag is fabricated.
        bench = _inline_all_bench({
            "subcontracts": {"pct_of_revenue": 0.12, "q1": 0.08, "median": 0.12, "q3": 0.19},
        })
        rows = [_row("Sales of goods and services", income=172000.0),
                _row("Advertising and promotion", expenses=22360.0)]
        r = evaluate_reasonableness(rows, GOLDEN_NAICS, FULL_YEAR_START, FULL_YEAR_END,
                                    benchmarks=bench, mapping=_mapping())
        adv = next(c for c in r.comparisons if c.gifi_bucket == "advertising")
        assert adv.status == "no_benchmark"
        assert adv.peer_pct is None and adv.peer_band is None
        assert adv.unavailable_reason == "grouped_other"
        assert not any(
            f.gifi_bucket == "advertising" and (f.flag_key or "").startswith("ratio_")
            for f in r.flags
        )

    def test_sub_min_n_cohort_is_suppressed(self):
        # The sample's 5M-20M cell is below the density gate. An $8.6M business
        # must fall back to the dense 30K-5M cohort, NEVER benchmark the thin
        # cell.
        thin = _cell("all", band="5M-20M")
        assert thin["business_count"] < ReasonablenessConfig().min_business_count
        rows = [_row("Sales of goods and services", income=8_600_000.0),
                _row("Advertising and promotion", expenses=430000.0)]
        r = _run(rows, start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.suppressed_reason is None
        assert r.revenue_band == "30K-5M"                            # density-gated fallback
        assert r.cohort_business_count == _cell("all")["business_count"]

    def test_no_default_spread_pct_path_remains(self):
        import re
        src = inspect.getsource(_rmod.evaluate_reasonableness)
        # Strip comments — the only surviving mention is a doc comment confirming
        # the deletion ("NEVER fabricate a ±default_spread_pct band"). The fabricated
        # ±spread CODE PATH (and any read of the config knob) must be gone.
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())
        assert "default_spread_pct" not in code, (
            "the fabricated ±spread path must be deleted, not just skipped"
        )
        assert ".default_spread_pct" not in inspect.getsource(_rmod), (
            "the default_spread_pct config knob must never be read"
        )

    def test_inverted_bounds_dropped_defensively(self):
        # A non-monotonic (q1 > q3) line must be treated as no usable band
        # (get_line drops it), never interpolated into a nonsense range.
        #
        # The sample cannot ship an inverted band to point at: it is generated,
        # and the ingest pipeline's validator rejects an unordered band, so one
        # could never survive a refresh. The guard is therefore fed a cell
        # inverted right here — the shape a stale or hand-edited table would
        # hand the engine, and the case the engine has to survive.
        cell_data = copy.deepcopy(_cell("all"))
        band = cell_data["lines"]["subcontracts"]
        band["q1"], band["q3"] = band["q3"], band["q1"]
        assert band["q1"] > band["q3"]
        line = BenchmarkCell(cell_data).get_line("subcontracts")
        assert line["q1"] is None and line["q3"] is None
        assert line["pct_of_revenue"] == pytest.approx(band["median"])  # central still honest


class TestStatusFromBounds:
    """`_status_from_bounds` classifies a ratio against REAL q1/q3 (IQR
    distance) or, when the band is absent, a median-only relative tolerance —
    never a fabricated spread, never a raw percentile string.

    The invented band below is q1=0.10, median=0.18, q3=0.30 (IQR 0.20), so with
    the default 1×IQR multiplier the "well" fences sit at -0.10 and 0.50."""

    _CFG = ReasonablenessConfig()

    def test_full_band_classification(self):
        assert _status_from_bounds(0.22, 0.10, 0.30, 0.18, self._CFG) == ("in_line", "n/a")
        assert _status_from_bounds(0.34, 0.10, 0.30, 0.18, self._CFG) == ("above", "high")
        assert _status_from_bounds(0.55, 0.10, 0.30, 0.18, self._CFG) == ("well_above", "high")
        assert _status_from_bounds(0.07, 0.10, 0.30, 0.18, self._CFG) == ("below", "low")
        # tighter band (IQR 0.10) → the lower fence is 0.20, so 0.12 is well below
        assert _status_from_bounds(0.12, 0.30, 0.40, 0.35, self._CFG) == ("well_below", "low")

    def test_degenerate_equal_bounds_no_zero_division(self):
        # q1 == q3 (IQR = 0) must not raise; the value sits exactly on the point.
        assert _status_from_bounds(0.06, 0.06, 0.06, 0.06, self._CFG) == ("in_line", "n/a")
        assert _status_from_bounds(0.15, 0.06, 0.06, 0.06, self._CFG)[0] in (
            "above", "well_above"
        )

    def test_null_bounds_use_median_only_tolerance(self):
        # No band → ±15% relative tolerance around the central value (0.153..0.207).
        assert _status_from_bounds(0.26, None, None, 0.18, self._CFG) == ("above", "high")
        assert _status_from_bounds(0.11, None, None, 0.18, self._CFG) == ("below", "low")
        assert _status_from_bounds(0.18, None, None, 0.18, self._CFG) == ("in_line", "n/a")

    def test_null_central_is_in_line(self):
        # Neither a band nor a central value → nothing to compare; never raises.
        assert _status_from_bounds(0.5, None, None, None, self._CFG) == ("in_line", "n/a")


class TestSizeCohortSelection:
    """The user's annualized revenue is matched to the rev_q* cohort whose $
    bounds bracket it; a density-gated (<min_business_count) rev_q* widens to the
    whole-band 'all' cohort; the (depth, band, geo, cohort) trail is exposed."""

    def _sized(self, income, expense=30000.0):
        return [_row("Sales of goods and services", income=income),
                _row("Labour and commissions", expenses=expense)]

    def test_revenue_bracketed_to_correct_rev_quartile(self):
        lo, hi = _rev_cohort_bounds("rev_q1")
        assert lo <= 104000.0 <= hi
        r = _run(self._sized(104000.0, 34000.0), start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.size_cohort == "rev_q1"
        assert r.cohort_trail["cohort"] == "rev_q1"

    def test_higher_revenue_picks_higher_quartile(self):
        lo2, hi2 = _rev_cohort_bounds("rev_q2")
        lo3, hi3 = _rev_cohort_bounds("rev_q3")
        assert lo2 <= 780000.0 <= hi2 and lo3 <= 1_950_000.0 <= hi3
        r2 = _run(self._sized(780000.0, 260000.0), start=FULL_YEAR_START, end=FULL_YEAR_END)
        r3 = _run(self._sized(1_950_000.0, 640000.0), start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r2.size_cohort == "rev_q2"
        assert r3.size_cohort == "rev_q3"

    def test_min_n_rev_quartile_widens_to_all(self):
        bench = copy.deepcopy(_benchmarks())
        bench["cells"][f"{GOLDEN_NAICS}|30K-5M|rev_q1"]["business_count"] = 9  # below min-N
        r = _run(self._sized(104000.0, 34000.0), benchmarks=bench,
                 start=FULL_YEAR_START, end=FULL_YEAR_END)
        assert r.size_cohort == "all"                 # widened; never the N=9 cohort
        assert r.suppressed_reason is None            # the dense 'all' cell rescues it
        assert r.cohort_business_count == _cell("all")["business_count"]

    def test_cohort_trail_populated(self):
        # The golden annualizes to ~$392K, inside the revenue window the sample
        # deliberately leaves uncovered — so no size cohort brackets it and the
        # whole-band 'all' cell is the honest answer.
        r = _run(_golden_rows())
        brackets = [_rev_cohort_bounds(q) for q in ("rev_q1", "rev_q2", "rev_q3", "rev_q4")]
        assert not any(lo <= r.annualized_revenue <= hi for lo, hi in brackets)
        assert r.naics_fallback_level == 4             # the sample is 4-digit only
        assert r.revenue_band == "30K-5M"
        assert r.cohort_province is None               # geo = national
        assert r.size_cohort == "all"
        assert r.cohort_trail == {
            "naics_depth": 4, "band": "30K-5M", "geo": "national", "cohort": "all",
        }


class TestProfitabilityCohort:
    """Always-on, PEER-DESCRIPTIVE user-vs-pm_q4 comparison. The engine NEVER
    labels, computes or implies the USER'S OWN profitability rank. Suppressed
    entirely when the pm cohort is below min-N."""

    _BANNED_USER_RANKING = (
        "you're in the", "you are in the", "your quartile", "bottom quarter",
        "least profitable", "you rank", "your profitability",
    )

    def test_section_present_and_peer_descriptive(self):
        r = _run(_golden_rows())
        assert r.profitability, "always-on section should be populated"
        assert r.profitability_cohort_label
        line = next(p for p in r.profitability if p.gifi_bucket == "subcontracts")
        assert line.peer_pct == pytest.approx(_peer_pct("subcontracts", "pm_q4"))
        assert line.your_pct == pytest.approx(GOLDEN_CONTRACTORS / GOLDEN_REVENUE)
        assert line.business_count >= ReasonablenessConfig().min_business_count

    def test_no_user_margin_language(self):
        r = _run(_golden_rows())
        blobs = [r.profitability_cohort_label or ""]
        for p in r.profitability:
            blobs += [p.label, p.note]
        for text in blobs:
            low = text.lower()
            for banned in self._BANNED_USER_RANKING:
                assert banned not in low, f"user-ranking leak: {text!r}"

    def test_no_user_margin_rank_field(self):
        r = _run(_golden_rows())
        assert not hasattr(r, "user_margin_quartile")
        assert not hasattr(r, "profitability_cohort")  # cohort is a str label, not a rank
        for p in r.profitability:
            assert not hasattr(p, "status")
            assert not hasattr(p, "user_margin_quartile")
            assert not hasattr(p, "direction")

    def test_note_always_pairs_a_defuser(self):
        r = _run(_golden_rows())
        for p in r.profitability:
            low = p.note.lower()
            assert "differ" in low or "vary" in low, f"missing defuser: {p.note!r}"

    def test_suppressed_under_min_n(self):
        bench = copy.deepcopy(_benchmarks())
        bench["cells"][f"{GOLDEN_NAICS}|30K-5M|pm_q4"]["business_count"] = 7  # below min-N
        r = _run(_golden_rows(), benchmarks=bench)
        assert r.profitability == []
        assert r.profitability_cohort_label is None


class TestRecommendationProse:
    """Deterministic recommendation templates (NO LLM). Every emitted
    recommendation passes _reject_forbidden_language, carries no forbidden
    substring, and stays in the bookkeeping lane (no business-strategy advice)."""

    def _recs(self, r):
        recs = [c.recommendation for c in r.comparisons if c.recommendation]
        recs += [f.recommendation for f in r.flags if f.recommendation]
        return recs

    def test_recommendations_pass_forbidden_guard(self):
        r = _run(_golden_rows())
        recs = self._recs(r)
        assert recs, "concern / no-benchmark rows should carry a recommendation"
        for text in recs:
            assert not _reject_forbidden_language(text), text
            low = text.lower()
            for sub in _FORBIDDEN_SUBSTRINGS:
                assert sub not in low, f"{sub!r} in {text!r}"

    def test_recommendations_are_bookkeeping_not_business_advice(self):
        r = _run(_golden_rows())
        for text in self._recs(r):
            low = text.lower()
            for banned in ("spend less", "cut your", "reduce your spending",
                           "you should lower", "increase revenue", "hire more"):
                assert banned not in low, f"business-advice leak: {text!r}"

    def test_recommendations_deterministic(self):
        a = [c.recommendation for c in _run(_golden_rows()).comparisons]
        b = [c.recommendation for c in _run(_golden_rows()).comparisons]
        assert a == b

    def test_no_llm_in_engine_source(self):
        # The recommendations are deterministic templates. No model call, and no
        # hook for one: an LLM in this path would make the same books produce
        # different prose on two runs, and nothing here could be re-derived.
        src = inspect.getsource(_rmod).lower()
        assert "openai" not in src and "gemini" not in src
        assert "reasonableness_narrative" not in src

    def test_profitability_note_helper_is_clean(self):
        note = _profitability_note("Contractors", 0.40, 0.10)
        assert not _reject_forbidden_language(note)
        assert "differ" in note.lower() or "vary" in note.lower()

    def test_recommendation_helper_none_when_in_line(self):
        assert _recommendation("Wages", "in_line", _peer_pct("salaries_wages"), 0.35,
                               _cell("all")["business_count"], "CAD") is None


class TestForbiddenLanguageExtended:
    """The extended banned framing is registered and scanned across EVERY
    emitted surface. Stats vocabulary ("quartile"/"percentile") is modelled
    internally but NEVER surfaced in user-facing copy."""

    NEW_BANNED = (
        "red flag", "flagged", "the cra will notice", "cra will notice", "suspicious",
        "at risk", "overspending", "too high",
        "you're less profitable", "you are less profitable", "underperforming",
        "below-average business",
    )

    def test_new_substrings_registered(self):
        for s in self.NEW_BANNED + ("quartile", "percentile"):
            assert s in _FORBIDDEN_SUBSTRINGS, f"{s!r} must be in the guard tuple"

    def test_guard_rejects_each_new_phrase(self):
        for bad in ("This is a red flag", "The CRA will notice this",
                    "You're overspending on travel", "Your travel is too high",
                    "You're less profitable than peers", "This looks suspicious"):
            assert _reject_forbidden_language(bad), bad

    def _surface(self, r):
        blobs = [r.band_label, r.attribution, r.profitability_cohort_label or "", *r.reassurance]
        for f in r.flags:
            blobs += [f.title, f.prompt, f.category, f.recommendation or ""]
        for c in r.comparisons:
            blobs += [c.label, c.category, c.recommendation or ""]
        for p in r.profitability:
            blobs += [p.label, p.note]
        return [b for b in blobs if b]

    def test_full_output_surface_scan(self):
        r = _run(_golden_rows())
        for text in self._surface(r):
            assert not _reject_forbidden_language(text), f"forbidden framing: {text!r}"
            low = text.lower()
            for sub in _FORBIDDEN_SUBSTRINGS:
                assert sub not in low, f"{sub!r} in {text!r}"

    def test_stats_vocab_absent_from_user_facing_strings(self):
        r = _run(_golden_rows())
        for text in self._surface(r):
            low = text.lower()
            assert "quartile" not in low and "percentile" not in low, (
                f"stats vocab surfaced: {text!r}"
            )


class TestMedianFirst:
    """Financials are right-skewed, so the surfaced peer figure is the MEDIAN,
    never a mean-derived band."""

    def test_comparison_leads_with_median(self):
        r = _run(_golden_rows())
        line = _peer_line("subcontracts")
        sub = next(c for c in r.comparisons if c.gifi_bucket == "subcontracts")
        assert sub.peer_pct == pytest.approx(line["median"], abs=0.001)
        assert sub.peer_band == [line["q1"], line["q3"]]        # the honest [q1, q3]


class TestVintageAndCount:
    """Surface the real business_count + benchmark_year (vintage) on every data
    surface; a suppressed cohort carries neither."""

    def test_business_count_and_quality_surfaced(self):
        r = _run(_golden_rows())
        cell = _cell("all")
        assert r.cohort_business_count == cell["business_count"]
        assert r.cohort_sample_size == cell["business_count"]  # aligned with business_count
        assert r.cohort_quality == cell["quality"]
        assert r.cohort_quality in ("A", "B", "C", "D", "E")

    def test_benchmark_year_surfaced(self):
        r = _run(_golden_rows())
        assert r.benchmark_year == _benchmarks()["meta"]["source_year"]

    def test_suppressed_cohort_has_no_count(self):
        r = _run(_golden_rows(), naics="999999")
        assert r.suppressed_reason == "no_benchmark_cell"
        assert r.cohort_business_count is None
        assert r.cohort_quality is None

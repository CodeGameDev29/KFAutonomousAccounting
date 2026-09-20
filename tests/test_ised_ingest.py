"""Offline unit tests for the ISED FPD → benchmark JSON re-ingest.

These NEVER hit the network. They drive ``scripts.ingest_ised_benchmarks`` with
synthetic CSV strings so each behaviour of the transform is pinned:

  * the three suppression sentinels collapse to None (percentages only);
  * business_count / $-ranges are NOT nulled by the >=9999 percentage catch-all;
  * quality letters parse; blanks/junk → None;
  * the monotonicity gate keeps a monotone band but nulls an inverted one
    (never sorts to fabricate one);
  * business_count + quality + revenue ranges join from total_revenue;
  * a full parse emits ``all`` + ``rev_q*`` + ``pm_q*`` cohort cells with the
    ``<naics>|<band>|<cohort>`` key grammar, rev_q* revenue ranges, and
    national-only cohort cells;
  * low business_count is advisory, not a hard validate error;
  * validation without an anchor checks structure alone, and a supplied anchor
    that the data misses aborts the run without overwriting the output.

Every test seeds an input the transform would get wrong if the behaviour it
pins were missing.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import scripts.ingest_ised_benchmarks as ing

#: A cell key in the published ``<naics>|<band>|<cohort>`` grammar. Nothing in
#: the pipeline is pinned to this industry — it is just the key the synthetic
#: CSVs below emit.
SAMPLE_KEY = "5414|30K-5M|all"

# ── pure parsers ─────────────────────────────────────────────────────────────

def test_num_recognizes_all_three_sentinels():
    # The published tables use three distinct suppression sentinels, not one:
    # every variant has to collapse to None or a suppressed cell reads as data.
    assert ing._num("9999") is None
    assert ing._num("99999.9") is None
    assert ing._num("999999") is None
    assert ing._num("12.8") == 0.128        # real percentage still parses
    assert ing._num("") is None
    assert ing._num("n/a") is None


def test_int_does_not_treat_large_count_as_sentinel():
    # A real cohort count runs to tens of thousands of firms — the >=9999
    # percentage catch-all must NOT null it. Only the explicit big sentinels
    # and blanks are suppressed.
    assert ing._int("74086") == 74086
    assert ing._int("8,526") == 8526
    assert ing._int("999999") is None
    assert ing._int("99999.9") is None
    assert ing._int("") is None
    assert ing._int("x") is None


def test_revk_scales_thousands_to_dollars_without_9999_catch_all():
    assert ing._revk("30") == 30000.0        # $30K
    assert ing._revk("5000") == 5000000.0    # $5M
    assert ing._revk("20000") == 20000000.0  # 5M-20M band top — >9999 but real
    assert ing._revk("999999") is None
    assert ing._revk("") is None


def test_quality_letter():
    assert ing._quality("A") == "A"
    assert ing._quality(" e ") == "E"
    assert ing._quality("Z") is None
    assert ing._quality("") is None
    assert ing._quality(None) is None


def test_monotone():
    assert ing._monotone([1, 2, 3, 4])
    assert ing._monotone([4, 3, 2, 1])
    assert ing._monotone([5])
    assert not ing._monotone([3.6, 4.7, 4.1, 8.6])  # a zig-zag is not a band


# ── monotonicity gate ────────────────────────────────────────────────────────

def test_line_band_keeps_a_monotone_band():
    rep, dropped = ing._line_band(0.128, [0.04, 0.08, 0.15, 0.22])
    assert dropped is False
    assert isinstance(rep, dict)
    assert rep["q1"] == 0.04 and rep["q3"] == 0.22
    assert rep["median"] == 0.128 and rep["pct_of_revenue"] == 0.128
    assert rep["q1"] <= rep["median"] <= rep["q3"]


def test_line_band_nulls_an_inverted_band_and_never_sorts():
    # Non-monotone cohort figures (q1 > q3): sorting them would fabricate a band
    # that the source never published, so the gate drops to a bare float instead.
    rep, dropped = ing._line_band(0.04, [0.02, 0.05, 0.03, 0.08])
    assert dropped is True
    assert rep == 0.04                       # bare float → no band
    assert not isinstance(rep, dict)


def test_line_band_average_outside_band_is_dropped():
    # Monotone values but the published average sits outside [min,max] → mis-scale
    # guard nulls the band.
    rep, dropped = ing._line_band(0.99, [0.04, 0.08, 0.15, 0.22])
    assert dropped is True
    assert rep == 0.99


def test_line_band_too_few_cohort_figures_no_band_not_counted_as_inversion():
    rep, dropped = ing._line_band(0.05, [0.04, None, None, 0.06])
    assert dropped is False
    assert rep == 0.05


# ── header column mapping ────────────────────────────────────────────────────

def _block(label, quality="A"):
    """A 7-cell expenses header block for one line."""
    return [label, "Quality Indicator", f"Bottom Quartile {label}",
            f"Lower Middle {label}", f"Upper Middle {label}",
            f"Top Quartile {label}", "% of Businesses Reporting"]


def test_line_columns_locates_avg_quality_and_quartile_subcols():
    header = (["Incorporation Status", "Naics code", "Province/Canada"]
              + _block("Purchases, materials and sub-contracts")
              + _block("Net Profit/Loss"))
    cols, net_block = ing._line_columns(header)
    assert "subcontracts" in cols
    sc = cols["subcontracts"]
    assert sc["avg"] == 3
    assert sc["quality"] == 4
    assert sc["rq"] == [5, 6, 7, 8]          # the four by-revenue sub-columns
    assert net_block is not None and net_block["avg"] == 10


def test_revenue_columns_and_parse_join_count_quality_ranges():
    header = ["Incorporation Status", "Naics_code", "Province",
              "Whole Industry (Reliability)",
              "Whole industry low revenue range", "Whole industry High revenue range",
              "Bottom quartile Low revenue range", "Bottom quartile High revenue range",
              "Lower middle Low revenue range", "Lower middle High revenue range",
              "Upper middle Low revenue range", "Upper middle High revenue range",
              "Top quartile Low revenue range", "Top quartile High revenue range",
              "Whole industry Total revenue", "Quality Indicator"]
    rev_cols = ing._revenue_columns(header)
    assert rev_cols["count"] == 3
    assert rev_cols["quality"] == 15
    assert rev_cols["ranges"]["rev_q2"] == (8, 9)
    ing._guard_revenue_columns(rev_cols, "test")  # must not raise

    rows = [header,
            ["3", "5414", "00", "74086", "30", "5000",
             "30", "43", "43", "67", "67", "127", "127", "5000", "999", "A"]]
    parsed = ing._parse_revenue(rows, rev_cols)
    info = parsed[("5414", "CA")]
    assert info["business_count"] == 74086
    assert info["quality"] == "A"
    assert info["ranges"]["rev_q2"] == (43000.0, 67000.0)


def test_geo_rows_prefers_incorp_3():
    header = ["Incorporation Status", "Naics code", "Province/Canada", "x"]
    rows = [header,
            ["1", "5414", "00", "a"],
            ["3", "5414", "00", "b"],   # all-businesses cohort preferred
            ["2", "5414", "46", "c"]]
    out = ing._geo_rows(rows)
    assert out[("5414", "CA")][0] == "3"
    assert out[("5414", "46")][0] == "2"  # only incorp 2 available for that province


def test_point_cell_lines_reads_the_named_quartile_subcolumn():
    header = (["Incorporation Status", "Naics code", "Province/Canada"]
              + _block("Labour and Commissions"))
    cols, _ = ing._line_columns(header)
    row = ["3", "5414", "00", "36.5", "A", "20", "30", "40", "50", "99"]
    #                              avg          q1    q2    q3    q4
    lines_q1, _ = ing._point_cell_lines(row, cols, 0)   # bottom quartile
    lines_q4, _ = ing._point_cell_lines(row, cols, 3)   # top quartile
    assert lines_q1["salaries_wages"] == 0.20
    assert lines_q4["salaries_wages"] == 0.50


# ── full parse (monkeypatched network) ───────────────────────────────────────

def _csv_bytes(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("cp1252", "replace")


_EXP_LINES = [
    "Cost of Sales (direct expenses)",
    "Purchases, materials and sub-contracts",
    "Labour and Commissions",
    "Wages and Benefits",
    "Professional and business fees",
    "Rent",
]


def _expenses_csv(pm=False):
    header = ["Incorporation Status", "Naics code", "Province/Canada"]
    for lbl in _EXP_LINES:
        header += _block(lbl)
    header += _block("Net Profit/Loss")

    def blockvals(avg, q):
        # avg then quality then 4 quartile figures then %reporting
        return [avg, "A", q[0], q[1], q[2], q[3], "90"]

    if not pm:
        # revenue-quartile figures. subcontracts is monotone (band kept); rent is
        # a zig-zag (band nulled). cost_of_sales avg 17.1 → gross_margin 0.829.
        vals = {
            "Cost of Sales (direct expenses)": ("17.1", ["10", "14", "18", "24"]),
            "Purchases, materials and sub-contracts": ("12.8", ["4", "8", "15", "22"]),
            "Labour and Commissions": ("36.5", ["20", "30", "42", "50"]),
            "Wages and Benefits": ("4.3", ["1", "3", "5", "7"]),
            "Professional and business fees": ("4.7", ["3", "4", "5", "6"]),
            "Rent": ("2.6", ["2", "5", "3", "8"]),          # zig-zag → band dropped
            "Net Profit/Loss": ("18.8", ["5", "12", "20", "30"]),
        }
    else:
        # profit-margin quartiles: pm_q4 (top margin) spends less on labour.
        vals = {
            "Cost of Sales (direct expenses)": ("15.0", ["20", "17", "14", "10"]),
            "Purchases, materials and sub-contracts": ("11.0", ["18", "13", "9", "6"]),
            "Labour and Commissions": ("33.0", ["45", "38", "30", "25"]),
            "Wages and Benefits": ("4.0", ["6", "5", "4", "3"]),
            "Professional and business fees": ("4.5", ["6", "5", "4", "4"]),
            "Rent": ("2.4", ["3", "3", "2", "2"]),
            "Net Profit/Loss": ("22.0", ["5", "15", "28", "41"]),
        }
    row = ["3", "5414", "00"]
    for lbl in _EXP_LINES + ["Net Profit/Loss"]:
        avg, q = vals[lbl]
        row += blockvals(avg, q)
    return _csv_bytes([header, row])


def _revenue_csv(pm=False):
    header = ["Incorporation Status", "Naics_code", "Province",
              "Whole Industry (Reliability)",
              "Whole industry low revenue range", "Whole industry High revenue range",
              "Bottom quartile Low revenue range", "Bottom quartile High revenue range",
              "Lower middle Low revenue range", "Lower middle High revenue range",
              "Upper middle Low revenue range", "Upper middle High revenue range",
              "Top quartile Low revenue range", "Top quartile High revenue range",
              "Whole industry Total revenue", "Quality Indicator"]
    row = ["3", "5414", "00", "74086", "30", "5000",
           "30", "43", "43", "67", "67", "127", "127", "5000", "999", "A"]
    return _csv_bytes([header, row])


def _profit_csv():
    header = ["Incorporation Status", "Naics code", "Canada", "% of Profitable businesses"]
    return _csv_bytes([header, ["3", "5414", "00", "84.5"]])


def _naics_csv():
    return _csv_bytes([["code", "label"],
                       ["5414", "Specialized design services"]])


_RESOURCES = {
    "naics_descriptors_2024.csv": _naics_csv,
    "total_expenses_percentage_2024_30k_5m.csv": lambda: _expenses_csv(pm=False),
    "total_revenue_2024_30k_5m.csv": lambda: _revenue_csv(pm=False),
    "profit_non_profit_businesses_2024_30k_5m.csv": _profit_csv,
    "profit_margin_total_expenses_percentage_2024_30k_5m.csv": lambda: _expenses_csv(pm=True),
    "profit_margin_total_revenue_2024_30k_5m.csv": lambda: _revenue_csv(pm=True),
}


@pytest.fixture()
def parsed(monkeypatch):
    monkeypatch.setattr(ing, "_BANDS", [("30K-5M", "30k_5m")])

    def fake_http_get(url, timeout=180):
        assert "package_show" in url
        resources = [{"url": "https://example.test/" + name} for name in _RESOURCES]
        return json.dumps({"result": {"resources": resources}}).encode("utf-8")

    def fake_download(url, raw_dir):
        return _RESOURCES[url.rsplit("/", 1)[-1]]()

    monkeypatch.setattr(ing, "_http_get", fake_http_get)
    monkeypatch.setattr(ing, "_download", fake_download)
    return ing._download_and_parse(2024, raw_dir=ing.PROJECT_ROOT / "data" / "fpd_raw", source_url=None)


def test_full_parse_emits_all_and_cohort_cells(parsed):
    cells = parsed["cells"]
    # cohort key grammar <naics>|<band>|<cohort>
    assert "5414|30K-5M|all" in cells
    for q in ("rev_q1", "rev_q2", "rev_q3", "rev_q4"):
        assert f"5414|30K-5M|{q}" in cells
    for q in ("pm_q1", "pm_q2", "pm_q3", "pm_q4"):
        assert f"5414|30K-5M|{q}" in cells


def test_all_cell_carries_business_count_and_bands(parsed):
    cell = parsed["cells"]["5414|30K-5M|all"]
    assert cell["cohort"] == "all"
    assert cell["business_count"] == 74086       # joined from total_revenue
    assert cell["sample_size"] == 74086          # deprecated alias preserved
    assert cell["quality"] == "A"
    # subcontracts was monotone → an ordered band is kept.
    sc = cell["lines"]["subcontracts"]
    assert isinstance(sc, dict)
    assert sc["q1"] <= sc["median"] <= sc["q3"]
    # rent zig-zagged → the band was nulled to a bare float (no fabricated range).
    assert not isinstance(cell["lines"]["rent"], dict)


def test_rev_cohort_cells_carry_dollar_ranges_national_only(parsed):
    cells = parsed["cells"]
    rq2 = cells["5414|30K-5M|rev_q2"]
    assert rq2["cohort_revenue_low"] == 43000.0
    assert rq2["cohort_revenue_high"] == 67000.0
    assert rq2["business_count"] == round(74086 / 4)
    # point cell — lines are bare floats, no bands.
    assert all(not isinstance(v, dict) for v in rq2["lines"].values())
    # no provincial cohort variant was emitted.
    assert not any(k.startswith("5414|30K-5M|rev_q2|") for k in cells)


def test_pm_q4_is_top_margin_peer_no_revenue_range(parsed):
    pm4 = parsed["cells"]["5414|30K-5M|pm_q4"]
    assert pm4["cohort"] == "pm_q4"
    assert pm4["cohort_revenue_low"] is None
    assert pm4["cohort_revenue_high"] is None
    # top-margin quarter spends the least on labour of the four pm cohorts.
    labour = [parsed["cells"][f"5414|30K-5M|pm_q{i}"]["lines"]["salaries_wages"] for i in range(1, 5)]
    assert labour == sorted(labour, reverse=True)
    assert pm4["business_count"] >= ing.MIN_SAMPLE_SIZE


def test_full_parse_validates_and_meta_is_2024(parsed, tmp_path):
    assert parsed["meta"]["source_year"] == 2024
    assert parsed["meta"]["cohorts"][0] == "all"
    assert "rev_q4" in parsed["meta"]["cohorts"] and "pm_q4" in parsed["meta"]["cohorts"]
    assert parsed["meta"]["min_business_count"] == 20
    assert "Open Government Licence" in parsed["meta"]["attribution"]
    errors = ing._validate(parsed, tmp_path / "does_not_exist.json")
    assert errors == [], errors


# ── validate behaviour changes ───────────────────────────────────────────────

def _min_meta():
    return {"attribution": ing._ATTRIBUTION}


def test_validate_low_business_count_is_advisory_not_error(tmp_path):
    data = {
        "meta": _min_meta(),
        "cells": {
            SAMPLE_KEY: {
                "cohort": "all", "business_count": 5,   # < 20, must NOT hard-error
                "quality": "A", "pct_profitable": 0.8,
                "ratios": {"gross_margin": 0.829, "net_profit_margin": 0.188},
                "lines": {"operating_revenue": 1.0,
                          "subcontracts": 0.128, "salaries_wages": 0.365,
                          "professional_fees": 0.047},
            }
        },
    }
    assert ing._validate(data, tmp_path / "x.json") == []


def test_validate_rejects_bad_quality_letter(tmp_path):
    data = {
        "meta": _min_meta(),
        "cells": {
            SAMPLE_KEY: {
                "cohort": "all", "business_count": 100, "quality": "Z",
                "pct_profitable": 0.8,
                "ratios": {"gross_margin": 0.829, "net_profit_margin": 0.188},
                "lines": {"operating_revenue": 1.0,
                          "subcontracts": 0.128, "salaries_wages": 0.365,
                          "professional_fees": 0.047},
            }
        },
    }
    errors = ing._validate(data, tmp_path / "x.json")
    assert any("quality" in e for e in errors)


def test_validate_rejects_unordered_band(tmp_path):
    data = {
        "meta": _min_meta(),
        "cells": {
            SAMPLE_KEY: {
                "cohort": "all", "business_count": 100, "quality": "A",
                "pct_profitable": 0.8,
                "ratios": {"gross_margin": 0.829, "net_profit_margin": 0.188},
                "lines": {"operating_revenue": 1.0,
                          # q1 > q3 → an unordered (fabricated) band must be caught.
                          "subcontracts": {"pct_of_revenue": 0.128, "q1": 0.30,
                                           "median": 0.128, "q3": 0.05},
                          "salaries_wages": 0.365, "professional_fees": 0.047},
            }
        },
    }
    errors = ing._validate(data, tmp_path / "x.json")
    assert any("band not ordered" in e for e in errors)


# ── the optional anchor ──────────────────────────────────────────────────────
#
# Structure and plausibility are checked for every table. The anchor is opt-in:
# one named cell, the figures a caller declares it should carry, and a run that
# aborts rather than overwrite --out when they stop matching.

def _one_good_cell(**overrides):
    cell = {
        "cohort": "all", "business_count": 4200, "quality": "A",
        "pct_profitable": 0.80,
        "ratios": {"gross_margin": 0.80, "net_profit_margin": 0.20},
        "lines": {"operating_revenue": 1.0, "subcontracts": 0.13,
                  "salaries_wages": 0.35, "professional_fees": 0.05},
    }
    cell.update(overrides)
    return {"meta": _min_meta(), "cells": {SAMPLE_KEY: cell}}


def _anchor_file(tmp_path, **body):
    path = tmp_path / "anchor.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_no_anchor_supplied_validates_on_structure_alone(tmp_path):
    # Nobody inherits an industry's expected ratios: with no anchor the data
    # only has to be structurally sound and plausible.
    assert ing._validate(_one_good_cell(), tmp_path / "x.json") == []
    assert ing.load_anchor(None) is None


def test_anchor_supplied_and_met_validates(tmp_path):
    anchor = ing.load_anchor(_anchor_file(
        tmp_path, key=SAMPLE_KEY, tolerance=0.02,
        lines={"subcontracts": 0.13, "salaries_wages": 0.36},
        ratios={"net_profit_margin": 0.20},
    ))
    assert anchor["key"] == SAMPLE_KEY and anchor["tolerance"] == 0.02
    # salaries_wages is 0.35 against an expected 0.36 — inside the tolerance.
    assert ing._validate(_one_good_cell(), tmp_path / "x.json", anchor=anchor) == []


def test_anchor_supplied_and_missed_is_an_error(tmp_path):
    anchor = ing.load_anchor(_anchor_file(
        tmp_path, key=SAMPLE_KEY, tolerance=0.02,
        lines={"subcontracts": 0.30},            # data says 0.13
        ratios={"net_profit_margin": 0.20},
    ))
    errors = ing._validate(_one_good_cell(), tmp_path / "x.json", anchor=anchor)
    assert any("subcontracts" in e and "outside" in e for e in errors)
    assert not any("net_profit_margin" in e for e in errors)  # that one matched


def test_anchor_naming_an_absent_cell_is_an_error(tmp_path):
    anchor = ing.load_anchor(_anchor_file(
        tmp_path, key="9999|30K-5M|all", ratios={"net_profit_margin": 0.20}))
    errors = ing._validate(_one_good_cell(), tmp_path / "x.json", anchor=anchor)
    assert any("absent" in e for e in errors)


@pytest.mark.parametrize("body", [
    {},                                             # no key
    {"key": SAMPLE_KEY},                            # checks nothing
    {"key": SAMPLE_KEY, "lines": "subcontracts"},   # wrong shape
    {"key": SAMPLE_KEY, "lines": {"subcontracts": "high"}},   # not a number
    {"key": SAMPLE_KEY, "lines": {"subcontracts": 0.13}, "tolerance": 0},
])
def test_unusable_anchor_file_fails_loud(tmp_path, body):
    # An anchor that was asked for and cannot be used must raise, never be
    # silently skipped — a skipped check reads exactly like a passing one.
    with pytest.raises(RuntimeError):
        ing.load_anchor(_anchor_file(tmp_path, **body))


def test_missing_anchor_file_fails_loud(tmp_path):
    with pytest.raises(RuntimeError):
        ing.load_anchor(tmp_path / "not_here.json")


def test_emit_writes_when_no_anchor_is_supplied(tmp_path):
    out = tmp_path / "benchmarks.json"
    assert ing._emit(_one_good_cell(), out, dry_run=False) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["cells"][SAMPLE_KEY]


def test_emit_aborts_and_leaves_out_untouched_when_anchor_missed(tmp_path):
    out = tmp_path / "benchmarks.json"
    prior = _one_good_cell()
    assert ing._emit(prior, out, dry_run=False) == 0
    before = out.read_bytes()

    anchor = ing.load_anchor(_anchor_file(
        tmp_path, key=SAMPLE_KEY, lines={"subcontracts": 0.30}))
    moved = _one_good_cell()
    moved["cells"][SAMPLE_KEY]["quality"] = "B"      # a change that must NOT land
    assert ing._emit(moved, out, dry_run=False, anchor=anchor) == 1
    assert out.read_bytes() == before                # the refresh did not write


def test_emit_writes_when_the_anchor_is_met(tmp_path):
    out = tmp_path / "benchmarks.json"
    anchor = ing.load_anchor(_anchor_file(
        tmp_path, key=SAMPLE_KEY, lines={"subcontracts": 0.13}))
    assert ing._emit(_one_good_cell(), out, dry_run=False, anchor=anchor) == 0
    assert out.exists()


# ── provenance ───────────────────────────────────────────────────────────────

def test_a_table_of_invented_figures_must_declare_itself(tmp_path):
    # A synthetic table says so in meta.notice and carries NO attribution:
    # attributing made-up numbers to a public release is the failure mode.
    silent = _one_good_cell()
    silent["meta"] = {"synthetic": True}
    assert any("notice" in e for e in ing._validate(silent, tmp_path / "x.json"))

    misattributed = _one_good_cell()
    misattributed["meta"] = {"synthetic": True, "notice": "Invented figures.",
                             "attribution": ing._ATTRIBUTION}
    assert any("attribution" in e
               for e in ing._validate(misattributed, tmp_path / "x.json"))

    honest = _one_good_cell()
    honest["meta"] = {"synthetic": True, "notice": "Invented figures."}
    assert ing._validate(honest, tmp_path / "x.json") == []


def test_a_downloaded_table_must_carry_the_licence_attribution(tmp_path):
    data = _one_good_cell()
    data["meta"] = {"source_year": 2024}
    assert any("attribution" in e for e in ing._validate(data, tmp_path / "x.json"))


def test_seed_only_emits_an_honest_illustrative_table(tmp_path, monkeypatch):
    monkeypatch.delenv(ing.ANCHOR_ENV_VAR, raising=False)
    out = tmp_path / "seeded.json"
    assert ing.main(["--seed-only", "--out", str(out)]) == 0
    meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
    assert meta["synthetic"] is True
    assert "not industry data" in meta["notice"].lower()
    assert "attribution" not in meta       # nothing licensed is in this file
    # Byte-identical on a second run — the offline path is deterministic.
    first = out.read_bytes()
    assert ing.main(["--seed-only", "--out", str(out)]) == 0
    assert out.read_bytes() == first


# ── the committed samples ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "ised_benchmarks_sample.json",
    "ised_benchmarks_cohorts_sample.json",
])
def test_committed_benchmark_fixtures_pass_this_pipeline_validator(tmp_path, name):
    """The benchmark fixtures are generated, not hand-kept, so they have to
    survive the same checks the pipeline applies to a downloaded table —
    ordered bands, plausible lines and margins, and an honest meta."""
    path = Path(__file__).parent / "fixtures" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert ing._validate(data, tmp_path / "does_not_exist.json") == []

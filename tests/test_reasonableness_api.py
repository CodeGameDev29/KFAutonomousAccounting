"""Integration tests for the Reasonableness API (GET /flags + dismiss + onboarding NAICS).

All figures here are synthetic: the same invented six-month P&L for
``Example Corp Inc.`` used by ``tests/test_reasonableness.py`` (see that module
for why each line is the size it is), served through a fake database. The peer
numbers come from ``tests/fixtures/ised_benchmarks_cohorts_sample.json``, which
``scripts/gen_fixtures.py`` writes and whose every figure is invented — it is
the benchmark table's shape, not a sample of any published release.

Auth + db are overridden with fakes; ``is_enabled`` is monkeypatched per test.
The wire band enum (``few``) and the ``status`` discriminator are asserted here
(the engine's internal ``a_few`` is asserted in tests/test_reasonableness.py).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Extended with the outlier/alarm framing phrases so the golden-payload blob
# scan in the happy-path test enforces them. NOTE: "quartile"/"percentile" are
# DELIBERATELY excluded from this str(body) scan — `quartile` is a legitimate
# ReasonablenessMeta field KEY, so it appears in str(body) as a key name. Those
# two stats-vocab tokens are scanned over user-facing VALUE strings only (see
# test_no_new_forbidden_or_stats_vocab_in_body).
FORBIDDEN = [
    "audit risk score", "audit risk", "risk score", "cra-proof", "audit shield",
    "won't get audited", "safe from cra", "audit probability", "chance of audit",
    # outlier / alarm framing, and any ranking of the user's own profitability
    "red flag", "flagged", "the cra will notice", "cra will notice", "suspicious",
    "at risk", "overspending", "too high", "you're less profitable",
    "you are less profitable", "underperforming", "below-average business",
]

_FIXTURE = Path(__file__).parent / "fixtures" / "ised_benchmarks_cohorts_sample.json"

#: The NAICS code the invented company files under, and the one the generated
#: cohort sample is keyed on.
GOLDEN_NAICS = "5414"

#: The invented reporting window: Apr–Sep 2026, a partial year.
GOLDEN_RANGE = "start=2026-04-01&end=2026-09-30"
FULL_YEAR_RANGE = "start=2026-01-01&end=2026-12-31"

#: Restated once so the expectations below are derived, not pasted.
GOLDEN_REVENUE = 196400.00
GOLDEN_CONTRACTORS = 61900.00


def _fixture_benchmarks() -> dict:
    """The generated benchmark sample: ordered bands, a business_count on every
    cell, and the rev_q*/pm_q* cohorts. The route reads whatever table is
    installed, and a table without business_counts suppresses to `no_data`
    under the density gate, so the API tests inject this one to exercise the OK
    path deterministically. Every figure in it is invented."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _benchmarks_keyed_to(naics: str) -> dict:
    """The same sample, re-keyed to another NAICS code.

    The sample carries one invented industry. A test that needs the cohort to
    answer for a different code copies the cells under that code, rather than
    pinning the fixture to whatever the industry map happens to hold."""
    b = copy.deepcopy(_fixture_benchmarks())
    b["cells"] = {
        key.replace(f"{GOLDEN_NAICS}|", f"{naics}|", 1): {**cell, "naics": naics}
        for key, cell in b["cells"].items()
    }
    return b


def _all_cell() -> dict:
    return _fixture_benchmarks()["cells"][f"{GOLDEN_NAICS}|30K-5M|all"]


#: Invented provincial margin, deliberately unlike the national one.
PROVINCE_NET_MARGIN = 0.194


def _fixture_with_province() -> dict:
    """The sample + a provincial cohort cell (``…|all|46``, Manitoba's StatCan
    SGC code) so the province-cohort selection path is reachable in the API
    test."""
    b = copy.deepcopy(_fixture_benchmarks())
    prov = copy.deepcopy(b["cells"][f"{GOLDEN_NAICS}|30K-5M|all"])
    prov["province"] = "46"
    prov["province_name"] = "Manitoba"
    prov["ratios"] = {"gross_margin": prov["ratios"]["gross_margin"],
                    "net_profit_margin": PROVINCE_NET_MARGIN}
    b["cells"][f"{GOLDEN_NAICS}|30K-5M|all|46"] = prov
    return b


def _user():
    from server.auth import AuthUser

    return AuthUser(
        id="00000000-0000-0000-0000-000000000001",
        email="tester@example.com",
        role="authenticated",
        email_verified=True,
    )


def _row(category, income=0.0, expenses=0.0, currency="CAD"):
    return {
        "category": category, "currency": currency, "income": income,
        "expenses": expenses, "net": income - expenses, "transaction_count": 1,
    }


def _golden_rows():
    """The invented Example Corp Inc. P&L, in the FIXED Canada ISED/GIFI
    vocabulary. See tests/test_reasonableness.py::_golden_rows for the rationale
    behind each line; the headline is $61,900 of contractors against $20,600 of
    payroll on $196,400 of operating sales."""
    return [
        _row("Sales of goods and services", income=128750.00),
        _row("Sales of goods and services", income=67650.00),
        _row("Refund/Credit", income=2240.00),
        _row("All other revenues", income=168.00),
        _row("Purchases, materials and sub-contracts", expenses=27400.00),
        _row("Purchases, materials and sub-contracts", expenses=21300.00),
        _row("Purchases, materials and sub-contracts", expenses=13200.00),
        _row("Labour and commissions", expenses=20600.00),
        _row("Income tax and GST/HST remittance", expenses=6450.00),
        _row("Travel", expenses=13400.00),
        _row("Software and IT", expenses=2150.00),
        _row("Software and IT", expenses=1380.00),
        _row("Insurance", expenses=385.00),
        _row("Owner's draw", expenses=16200.00),
    ]


class _FakeDB:
    def __init__(self, *, rows=None, naics=GOLDEN_NAICS, industry="Creative & Design",
                 province=None, dismissed=None):
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self._rows = rows if rows is not None else _golden_rows()
        self._naics = naics
        self._industry = industry
        self._province = province
        self._dismissed = list(dismissed or [])
        self.upserts: list[str] = []
        self.deletes: list[str] = []
        self.profile_upserts: list[dict] = []

    def get_business_profile(self):
        return {"naics_code": self._naics, "base_currency": "CAD",
                "industry": self._industry, "province": self._province}

    def get_category_breakdown(self, s, e):
        return self._rows

    def get_latest_transaction_month(self):
        return (2026, 9)

    def get_dismissed_reasonableness_flags(self):
        return [
            {"flag_key": k, "note": None, "naics_code": None, "dismissed_at": None}
            for k in self._dismissed
        ]

    def upsert_dismissed_reasonableness_flag(self, flag_key, note=None, naics_code=None):
        self.upserts.append(flag_key)

    def delete_dismissed_reasonableness_flag(self, flag_key):
        self.deletes.append(flag_key)
        return True

    def upsert_business_profile(self, data):
        self.profile_upserts.append(data)


def _client(db, *, enabled=True, monkeypatch, router_name="reasonableness", benchmarks=None):
    # Override using the ROUTE MODULE's OWN dependency references so the
    # override matches even when another test reloaded server.deps/server.auth
    # (pytest-randomly shuffles order). Importing get_db fresh here could yield
    # a different object than the router's Depends captured at import time,
    # causing the override to silently miss and hit the real DB.
    if router_name == "reasonableness":
        import server.api.reasonableness as mod
        monkeypatch.setattr(mod, "is_enabled", lambda _db, _uid, _flag: enabled)
        # Inject the hermetic generated fixture in place of the shipped
        # benchmark file. `_load_benchmarks` is module-global and lru_cached;
        # replacing the attribute makes every call site (engine input +
        # attribution/geo/meta helpers) read the fixture. monkeypatch reverts it.
        bench = benchmarks if benchmarks is not None else _fixture_benchmarks()
        monkeypatch.setattr(mod, "_load_benchmarks", lambda: bench)
    else:
        import server.api.onboarding as mod

    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[mod.get_current_user] = lambda: _user()
    app.dependency_overrides[mod.get_db] = lambda: db
    return TestClient(app)


# ── GET /flags ───────────────────────────────────────────────────────────────

class TestFlagsEndpoint:
    def test_the_happy_path_answers_with_the_golden_result(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Two elevated findings and nothing that stands out: engine "a_few" -> wire "few".
        assert body["band"] == "few"
        assert body["flags"][0]["id"] == "pattern_contractor_heavy"
        first = body["flags"][0]
        for key in ("id", "category", "severity", "direction", "title", "prompt",
                    "your_pct", "peer_pct", "peer_band", "amount", "currency", "dismissed"):
            assert key in first
        assert first["amount"] == pytest.approx(GOLDEN_CONTRACTORS, abs=0.01)
        # The share on the wire is the flag's own dollars over the revenue base
        # the response reports — the two must never disagree.
        assert first["your_pct"] == pytest.approx(
            first["amount"] / body["meta"]["operating_revenue"], abs=1e-6
        )
        # The sample carries no attribution of its own — invented figures have
        # nothing to attribute — so this is the route's own attribution for the
        # licensed table it reads in production. It must always be present.
        assert "Open Government Licence" in body["meta"]["attribution"]
        assert body["meta"]["naics_code"] == GOLDEN_NAICS
        assert body["meta"]["partial_year"] is True
        blob = str(body).lower()
        for bad in FORBIDDEN:
            assert bad not in blob

    def test_an_anonymous_request_is_rejected(self):
        # No auth override → real get_current_user rejects the anonymous request.
        from server.api.reasonableness import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code in (401, 403)
        assert "pattern_contractor_heavy" not in resp.text

    def test_every_material_category_has_a_comparison(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        body = resp.json()
        comps = body["comparisons"]
        assert comps, "per-category comparisons should always be present when ok"
        # every comparison carries its peer average + a plain status
        for c in comps:
            assert set(("label", "your_pct", "peer_pct", "status")) <= set(c)
            assert c["status"] in ("in_line", "above", "below", "well_above",
                                   "well_below", "no_benchmark", "watch")
        sub = next(c for c in comps if c["gifi_bucket"] == "subcontracts")
        # ~31.5% of revenue against the sample's band: more than one IQR past q3.
        line = _all_cell()["lines"]["subcontracts"]
        assert sub["your_pct"] > line["q3"] + (line["q3"] - line["q1"])
        assert sub["status"] == "well_above"
        assert sub["amount"] == pytest.approx(GOLDEN_CONTRACTORS, abs=0.01)
        assert sub["peer_pct"] > 0  # industry median shown
        # travel is surfaced even without an ISED benchmark line
        travel = next(c for c in comps if c["gifi_bucket"] == "travel")
        assert travel["status"] == "watch"
        assert travel["peer_pct"] is None

    def test_the_cohort_it_compared_against_is_disclosed(self, monkeypatch):
        """Everything the card says about the peer cohort is the cell's own
        published value, so a stale or re-ingested table cannot silently change
        what the user is told they were compared against."""
        db = _FakeDB()
        cell = _all_cell()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        m = resp.json()["meta"]
        assert m["cohort_label"] == cell["naics_label"]
        assert m["revenue_band_label"] == "$30K–$5M in annual revenue"
        assert m["geography"] == _fixture_benchmarks()["meta"]["geography"]
        # The re-ingested table carries a real business_count, so the cohort
        # sample_size is the honest N behind the 'all' cell. Aligns with
        # meta["business_count"].
        assert m["sample_size"] == cell["business_count"]
        assert m["business_count"] == cell["business_count"]
        assert m["pct_profitable"] == pytest.approx(cell["pct_profitable"])
        assert m["net_profit_margin"] == pytest.approx(cell["ratios"]["net_profit_margin"])
        assert m["gross_margin"] == pytest.approx(cell["ratios"]["gross_margin"])
        assert m["source_url"] == cell["source_url"]
        # Six months of revenue annualizes to more than the in-period figure.
        assert m["annualized_revenue"] > m["operating_revenue"]

    def test_a_provincial_profile_uses_the_provincial_cohort(self, monkeypatch):
        # Provincial profile → the provincial cohort (injected into the sample).
        db = _FakeDB(province="Manitoba")
        client = _client(db, monkeypatch=monkeypatch, benchmarks=_fixture_with_province())
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        m = resp.json()["meta"]
        assert m["cohort_province"] == "Manitoba"
        assert "provincial" in (m["geography"] or "")
        assert m["net_profit_margin"] == pytest.approx(PROVINCE_NET_MARGIN)

    def test_no_province_on_the_profile_uses_the_national_cohort(self, monkeypatch):
        db = _FakeDB(province=None)
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        m = resp.json()["meta"]
        assert m["cohort_province"] is None
        assert m["geography"] == "Canada (national)"

    # ── No percentile / peer_lower fields anywhere on the wire ───────────

    def _golden_body(self, monkeypatch, **db_kw):
        db = _FakeDB(**db_kw)
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code == 200
        return resp.json()

    def test_new_comparison_fields_present_and_typed(self, monkeypatch):
        body = self._golden_body(monkeypatch)
        assert body["comparisons"]
        for c in body["comparisons"]:
            # NO percentile / peer_lower / peer_median / peer_upper anywhere.
            for absent in ("percentile", "peer_lower", "peer_median", "peer_upper"):
                assert absent not in c, f"{absent!r} must not exist"
            # New fields present + correctly typed (None allowed).
            assert c["peer_pct"] is None or isinstance(c["peer_pct"], (int, float))
            assert c["peer_band"] is None or (
                isinstance(c["peer_band"], list) and len(c["peer_band"]) == 2
            )
            assert c["business_count"] is None or isinstance(c["business_count"], int)
            assert c["quality"] is None or isinstance(c["quality"], str)
            assert c["recommendation"] is None or isinstance(c["recommendation"], str)
            assert c["unavailable_reason"] in (
                None, "grouped_other", "insufficient_filings"
            )
            assert isinstance(c["weight"], (int, float))

    def test_subcontracts_carries_real_band(self, monkeypatch):
        body = self._golden_body(monkeypatch)
        cell = _all_cell()
        line = cell["lines"]["subcontracts"]
        sub = next(c for c in body["comparisons"] if c["gifi_bucket"] == "subcontracts")
        # honest [q1, q3] carried through to the wire, not fabricated
        assert sub["peer_band"] == [line["q1"], line["q3"]]
        assert sub["peer_pct"] == pytest.approx(line["median"])  # median-first central
        assert sub["business_count"] == cell["business_count"]
        assert sub["quality"] == cell["quality"]

    def test_null_bounds_line_shows_suppressed_status(self, monkeypatch):
        # The golden has buckets with no ISED line (travel, software/IT, the CRA
        # remittance, the owner's draw — all grouped into "other expenses").
        # Those rows are still present, but honestly suppressed: null peer, null band.
        body = self._golden_body(monkeypatch)
        suppressed = [c for c in body["comparisons"] if c["status"] == "no_benchmark"]
        assert suppressed, "a grouped-into-other bucket should surface as no_benchmark"
        for c in suppressed:
            assert c["peer_pct"] is None
            assert c["peer_band"] is None              # NEVER a fabricated ±spread
            assert c["unavailable_reason"] in ("grouped_other", "insufficient_filings")

    def test_profitability_section_in_body(self, monkeypatch):
        body = self._golden_body(monkeypatch)
        prof = body["profitability"]
        assert prof, "always-on profitability section should be populated"
        assert body["profitability_cohort_label"]
        for line in prof:
            assert line["your_pct"] is not None
            assert line["peer_pct"] is not None
            assert isinstance(line["business_count"], int) and line["business_count"] >= 20
            assert line["note"]
            # No status / direction / user-margin rank field on the wire: the
            # section describes the peer cohort, never ranks the reader.
            for absent in ("status", "direction", "user_margin_quartile"):
                assert absent not in line, f"{absent!r} must not exist"

    def test_profitability_no_user_ranking_in_body(self, monkeypatch):
        body = self._golden_body(monkeypatch)
        blob = str(body["profitability"]).lower() + " " + str(
            body.get("profitability_cohort_label") or ""
        ).lower()
        for banned in ("you're in the", "you are in the", "your quartile", "bottom quarter",
                       "least profitable", "you rank", "your profitability",
                       "user_margin_quartile"):
            assert banned not in blob, f"user-ranking leak: {banned!r}"

    def test_meta_exposes_cohort_trail(self, monkeypatch):
        m = self._golden_body(monkeypatch)["meta"]
        year = _fixture_benchmarks()["meta"]["source_year"]
        # ~$392K annualized sits inside the revenue window the sample
        # deliberately leaves uncovered, so no size cohort brackets it: the
        # whole-band cell it is.
        assert m["size_cohort"] == "all"
        assert m["business_count"] == _all_cell()["business_count"]
        assert m["cohort_quality"] == _all_cell()["quality"]
        assert m["naics_match_depth"] == 4        # the sample is 4-digit only
        assert m["benchmark_year"] == year
        assert m["data_vintage"] == year
        assert m["cohort_trail"] == {
            "naics_depth": 4, "band": "30K-5M", "geo": "national", "cohort": "all",
        }

    def test_no_new_forbidden_or_stats_vocab_in_body(self, monkeypatch):
        body = self._golden_body(monkeypatch)
        # (a) Extended FORBIDDEN framing over the whole payload (safe as key names
        #     never contain these phrases).
        blob = str(body).lower()
        for bad in FORBIDDEN:
            assert bad not in blob, f"forbidden framing in body: {bad!r}"
        # (b) Stats vocab ("quartile"/"percentile") must never appear in a
        #     user-facing VALUE string (they legitimately exist as meta KEY names).
        surfaced: list[str] = [body.get("band_label") or ""]
        surfaced += body.get("reassurances", [])
        surfaced.append(body.get("profitability_cohort_label") or "")
        for f in body["flags"]:
            surfaced += [f.get("title") or "", f.get("prompt") or "",
                         f.get("recommendation") or ""]
        for c in body["comparisons"]:
            surfaced += [c.get("label") or "", c.get("recommendation") or ""]
        for p in body["profitability"]:
            surfaced += [p.get("label") or "", p.get("note") or ""]
        for text in surfaced:
            low = text.lower()
            assert "quartile" not in low and "percentile" not in low, (
                f"stats vocab surfaced in user-facing copy: {text!r}"
            )

    def test_suppressed_cohort_response_shape(self, monkeypatch):
        # A NAICS with no cohort in the table → no_data; no metadata leaks.
        db = _FakeDB(naics="999999")
        client = _client(db, monkeypatch=monkeypatch)
        body = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}").json()
        assert body["status"] == "no_data"
        assert body["comparisons"] == []
        assert body["meta"]["business_count"] is None

    def test_the_revenue_base_on_the_wire_is_operating_sales_only(self, monkeypatch):
        """The revenue denominator on the wire must be operating sales only.

        The golden P&L carries $168 of interest earned under the canonical name
        "All other revenues", which the fixed taxonomy classifies as
        non-operating. The engine's own crosswalk excludes it (asserted in
        tests/test_reasonableness.py::TestDenominator), and the route has to use
        that same crosswalk rather than the bare YAML, whose `categories:` block
        lives in config/ised_categories.py — reading the YAML alone leaves every
        canonical name to fall through the fuzzy fallback rules, which puts the
        interest into operating revenue and leaves payroll short of the ISED
        wages line entirely.
        """
        body = self._golden_body(monkeypatch)
        assert body["meta"]["operating_revenue"] == pytest.approx(GOLDEN_REVENUE, abs=0.01)
        # Payroll must reach the ISED wages line rather than "other operating".
        assert any(c["gifi_bucket"] == "salaries_wages" for c in body["comparisons"])

    def test_a_stored_industry_label_resolves_to_its_cohort(self, monkeypatch):
        # No stored NAICS code, but the coarse industry label maps to a cohort.
        # The label and its code are taken from the engine's own map, and the
        # sample is re-keyed to that code, so this test asserts the plumbing
        # rather than which industries happen to be mapped.
        from core.reasonableness import INDUSTRY_TO_NAICS

        label, mapped = next(iter(INDUSTRY_TO_NAICS.items()))
        db = _FakeDB(naics=None, industry=label)
        client = _client(db, monkeypatch=monkeypatch,
                         benchmarks=_benchmarks_keyed_to(mapped))
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["meta"]["naics_source"] == "industry_map"
        assert body["meta"]["naics_code"] == mapped
        assert body["comparisons"]

    def test_the_card_hides_when_nothing_fits(self, monkeypatch):
        # No code, unmapped industry, no clear spending signature → card hides.
        db = _FakeDB(
            naics=None, industry="Nonprofit",
            rows=[_row("Client Revenue", income=152000.0), _row("Others", expenses=46000.0)],
        )
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("missing_naics", "no_data")
        assert body["flags"] == []
        assert body["comparisons"] == []

    def test_a_year_under_the_floor_answers_below_floor(self, monkeypatch):
        # A whole year at $14,500 is well under the $30,000 floor.
        db = _FakeDB(rows=[_row("Sales of goods and services", income=14500.0),
                           _row("Travel", expenses=2700.0)])
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{FULL_YEAR_RANGE}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "below_floor"
        assert body["flags"] == []
        assert body["meta"]["below_floor"] is True

    def test_no_rows_answers_no_data(self, monkeypatch):
        db = _FakeDB(rows=[])
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "no_data"

    def test_the_feature_gate_closed_answers_no_data(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, enabled=False, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "no_data"
        assert body["flags"] == []
        assert "Open Government Licence" in body["meta"]["attribution"]

    def test_bad_date_range_400(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get("/api/reasonableness/flags?start=2026-09-30&end=2026-04-01")
        assert resp.status_code == 400

    def test_one_of_pair_400(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get("/api/reasonableness/flags?start=2026-04-01")
        assert resp.status_code == 400


# ── Dismiss ──────────────────────────────────────────────────────────────────

class TestDismissEndpoint:
    def test_dismissing_a_flag_is_stored(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.post(
            "/api/reasonableness/flags/pattern_travel_over_threshold/dismiss",
            json={"dismissed": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"id": "pattern_travel_over_threshold", "dismissed": True}
        assert db.upserts == ["pattern_travel_over_threshold"]

    def test_a_dismissed_flag_is_gone_on_the_next_fetch(self, monkeypatch):
        # The travel threshold flag IS in the golden result, so dismissing it has
        # to actually remove it — and leave the headline in first place.
        db = _FakeDB(dismissed=["pattern_travel_over_threshold"])
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.get(f"/api/reasonableness/flags?{GOLDEN_RANGE}")
        body = resp.json()
        keys = [f["id"] for f in body["flags"]]
        assert "pattern_travel_over_threshold" not in keys
        assert keys[0] == "pattern_contractor_heavy"
        assert "pattern_travel_over_threshold" in body["meta"]["dismissed_flag_keys"]

    def test_undismissing_deletes_the_row(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.post(
            "/api/reasonableness/flags/ratio_subcontracts_high/dismiss",
            json={"dismissed": False},
        )
        assert resp.status_code == 200
        assert db.deletes == ["ratio_subcontracts_high"]

    def test_an_invalid_flag_id_is_rejected(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch)
        resp = client.post(
            "/api/reasonableness/flags/BAD-ID!/dismiss", json={"dismissed": True}
        )
        assert resp.status_code == 400
        assert db.upserts == []


# ── Onboarding NAICS plumbing ────────────────────────────────────────────────

class TestOnboardingNaics:
    def test_a_posted_naics_code_is_stored(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch, router_name="onboarding")
        resp = client.post(
            "/api/onboarding/profile",
            json={"company_name": "Example Corp Inc.", "naics_code": "541430"},
        )
        assert resp.status_code == 200
        assert db.profile_upserts
        assert db.profile_upserts[-1]["naics_code"] == "541430"

    def test_picker_6digit_code_stored(self, monkeypatch):
        # The NAICS picker (from naics.ts) sends a 6-digit code + industry label.
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch, router_name="onboarding")
        resp = client.post(
            "/api/onboarding/profile",
            json={"company_name": "Example Corp Inc.", "naics_code": "541430",
                  "industry": "Creative & Design"},
        )
        assert resp.status_code == 200
        assert db.profile_upserts[-1]["naics_code"] == "541430"

    def test_clearing_the_code_overwrites_the_stored_value(self, monkeypatch):
        # Clearing a set NAICS must send "" (overwrites via COALESCE), not None
        # (which would preserve the old value).
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch, router_name="onboarding")
        resp = client.post(
            "/api/onboarding/profile",
            json={"company_name": "Example Corp Inc.", "naics_code": ""},
        )
        assert resp.status_code == 200
        assert db.profile_upserts[-1]["naics_code"] == ""

    def test_an_absent_field_preserves_the_stored_value(self, monkeypatch):
        # Field absent from payload → None → upsert preserves stored value.
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch, router_name="onboarding")
        resp = client.post("/api/onboarding/profile",
                           json={"company_name": "Example Corp Inc."})
        assert resp.status_code == 200
        assert db.profile_upserts[-1]["naics_code"] is None

    def test_a_malformed_code_is_rejected(self, monkeypatch):
        db = _FakeDB()
        client = _client(db, monkeypatch=monkeypatch, router_name="onboarding")
        resp = client.post(
            "/api/onboarding/profile",
            json={"company_name": "Example Corp Inc.", "naics_code": "not-a-code"},
        )
        assert resp.status_code == 422

    def test_the_profile_round_trips_its_naics_code(self):
        db = _FakeDB()
        assert db.get_business_profile()["naics_code"] == GOLDEN_NAICS

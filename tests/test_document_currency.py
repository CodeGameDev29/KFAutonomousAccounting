"""The receipt's currency, and how the engine knows it (core/document_currency.py).

Every receipt, payload, amount and marker in this module is invented — see
``tests/fixtures/README.md``.

The problem the module exists for: a card line reading
``EXMPL SVC *SUBSCRIPTION SAMPLETOWNCA``, C$28.00, matched against its own
receipt — Bluepeak Software Inc, subtotal 25.00, other tax 3.00, total 28.00.
The receipt prints a bare "$" beside a US mailing address and states no code, so
a prompt that asks a model to *name* the currency gets USD, and the pair then
scores **Amount 0**: "Cross-currency amounts do not reconcile at any plausible
1.20-1.55 rate ($28.00 CAD vs $28.00 USD, implied rate 1.0000)".

The tax line settles it without guessing: 3.00 on 25.00 is exactly 12% — 5% GST
+ 7% provincial sales tax — and no US jurisdiction charges that rate. So the
prompt asks for the currency text beside the total to be *quoted* rather than
inferred from an address, and the provenance column (``stated`` | ``inferred``
| ``assumed`` | None) keeps the two cases apart: a *stated* USD invoice against
a CAD bank row must still fail, while an *unstated* one must not.
"""

from __future__ import annotations

from decimal import Decimal

from core.document_currency import (
    ASSUMED,
    INFERRED,
    STATED,
    canadian_tax_signal,
    classify_currency_marker,
    currency_fields,
    currency_from_extraction,
    currency_from_marker,
    marker_from_text,
    normalize_currency_code,
    resolve_document_currency,
)

#: The invented receipt: 12% on the subtotal, and a code the model guessed.
SUBSCRIPTION_RECEIPT = {
    "vendor": "Bluepeak Software Inc",
    "currency": "USD",          # what the model guessed from the US address
    "subtotal": "25.00",
    "tax_other": "3.00",
    "total": "28.00",
}


# ── The tax signal ────────────────────────────────────────────────────────

class TestCanadianTaxSignal:
    def test_twelve_percent_fires(self):
        """3.00 on 25.00 is 12.0% — 5% GST + 7% provincial sales tax."""
        assert canadian_tax_signal(
            {"subtotal": "25.00", "tax_other": "3.00", "total": "28.00"}
        )

    def test_ontario_thirteen_percent_fires(self):
        """39.00 on 300.00 is 13.0% — Ontario HST."""
        assert canadian_tax_signal(
            {"subtotal": "300.00", "tax_other": "39.00", "total": "339.00"}
        )

    def test_gst_only_five_percent_fires(self):
        """24.00 on 480.00 is 5.0% — GST alone, no provincial tax."""
        assert canadian_tax_signal(
            {"subtotal": "480.00", "tax_other": "24.00", "total": "504.00"}
        )

    def test_a_us_sales_tax_rate_does_not_fire(self):
        """26.63 on 300.00 is 8.877% (New York City), not a Canadian rate."""
        assert not canadian_tax_signal(
            {"subtotal": "300.00", "tax_other": "26.63", "total": "326.63"}
        )

    def test_a_named_canadian_tax_fires_on_its_own(self):
        """GST/HST/PST are Canadian statutes; a figure beside one is enough."""
        assert canadian_tax_signal({"tax_gst": "1.25"})
        assert canadian_tax_signal({"gst": "1.25"})          # extraction spelling
        assert canadian_tax_signal({"tax_pst": "1.75"})

    def test_zero_tax_fields_are_not_a_signal(self):
        assert not canadian_tax_signal(
            {"tax_gst": "0", "tax_hst": "0", "tax_pst": "0", "subtotal": "56.00"}
        )

    def test_no_subtotal_no_rate_to_read(self):
        assert not canadian_tax_signal({"total": "28.00"})

    def test_total_minus_subtotal_is_used_when_no_tax_line_is_itemised(self):
        """336.00 - 300.00 is 12% of the subtotal, itemised or not."""
        assert canadian_tax_signal({"subtotal": "300.00", "total": "336.00"})

    def test_garbage_never_raises(self):
        assert not canadian_tax_signal(
            {"subtotal": set(), "tax_gst": object(), "total": b"bytes"}
        )
        assert not canadian_tax_signal(None)


# ── The resolver ──────────────────────────────────────────────────────────

class TestResolveDocumentCurrency:
    def test_a_stated_code_wins_over_everything(self):
        """A genuinely stated USD invoice stays USD, however Canadian its tax
        line happens to read."""
        doc = dict(SUBSCRIPTION_RECEIPT, currency_source="stated")
        assert resolve_document_currency(doc) == ("USD", STATED)

    def test_the_subscription_receipt_resolves_to_cad(self):
        """A legacy row whose stored code disagrees with its own tax line."""
        assert resolve_document_currency(SUBSCRIPTION_RECEIPT) == ("CAD", INFERRED)

    def test_an_assumed_row_with_a_tax_line_is_inferred(self):
        doc = dict(SUBSCRIPTION_RECEIPT, currency=None, currency_source="assumed")
        assert resolve_document_currency(doc) == ("CAD", INFERRED)

    def test_a_legacy_row_with_a_code_and_no_tax_signal_is_unchanged(self):
        assert resolve_document_currency({"currency": "USD"}) == ("USD", STATED)

    def test_a_legacy_cad_row_beside_a_cad_tax_line_stays_plain(self):
        """The inference agrees with the row, so there is nothing to explain."""
        assert resolve_document_currency(
            {"currency": "CAD", "subtotal": "300.00", "tax_gst": "15.00"}
        ) == ("CAD", STATED)

    def test_nothing_at_all_is_assumed(self):
        assert resolve_document_currency({}) == (None, ASSUMED)
        assert resolve_document_currency({"currency": None}) == (None, ASSUMED)

    def test_a_bare_dollar_sign_is_not_a_currency(self):
        assert resolve_document_currency({"currency": "$"}) == (None, ASSUMED)

    def test_an_inferred_row_keeps_its_provenance(self):
        assert resolve_document_currency(
            {"currency": "CAD", "currency_source": "inferred"}
        ) == ("CAD", INFERRED)

    def test_it_reads_an_object_as_well_as_a_mapping(self):
        from datetime import date

        from models.document import Document

        doc = Document(
            original_filename="20260306-bluepeak-subscription.png",
            file_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            vendor="Bluepeak Software Inc", document_date=date(2026, 3, 6),
            currency="USD", subtotal=Decimal("25.00"),
            tax_other=Decimal("3.00"), total=Decimal("28.00"),
        )
        assert resolve_document_currency(doc) == ("CAD", INFERRED)


class TestNormalizeCurrencyCode:
    def test_it_upper_cases_a_real_code(self):
        assert normalize_currency_code("usd") == "USD"

    def test_it_rejects_everything_that_is_not_one(self):
        for junk in (None, "", "$", "CA$", "dollars", 12, object()):
            assert normalize_currency_code(junk) is None


# ── Extraction-time resolution ────────────────────────────────────────────

class TestCurrencyFromExtraction:
    def test_a_payload_with_no_currency_and_a_canadian_tax_line(self):
        """The subscription payload, as the prompt returns it: no code at all,
        and a 12% tax line that can only be Canadian."""
        payload = {
            "vendor": "Bluepeak Software Inc", "date": "2026-03-06",
            "subtotal": "25.00", "other_tax": "3.00", "total": "28.00",
        }
        assert currency_from_extraction(payload) == ("CAD", INFERRED)

    def test_a_quoted_code_is_stated(self):
        assert currency_from_extraction(
            {"currency_marker": "USD", "currency": "USD"}
        ) == ("USD", STATED)

    def test_a_code_nobody_quoted_is_not_stated(self):
        """The model's reading on its own is an opinion, not a quotation.

        This is the hole the marker closes: "emit `currency` only when the
        document states one" still leaves the model deciding what "states"
        means, and on a bare "$" under a US address it decides USD.
        """
        assert currency_from_extraction({"currency": "USD"}) == (None, ASSUMED)

    def test_nothing_stated_and_nothing_inferable_is_assumed(self):
        assert currency_from_extraction({"total": "41.25"}) == (None, ASSUMED)

    def test_the_document_it_builds_carries_both(self):
        from core.extraction import build_document_from_extraction

        doc = build_document_from_extraction(
            extraction_data={
                "vendor": "Bluepeak Software Inc", "date": "2026-03-06",
                "subtotal": "25.00", "other_tax": "3.00", "total": "28.00",
            },
            original_filename="20260306-bluepeak-subscription.png",
            file_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            raw_json="{}",
        )
        assert doc.currency == "CAD"
        assert doc.currency_source == INFERRED

    def test_a_bare_dollar_sign_leaves_the_column_null(self):
        from core.extraction import build_document_from_extraction

        doc = build_document_from_extraction(
            extraction_data={
                "vendor": "Sample Street Cafe", "total": "41.25", "currency": "$",
            },
            original_filename="20260311-sample-street-cafe.png",
            file_hash="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2",
            raw_json="{}",
        )
        assert doc.currency is None
        assert doc.currency_source == ASSUMED


class TestTheCurrencyMarkerDecides:
    """The six cases the marker exists for.

    The model is asked to *quote* the currency text printed next to the total,
    not to decide what it means. Quoting is a transcription and deciding is an
    opinion, and only the transcription earns ``stated``.
    """

    def test_the_model_says_usd_the_page_says_only_a_dollar_sign(self):
        """The scenario in the module docstring, with the guess still in the
        model's head — and a 12% tax line that overrules it."""
        assert currency_from_extraction({
            "vendor": "Bluepeak Software Inc", "currency": "USD",
            "currency_marker": "$",
            "subtotal": "25.00", "other_tax": "3.00", "total": "28.00",
        }) == ("CAD", INFERRED)

    def test_a_genuinely_quoted_us_dollar_stays_usd(self):
        """The other direction: a stated USD invoice must still fail a CAD bank
        line — even though 60 on 1200 is 5%, which on its own would read as
        GST."""
        assert currency_from_extraction({
            "currency": "USD", "currency_marker": "US$",
            "subtotal": "1200", "gst": "60", "total": "1260",
        }) == ("USD", STATED)

    def test_a_quoted_canadian_dollar_is_stated_cad(self):
        assert currency_from_extraction(
            {"currency": "CAD", "currency_marker": "CA$"}
        ) == ("CAD", STATED)

    def test_a_bare_dollar_with_no_tax_line_states_nothing(self):
        """Nobody wrote a currency down, so the engine does not write one down.

        Keeping the model's USD as *inferred* rather than dropping it would
        score the amount zero against a CAD bank line exactly as a stated USD
        would — the same wrong answer, one provenance lower. ``assumed`` is what
        lets `compare_amounts` read the receipt in the transaction's own
        currency and say so.
        """
        assert currency_from_extraction(
            {"vendor": "Sample Street Cafe", "currency": "USD",
             "currency_marker": "$", "total": "41.25"}
        ) == (None, ASSUMED)

    def test_a_yen_sign_is_cny(self):
        assert currency_from_extraction(
            {"currency_marker": "¥", "total": "1200"}
        ) == ("CNY", STATED)

    def test_an_empty_marker_and_no_code_falls_to_the_tax_line(self):
        """13% on the subtotal is Ontario HST and nothing else."""
        assert currency_from_extraction({
            "currency_marker": "", "subtotal": "300.00",
            "other_tax": "39.00", "total": "339.00",
        }) == ("CAD", INFERRED)

    def test_the_marker_outranks_a_disagreeing_model_reading(self):
        assert currency_from_extraction(
            {"currency": "USD", "currency_marker": "C$", "total": "56.00"}
        ) == ("CAD", STATED)

    def test_an_unparsable_marker_leaves_the_model_its_reading(self):
        """"kr" is not a dollar ambiguity — whatever misled the model, it was
        not the question of whose dollar this is, so its reading stands."""
        assert currency_from_extraction(
            {"currency": "SEK", "currency_marker": "kr"}
        ) == ("SEK", STATED)

    def test_an_unrecognised_qualifier_on_a_dollar_stays_ambiguous(self):
        """A model that quotes "Total $" has not said whose dollar this is.

        Reading unknown letters beside a "$" as "so it is not the bare-dollar
        case" would hand the model's guess back its authority through the one
        gap the marker exists to close.
        """
        assert currency_from_extraction(
            {"currency": "USD", "currency_marker": "Total $", "total": "41.25"}
        ) == (None, ASSUMED)

    def test_the_document_it_builds_carries_the_markers_verdict(self):
        from core.extraction import build_document_from_extraction

        doc = build_document_from_extraction(
            extraction_data={
                "vendor": "Bluepeak Software Inc", "date": "2026-03-06",
                "currency": "USD", "currency_marker": "$", "subtotal": "25.00",
                "other_tax": "3.00", "total": "28.00",
            },
            original_filename="20260306-bluepeak-subscription.pdf",
            file_hash="ccccccccccccccccccccccccccccccc3",
            raw_json="{}",
        )
        assert (doc.currency, doc.currency_source) == ("CAD", INFERRED)


class TestCurrencyFromMarker:
    def test_the_markers_that_name_exactly_one_currency(self):
        cases = {
            "US$": "USD", "U.S.$": "USD", "USD": "USD", "usd": "USD",
            "C$": "CAD", "CA$": "CAD", "CAD": "CAD", "CDN$": "CAD",
            "A$": "AUD", "NZ$": "NZD", "HK$": "HKD",
            "€": "EUR", "£": "GBP", "¥": "CNY", "RMB": "CNY", "元": "CNY",
            "US dollars": "USD", "Canadian dollars": "CAD",
            "$ USD": "USD", "CAD $": "CAD",
        }
        for marker, expected in cases.items():
            assert currency_from_marker(marker) == expected, marker

    def test_the_markers_that_name_none(self):
        for marker in ("$", "$$", " $ ", "dollars", "", None, "n/a", "  "):
            assert currency_from_marker(marker) is None, marker

    def test_a_three_letter_word_that_is_not_a_currency_is_not_read_as_one(self):
        assert currency_from_marker("TAX") is None
        assert currency_from_marker("NET") is None

    def test_it_never_raises(self):
        for junk in (object(), b"bytes", 12, ["$"], {"a": 1}):
            assert classify_currency_marker(junk)[1] in (
                "named", "dollar", "absent", "unreadable"
            )

    def test_the_kinds(self):
        assert classify_currency_marker("US$") == ("USD", "named")
        assert classify_currency_marker("$") == (None, "dollar")
        assert classify_currency_marker("") == (None, "absent")
        assert classify_currency_marker(None) == (None, "absent")
        assert classify_currency_marker("R$") == (None, "dollar")
        assert classify_currency_marker("kr") == (None, "unreadable")


class TestMarkerFromText:
    """A PDF's own text layer is a better witness than a transcription of it."""

    def test_a_code_printed_beside_the_total_is_evidence(self):
        text = "Invoice EX-0044\nSubtotal  1,200.00\nTotal  USD 1,260.00\n"
        assert marker_from_text(text) == "USD"

    def test_a_trailing_code_counts_too(self):
        assert marker_from_text("Amount due: 1,260.00 CAD") == "CAD"

    def test_a_bare_dollar_sign_is_never_evidence(self):
        assert marker_from_text("Total  $28.00\nTax $3.00\n") is None

    def test_a_code_with_no_figure_beside_it_is_not_evidence(self):
        """The 'USD' in a footer about wire fees says nothing about this bill."""
        assert marker_from_text(
            "Wire transfers are settled in USD by the issuing bank.\nTotal $480.00"
        ) is None

    def test_two_currencies_in_one_document_state_nothing(self):
        assert marker_from_text(
            "Subtotal USD 1,200.00\nConverted at 1.38: CAD 1,656.00"
        ) is None

    def test_it_never_raises(self):
        assert marker_from_text(None) is None
        assert marker_from_text(object()) is None


class TestTheExtractionPromptStopsGuessing:
    def test_it_forbids_inferring_a_currency_from_an_address(self):
        from core.extraction import build_extraction_prompt

        prompt = build_extraction_prompt()
        assert "OMIT this key" in prompt
        assert "Do not infer the currency from an address" in prompt
        # No instruction lets a jurisdiction guess stand in for a printed code.
        assert "For $ in Canadian context" not in prompt

    def test_it_asks_for_the_marker_to_be_copied_exactly(self):
        from core.extraction import build_extraction_prompt

        prompt = build_extraction_prompt()
        assert '"currency_marker"' in prompt
        assert "QUOTED EXACTLY" in prompt
        assert "Copy, do not decide" in prompt
        # Quoted before it is read: the schema order is the order of work.
        assert prompt.index('"currency_marker"') < prompt.index('"currency"')

    def test_a_page_batch_is_asked_for_the_marker_too(self):
        """A long invoice is read a few pages at a time; the marker rides along.

        `_RECEIPT_IDENTITY_FIELDS` carries it across the merge, so the pages
        that print the grand total are also the pages that can state its
        currency.
        """
        from core.extraction import (
            _BATCH_NON_CONTENT_KEYS,
            _RECEIPT_IDENTITY_FIELDS,
            build_extraction_prompt,
        )

        assert '"currency_marker"' in build_extraction_prompt(
            batch=(1, 4), pages_total=8
        )
        assert "currency_marker" in _RECEIPT_IDENTITY_FIELDS
        assert "currency_marker" in _BATCH_NON_CONTENT_KEYS


class TestCurrencyFields:
    def test_it_maps_a_row_tail_onto_the_names_the_resolver_reads(self):
        fields = currency_fields(
            "USD", ("stated", "25.00", None, None, None, "3.00"),
        )
        assert fields == {
            "currency": "USD", "currency_source": "stated", "subtotal": "25.00",
            "tax_gst": None, "tax_hst": None, "tax_pst": None, "tax_other": "3.00",
        }
        # Stated beats the 12% tax line the same row carries.
        assert resolve_document_currency(fields) == ("USD", STATED)

    def test_a_short_tail_is_tolerated(self):
        assert currency_fields("CAD", ())["currency_source"] is None

"""Receipt search survives extraction misreads.

A receipt is only findable by the text the vision model read off the image, and
handwriting is exactly where a vision model slips. Take a photographed cheque
payable to JORDAN AVERY that the extractor reads as "KORDAN AVERY": the row is
in the database, status EXTRACTED, amount and date correct — but a search for
the name printed on the cheque returns nothing, so the receipt reads as
"never uploaded".

The search therefore tolerates one character of error on the free-text columns
the extractor populates, while staying tight enough not to drag in unrelated
vendors.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from db.database_pg import (
    _FUZZY_MIN_LEN,
    _build_doc_search_predicate,
    _one_edit_patterns,
)


def _like_to_regex(pattern: str) -> re.Pattern:
    """Compile a LIKE body (with '_' wildcards and backslash escapes)."""
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            out += re.escape(pattern[i + 1])
            i += 2
            continue
        out += "." if ch == "_" else re.escape(ch)
        i += 1
    return re.compile(out, re.IGNORECASE)


def _matches(term: str, haystack: str) -> bool:
    return any(_like_to_regex(p).search(haystack) for p in _one_edit_patterns(term))


class TestOneEditPatterns:

    def test_the_cheque_case(self):
        """JORDAN must reach the vendor the extractor stored, "KORDAN AVERY"."""
        assert _matches("JORDAN", "KORDAN AVERY")

    @pytest.mark.parametrize(
        "typed,stored",
        [
            ("JORDAN", "KORDAN"),        # substitution
            ("JORDAN", "JORDN"),         # stored is missing a character
            ("JORDAN", "JORDANN"),       # stored has an extra character
            ("Wholesale", "Wh0lesale"),  # digit-for-letter, a classic OCR slip
            ("Bluepeak", "BIuepeak"),    # lower-case l -> capital I
        ],
    )
    def test_each_edit_kind(self, typed, stored):
        assert _matches(typed, stored)

    def test_two_substitutions_are_out_of_budget(self):
        """"Bluepeak" -> "BIuepe4k" is two edits; one is the whole tolerance."""
        assert not _matches("Bluepeak", "BIuepe4k")

    def test_exact_still_matches(self):
        assert _matches("JORDAN", "JORDAN AVERY")

    @pytest.mark.parametrize(
        "other",
        ["Alpha Computers", "Cloud Services Inc.", "Cedarview Supplies Ltd.",
         "Coffee Shop", "Hardware Store"],
    )
    def test_unrelated_vendors_are_not_dragged_in(self, other):
        """One edit of tolerance, not a free-for-all."""
        assert not _matches("JORDAN", other)

    def test_two_errors_still_miss(self):
        """The budget is one character; two is a different word."""
        assert not _matches("JORDAN", "KORDIN")

    @pytest.mark.parametrize("short", ["Ave", "Ltd", "Co", "a"])
    def test_short_terms_get_no_fuzzy_arm(self, short):
        """A one-edit window on three characters matches nearly everything."""
        assert _one_edit_patterns(short) == []

    def test_min_length_boundary(self):
        assert _one_edit_patterns("a" * (_FUZZY_MIN_LEN - 1)) == []
        assert _one_edit_patterns("a" * _FUZZY_MIN_LEN) != []

    def test_pattern_count_is_bounded(self):
        """A long paste must not turn one keystroke into hundreds of clauses."""
        assert len(_one_edit_patterns("A" * 200)) <= 24

    def test_like_metacharacters_in_the_query_are_escaped(self):
        """A typed '%' is a literal, not a wildcard that matches everything."""
        patterns = _one_edit_patterns("100%_off")
        assert patterns
        # Every raw % / _ from the input survives only in escaped form; the
        # single unescaped '_' each pattern is allowed is the edit wildcard.
        for p in patterns:
            unescaped = re.sub(r"\\.", "", p)
            assert "%" not in unescaped
            assert unescaped.count("_") <= 1


class TestDocSearchPredicate:

    def test_fuzzy_arm_is_wired_into_the_predicate(self):
        sql, params = _build_doc_search_predicate("JORDAN", alias="d.")
        assert sql.count("ILIKE") == len(params)
        # Substring arm over the four columns, plus a fuzzy arm over the two
        # free-text ones — so strictly more than the substring arm alone.
        baseline, _ = _build_doc_search_predicate("Ave", alias="d.")
        assert sql.count("ILIKE") > baseline.count("ILIKE")

    def test_structured_columns_get_no_fuzzy_arm(self):
        """A near-miss on a currency or a date is noise, not a rescue."""
        sql, _ = _build_doc_search_predicate("JORDAN", alias="d.")
        assert sql.count("d.currency ILIKE") == 1
        assert sql.count("d.document_date::text ILIKE") == 1
        assert sql.count("d.vendor ILIKE") > 1

    def test_id_and_amount_arms_survive(self):
        sql, params = _build_doc_search_predicate("5")
        assert "id = %s" in sql
        assert 5 in params

        sql, params = _build_doc_search_predicate("2481.60")
        assert "ABS(CAST(" in sql
        # The amount arm parameterises a Decimal, not a float.
        assert any(
            isinstance(p, Decimal) and p == Decimal("2481.60") for p in params
        ), params

    def test_alias_prefixes_every_column(self):
        sql, _ = _build_doc_search_predicate("JORDAN", alias="d.")
        assert "vendor ILIKE" in sql
        assert " vendor ILIKE" not in sql.replace("d.vendor ILIKE", "")

"""The compact statement wire shape, and the budget sized to it.

SYNTHETIC DATA. The statement modelled below is the generated fixture described
in ``tests/fixtures/README.md``; every figure is read from
``scripts/gen_fixtures.py`` or written here. No real statement is represented.

The model is asked for ``{"d","desc","amt","bal"}`` with nulls omitted, and
``_parse_llm_json`` maps that back onto TransactionRow / StatementMetadata so
nothing downstream changes. The long alternative — every field on every row,
nulls included, plus a 2-5 sentence ``reasoning`` — spends output tokens on
every page for data the engine discards.

These tests hold both halves of that contract: the compact shape maps correctly
(including the sign convention, which is the ONLY record of debit vs credit),
and the long legacy shape still validates, because the correction retry and any
hosted provider can still produce it.

The compact payload is built from the fixture generator's own constants rather
than typed out, so the balance the validator is asked to land on is the balance
the statement actually adds up to — not a number chosen to make the test pass.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.llm_statement_parser as parser
from core.llm_statement_parser import (
    STATEMENT_EXPECTED_ROWS_PER_PAGE,
    STATEMENT_MIN_OUTPUT_TOKENS,
    STATEMENT_OUTPUT_BASE_TOKENS,
    STATEMENT_OUTPUT_TOKENS_PER_ROW,
    BankStatementExtraction,
    _parse_llm_json,
    statement_max_output_tokens,
    validate_statement_extraction,
)
from scripts.gen_fixtures import (
    BANK,
    STATEMENT_OPENING,
    STATEMENT_PERIOD_END,
    STATEMENT_ROWS,
    statement_running_balances,
)

# The generated statement covers one February; the generator states only its end
# date, so the start is fixed here, before the first row.
PERIOD_START = date(2026, 2, 1)
BALANCES = statement_running_balances()
CLOSING = BALANCES[-1]


def _iso(day_token: str) -> str:
    """``"Feb03"`` → ``"2026-02-03"``, year taken from the statement period."""
    return datetime.strptime(
        f"{day_token}{STATEMENT_PERIOD_END.year}", "%b%d%Y"
    ).date().isoformat()


def _expected_type(delta: Decimal) -> str:
    """What the sign of ``amt`` means, restated independently of the parser."""
    return "credit" if delta > 0 else "debit"


_COMPACT = json.dumps({
    "metadata": {
        "institution": BANK,
        "account_type": "checking",
        "statement_period_start": PERIOD_START.isoformat(),
        "statement_period_end": STATEMENT_PERIOD_END.isoformat(),
        "opening_balance": float(STATEMENT_OPENING),
        "closing_balance": float(CLOSING),
        "currency": "usd",
    },
    "transactions": [
        {"d": _iso(day), "desc": desc, "amt": float(delta), "bal": float(balance)}
        for (day, desc, delta), balance in zip(STATEMENT_ROWS, BALANCES)
    ],
})

# The long shape a correction retry or a hosted provider can still produce: three
# withdrawals off an invented opening balance, each with its running balance.
_LEGACY_OPENING = Decimal("1200.00")
_LEGACY_DEBITS = (
    ("2026-01-05", Decimal("41.25"), "SAMPLE STREET CAFE", None),
    ("2026-01-10", Decimal("56.00"), "ANYTOWN PARKING AUTHORITY", "REF-0042"),
    ("2026-01-20", Decimal("120.00"), "HARBOURVIEW PRINT SHOP", None),
)


def _legacy_rows() -> list[dict]:
    rows = []
    balance = _LEGACY_OPENING
    for i, (day, amount, description, external_id) in enumerate(_LEGACY_DEBITS):
        balance -= amount
        row = {
            "date": day,
            "amount": f"{amount:.2f}",
            "description": description,
            "type": "debit",
            "running_balance": f"{balance:.2f}",
        }
        if i == 0:
            row["balance_delta"] = f"{-amount:.2f}"
            row["external_id"] = None
        if external_id:
            row["external_id"] = external_id
        rows.append(row)
    return rows


_LEGACY_CLOSING = _LEGACY_OPENING - sum(amount for _d, amount, _desc, _x in _LEGACY_DEBITS)

_LEGACY = json.dumps({
    "metadata": {
        "institution": BANK,
        "account_type": "CHECKING",
        "account_number": "5678",
        "statement_period_start": "2026-01-01",
        "statement_period_end": "2026-01-31",
        "opening_balance": f"{_LEGACY_OPENING:.2f}",
        "closing_balance": f"{_LEGACY_CLOSING:.2f}",
        "currency": "CAD",
        "multi_account": False,
        "page_count": 2,
    },
    "transactions": _legacy_rows(),
    "reasoning": "All three rows sit in the withdrawals column.",
})


class TestCompactRowMapping:
    def test_short_keys_become_transaction_rows(self):
        extraction = BankStatementExtraction(**_parse_llm_json(_COMPACT))
        rows = extraction.transactions

        assert len(rows) == len(STATEMENT_ROWS)
        first_day, first_desc, first_delta = STATEMENT_ROWS[0]
        assert rows[0].date == _iso(first_day)
        assert rows[0].description == first_desc
        assert rows[0].amount == f"{abs(first_delta):.2f}"
        assert rows[0].running_balance == f"{BALANCES[0]:.2f}"

    def test_the_sign_of_amt_is_the_direction(self):
        rows = BankStatementExtraction(**_parse_llm_json(_COMPACT)).transactions

        assert [row.type for row in rows] == [
            _expected_type(delta) for _day, _desc, delta in STATEMENT_ROWS
        ]
        # `amount` stays absolute for everything downstream; only `type` carries
        # the direction once the row has been mapped.
        outgoing = next(i for i, r in enumerate(STATEMENT_ROWS) if r[2] < 0)
        assert rows[outgoing].amount == f"{abs(STATEMENT_ROWS[outgoing][2]):.2f}"
        assert not rows[outgoing].amount.startswith("-")

    def test_omitted_keys_become_none_not_errors(self):
        payload = json.dumps({
            "metadata": {
                "institution": "Wise",
                "account_type": "FX_MULTI_CURRENCY",
                "statement_period_start": "2026-03-01",
                "statement_period_end": "2026-03-31",
                "opening_balance": 0,
                "closing_balance": -9.75,
                "currency": "USD",
            },
            "transactions": [{"d": "2026-03-02", "desc": "CARD FEE", "amt": -9.75}],
        })
        extraction = BankStatementExtraction(**_parse_llm_json(payload))

        row = extraction.transactions[0]
        assert row.running_balance is None
        assert row.balance_delta is None
        assert row.external_id is None
        assert extraction.metadata.account_number is None
        assert extraction.metadata.multi_account is False
        assert extraction.metadata.page_count == 0, "filled in from the file, not the model"
        assert extraction.reasoning is None

    def test_numbers_are_accepted_where_strings_are_stored(self):
        meta = BankStatementExtraction(**_parse_llm_json(_COMPACT)).metadata

        assert meta.opening_balance == f"{STATEMENT_OPENING:.2f}"
        assert meta.closing_balance == f"{CLOSING:.2f}"

    def test_lowercase_enums_and_unpadded_dates_are_normalized(self):
        payload = json.dumps({
            "metadata": {
                "institution": BANK,
                "account_type": "credit_card",
                "statement_period_start": "2026-1-5",
                "statement_period_end": "2026-01-31",
                "opening_balance": 10,
                "closing_balance": 20,
                "currency": "usd",
            },
            "transactions": [{"d": "2026-1-9", "desc": "PURCHASE", "amt": -10}],
        })
        extraction = BankStatementExtraction(**_parse_llm_json(payload))

        assert extraction.metadata.account_type == "CREDIT_CARD"
        assert extraction.metadata.currency == "USD"
        assert extraction.metadata.statement_period_start == "2026-01-05"
        assert extraction.transactions[0].date == "2026-01-09"

    def test_notes_lands_in_the_reasoning_field(self):
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CHECKING",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": 10, "closing_balance": 10, "currency": "CAD",
            },
            "transactions": [],
            "notes": "The fourth row was illegible.",
        })
        extraction = BankStatementExtraction(**_parse_llm_json(payload))

        assert extraction.reasoning == "The fourth row was illegible."

    def test_an_explicit_type_wins_when_the_amount_is_unsigned(self):
        """The legacy shape puts direction in `type` and keeps amount positive."""
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CHECKING",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": 300.00, "closing_balance": 244.00, "currency": "CAD",
            },
            "transactions": [
                {"date": "2026-01-05", "amount": "56.00", "description": "MONTHLY PLAN FEE",
                 "type": "debit"},
            ],
        })
        rows = BankStatementExtraction(**_parse_llm_json(payload)).transactions

        assert rows[0].type == "debit"
        assert rows[0].amount == "56.00"

    def test_a_negative_amount_overrides_a_contradictory_type(self):
        """A minus sign is an unambiguous statement about direction; a word is not."""
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CHECKING",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": 300.00, "closing_balance": 244.00, "currency": "CAD",
            },
            "transactions": [
                {"d": "2026-01-05", "desc": "MONTHLY PLAN FEE", "amt": -56, "type": "credit"},
            ],
        })
        rows = BankStatementExtraction(**_parse_llm_json(payload)).transactions

        assert rows[0].type == "debit"

    def test_money_strings_with_symbols_and_brackets_still_parse(self):
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CHECKING",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": "$1,234.56", "closing_balance": "1,193.31", "currency": "CAD",
            },
            "transactions": [
                {"d": "2026-01-05", "desc": "SAMPLE STREET CAFE", "amt": "(41.25)",
                 "bal": "$1,193.31"},
            ],
        })
        extraction = BankStatementExtraction(**_parse_llm_json(payload))

        assert extraction.metadata.opening_balance == "1234.56"
        # Accountant's brackets are a minus sign, so the row is money out.
        assert extraction.transactions[0].type == "debit"
        assert extraction.transactions[0].amount == "41.25"
        assert extraction.transactions[0].running_balance == "1193.31"

    def test_the_legacy_long_shape_still_validates(self):
        extraction = BankStatementExtraction(**_parse_llm_json(_LEGACY))

        assert len(extraction.transactions) == len(_LEGACY_DEBITS)
        assert extraction.transactions[0].type == "debit"
        assert extraction.transactions[1].external_id == _LEGACY_DEBITS[1][3]
        assert extraction.metadata.page_count == 2
        assert extraction.reasoning.startswith("All three rows")

    def test_markdown_fences_are_still_stripped(self):
        fenced = "```json\n" + _COMPACT + "\n```"
        rows = BankStatementExtraction(**_parse_llm_json(fenced)).transactions
        assert len(rows) == len(STATEMENT_ROWS)

    def test_an_empty_row_is_dropped_rather_than_validated(self):
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CHECKING",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": 10, "closing_balance": 10, "currency": "CAD",
            },
            "transactions": [{}, {"d": "2026-01-05", "desc": "MONTHLY PLAN FEE", "amt": -0.0}],
        })
        rows = BankStatementExtraction(**_parse_llm_json(payload)).transactions
        assert len(rows) == 1


class TestValidatorUnchangedByTheTrim:
    def test_the_balance_validator_still_lands_on_zero(self):
        """The generated statement reconciles exactly, so anything else is a bug."""
        extraction = BankStatementExtraction(**_parse_llm_json(_COMPACT))
        confidence, flags, summary = validate_statement_extraction(
            extraction, "test_bank_statement.pdf"
        )

        assert summary["balance_difference"] == Decimal("0.00")
        assert summary["computed_closing"] == CLOSING
        assert flags == []
        assert confidence == "HIGH"

    def test_a_credit_card_statement_still_inverts(self):
        """A card balance is money owed: purchases raise it, payments lower it."""
        payload = json.dumps({
            "metadata": {
                "institution": BANK, "account_type": "CREDIT_CARD",
                "statement_period_start": "2026-01-01", "statement_period_end": "2026-01-31",
                "opening_balance": 480.00, "closing_balance": 660.00, "currency": "CAD",
            },
            "transactions": [
                {"d": "2026-01-05", "desc": "EXAMPLE RETAILER MARKETPLACE", "amt": -300.00},
                {"d": "2026-01-20", "desc": "PAYMENT RECEIVED", "amt": 120.00},
            ],
        })
        extraction = BankStatementExtraction(**_parse_llm_json(payload))
        _confidence, _flags, summary = validate_statement_extraction(extraction, "card.pdf")

        assert summary["balance_difference"] == Decimal("0.00")
        # ...and the deposit-account equation would have been out by twice the
        # payment total, which is the failure this inversion exists to prevent.
        deposit_equation = (
            summary["opening"] + summary["sum_credits"] - summary["sum_debits"]
        )
        assert deposit_equation != summary["closing"]


class TestPromptShape:
    def test_the_prompt_carries_the_key_legend_once(self):
        prompt = parser.BANK_STATEMENT_SYSTEM_PROMPT

        for key in ('"d"', '"desc"', '"amt"', '"bal"'):
            assert key in prompt
        assert "OMIT any key you cannot fill" in prompt
        assert "SIGNED amount" in prompt
        # page_count is a fact about the file, not a question for the model.
        assert "page_count" not in prompt

    def test_the_prompt_still_carries_every_accuracy_rule(self):
        prompt = parser.BANK_STATEMENT_SYSTEM_PROMPT

        for rule in (
            "DATE NORMALIZATION",
            "SKIP-PHRASE FILTER",
            "RUNNING BALANCE DELTA CROSS-CHECK",
            "MULTI-PAGE HANDLING",
            "Carried Forward",
            "CREDIT_CARD accounts",
        ):
            assert rule in prompt

    def test_the_schema_correction_message_describes_the_compact_shape(self):
        assert '"amt"' in parser._SCHEMA_CORRECTION_MESSAGE
        assert "compact keys" in parser._SCHEMA_CORRECTION_MESSAGE


class TestOutputBudget:
    def test_the_budget_scales_with_expected_rows(self):
        assert statement_max_output_tokens(2) == (
            STATEMENT_OUTPUT_BASE_TOKENS
            + STATEMENT_OUTPUT_TOKENS_PER_ROW * STATEMENT_EXPECTED_ROWS_PER_PAGE * 2
        )
        assert statement_max_output_tokens(6) > statement_max_output_tokens(2)

    def test_the_budget_never_drops_below_the_floor(self):
        assert statement_max_output_tokens(0) >= STATEMENT_MIN_OUTPUT_TOKENS
        assert statement_max_output_tokens(1) >= STATEMENT_MIN_OUTPUT_TOKENS
        assert STATEMENT_MIN_OUTPUT_TOKENS == 1024

    def test_a_two_page_statement_gets_room_for_a_dense_statement(self):
        # The budget has to cover the worst realistic case — 25 rows a page —
        # not the four rows the generated fixture happens to carry.
        assert statement_max_output_tokens(2) >= (
            STATEMENT_OUTPUT_TOKENS_PER_ROW * STATEMENT_EXPECTED_ROWS_PER_PAGE * 2
        )


@pytest.mark.asyncio
class TestStatementCallBudgetAndProgress:
    """The budget and the progress callback reach the wire, on the real code path."""

    @pytest.fixture()
    def local_enabled(self, monkeypatch):
        from dataclasses import replace

        import config.settings as settings

        monkeypatch.setattr(
            settings, "local_llm_config",
            replace(settings.local_llm_config, base_url="http://test-local:8000/v1",
                    model="primary"),
        )
        monkeypatch.setattr(
            settings, "gemini_config", replace(settings.gemini_config, api_key="", enabled=False)
        )
        monkeypatch.setattr(
            settings, "openai_config", replace(settings.openai_config, api_key="", enabled=False)
        )

    def _client(self, contents, finish_reasons=None):
        responses = []
        for i, content in enumerate(contents):
            response = MagicMock()
            reason = (finish_reasons or ["stop"] * len(contents))[i]
            response.choices = [
                MagicMock(message=MagicMock(content=content), finish_reason=reason)
            ]
            response.usage = MagicMock(prompt_tokens=3000, completion_tokens=400)
            responses.append(response)
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=responses)
        return client

    async def test_the_text_mode_call_carries_the_page_sized_budget(self, local_enabled):
        from core.extraction import _PageProgress

        client = self._client([_COMPACT])
        seen: list[tuple[int, int]] = []
        extraction, _ti, _to, _cost = await parser._extract_statement_with_local(
            "statement text", None, parser.BANK_STATEMENT_SYSTEM_PROMPT, None,
            client=client,
            progress=_PageProgress(lambda done, total: seen.append((done, total)), 2),
            pages_total=2,
        )

        assert len(extraction.transactions) == len(STATEMENT_ROWS)
        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["max_tokens"] == statement_max_output_tokens(2)
        assert kwargs["temperature"] == 0
        assert kwargs["response_format"] == {"type": "json_object"}
        assert seen == [(2, 2)]

    async def test_a_truncated_statement_is_re_asked_at_double_the_budget(self, local_enabled):
        truncated = '{"metadata": {"institution": "BMO Business Banking", "account_ty'
        client = self._client([truncated, _COMPACT], finish_reasons=["length", "stop"])

        extraction, _ti, _to, _cost = await parser._extract_statement_with_local(
            "statement text", None, parser.BANK_STATEMENT_SYSTEM_PROMPT, None, client=client,
            pages_total=2,
        )

        budgets = [
            call.kwargs["max_tokens"]
            for call in client.chat.completions.create.await_args_list
        ]
        assert budgets == [
            statement_max_output_tokens(2), statement_max_output_tokens(2) * 2
        ]
        assert len(extraction.transactions) == len(STATEMENT_ROWS)

    async def test_the_page_count_comes_from_the_file_not_the_model(
        self, tmp_path, monkeypatch, local_enabled
    ):
        import fitz

        pdf = tmp_path / "statement.pdf"
        doc = fitz.open()
        for _ in range(3):
            doc.new_page()
        doc.save(str(pdf))
        doc.close()

        monkeypatch.setattr(parser, "_pdfplumber_text_density", lambda p: 1.0)
        monkeypatch.setattr(parser, "_extract_pdf_text", lambda p: "statement text")

        async def _local(text_content, images, prompt, db, **_kw):
            extraction = BankStatementExtraction(**_parse_llm_json(_COMPACT))
            return extraction, 100, 20, 0.0

        monkeypatch.setattr(parser, "_extract_statement_with_local", _local)

        seen: list[tuple[int, int]] = []
        result = await parser.parse_statement_with_llm(
            pdf, "user-1", "statement.pdf",
            progress_cb=lambda done, total: seen.append((done, total)),
        )

        # The model's payload never mentions a page count; the file has three.
        assert result.metadata.page_count == 3
        assert seen[0] == (0, 3), "the mode is announced before any page is read"
        assert result.balance_difference == "0.00"
        assert result.confidence == "HIGH"

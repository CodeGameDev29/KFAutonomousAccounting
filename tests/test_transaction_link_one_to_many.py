"""One-to-many transaction linking.

At most ONE active link per transaction, as source or target, is the wrong
constraint for a common shape: a bulk order is charged once and refunded item
by item, so a single -$480.00 charge faces three separate credits. Under 1:1 the
user can link exactly one of them and the rest stay UNMATCHED forever,
permanently demanding receipts that do not exist.

These tests pin the pieces that make the fan-out possible and — just as
importantly — the guards that keep it honest: pair-level dedup, no self-links,
non-refund types staying strictly 1:1, and refund legs never adding up to more
than the charge they offset.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.llm_transaction_linker import LINK_THRESHOLD, _greedy_assign
from core.transaction_linker import (
    FAN_OUT_LINK_TYPES,
    TransactionLinkCandidate,
    TransactionLinkerResult,
    _build_chains,
    run_transaction_linking,
)
from models.transaction import AccountType, Transaction, TransactionStatus
from server.api.link_view import build_counterpart_index, linked_summaries
from server.api.transactions import MAX_LINK_GROUP_SIZE, CreateLinkRequest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_txn(
    txn_id: int,
    desc: str,
    amount: Decimal,
    txn_type: str = "DEBIT",
    category: str | None = None,
    acct: AccountType = AccountType.CAD,
    dt: date = date(2026, 3, 2),
    status: TransactionStatus = TransactionStatus.UNMATCHED,
) -> Transaction:
    txn = Transaction(
        account=acct,
        transaction_type=txn_type,
        date_posted=dt,
        amount=amount,
        currency="CAD",
        description=desc,
        source_file="bank.csv",
        source_row=txn_id,
        status=status,
        category=category,
    )
    txn.id = txn_id
    return txn


def _llm_result(src: int, tgt: int, factor: float, link_type: str) -> dict:
    return {
        "pair_id": f"p_{src}_{tgt}",
        "source_id": src,
        "target_id": tgt,
        "linkage_factor": factor,
        "link_type": link_type,
        "reasoning": "",
    }


# ═══════════════════════════════════════════════════════════════════════
# Schema — no 1:1 indexes, and the guards that must survive
# ═══════════════════════════════════════════════════════════════════════

class TestSchemaAllowsFanOut:

    def test_unique_1to1_indexes_absent_from_schema(self):
        """A fresh database must not be born with the 1:1 constraint."""
        schema = (REPO_ROOT / "db" / "schema_pg.sql").read_text(encoding="utf-8")
        assert "idx_transaction_links_source_unique" not in schema
        assert "idx_transaction_links_target_unique" not in schema

    def test_pair_dedup_and_self_link_guards_survive(self):
        """1:N is many distinct pairs — never a repeated one, never a self-link."""
        schema = (REPO_ROOT / "db" / "schema_pg.sql").read_text(encoding="utf-8")
        assert "idx_transaction_links_dedup" in schema
        assert "chk_no_self_link" in schema
        stmt = schema[schema.index("idx_transaction_links_dedup"):]
        stmt = stmt[:stmt.index(";")]
        assert "LEAST" in stmt and "GREATEST" in stmt

    def test_cycle_trigger_carries_a_visited_set(self):
        """Without it, an anchor with N legs makes the walk branch ~N^19 times.

        The walk is undirected: it steps anchor -> leg -> anchor and
        re-branches on every return. One link per transaction bounds that at a
        row or two; a fan-out turns it into an exponential blowup that hangs
        the INSERT.
        """
        sql = (REPO_ROOT / "db" / "schema_pg.sql").read_text(encoding="utf-8")
        fn = sql[sql.index("fn_prevent_link_cycle"):]
        fn = fn[:fn.index("$$ LANGUAGE plpgsql;")]
        assert "visited" in fn
        assert "= ANY(lw.visited)" in fn


# ═══════════════════════════════════════════════════════════════════════
# LLM linker greedy assignment
# ═══════════════════════════════════════════════════════════════════════

class TestGreedyFanOut:

    def test_refund_anchor_keeps_every_leg(self):
        """One charge, three credits — all three survive.

        The model returns p_1_2, p_1_3 and p_1_4 above threshold; strict 1:1
        would keep the first and discard the other two silently.
        """
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, 0.85, "REFUND"),
            _llm_result(1, 4, 0.80, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 3
        assert {link.target_transaction_id for link in links} == {2, 3, 4}
        assert {link.source_transaction_id for link in links} == {1}

    def test_fan_out_legs_share_one_chain_id(self):
        """The group must be readable back as a group."""
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, 0.85, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        chain_ids = {link.chain_id for link in links}
        assert len(chain_ids) == 1
        assert chain_ids != {None}
        assert sorted(link.chain_position for link in links) == [0, 1]

    def test_two_separate_refund_anchors_get_separate_chains(self):
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(3, 4, 0.85, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len({link.chain_id for link in links}) == 2

    def test_transfers_stay_one_to_one(self):
        """Only refunds fan out. One credit-card payment settles one balance.

        A CC payment linked to two different statement charges would be a
        reconciliation error, so the 1:1 discipline has to survive for
        everything outside FAN_OUT_LINK_TYPES.
        """
        for link_type in ["CREDIT_CARD_PAYMENT", "INTERNAL_TRANSFER",
                          "FX_CONVERSION", "WISE_TRANSFER", "OTHER"]:
            results = [
                _llm_result(1, 2, 0.90, link_type),
                _llm_result(1, 3, 0.85, link_type),
            ]
            links = _greedy_assign(results, LINK_THRESHOLD)
            assert len(links) == 1, link_type
            assert links[0].target_transaction_id == 2, link_type
            assert links[0].chain_id is None, link_type

    def test_a_consumed_target_never_becomes_an_anchor(self):
        """Once 2 is a leg of 1's group it cannot start a group of its own."""
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(2, 3, 0.85, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1
        assert (links[0].source_transaction_id, links[0].target_transaction_id) == (1, 2)

    def test_an_anchor_never_becomes_someone_elses_target(self):
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(3, 1, 0.85, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1
        assert links[0].source_transaction_id == 1

    def test_anchor_cannot_mix_link_types(self):
        """A group is one kind of relationship, not a grab bag."""
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, 0.85, "OTHER"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1
        assert links[0].link_type == "REFUND"

    def test_below_threshold_legs_still_rejected(self):
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, LINK_THRESHOLD - 0.01, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1


# ═══════════════════════════════════════════════════════════════════════
# Deterministic linker — eligibility and chain identity
# ═══════════════════════════════════════════════════════════════════════

class TestDeterministicLinkerEligibility:

    def test_refund_anchor_stays_eligible_for_more_legs(self):
        """A charge that already carries one refund leg can gain another.

        It is LINKED precisely because a leg exists; reading that status as
        "done" drops it, and refunds 2..N become unreachable from automation.
        """
        anchor = _make_txn(1, "EXAMPLE RETAILER MKTP CA", Decimal("480.00"),
                           category="Credit card payment",
                           status=TransactionStatus.LINKED)
        result = run_transaction_linking(
            transactions=[anchor],
            existing_links=[{
                "source_transaction_id": 1,
                "target_transaction_id": 2,
                "link_type": "REFUND",
            }],
        )
        # Evaluated (and left unlinked for want of a partner) rather than filtered out.
        assert 1 in result.unlinked_transfers

    def test_a_transactions_target_is_never_re_offered(self):
        leg = _make_txn(2, "EXAMPLE RETAILER MKTP CA refund", Decimal("120.00"),
                        txn_type="CREDIT", category="Credit card payment",
                        status=TransactionStatus.LINKED)
        result = run_transaction_linking(
            transactions=[leg],
            existing_links=[{
                "source_transaction_id": 1,
                "target_transaction_id": 2,
                "link_type": "REFUND",
            }],
        )
        assert 2 not in result.unlinked_transfers

    def test_non_refund_source_is_still_spoken_for(self):
        """A completed credit-card payment is done; it takes no second leg."""
        anchor = _make_txn(1, "PAYMENT THANK YOU", Decimal("1200.00"),
                           category="Credit card payment",
                           status=TransactionStatus.LINKED)
        result = run_transaction_linking(
            transactions=[anchor],
            existing_links=[{
                "source_transaction_id": 1,
                "target_transaction_id": 2,
                "link_type": "CREDIT_CARD_PAYMENT",
            }],
        )
        assert 1 not in result.unlinked_transfers

    def test_refund_is_the_only_fan_out_type(self):
        assert FAN_OUT_LINK_TYPES == frozenset({"REFUND"})


class TestChainIdentityPreserved:

    def test_build_chains_keeps_an_existing_fan_out_chain_id(self):
        """A group that already minted a chain_id must not be renumbered."""
        shared = "11111111-1111-1111-1111-111111111111"
        result = TransactionLinkerResult(links=[
            TransactionLinkCandidate(1, 2, "REFUND", 0.9, 0, 0, 0, 0, "",
                                     chain_id=shared, chain_position=0),
            TransactionLinkCandidate(1, 3, "REFUND", 0.8, 0, 0, 0, 0, "",
                                     chain_id=shared, chain_position=1),
        ])
        _build_chains(result)
        assert {link.chain_id for link in result.links} == {shared}
        assert [link.chain_position for link in result.links] == [0, 1]

    def test_build_chains_still_labels_an_unlabelled_component(self):
        result = TransactionLinkerResult(links=[
            TransactionLinkCandidate(1, 2, "INTERNAL_TRANSFER", 0.9, 0, 0, 0, 0, ""),
            TransactionLinkCandidate(2, 3, "INTERNAL_TRANSFER", 0.8, 0, 0, 0, 0, ""),
        ])
        _build_chains(result)
        chain_ids = {link.chain_id for link in result.links}
        assert len(chain_ids) == 1 and None not in chain_ids


# ═══════════════════════════════════════════════════════════════════════
# API request contract
# ═══════════════════════════════════════════════════════════════════════

class TestCreateLinkRequest:

    def test_plural_targets_accepted(self):
        body = CreateLinkRequest(
            source_transaction_id=1,
            target_transaction_ids=[2, 3, 4],
            link_type="REFUND",
        )
        assert body.resolved_targets() == [2, 3, 4]

    def test_singular_target_still_accepted(self):
        """Callers that send the singular field keep working."""
        body = CreateLinkRequest(
            source_transaction_id=1, target_transaction_id=2, link_type="REFUND",
        )
        assert body.resolved_targets() == [2]

    def test_duplicate_targets_collapse(self):
        body = CreateLinkRequest(
            source_transaction_id=1,
            target_transaction_ids=[2, 3, 2],
            target_transaction_id=3,
            link_type="REFUND",
        )
        assert body.resolved_targets() == [2, 3]

    def test_no_targets_is_empty(self):
        body = CreateLinkRequest(source_transaction_id=1, link_type="REFUND")
        assert body.resolved_targets() == []

    def test_group_size_is_bounded(self):
        assert 1 < MAX_LINK_GROUP_SIZE <= 50


# ═══════════════════════════════════════════════════════════════════════
# Report payloads — the accountant must see the whole group
# ═══════════════════════════════════════════════════════════════════════

class TestReportCounterparts:

    def _batch(self):
        charge = _make_txn(1, "EXAMPLE RETAILER MKTP CA", Decimal("480.00"))
        legs = [
            _make_txn(2, "EXAMPLE RETAILER refund 1", Decimal("120.00"), txn_type="CREDIT"),
            _make_txn(3, "EXAMPLE RETAILER refund 2", Decimal("60.00"), txn_type="CREDIT"),
            _make_txn(4, "EXAMPLE RETAILER refund 3", Decimal("90.00"), txn_type="CREDIT"),
        ]
        txns = [charge, *legs]
        links = [
            {"id": 10, "source_transaction_id": 1, "target_transaction_id": 2, "link_type": "REFUND"},
            {"id": 11, "source_transaction_id": 1, "target_transaction_id": 3, "link_type": "REFUND"},
            {"id": 12, "source_transaction_id": 1, "target_transaction_id": 4, "link_type": "REFUND"},
        ]
        return {t.id: t for t in txns}, links

    def test_hub_lists_every_leg(self):
        """A scalar counterpart map would keep only the last leg the query yielded."""
        lookup, links = self._batch()
        index = build_counterpart_index(links, lookup)
        summaries = linked_summaries(1, index, lookup)
        assert [s["id"] for s in summaries] == [2, 3, 4]

    def test_each_leg_points_back_at_the_hub(self):
        lookup, links = self._batch()
        index = build_counterpart_index(links, lookup)
        for leg_id in (2, 3, 4):
            assert [s["id"] for s in linked_summaries(leg_id, index, lookup)] == [1]

    def test_ordering_is_deterministic(self):
        """Same group, same order, every render — index by link id."""
        lookup, links = self._batch()
        shuffled = [links[2], links[0], links[1]]
        assert (
            [s["id"] for s in linked_summaries(1, build_counterpart_index(shuffled, lookup), lookup)]
            == [s["id"] for s in linked_summaries(1, build_counterpart_index(links, lookup), lookup)]
        )

    def test_counterparts_outside_the_batch_are_skipped(self):
        """A month report can only anchor rows it actually holds."""
        lookup, links = self._batch()
        lookup.pop(4)
        index = build_counterpart_index(links, lookup)
        assert [s["id"] for s in linked_summaries(1, index, lookup)] == [2, 3]


# ═══════════════════════════════════════════════════════════════════════
# The guards the fan-out itself made necessary
# ═══════════════════════════════════════════════════════════════════════

class TestFanOutAmountCap:
    """Refund legs may not add up to more than the charge they offset.

    With no unique index left to act as a database backstop, this cap is the
    only thing standing between the linkers and a group that claims $850 of
    credits refunded a $480.00 charge.
    """

    def _txns(self, anchor_amount, leg_amounts, currency="CAD", leg_currency=None):
        anchor = _make_txn(1, "EXAMPLE RETAILER MKTP CA", Decimal(anchor_amount))
        anchor.currency = currency
        legs = []
        for i, amt in enumerate(leg_amounts, start=2):
            leg = _make_txn(i, f"EXAMPLE RETAILER refund {i}", Decimal(amt), txn_type="CREDIT")
            leg.currency = leg_currency or currency
            legs.append(leg)
        return {t.id: t for t in [anchor, *legs]}

    def test_llm_linker_rejects_legs_exceeding_the_anchor(self):
        """The LLM reasons pair by pair and never sees the running total."""
        txn_by_id = self._txns("480.00", ["400.00", "450.00"])
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, 0.85, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD, txn_by_id=txn_by_id)
        assert len(links) == 1
        assert links[0].target_transaction_id == 2

    def test_llm_linker_accepts_legs_that_fit(self):
        txn_by_id = self._txns("480.00", ["120.00", "60.00", "90.00"])
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),
            _llm_result(1, 3, 0.85, "REFUND"),
            _llm_result(1, 4, 0.80, "REFUND"),
        ]
        links = _greedy_assign(results, LINK_THRESHOLD, txn_by_id=txn_by_id)
        assert len(links) == 3

    def test_a_rejected_leg_does_not_consume_the_anchor_budget(self):
        """An over-budget leg must not poison the legs considered after it."""
        txn_by_id = self._txns("500.00", ["450.00", "400.00", "40.00"])
        results = [
            _llm_result(1, 2, 0.90, "REFUND"),   # 450 — fits
            _llm_result(1, 3, 0.85, "REFUND"),   # +400 = 850 — rejected
            _llm_result(1, 4, 0.80, "REFUND"),   # +40 = 490 — must still fit
        ]
        links = _greedy_assign(results, LINK_THRESHOLD, txn_by_id=txn_by_id)
        assert {link.target_transaction_id for link in links} == {2, 4}

    def test_cross_currency_legs_are_not_compared_by_raw_magnitude(self):
        """US$220 is not "more than" C$300; REFUND pairs are cross-currency by design.

        VALID_ACCOUNT_PAIRS["REFUND"] is entirely CreditCard/Wise <-> CAD/USD,
        so comparing bare Decimals would reject perfectly good refunds.
        """
        anchor = _make_txn(1, "EXAMPLE RETAILER REFUND", Decimal("300.00"))
        anchor.currency = "CAD"
        leg = _make_txn(2, "EXAMPLE RETAILER REFUND", Decimal("220.00"), txn_type="CREDIT")
        leg.currency = "USD"
        links = _greedy_assign(
            [_llm_result(1, 2, 0.90, "REFUND")],
            LINK_THRESHOLD,
            txn_by_id={1: anchor, 2: leg},
        )
        assert len(links) == 1

    def test_deterministic_linker_shares_the_currency_rule(self):
        from core.transaction_linker import FAN_OUT_TOLERANCE
        assert FAN_OUT_TOLERANCE == Decimal("0.01")
        source = (
            Path(__file__).resolve().parent.parent / "core" / "transaction_linker.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("fans_out = candidate.link_type"):]
        block = block[:block.index("result.links.append")]
        assert "leg_txn.currency == anchor_txn.currency" in block


class TestOrphanedStatusOrdering:
    """Delete the links first, THEN decide who is orphaned.

    Judging a partner while its sibling links are still on the table is only
    safe at 1:1. With a fan-out, an anchor whose two legs both live in the
    statement being deleted sees a surviving sibling on each pass, stays
    LINKED, and then loses both links — left LINKED with nothing linked to it,
    so it never goes back to asking for a receipt.
    """

    @pytest.mark.parametrize(
        "path,marker,revert",
        [
            # This one delegates the re-derivation to the shared helper.
            ("db/database_pg.py", "def delete_transactions_by_source_file",
             "_revert_orphaned_link_status"),
            ("server/api/transactions.py", "async def delete_source_file",
             "SET status = 'UNMATCHED'"),
        ],
    )
    def test_links_are_deleted_before_statuses_are_re_derived(self, path, marker, revert):
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        body = source[source.index(marker):]
        body = body[:body.index("DELETE FROM transactions")]

        delete_at = body.index("DELETE FROM transaction_links")
        revert_at = body.index(revert)
        assert delete_at < revert_at, (
            f"{path}: partner statuses are re-derived before the links are gone"
        )

    def test_no_self_excluding_sibling_count_remains(self):
        """`AND id != %s` counts siblings that are about to be deleted."""
        for path in ("db/database_pg.py", "server/api/transactions.py"):
            source = (REPO_ROOT / path).read_text(encoding="utf-8")
            assert "AND id != %s AND status != 'USER_REJECTED'" not in source, path


class TestSummaryIsAnchorSided:
    """Only the anchor nets its legs.

    Read from a leg, the arithmetic inverts: the +$120.00 credit of a -$480.00
    group would report "$480.00 of $120.00 offset — $0.00 net", i.e. 409%
    covered and fully settled.
    """

    def test_summary_filters_to_links_this_transaction_anchors(self):
        source = (REPO_ROOT / "db" / "database_pg.py").read_text(encoding="utf-8")
        body = source[source.index("def get_transaction_link_summary"):]
        body = body[:body.index("return {")]
        assert 'link.get("anchor_transaction_id") == transaction_id' in body

    def test_get_transaction_links_exposes_the_anchor(self):
        source = (REPO_ROOT / "db" / "database_pg.py").read_text(encoding="utf-8")
        body = source[source.index("def get_transaction_links"):]
        body = body[:body.index("def get_transaction_link_summary")]
        assert '"anchor_transaction_id"' in body
        assert "tl.source_transaction_id" in body


class TestLinkSearchHidesNothing:
    """A search must never answer an exact id with silence.

    Excluding already-linked counterparts is right for SUGGESTIONS — offering
    them can only produce a duplicate-pair 409. Applying it to a search makes
    typing a transaction id return an empty list, which is indistinguishable
    from a broken search: nothing on screen would say the row exists and is
    already spoken for. Search returns those rows flagged and disabled.
    """

    def _endpoint_body(self) -> str:
        source = (REPO_ROOT / "server" / "api" / "transactions.py").read_text(encoding="utf-8")
        body = source[source.index("async def available_transaction_matches"):]
        return body[:body.index("# ------------------------------------------------------------------")]

    def test_already_linked_exclusion_is_suggestion_only(self):
        body = self._endpoint_body()
        # The NOT IN exclusion must sit inside the no-search branch, before the
        # `else:` that builds the search predicate.
        exclusion_at = body.index("id NOT IN (")
        search_branch_at = body.index("db.txn_search_predicate(search)")
        assert exclusion_at < search_branch_at, (
            "the already-linked exclusion must not reach search mode"
        )

    def test_self_exclusion_is_suggestion_only(self):
        body = self._endpoint_body()
        assert body.index('conditions.append("id != %s")') < body.index(
            "db.txn_search_predicate(search)"
        )

    def test_candidates_carry_a_reason_instead_of_vanishing(self):
        body = self._endpoint_body()
        assert '"unavailable_reason": unavailable_reason' in body
        assert 'unavailable_reason = "self"' in body
        assert 'unavailable_reason = "already_linked"' in body

    def test_unavailable_rows_sort_below_selectable_ones(self):
        """Context must never outrank something the user can actually pick."""
        body = self._endpoint_body()
        sort_line = body[body.index("candidates.sort("):]
        sort_line = sort_line[:sort_line.index(")\n")]
        assert 'c["unavailable_reason"] is not None' in sort_line

"""A transaction is never MATCHED with nothing proving it.

The state to avoid is ``status='MATCHED'`` with no active match row behind it:
``/api/transactions/<id>/proofs`` answers with zero proofs and
``matched_document`` is null. That is not cosmetic. It shows the user a
reconciled row with no receipt behind it, it counts toward CRA readiness, and
because the matcher only ever considers UNMATCHED rows, the receipt that would
prove it can never be found again.

The rule these tests pin: every verb that changes a match row re-derives both
sides **in the same database transaction**, from the rows that survive —
approved match proves it, a PENDING_REVIEW match is neutral (the storage rule
already decided), an active link explains it, nothing means unmatched.

These run against a live PostgreSQL, under a synthetic user id, and are skipped
if it is not reachable. They have to: the re-derivation is SQL, and a fake
cursor would prove nothing about whether it lands.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from fastapi.testclient import TestClient


def _dsn() -> str | None:
    """The throwaway database the suite is running against.

    ``conftest.py`` has already resolved this and written it into the
    environment — a ``_test`` sibling of whatever ``DATABASE_URL`` says, created
    and bootstrapped if it did not exist. Reading ``.env`` here instead would
    seed rows into the *application's* database while the endpoints under test
    read the test one, and every assertion below would fail with "not found".
    """
    value = os.environ.get("DATABASE_URL", "")
    # A placeholder DSN like test:test@localhost points at nothing.
    if value and "test:test@localhost" not in value:
        return value
    return None


@pytest.fixture(scope="module")
def pool():
    dsn = _dsn()
    if not dsn:
        pytest.skip("No DATABASE_URL — status re-derivation needs the real database")
    try:
        import psycopg2.pool
        created = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn)
        conn = created.getconn()
        created.putconn(conn)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    yield created
    created.closeall()


@pytest.fixture()
def uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def seed(pool, uid):
    """Insert rows for one synthetic tenant, and remove them afterwards."""

    class Seeder:
        def __init__(self) -> None:
            self.user_id = uid

        def _exec(self, sql, params, fetch=False):
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone() if fetch else None
                conn.commit()
                return row
            finally:
                pool.putconn(conn)

        def txn(self, *, amount="-41.25", status="UNMATCHED",
                source_file="seed_statement.csv", description="EXAMPLE RETAILER",
                date_posted="2026-01-15", row=1) -> int:
            return self._exec(
                """INSERT INTO transactions
                   (user_id, account, transaction_type, date_posted, amount,
                    currency, description, source_file, source_row, status)
                   VALUES (%s, 'CAD', 'DEBIT', %s, %s, 'CAD', %s, %s, %s, %s)
                   RETURNING id""",
                (uid, date_posted, amount, description, source_file, row, status),
                fetch=True,
            )[0]

        def doc(self, *, status="EXTRACTED", total="41.25", vendor="Example.ca",
                filename="receipt.pdf", doc_date="2026-01-15",
                stored_path=None) -> int:
            return self._exec(
                """INSERT INTO documents
                   (user_id, original_filename, file_hash, vendor, document_date,
                    currency, total, status, stored_path)
                   VALUES (%s, %s, %s, %s, %s, 'CAD', %s, %s, %s)
                   RETURNING id""",
                (uid, filename, f"hash-{uuid.uuid4()}", vendor, doc_date,
                 total, status, stored_path),
                fetch=True,
            )[0]

        def match(self, txn_id: int, doc_id: int, *, status="PENDING_REVIEW",
                  confidence=0.96, source="AUTO", match_type="ONE_TO_ONE",
                  user_action=None) -> int:
            return self._exec(
                """INSERT INTO reconciliation_matches
                   (user_id, transaction_id, document_id, confidence_score,
                    amount_score, date_score, vendor_score, match_type, status,
                    match_source, user_action)
                   VALUES (%s, %s, %s, %s, 1.0, 1.0, 0.8, %s, %s, %s, %s)
                   RETURNING id""",
                (uid, txn_id, doc_id, confidence, match_type, status, source,
                 user_action),
                fetch=True,
            )[0]

        def link(self, src: int, tgt: int, *, status="USER_APPROVED") -> int:
            return self._exec(
                """INSERT INTO transaction_links
                   (user_id, source_transaction_id, target_transaction_id,
                    link_type, status)
                   VALUES (%s, %s, %s, 'INTERNAL_TRANSFER', %s)
                   RETURNING id""",
                (uid, src, tgt, status),
                fetch=True,
            )[0]

        def txn_status(self, txn_id: int) -> str | None:
            row = self._exec(
                "SELECT status FROM transactions WHERE id = %s AND user_id = %s",
                (txn_id, uid), fetch=True,
            )
            return row[0] if row else None

        def doc_status(self, doc_id: int) -> str | None:
            row = self._exec(
                "SELECT status FROM documents WHERE id = %s AND user_id = %s",
                (doc_id, uid), fetch=True,
            )
            return row[0] if row else None

        def match_status(self, match_id: int) -> str | None:
            row = self._exec(
                "SELECT status FROM reconciliation_matches WHERE id = %s",
                (match_id,), fetch=True,
            )
            return row[0] if row else None

    seeder = Seeder()
    yield seeder
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            for sql in (
                "DELETE FROM ledger_entries WHERE user_id = %s",
                "DELETE FROM reconciliation_matches WHERE user_id = %s",
                "DELETE FROM transaction_links WHERE user_id = %s",
                "DELETE FROM transactions WHERE user_id = %s",
                "DELETE FROM documents WHERE user_id = %s",
                "DELETE FROM statements WHERE user_id = %s",
                "DELETE FROM processed_files WHERE user_id = %s",
                "DELETE FROM audit_log WHERE user_id = %s",
            ):
                cur.execute(sql, (uid,))
        conn.commit()
    finally:
        pool.putconn(conn)


@pytest.fixture()
def client(pool, uid):
    """The real app, as the synthetic tenant, against the real database."""
    from db.database_pg import DatabasePg
    from server.app import app
    from server.auth import AuthUser, get_current_user
    from server.deps import get_db

    user = AuthUser(id=uid, email=f"{uid}@example.com", role="authenticated",
                    email_verified=True)
    db = DatabasePg(pool, uid)

    async def _user():
        return user

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        for key in (get_current_user, get_db):
            app.dependency_overrides.pop(key, None)


@pytest.fixture()
def db(pool, uid):
    from db.database_pg import DatabasePg
    return DatabasePg(pool, uid)


# ── Reject ────────────────────────────────────────────────────────────────

class TestReject:
    def test_rejecting_the_only_proof_unmatches_both_sides(self, seed, client):
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        match_id = seed.match(txn_id, doc_id)

        resp = client.patch(f"/api/reconciliation/matches/{match_id}/reject")

        assert resp.status_code == 200, resp.text
        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"

    def test_a_surviving_proof_keeps_the_transaction_matched(self, seed, client):
        txn_id = seed.txn(status="MATCHED")
        doc_a, doc_b = seed.doc(status="MATCHED"), seed.doc(status="MATCHED")
        keep = seed.match(txn_id, doc_a, status="USER_APPROVED",
                          match_type="MANY_TO_ONE")
        drop = seed.match(txn_id, doc_b, match_type="MANY_TO_ONE")

        assert client.patch(
            f"/api/reconciliation/matches/{drop}/reject"
        ).status_code == 200

        assert seed.match_status(keep) == "USER_APPROVED"
        assert seed.txn_status(txn_id) == "MATCHED", (
            "the other proof still proves it — rejecting one must not unmatch"
        )
        assert seed.doc_status(doc_a) == "MATCHED"
        assert seed.doc_status(doc_b) == "EXTRACTED"

    def test_a_split_receipt_still_proving_elsewhere_is_not_released(
        self, seed, client,
    ):
        """One receipt, two transactions (ONE_TO_MANY). Rejecting one pair must
        not hand the receipt back to the candidate pool while it still proves
        the other."""
        txn_a, txn_b = seed.txn(status="MATCHED"), seed.txn(status="MATCHED", row=2)
        doc_id = seed.doc(status="MATCHED")
        keep = seed.match(txn_a, doc_id, status="USER_APPROVED",
                          match_type="ONE_TO_MANY")
        drop = seed.match(txn_b, doc_id, match_type="ONE_TO_MANY")

        assert client.patch(
            f"/api/reconciliation/matches/{drop}/reject"
        ).status_code == 200

        assert seed.match_status(keep) == "USER_APPROVED"
        assert seed.doc_status(doc_id) == "MATCHED"
        assert seed.txn_status(txn_a) == "MATCHED"
        assert seed.txn_status(txn_b) == "UNMATCHED"

    def test_a_linked_transaction_falls_back_to_linked(self, seed, client):
        other = seed.txn(row=9, amount="41.25", description="TRANSFER IN")
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        match_id = seed.match(txn_id, doc_id)
        seed.link(txn_id, other)

        assert client.patch(
            f"/api/reconciliation/matches/{match_id}/reject"
        ).status_code == 200

        assert seed.txn_status(txn_id) == "LINKED"

    def test_rejecting_through_the_review_endpoint_also_reverts(self, seed, client):
        """`POST /api/transactions/{id}/review` goes through
        DatabasePg.update_match_status, which must not stop at the match row and
        leave the transaction MATCHED with nothing proving it."""
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        match_id = seed.match(txn_id, doc_id)

        resp = client.post(
            f"/api/transactions/{txn_id}/review", json={"action": "reject"},
        )

        assert resp.status_code == 200, resp.text
        assert seed.match_status(match_id) == "USER_REJECTED"
        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"


# ── Unmatch ───────────────────────────────────────────────────────────────

class TestUnmatch:
    def test_unmatching_an_approved_match_reverts_both_sides(self, seed, client):
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        match_id = seed.match(txn_id, doc_id, status="USER_APPROVED")

        resp = client.post(f"/api/reconciliation/matches/{match_id}/unmatch")

        assert resp.status_code == 200, resp.text
        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"

    def test_unmatching_one_of_two_proofs_keeps_the_transaction_matched(
        self, seed, client,
    ):
        txn_id = seed.txn(status="MATCHED")
        doc_a, doc_b = seed.doc(status="MATCHED"), seed.doc(status="MATCHED")
        seed.match(txn_id, doc_a, status="USER_APPROVED", match_type="MANY_TO_ONE")
        drop = seed.match(txn_id, doc_b, status="USER_APPROVED",
                          match_type="MANY_TO_ONE")

        assert client.post(
            f"/api/reconciliation/matches/{drop}/unmatch"
        ).status_code == 200

        assert seed.txn_status(txn_id) == "MATCHED"
        assert seed.doc_status(doc_b) == "EXTRACTED"

    def test_removing_the_last_proof_reverts_the_transaction(self, seed, client):
        """The proofs endpoint (DELETE /transactions/{id}/proofs/{proof_id})."""
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        proof_id = seed.match(txn_id, doc_id, status="USER_APPROVED")

        resp = client.delete(f"/api/transactions/{txn_id}/proofs/{proof_id}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["transaction_status_after"] == "UNMATCHED"
        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"


# ── Document delete ───────────────────────────────────────────────────────

class TestDocumentDelete:
    def test_deleting_the_only_receipt_unmatches_the_transaction(self, seed, client):
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        resp = client.delete(f"/api/receipts/{doc_id}")

        assert resp.status_code == 200, resp.text
        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) is None

    def test_deleting_one_of_two_receipts_keeps_the_transaction_matched(
        self, seed, client,
    ):
        txn_id = seed.txn(status="MATCHED")
        doc_a, doc_b = seed.doc(status="MATCHED"), seed.doc(status="MATCHED")
        seed.match(txn_id, doc_a, status="USER_APPROVED", match_type="MANY_TO_ONE")
        seed.match(txn_id, doc_b, status="USER_APPROVED", match_type="MANY_TO_ONE")

        assert client.delete(f"/api/receipts/{doc_b}").status_code == 200

        assert seed.txn_status(txn_id) == "MATCHED"

    def test_purge_by_hash_also_reverts(self, seed, db, pool, uid):
        """The by-hash purge writes no audit row, so a purge that reverted
        nothing would strand MATCHED transactions with no trace of why."""
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_hash FROM documents WHERE id = %s", (doc_id,),
                )
                file_hash = cur.fetchone()[0]
        finally:
            pool.putconn(conn)

        db.purge_document_by_hash(file_hash)

        assert seed.txn_status(txn_id) == "UNMATCHED"

    def test_delete_match_reverts(self, seed, db):
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        match_id = seed.match(txn_id, doc_id, status="USER_APPROVED")

        assert db.delete_match(match_id) is True

        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"


# ── Statement delete ──────────────────────────────────────────────────────

class TestStatementDelete:
    def test_deleting_a_statement_releases_its_receipts(self, seed, client):
        txn_id = seed.txn(status="MATCHED", source_file="march.csv")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        resp = client.delete("/api/transactions/source-files/march.csv")

        assert resp.status_code == 200, resp.text
        assert seed.txn_status(txn_id) is None
        assert seed.doc_status(doc_id) == "EXTRACTED"

    def test_a_split_receipt_proving_another_statement_is_kept(self, seed, client):
        kept = seed.txn(status="MATCHED", source_file="february.csv", row=1)
        gone = seed.txn(status="MATCHED", source_file="march.csv", row=2)
        doc_id = seed.doc(status="MATCHED")
        seed.match(kept, doc_id, status="USER_APPROVED", match_type="ONE_TO_MANY")
        seed.match(gone, doc_id, status="USER_APPROVED", match_type="ONE_TO_MANY")

        assert client.delete(
            "/api/transactions/source-files/march.csv"
        ).status_code == 200

        assert seed.doc_status(doc_id) == "MATCHED"
        assert seed.txn_status(kept) == "MATCHED"

    def test_a_partner_left_with_nothing_goes_back_to_unmatched(self, seed, client):
        """The surviving side of a deleted link, wrongly left MATCHED with no
        match row, is exactly the stranded state."""
        partner = seed.txn(status="MATCHED", source_file="february.csv", row=1)
        gone = seed.txn(status="LINKED", source_file="march.csv", row=2)
        seed.link(partner, gone)

        assert client.delete(
            "/api/transactions/source-files/march.csv"
        ).status_code == 200

        assert seed.txn_status(partner) == "UNMATCHED"

    def test_the_db_method_releases_receipts_the_same_way(self, seed, db):
        txn_id = seed.txn(status="MATCHED", source_file="april.csv")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        assert db.delete_transactions_by_source_file("april.csv") == 1

        assert seed.doc_status(doc_id) == "EXTRACTED"


# ── The startup repair ────────────────────────────────────────────────────

class TestStatusRepair:
    def test_it_finds_and_fixes_a_transaction_matched_by_nothing(self, seed, pool):
        from server.jobs.status_repair import repair_status_drift

        stranded = seed.txn(status="MATCHED")  # MATCHED, with nothing proving it

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(stranded) == "UNMATCHED"
        assert any(
            row["id"] == stranded and row["new_status"] == "UNMATCHED"
            for row in result["transactions_unproven"]
        ), result

    def test_a_stranded_transaction_with_a_link_becomes_linked(self, seed, pool):
        from server.jobs.status_repair import repair_status_drift

        other = seed.txn(row=8, description="TRANSFER IN")
        stranded = seed.txn(status="MATCHED", row=9)
        seed.link(stranded, other)

        repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(stranded) == "LINKED"

    def test_a_pending_review_match_is_left_alone(self, seed, pool):
        """The review queue's normal state: a pair above the confidence floor
        was stored PENDING_REVIEW and marked MATCHED. Nothing to repair."""
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="PENDING_REVIEW")

        repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(txn_id) == "MATCHED"
        assert seed.doc_status(doc_id) == "MATCHED"

    def test_a_flagged_pair_left_in_the_pool_is_left_alone(self, seed, pool):
        """A pair below the confidence floor is stored PENDING_REVIEW with the
        transaction deliberately UNMATCHED so it stays in the candidate pool.
        The repair must not 'fix' that into MATCHED."""
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="UNMATCHED")
        doc_id = seed.doc(status="EXTRACTED")
        seed.match(txn_id, doc_id, status="PENDING_REVIEW", confidence=0.54)

        repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(txn_id) == "UNMATCHED"
        assert seed.doc_status(doc_id) == "EXTRACTED"

    def test_it_releases_a_receipt_nothing_points_at(self, seed, pool):
        from server.jobs.status_repair import repair_status_drift

        doc_id = seed.doc(status="MATCHED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.doc_status(doc_id) == "EXTRACTED"
        assert any(row["id"] == doc_id for row in result["documents_released"])

    def test_it_promotes_what_the_rows_prove(self, seed, pool):
        """The inverse drift: a receipt released while an approved match still
        points at it, so the next run could hand the same receipt to a second
        transaction."""
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="UNMATCHED")
        doc_id = seed.doc(status="EXTRACTED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(txn_id) == "MATCHED"
        assert seed.doc_status(doc_id) == "MATCHED"
        assert any(row["id"] == txn_id for row in result["transactions_promoted"])
        assert any(row["id"] == doc_id for row in result["documents_promoted"])

    def test_ignored_and_excluded_are_never_overwritten(self, seed, pool):
        """IGNORED ("needs no receipt") and EXCLUDED ("isn't real") are
        statements about the row, not about matching."""
        from server.jobs.status_repair import repair_status_drift

        ignored = seed.txn(status="IGNORED", row=1)
        excluded = seed.txn(status="EXCLUDED", row=2)
        doc_id = seed.doc(status="EXTRACTED")
        seed.match(ignored, doc_id, status="USER_APPROVED")

        repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(ignored) == "IGNORED"
        assert seed.txn_status(excluded) == "EXCLUDED"

    def test_a_clean_tenant_reports_nothing(self, seed, pool):
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED")
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        repair_status_drift(pool=pool, user_id=seed.user_id)  # clean up anything pre-existing
        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert result["total"] == 0, result
        assert seed.txn_status(txn_id) == "MATCHED"


# ── A matched proof whose file is gone ────────────────────────────────────
#
# The match is real; the file is not. Demoting the transaction would throw away
# a reconciliation a human approved and would free the receipt to be matched to
# a *second* transaction on the next run — so the row stays MATCHED, the sweep
# logs it, and the audit binder's "Matched, proof file missing" list says the
# same thing about the same row. These tests pin both halves of that agreement.


@pytest.fixture()
def stored_file():
    """Write real bytes into the file store, and take them away afterwards."""
    from core.file_storage import storage_root

    written: list = []

    def _write(path: str) -> str:
        target = storage_root() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4 proof\n")
        written.append(target)
        return path

    yield _write
    for target in written:
        try:
            target.unlink(missing_ok=True)
            target.parent.rmdir()
        except OSError:
            pass


class TestMissingProofFiles:
    def test_a_matched_row_whose_proof_file_is_gone_stays_matched(self, seed, pool):
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(
            status="MATCHED",
            stored_path=f"{seed.user_id}/202601/deadbeef00000000_gone.pdf",
        )
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert seed.txn_status(txn_id) == "MATCHED", "the match is real; the file is gone"
        assert seed.doc_status(doc_id) == "MATCHED"
        reported = result["documents_file_missing"]
        assert [(r["transaction_id"], r["document_id"]) for r in reported] == [
            (txn_id, doc_id)
        ]
        assert reported[0]["stored_path"].endswith("gone.pdf")
        # Nothing was corrected, so nothing is counted as a correction.
        assert result["total"] == 0, result

    def test_it_is_logged_at_warning_rather_than_passed_over(self, seed, pool, caplog):
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(
            status="MATCHED",
            filename="orchid-telecom-feb.pdf",
            stored_path=f"{seed.user_id}/202602/cafebabe00000000_telecom-feb.pdf",
        )
        seed.match(txn_id, doc_id, status="AUTO_APPROVED")

        with caplog.at_level(logging.WARNING, logger="server.jobs.status_repair"):
            repair_status_drift(pool=pool, user_id=seed.user_id)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            f"Transaction {txn_id} is MATCHED to document {doc_id}" in m
            and "stored file is gone" in m
            and "left MATCHED" in m
            for m in messages
        ), messages

    def test_a_proof_file_that_is_on_the_disk_is_never_reported(
        self, seed, pool, stored_file
    ):
        from server.jobs.status_repair import repair_status_drift

        path = stored_file(f"{seed.user_id}/202601/0123456789abcdef_receipt.pdf")
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED", stored_path=path)
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert result["documents_file_missing"] == []
        assert seed.txn_status(txn_id) == "MATCHED"

    def test_a_windows_written_path_still_finds_its_file(
        self, seed, pool, stored_file
    ):
        """Rows written on Windows carry backslashes; the file is still there."""
        from server.jobs.status_repair import repair_status_drift

        path = stored_file(f"{seed.user_id}/202601/fedcba9876543210_receipt.pdf")
        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED", stored_path=path.replace("/", "\\"))
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert result["documents_file_missing"] == []
        assert seed.doc_status(doc_id) == "MATCHED"

    def test_a_document_with_no_stored_path_is_a_different_problem(self, seed, pool):
        """No path on record is not a missing file — the binder says so too."""
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(status="MATCHED", stored_path=None)
        seed.match(txn_id, doc_id, status="USER_APPROVED")
        blank = seed.txn(status="MATCHED", row=2)
        blank_doc = seed.doc(status="MATCHED", stored_path="   ")
        seed.match(blank, blank_doc, status="USER_APPROVED")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert result["documents_file_missing"] == []
        assert seed.txn_status(txn_id) == "MATCHED"
        assert seed.txn_status(blank) == "MATCHED"

    def test_a_pending_review_pair_is_not_reported_as_a_missing_proof(self, seed, pool):
        """Nothing approved proves that row yet — the binder calls that stale, not gone."""
        from server.jobs.status_repair import repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(
            status="MATCHED",
            stored_path=f"{seed.user_id}/202601/1111222233334444_gone.pdf",
        )
        seed.match(txn_id, doc_id, status="PENDING_REVIEW")

        result = repair_status_drift(pool=pool, user_id=seed.user_id)

        assert result["documents_file_missing"] == []
        assert seed.txn_status(txn_id) == "MATCHED"

    def test_the_file_missing_column_is_filled_in_when_the_schema_has_one(
        self, seed, pool
    ):
        """`documents` need not carry that column; the write path has to work
        wherever it does."""
        from server.jobs.status_repair import _FILE_MISSING_COLUMN, repair_status_drift

        txn_id = seed.txn(status="MATCHED")
        doc_id = seed.doc(
            status="MATCHED",
            stored_path=f"{seed.user_id}/202601/5555666677778888_gone.pdf",
        )
        seed.match(txn_id, doc_id, status="USER_APPROVED")

        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'ALTER TABLE documents ADD COLUMN IF NOT EXISTS '
                    f'"{_FILE_MISSING_COLUMN}" BOOLEAN'
                )
            conn.commit()
            result = repair_status_drift(pool=pool, user_id=seed.user_id)
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT "{_FILE_MISSING_COLUMN}" FROM documents WHERE id = %s',
                    (doc_id,),
                )
                flag = cur.fetchone()[0]
            conn.commit()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    f'ALTER TABLE documents DROP COLUMN IF EXISTS "{_FILE_MISSING_COLUMN}"'
                )
            conn.commit()
            pool.putconn(conn)

        assert flag is True
        assert result["documents_file_missing"][0]["flagged"] is True
        assert seed.txn_status(txn_id) == "MATCHED", "flagged, not demoted"

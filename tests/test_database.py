"""db/database.py — the SQLite persistence layer.

These tests state the contract the ``Database`` class must satisfy: the schema
it applies, and the round trip for every record type the engine stores —
transactions, documents, reconciliation matches, vendor rules, processed files,
API-call log rows and ledger entries. Each test drives a throwaway SQLite file
created by the ``db`` fixture.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from db.database import Database
from models.document import Document, DocumentStatus
from models.ledger_entry import LedgerEntry
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus
from models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "transactions",
    "documents",
    "reconciliation_matches",
    "ledger_entries",
    "vendor_rules",
    "processed_files",
    "api_calls",
    "gather_log",
    "gather_sources",
    "business_profile",
}


class TestDatabaseInit:
    """Verify that Database.__init__ applies the schema correctly."""

    def test_init_creates_tables(self, db: Database) -> None:
        """Every table the schema defines must exist after initialisation."""
        conn = sqlite3.connect(str(db.db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert tables == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TestTransactionCRUD:
    def test_insert_transaction(
        self, db: Database, sample_transactions: list[Transaction]
    ) -> None:
        """Insert a single transaction and retrieve it by ID."""
        txn = sample_transactions[0]
        txn_id = db.insert_transaction(txn)
        assert isinstance(txn_id, int) and txn_id > 0

        # Round-trip: fetch every unmatched row and pick out the one just written
        unmatched = db.get_unmatched_transactions()
        assert len(unmatched) >= 1
        fetched = next(t for t in unmatched if t.id == txn_id)
        assert fetched.account == txn.account
        assert fetched.amount == txn.amount
        assert fetched.date_posted == txn.date_posted
        assert fetched.description == txn.description
        assert fetched.status == TransactionStatus.UNMATCHED

    def test_duplicate_transaction_rejected(
        self, db: Database, sample_transactions: list[Transaction]
    ) -> None:
        """Inserting a transaction with the same (account, date, amount, description)
        must raise an IntegrityError (enforced by the composite unique index)."""
        txn = sample_transactions[0]
        db.insert_transaction(txn)
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_transaction(txn)

    def test_update_transaction_status(
        self, db: Database, sample_transactions: list[Transaction]
    ) -> None:
        txn_id = db.insert_transaction(sample_transactions[0])
        db.update_transaction_status(txn_id, TransactionStatus.MATCHED)

        # After update, should no longer appear in unmatched
        unmatched = db.get_unmatched_transactions()
        assert all(t.id != txn_id for t in unmatched)

    def test_get_unmatched_transactions(
        self, db: Database, sample_transactions: list[Transaction]
    ) -> None:
        """Only UNMATCHED transactions are returned."""
        ids = [db.insert_transaction(t) for t in sample_transactions]
        # Take the first two out of the unmatched pool, by both exit statuses
        db.update_transaction_status(ids[0], TransactionStatus.MATCHED)
        db.update_transaction_status(ids[1], TransactionStatus.IGNORED)

        unmatched = db.get_unmatched_transactions()
        unmatched_ids = {t.id for t in unmatched}
        assert ids[0] not in unmatched_ids
        assert ids[1] not in unmatched_ids
        assert ids[2] in unmatched_ids
        assert ids[3] in unmatched_ids
        assert ids[4] in unmatched_ids

    def test_get_transactions_by_month(
        self, db: Database, sample_transactions: list[Transaction]
    ) -> None:
        """Retrieve transactions filtered by year and month."""
        for t in sample_transactions:
            db.insert_transaction(t)

        jan_txns = db.get_transactions_by_month(2026, 1)
        assert len(jan_txns) == len(sample_transactions)

        # No transactions in February
        feb_txns = db.get_transactions_by_month(2026, 2)
        assert len(feb_txns) == 0


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class TestDocumentCRUD:
    def test_insert_document(
        self, db: Database, sample_documents: list[Document]
    ) -> None:
        doc = sample_documents[0]
        doc_id = db.insert_document(doc)
        assert isinstance(doc_id, int) and doc_id > 0

        unmatched = db.get_unmatched_documents()
        fetched = next(d for d in unmatched if d.id == doc_id)
        assert fetched.vendor == doc.vendor
        assert fetched.total == doc.total
        assert fetched.file_hash == doc.file_hash
        assert fetched.status == DocumentStatus.EXTRACTED

    def test_get_unmatched_documents(
        self, db: Database, sample_documents: list[Document]
    ) -> None:
        """Only documents with status EXTRACTED are considered unmatched."""
        ids = [db.insert_document(d) for d in sample_documents]
        # Simulate matching the first document by updating its status directly
        conn = sqlite3.connect(str(db.db_path))
        conn.execute(
            "UPDATE documents SET status = 'MATCHED' WHERE id = ?", (ids[0],)
        )
        conn.commit()
        conn.close()

        unmatched = db.get_unmatched_documents()
        unmatched_ids = {d.id for d in unmatched}
        assert ids[0] not in unmatched_ids
        assert ids[1] in unmatched_ids
        assert ids[2] in unmatched_ids


# ---------------------------------------------------------------------------
# Reconciliation matches
# ---------------------------------------------------------------------------


class TestMatchCRUD:
    def test_insert_match(
        self,
        db: Database,
        sample_transactions: list[Transaction],
        sample_documents: list[Document],
    ) -> None:
        txn_id = db.insert_transaction(sample_transactions[0])
        doc_id = db.insert_document(sample_documents[0])

        match = ReconciliationMatch(
            transaction_id=txn_id,
            document_id=doc_id,
            confidence_score=0.95,
            amount_score=1.0,
            date_score=0.85,
            vendor_score=0.92,
            match_type=MatchType.ONE_TO_ONE,
            status=MatchStatus.PENDING_REVIEW,
        )
        match_id = db.insert_match(match)
        assert isinstance(match_id, int) and match_id > 0


# ---------------------------------------------------------------------------
# Vendor rules
# ---------------------------------------------------------------------------


class TestVendorRules:
    def test_insert_vendor_rule(self, db: Database) -> None:
        rule = VendorRule(
            vendor_pattern="ORCHID TELECOM",
            category="Office IT",
            priority=80,
            source="manual",
        )
        rule_id = db.insert_vendor_rule(rule)
        assert isinstance(rule_id, int) and rule_id > 0

    def test_get_vendor_rules_ordered_by_priority(self, db: Database) -> None:
        """Rules must be returned in descending priority order."""
        rules = [
            VendorRule(vendor_pattern="ACME TELECOM", category="Office IT", priority=50, source="manual"),
            VendorRule(vendor_pattern="JETBRAINS", category="Office IT", priority=80, source="manual"),
            VendorRule(vendor_pattern="AMAZON", category="Office IT", priority=30, source="learned"),
            VendorRule(vendor_pattern="NORTHWIND", category="Income", priority=100, source="manual"),
        ]
        for r in rules:
            db.insert_vendor_rule(r)

        fetched = db.get_vendor_rules()
        assert len(fetched) == 4
        priorities = [r.priority for r in fetched]
        assert priorities == sorted(priorities, reverse=True)
        assert fetched[0].vendor_pattern == "NORTHWIND"
        assert fetched[-1].vendor_pattern == "AMAZON"


# ---------------------------------------------------------------------------
# Processed files
# ---------------------------------------------------------------------------


class TestProcessedFiles:
    def test_insert_processed_file(self, db: Database) -> None:
        pf_id = db.insert_processed_file(
            file_hash="abc123def456",
            original_filename="20260115-telecom-bill.pdf",
        )
        assert isinstance(pf_id, int) and pf_id > 0
        assert db.is_file_processed("abc123def456") is True
        assert db.is_file_processed("nonexistent_hash") is False

    def test_duplicate_file_hash_rejected(self, db: Database) -> None:
        """The same file_hash must not be inserted twice (UNIQUE constraint)."""
        db.insert_processed_file(
            file_hash="abc123def456",
            original_filename="20260115-telecom-bill.pdf",
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_processed_file(
                file_hash="abc123def456",
                original_filename="duplicate.pdf",
            )


# ---------------------------------------------------------------------------
# API call logging
# ---------------------------------------------------------------------------


class TestApiCallLogging:
    def test_log_api_call(self, db: Database) -> None:
        """Logging an API call persists the row, verified by reading it back in SQL."""
        db.log_api_call(
            endpoint="extraction",
            model="gemini-2.5-flash",
            tokens_in=1200,
            tokens_out=350,
            estimated_cost=0.0045,
        )
        conn = sqlite3.connect(str(db.db_path))
        rows = conn.execute("SELECT endpoint, model, tokens_in, tokens_out, estimated_cost FROM api_calls").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "extraction"
        assert rows[0][1] == "gemini-2.5-flash"
        assert rows[0][2] == 1200
        assert rows[0][3] == 350
        assert abs(rows[0][4] - 0.0045) < 1e-6


# ---------------------------------------------------------------------------
# Ledger entries
# ---------------------------------------------------------------------------


class TestLedgerEntries:
    def test_insert_ledger_entry(self, db: Database) -> None:
        entry = LedgerEntry(
            account=AccountType.CAD,
            month="Jan2026",
            transaction_type="DEBIT",
            date_posted=date(2026, 1, 15),
            amount=Decimal("81.45"),
            currency="CAD",
            description="ACME TELECOM PAYMENT",
            category="Office IT",
            document_link="data/books/2026/receipts/2026-01/telecom-bill.pdf",
        )
        entry_id = db.insert_ledger_entry(entry)
        assert isinstance(entry_id, int) and entry_id > 0

    def test_get_ledger_entries_by_month(self, db: Database) -> None:
        entries = [
            LedgerEntry(
                account=AccountType.CAD,
                month="Jan2026",
                transaction_type="DEBIT",
                date_posted=date(2026, 1, 15),
                amount=Decimal("81.45"),
                currency="CAD",
                description="ACME TELECOM PAYMENT",
                category="Office IT",
            ),
            LedgerEntry(
                account=AccountType.USD,
                month="Jan2026",
                transaction_type="CREDIT",
                date_posted=date(2026, 1, 10),
                amount=Decimal("12500.00"),
                currency="USD",
                description="WIRE TRANSFER NORTHWIND MEDIA LLC",
                category="Income",
            ),
            LedgerEntry(
                account=AccountType.CAD,
                month="Feb2026",
                transaction_type="DEBIT",
                date_posted=date(2026, 2, 5),
                amount=Decimal("120.00"),
                currency="CAD",
                description="ANYTOWN UTILITY CO",
                category="Office IT",
            ),
        ]
        for e in entries:
            db.insert_ledger_entry(e)

        jan = db.get_ledger_entries_by_month("Jan2026")
        assert len(jan) == 2

        feb = db.get_ledger_entries_by_month("Feb2026")
        assert len(feb) == 1
        assert feb[0].description == "ANYTOWN UTILITY CO"

        mar = db.get_ledger_entries_by_month("Mar2026")
        assert len(mar) == 0

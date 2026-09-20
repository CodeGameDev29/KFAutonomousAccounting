"""SQLite persistence layer for Autonomous Accounting.

This module is only safe for single-tenant local use: no query is scoped by
user_id, so it must never be imported when the app is serving more than one
account. The multi-tenant path uses db/database_pg.py instead.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

_DEPLOY_MODE = os.getenv("DEPLOY_MODE", "local").strip().lower()
if _DEPLOY_MODE == "cloud":
    raise RuntimeError(
        "db/database.py (legacy SQLite, single-tenant) was imported while "
        "DEPLOY_MODE=cloud. Cloud runtime must use db/database_pg.py. Refusing "
        "to load to prevent cross-tenant data leakage."
    )

from models.document import Document, DocumentStatus, LineItem  # noqa: E402
from models.ledger_entry import LedgerEntry  # noqa: E402
from models.match import MatchStatus, MatchType, ReconciliationMatch  # noqa: E402
from models.transaction import AccountType, Transaction, TransactionStatus  # noqa: E402
from models.vendor_rule import VendorRule  # noqa: E402

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_logger = logging.getLogger(__name__)


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def _date_or_none(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _datetime_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)


class Database:
    """Thin wrapper around a SQLite database for the accounting system.

    Uses a singleton pattern: all callers with the same resolved db_path
    share one connection, eliminating intra-process "database is locked"
    errors caused by multiple connections fighting for the write lock.
    """

    _instances: dict[str, Database] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, db_path: str) -> Database:
        canonical = str(Path(db_path).resolve())
        with cls._instance_lock:
            if canonical in cls._instances:
                return cls._instances[canonical]
            instance = super().__new__(cls)
            instance._initialized = False
            cls._instances[canonical] = instance
            return instance

    def __init__(self, db_path: str) -> None:
        if self._initialized:
            return
        self._initialized = True

        self.db_path: str = db_path
        self._write_lock = threading.Lock()
        self._max_retries = 8
        self._conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 60000")
        self._conn.execute("PRAGMA foreign_keys = ON")

        # Migrate dedup index BEFORE schema. Drop any old index variant so the
        # schema can recreate with the current definition (account, date, amount,
        # description, source_row). Remove exact dupes that would violate the new index.
        try:
            self._conn.execute("""
                DELETE FROM transactions
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM transactions
                    GROUP BY account, date_posted, amount, description, source_row
                )
            """)
            self._conn.execute("DROP INDEX IF EXISTS idx_transactions_dedup")
            self._commit_with_retry()
        except sqlite3.OperationalError:
            pass  # Table may not exist yet (fresh DB)

        schema_sql = _SCHEMA_PATH.read_text()
        self._conn.executescript(schema_sql)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the connection and remove from the singleton cache.

        Primarily used by tests that need to delete the temp DB file.
        """
        canonical = str(Path(self.db_path).resolve())
        with self._instance_lock:
            self._instances.pop(canonical, None)
        try:
            self._conn.close()
        except Exception:
            pass
        self._initialized = False

    # ------------------------------------------------------------------
    # Retry-aware commit (safety net for external DB contention)
    # ------------------------------------------------------------------

    def _commit_with_retry(self) -> None:
        """Commit with exponential backoff retry on 'database is locked'."""
        for attempt in range(self._max_retries):
            try:
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < self._max_retries - 1:
                    wait = 0.1 * (2 ** attempt)  # 0.1s, 0.2s, 0.4s, ... up to 12.8s
                    _logger.warning(
                        "database is locked on commit (attempt %d/%d), retrying in %.1fs",
                        attempt + 1, self._max_retries, wait,
                    )
                    time.sleep(wait)
                    continue
                raise

    # ------------------------------------------------------------------
    # Reset (empty the file, keeping its schema)
    # ------------------------------------------------------------------

    def reset_all_tables(self) -> dict[str, int]:
        """Delete all rows from every data table. Returns {table: deleted_count}."""
        tables = [
            "reconciliation_matches",
            "ledger_entries",
            "documents",
            "transactions",
            "processed_files",
            "vendor_rules",
            "api_calls",
            "gather_log",
            "gather_sources",
            "business_profile",
        ]
        counts: dict[str, int] = {}
        for table in tables:
            cursor = self._conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            counts[table] = cursor.fetchone()[0]
            self._conn.execute(f"DELETE FROM {table}")  # noqa: S608
        self._commit_with_retry()
        return counts

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def insert_transaction(self, txn: Transaction) -> int:
        cursor = self._conn.execute(
            """INSERT INTO transactions
               (account, transaction_type, date_posted, amount, currency,
                description, source_file, source_row, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                txn.account.value,
                txn.transaction_type,
                txn.date_posted.isoformat(),
                str(txn.amount),
                txn.currency,
                txn.description,
                txn.source_file,
                txn.source_row,
                txn.status.value,
            ),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def update_transaction_status(self, txn_id: int, status: TransactionStatus | str) -> None:
        status_val = status.value if hasattr(status, "value") else status
        self._conn.execute(
            "UPDATE transactions SET status = ? WHERE id = ?",
            (status_val, txn_id),
        )
        self._commit_with_retry()

    def get_unmatched_transactions(self) -> list[Transaction]:
        cursor = self._conn.execute(
            """SELECT id, account, transaction_type, date_posted, amount, currency,
                      description, source_file, source_row, status, created_at
               FROM transactions WHERE status = 'UNMATCHED'"""
        )
        return [self._row_to_transaction(row) for row in cursor.fetchall()]

    def get_transactions_by_month(
        self, year: int, month: int, lookback_days: int = 0,
    ) -> list[Transaction]:
        """Return transactions for the given calendar month.

        If ``lookback_days`` > 0, also include transactions from up to that
        many days before the start of the month.  This is useful when bank
        statement periods straddle month boundaries (e.g., Oct 30 appearing
        on the Nov statement).
        """
        from datetime import date as _date
        from datetime import timedelta
        month_start = _date(year, month, 1)
        if month < 12:
            month_end = _date(year, month + 1, 1)
        else:
            month_end = _date(year + 1, 1, 1)

        if lookback_days > 0:
            start = (month_start - timedelta(days=lookback_days)).isoformat()
        else:
            start = month_start.isoformat()
        end = month_end.isoformat()

        cursor = self._conn.execute(
            """SELECT id, account, transaction_type, date_posted, amount, currency,
                      description, source_file, source_row, status, created_at
               FROM transactions WHERE date_posted >= ? AND date_posted < ?""",
            (start, end),
        )
        return [self._row_to_transaction(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_transaction(row: tuple) -> Transaction:
        return Transaction(
            id=row[0],
            account=AccountType(row[1]),
            transaction_type=row[2],
            date_posted=date.fromisoformat(row[3]),
            amount=Decimal(row[4]),
            currency=row[5],
            description=row[6],
            source_file=row[7],
            source_row=row[8],
            status=TransactionStatus(row[9]),
            created_at=_datetime_or_none(row[10]),
        )

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def insert_document(self, doc: Document) -> int:
        line_items_json = None
        if doc.line_items is not None:
            line_items_json = json.dumps([li.model_dump(mode="json") for li in doc.line_items])

        cursor = self._conn.execute(
            """INSERT INTO documents
               (original_filename, stored_path, file_hash, vendor, document_date,
                currency, subtotal, tax_gst, tax_hst, tax_pst, tax_other, total,
                payment_method, invoice_number, line_items_json,
                extraction_confidence, extraction_raw_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.original_filename,
                doc.stored_path,
                doc.file_hash,
                doc.vendor,
                doc.document_date.isoformat() if doc.document_date else None,
                doc.currency,
                _str_or_none(doc.subtotal),
                _str_or_none(doc.tax_gst),
                _str_or_none(doc.tax_hst),
                _str_or_none(doc.tax_pst),
                _str_or_none(doc.tax_other),
                _str_or_none(doc.total),
                doc.payment_method,
                doc.invoice_number,
                line_items_json,
                doc.extraction_confidence,
                doc.extraction_raw_json,
                doc.status.value,
            ),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_unmatched_documents(self) -> list[Document]:
        cursor = self._conn.execute(
            """SELECT id, original_filename, stored_path, file_hash, vendor,
                      document_date, currency, subtotal, tax_gst, tax_hst, tax_pst,
                      tax_other, total, payment_method, invoice_number,
                      line_items_json, extraction_confidence, extraction_raw_json,
                      status, created_at
               FROM documents WHERE status = 'EXTRACTED'"""
        )
        return [self._row_to_document(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_document(row: tuple) -> Document:
        line_items = None
        if row[15] is not None:
            line_items = [LineItem(**item) for item in json.loads(row[15])]

        return Document(
            id=row[0],
            original_filename=row[1],
            stored_path=row[2],
            file_hash=row[3],
            vendor=row[4],
            document_date=_date_or_none(row[5]),
            currency=row[6],
            subtotal=_decimal_or_none(row[7]),
            tax_gst=_decimal_or_none(row[8]),
            tax_hst=_decimal_or_none(row[9]),
            tax_pst=_decimal_or_none(row[10]),
            tax_other=_decimal_or_none(row[11]),
            total=_decimal_or_none(row[12]),
            payment_method=row[13],
            invoice_number=row[14],
            line_items=line_items,
            extraction_confidence=row[16],
            extraction_raw_json=row[17],
            status=DocumentStatus(row[18]),
            created_at=_datetime_or_none(row[19]),
        )

    # ------------------------------------------------------------------
    # Reconciliation matches
    # ------------------------------------------------------------------

    def insert_match(self, match: ReconciliationMatch) -> int:
        cursor = self._conn.execute(
            """INSERT INTO reconciliation_matches
               (transaction_id, document_id, confidence_score, amount_score,
                date_score, vendor_score, match_type, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match.transaction_id,
                match.document_id,
                match.confidence_score,
                match.amount_score,
                match.date_score,
                match.vendor_score,
                match.match_type.value,
                match.status.value,
            ),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Vendor rules
    # ------------------------------------------------------------------

    def insert_vendor_rule(self, rule: VendorRule) -> int:
        cursor = self._conn.execute(
            """INSERT INTO vendor_rules (vendor_pattern, category, priority, source)
               VALUES (?, ?, ?, ?)""",
            (
                rule.vendor_pattern,
                rule.category,
                rule.priority,
                rule.source,
            ),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_vendor_rules(self) -> list[VendorRule]:
        cursor = self._conn.execute(
            """SELECT id, vendor_pattern, category, priority, source, created_at
               FROM vendor_rules ORDER BY priority DESC"""
        )
        return [
            VendorRule(
                id=row[0],
                vendor_pattern=row[1],
                category=row[2],
                priority=row[3],
                source=row[4],
                created_at=_datetime_or_none(row[5]),
            )
            for row in cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    # Processed files
    # ------------------------------------------------------------------

    def insert_processed_file(self, file_hash: str, original_filename: str) -> int:
        cursor = self._conn.execute(
            """INSERT INTO processed_files (file_hash, original_filename)
               VALUES (?, ?)""",
            (file_hash, original_filename),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def is_file_processed(self, file_hash: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM processed_files WHERE file_hash = ?", (file_hash,)
        )
        return cursor.fetchone() is not None

    # ------------------------------------------------------------------
    # API call logging
    # ------------------------------------------------------------------

    def log_api_call(
        self, endpoint: str, model: str, tokens_in: int, tokens_out: int, estimated_cost: float
    ) -> None:
        self._conn.execute(
            """INSERT INTO api_calls (endpoint, model, tokens_in, tokens_out, estimated_cost)
               VALUES (?, ?, ?, ?, ?)""",
            (endpoint, model, tokens_in, tokens_out, estimated_cost),
        )
        self._commit_with_retry()

    # ------------------------------------------------------------------
    # Ledger entries
    # ------------------------------------------------------------------

    def insert_ledger_entry(self, entry: LedgerEntry) -> int:
        cursor = self._conn.execute(
            """INSERT INTO ledger_entries
               (account, month, transaction_type, date_posted, amount, currency,
                description, category, document_link, note, match_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.account.value,
                entry.month,
                entry.transaction_type,
                entry.date_posted.isoformat(),
                str(entry.amount),
                entry.currency,
                entry.description,
                entry.category,
                entry.document_link,
                entry.note,
                entry.match_id,
            ),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_ledger_entries_by_month(self, month: str) -> list[LedgerEntry]:
        cursor = self._conn.execute(
            """SELECT id, account, month, transaction_type, date_posted, amount,
                      currency, description, category, document_link, note,
                      match_id, created_at
               FROM ledger_entries WHERE month = ?""",
            (month,),
        )
        return [
            LedgerEntry(
                id=row[0],
                account=AccountType(row[1]),
                month=row[2],
                transaction_type=row[3],
                date_posted=date.fromisoformat(row[4]),
                amount=Decimal(row[5]),
                currency=row[6],
                description=row[7],
                category=row[8],
                document_link=row[9],
                note=row[10],
                match_id=row[11],
                created_at=_datetime_or_none(row[12]),
            )
            for row in cursor.fetchall()
        ]

    def update_document_status(self, doc_id: int, status: str) -> None:
        """Update the status for a document."""
        self._conn.execute(
            "UPDATE documents SET status = ? WHERE id = ?",
            (status, doc_id),
        )
        self._commit_with_retry()

    def update_document_path(self, doc_id: int, new_path: str) -> None:
        """Update the stored_path for a document (used when refiling after reconciliation)."""
        self._conn.execute(
            "UPDATE documents SET stored_path = ? WHERE id = ?",
            (new_path, doc_id),
        )
        self._commit_with_retry()

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def get_latest_transaction_month(self) -> tuple[int, int] | None:
        """Return (year, month) of the most recent transaction, or None if no transactions."""
        cursor = self._conn.execute(
            "SELECT date_posted FROM transactions ORDER BY date_posted DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        d = date.fromisoformat(row[0])
        return d.year, d.month

    def get_matched_document_info(self) -> tuple[dict[int, str], dict[int, str]]:
        """Return matched doc paths and vendor names for transactions.

        Returns (paths_dict, vendor_dict) where:
            paths_dict: transaction_id -> document stored_path
            vendor_dict: transaction_id -> document vendor name
        """
        cursor = self._conn.execute(
            """SELECT rm.transaction_id, d.stored_path, d.vendor
               FROM reconciliation_matches rm
               JOIN documents d ON rm.document_id = d.id
               WHERE rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')"""
        )
        paths = {}
        vendors = {}
        for row in cursor.fetchall():
            paths[row[0]] = row[1] or ""
            vendors[row[0]] = row[2] or ""
        return paths, vendors

    def get_matched_document_paths(self) -> dict[int, str]:
        """Return a dict mapping transaction_id -> document stored_path for matched transactions."""
        cursor = self._conn.execute(
            """SELECT rm.transaction_id, d.stored_path
               FROM reconciliation_matches rm
               JOIN documents d ON rm.document_id = d.id
               WHERE rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')
                 AND d.stored_path IS NOT NULL"""
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Review helpers
    # ------------------------------------------------------------------

    def get_pending_reviews(self) -> list[ReconciliationMatch]:
        """Return all reconciliation matches in PENDING_REVIEW status."""
        cursor = self._conn.execute(
            """SELECT id, transaction_id, document_id, confidence_score,
                      amount_score, date_score, vendor_score,
                      match_type, status, reviewed_by, reviewed_at, created_at
               FROM reconciliation_matches
               WHERE status = 'PENDING_REVIEW'
               ORDER BY created_at ASC"""
        )
        results = []
        for row in cursor.fetchall():
            results.append(ReconciliationMatch(
                id=row[0],
                transaction_id=row[1],
                document_id=row[2],
                confidence_score=row[3],
                amount_score=row[4],
                date_score=row[5],
                vendor_score=row[6],
                match_type=MatchType(row[7]),
                status=MatchStatus(row[8]),
                reviewed_by=row[9],
                reviewed_at=row[10],
            ))
        return results

    def update_match_status(
        self, match_id: int, status: str, reviewed_by: str | None = None
    ) -> None:
        """Update a reconciliation match's status and reviewer info."""
        self._conn.execute(
            """UPDATE reconciliation_matches
               SET status = ?, reviewed_by = ?, reviewed_at = datetime('now')
               WHERE id = ?""",
            (status, reviewed_by, match_id),
        )
        self._commit_with_retry()

    # ------------------------------------------------------------------
    # Business profile (onboarding)
    # ------------------------------------------------------------------

    def get_business_profile(self) -> dict | None:
        """Return the business profile, or None if not set."""
        cursor = self._conn.execute(
            """SELECT id, company_name, fiscal_year, base_currency,
                      tax_jurisdiction, onboarding_complete, config_json,
                      created_at, updated_at
               FROM business_profile LIMIT 1"""
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "company_name": row[1],
            "fiscal_year": row[2],
            "base_currency": row[3],
            "tax_jurisdiction": row[4],
            "onboarding_complete": bool(row[5]),
            "config_json": json.loads(row[6]) if row[6] else None,
            "created_at": row[7],
            "updated_at": row[8],
        }

    def upsert_business_profile(self, data: dict) -> None:
        """Insert or update the business profile."""
        existing = self.get_business_profile()
        config_json = json.dumps(data.get("config_json")) if data.get("config_json") else None
        if existing:
            self._conn.execute(
                """UPDATE business_profile
                   SET company_name = ?, fiscal_year = ?, base_currency = ?,
                       tax_jurisdiction = ?, onboarding_complete = ?,
                       config_json = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    data.get("company_name", existing["company_name"]),
                    data.get("fiscal_year", existing["fiscal_year"]),
                    data.get("base_currency", existing["base_currency"]),
                    data.get("tax_jurisdiction", existing["tax_jurisdiction"]),
                    int(data.get("onboarding_complete", existing["onboarding_complete"])),
                    config_json,
                    existing["id"],
                ),
            )
        else:
            self._conn.execute(
                """INSERT INTO business_profile
                   (company_name, fiscal_year, base_currency, tax_jurisdiction,
                    onboarding_complete, config_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    data.get("company_name"),
                    data.get("fiscal_year"),
                    data.get("base_currency", "CAD"),
                    data.get("tax_jurisdiction", "CA"),
                    int(data.get("onboarding_complete", False)),
                    config_json,
                ),
            )
        self._commit_with_retry()

    def is_onboarding_complete(self) -> bool:
        """Check if onboarding has been completed."""
        profile = self.get_business_profile()
        return profile is not None and profile["onboarding_complete"]

    # ------------------------------------------------------------------
    # Gather sources
    # ------------------------------------------------------------------

    def get_gather_sources(self) -> list[dict]:
        """Return all configured gather sources."""
        cursor = self._conn.execute(
            """SELECT id, source_type, enabled, config_json, last_gathered,
                      created_at, updated_at
               FROM gather_sources ORDER BY source_type"""
        )
        return [
            {
                "id": row[0],
                "source_type": row[1],
                "enabled": bool(row[2]),
                "config_json": json.loads(row[3]) if row[3] else None,
                "last_gathered": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
            for row in cursor.fetchall()
        ]

    def upsert_gather_source(
        self, source_type: str, enabled: bool = True, config_json: dict | None = None
    ) -> None:
        """Insert or update a gather source configuration."""
        config_str = json.dumps(config_json) if config_json else None
        self._conn.execute(
            """INSERT INTO gather_sources (source_type, enabled, config_json)
               VALUES (?, ?, ?)
               ON CONFLICT(source_type) DO UPDATE SET
                   enabled = excluded.enabled,
                   config_json = excluded.config_json,
                   updated_at = datetime('now')""",
            (source_type, int(enabled), config_str),
        )
        self._commit_with_retry()

    def update_gather_source_last_gathered(self, source_type: str) -> None:
        """Update the last_gathered timestamp for a source."""
        self._conn.execute(
            """UPDATE gather_sources
               SET last_gathered = datetime('now'), updated_at = datetime('now')
               WHERE source_type = ?""",
            (source_type,),
        )
        self._commit_with_retry()

    # ------------------------------------------------------------------
    # Gather log (document dedup + audit)
    # ------------------------------------------------------------------

    def insert_gather_log(
        self,
        source: str,
        source_id: str | None,
        filename: str,
        file_hash: str | None = None,
        file_size: int | None = None,
        stored_path: str | None = None,
        metadata_json: dict | None = None,
    ) -> int:
        """Insert a gather log entry. Returns the new row ID."""
        meta_str = json.dumps(metadata_json) if metadata_json else None
        cursor = self._conn.execute(
            """INSERT INTO gather_log
               (source, source_id, filename, file_hash, file_size, stored_path, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, source_id, filename, file_hash, file_size, stored_path, meta_str),
        )
        self._commit_with_retry()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_gather_log_by_source_id(self, source: str, source_id: str) -> dict | None:
        """Look up a gather log entry by source + source_id (for dedup)."""
        cursor = self._conn.execute(
            """SELECT id, source, source_id, filename, file_hash, file_size,
                      stored_path, gathered_at, status, metadata_json
               FROM gather_log WHERE source = ? AND source_id = ?""",
            (source, source_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "source": row[1], "source_id": row[2],
            "filename": row[3], "file_hash": row[4], "file_size": row[5],
            "stored_path": row[6], "gathered_at": row[7], "status": row[8],
            "metadata_json": json.loads(row[9]) if row[9] else None,
        }

    def get_gather_log_by_hash(self, file_hash: str) -> dict | None:
        """Look up a gather log entry by file hash (for dedup)."""
        cursor = self._conn.execute(
            """SELECT id, source, source_id, filename, file_hash, file_size,
                      stored_path, gathered_at, status, metadata_json
               FROM gather_log WHERE file_hash = ? LIMIT 1""",
            (file_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "source": row[1], "source_id": row[2],
            "filename": row[3], "file_hash": row[4], "file_size": row[5],
            "stored_path": row[6], "gathered_at": row[7], "status": row[8],
            "metadata_json": json.loads(row[9]) if row[9] else None,
        }

    def update_gather_log_status(self, log_id: int, status: str) -> None:
        """Update a gather log entry's status."""
        self._conn.execute(
            "UPDATE gather_log SET status = ? WHERE id = ?",
            (status, log_id),
        )
        self._commit_with_retry()

    def get_gather_log_stats(self) -> dict:
        """Return summary counts of gather log entries by source and status."""
        cursor = self._conn.execute(
            """SELECT source, status, COUNT(*) as cnt
               FROM gather_log GROUP BY source, status"""
        )
        stats: dict[str, dict[str, int]] = {}
        for row in cursor.fetchall():
            stats.setdefault(row[0], {})[row[1]] = row[2]
        return stats

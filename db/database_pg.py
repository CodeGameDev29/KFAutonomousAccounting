"""PostgreSQL persistence layer for Autonomous Accounting.

The data layer the web application uses. Every instance is bound to one
user_id and every query is scoped to it, on top of the row-level-security
policies in db/schema_pg.sql. Connections come from a pool; see ``_conn`` for
how a transaction is dropped to the ``authenticated`` role so those policies
are what enforces isolation.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql as psql

from config.settings import reconciliation_config as _recon_cfg
from models.document import Document, DocumentStatus, LineItem
from models.ledger_entry import LedgerEntry
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, ImportConfidence, Transaction, TransactionStatus
from models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)


# Statuses a queued document can be taken back out of the queue from.
# 'running' is deliberately absent: the worker is holding that row, an LLM call
# and half a document's worth of writes, and removing it underneath the worker
# would strand both. Shared with server/api/jobs.py, which turns "not one of
# these" into a 409 a person can read.
REROUTABLE_JOB_STATUSES: tuple[str, ...] = ("queued", "waiting_for_model", "failed")


# The batch a user is currently watching, in one statement.
#
# A "batch" is everything dropped since this user's queue was last empty. The
# anchor is the most recent job that landed when nothing of theirs was still
# unfinished - the start of the current run - and the batch is every job created
# at or after that moment. "Clear finished" needs no special case: deleting the
# finished rows moves the anchor forward on its own, to the oldest job still on
# the list, which is exactly the batch the user is looking at afterwards.
#
# Defined here rather than in the browser because a browser can only count the
# rows it has seen since it loaded: the figure would reset on a reload, and two
# tabs on the same import would disagree. Takes the user id
# twice. Shared with server/jobs/document_queue.py (the SSE summary) so the poll
# and the event never say different numbers.
PROCESSING_JOB_BATCH_SQL = """
    WITH anchor AS (
        SELECT j.created_at
          FROM processing_jobs j
         WHERE j.user_id = %s
           AND NOT EXISTS (
               SELECT 1 FROM processing_jobs p
                WHERE p.user_id = j.user_id
                  AND (p.created_at, p.position, p.id)
                      < (j.created_at, j.position, j.id)
                  AND (p.finished_at IS NULL OR p.finished_at > j.created_at))
         ORDER BY j.created_at DESC, j.position DESC
         LIMIT 1
    )
    SELECT a.created_at,
           COUNT(k.id) AS total,
           COUNT(k.id) FILTER (
               WHERE k.status IN ('done', 'failed', 'cancelled')) AS finished
      FROM anchor a
      JOIN processing_jobs k
        ON k.user_id = %s AND k.created_at >= a.created_at
     GROUP BY a.created_at
"""


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def _date_or_none(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _datetime_or_none(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalized_amount_key(value) -> str | None:
    """A comparable form of a money column stored as TEXT.

    ``documents.total`` is a TEXT column, so "41.25", "41.250" and "$41.25"
    are three different strings for the same number. Two uploads of the same
    receipt must land on the same key or the twin rule below misses them.
    Returns None when the value is not a number at all.
    """
    if value is None:
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return str(abs(Decimal(text)).normalize())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _document_identity_keys(
    file_hash: str | None,
    vendor: str | None,
    total,
    currency: str | None,
    document_date,
) -> list[tuple]:
    """Keys under which two ``documents`` rows are the same piece of paper.

    Two of them, and both are needed:

    * ``file_hash`` — byte-identical uploads. Note that ``idx_documents_hash``
      is UNIQUE on (user_id, file_hash), so this key can only ever group rows
      across users; it is kept because it is the definition of a duplicate and
      costs nothing.
    * content identity — same vendor, same total, same currency, same document
      date. This is what catches the case the hash cannot: the same receipt
      uploaded half a dozen times, each PDF differing byte-for-byte because
      something stamped a timestamp into the file, so every copy carries a
      different hash. All four fields are required, so a document that has not
      been extracted has no twins.
    """
    keys: list[tuple] = []
    if file_hash:
        keys.append(("hash", file_hash))
    total_key = _normalized_amount_key(total)
    vendor_key = (vendor or "").strip().lower()
    date_key = str(document_date).strip()[:10] if document_date else ""
    if vendor_key and total_key is not None and date_key:
        keys.append((
            "content", vendor_key, total_key,
            (currency or "").strip().upper(), date_key,
        ))
    return keys


def _audit_value(value: dict | str | None) -> str | None:
    """Serialize an audit_log old_value/new_value for its TEXT column.

    A dict becomes JSON (every pre-existing caller); a plain string is stored
    as-is so a human-readable "before → after" survives to /activity intact.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


# Text columns a transaction search tests with substring (ILIKE) + regex arms.
# NOTE: `id` and `amount` are deliberately NOT here — they are numeric and get
# their own *exact* arms in _build_txn_search_predicate (id = N, ABS(amount) = N).
# Testing them as substrings made a numeric query like "50" match id 1500 and
# amount 2500.00, drowning the exact TXN-id / dollar-amount the user typed
# (requirement: money search must not filter the query against numeric
# transaction ids).
_TXN_SEARCH_COLUMNS = (
    "description",
    "category",
    "account",
    "note",
    "date_posted::text",
    "currency",
    "status",
    "source_file",
)

_REGEX_METACHARS = set(".^$*+?{}[]|()")

# Cap regex length — pathological patterns are a DoS vector in Postgres.
_MAX_REGEX_LEN = 200


def _regex_candidate(search: str) -> str | None:
    """Return ``search`` as a regex pattern if it plausibly is one.

    Plain words stay substring-only; only patterns containing regex
    metacharacters that also compile in Python get a regex arm.
    """
    if len(search) > _MAX_REGEX_LEN or not (_REGEX_METACHARS & set(search)):
        return None
    try:
        re.compile(search)
    except re.error:
        return None
    return search


# A monetary value the user might type into a search box: an optional sign, an
# optional '$', thousands separators, and an optional decimal part. Anything
# else (letters, dates like 2026-07, mixed junk) is rejected and falls through
# to the text/id arms.
_MONEY_RE = re.compile(r"[-+]?\d+(?:\.\d+)?$")


def _search_id(search: str) -> int | None:
    """Return the exact TXN/doc id a search targets, or None.

    A digits-only value matches the id exactly; a leading '#' (rows render as
    "#1234") and surrounding whitespace are tolerated. The isascii() guard
    keeps non-ASCII digits like '²' — whose .isdigit() is True but int() raises
    — out of the id arm.
    """
    s = search.strip().lstrip("#").strip()
    if s.isascii() and s.isdigit():
        return int(s)
    return None


def _search_amount(search: str) -> Decimal | None:
    """Return the ABSOLUTE monetary value a search targets, or None.

    Tolerates a leading '$', thousands separators, whitespace, and an optional
    sign so "$1,234.50", "-12.50", and "50" all parse. Callers compare the
    result against ABS(amount)/ABS(total), so "12.5" finds a -12.50 debit and a
    row stored as "12.500" alike — the precision/sign/format mismatches that
    made substring-on-text money search fail. Returns None for non-money input.
    """
    cleaned = search.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned or not _MONEY_RE.match(cleaned):
        return None
    try:
        return abs(Decimal(cleaned))
    except InvalidOperation:
        return None


def _build_txn_search_predicate(search: str, regex_pattern: str | None) -> tuple[str, list]:
    """WHERE fragment OR-ing every way ``search`` might match a transaction.

    The point is to exhaust every criterion rather than commit to one guess
    about what the person meant. For a single input the criteria are OR'd
    together, in priority order:

      1. Exact TXN id   — ``id = N``            (digits, '#' prefix tolerated)
      2. Text substring — ILIKE per text column (description, category, …)
      2b. Regex         — ``~*`` per text column (only if the input is a regex)
      3. Exact amount   — ``ABS(amount) = N``   (money-parseable input)

    So "50" matches TXN #50 AND every $50.00 row AND any text containing "50";
    "12.50" finds the $12.50 charge even when it is stored as "-12.50" or
    "12.5". Amount is compared numerically (never as text), which is what makes
    dollar-value search actually work.
    """
    safe = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_param = f"%{safe}%"
    clauses = []
    params: list = []

    # Priority 1: exact TXN id.
    txn_id = _search_id(search)
    if txn_id is not None:
        clauses.append("id = %s")
        params.append(txn_id)

    # Priority 2 / 2b: text columns (substring, then regex).
    for col in _TXN_SEARCH_COLUMNS:
        clauses.append(f"{col} ILIKE %s ESCAPE '\\'")
        params.append(like_param)
    if regex_pattern is not None:
        for col in _TXN_SEARCH_COLUMNS:
            clauses.append(f"{col} ~* %s")
            params.append(regex_pattern)

    # Priority 3: exact monetary value. `amount` is NOT NULL and always a clean
    # decimal string (same cast the min/max filters rely on), so no cast guard
    # is needed here.
    amount = _search_amount(search)
    if amount is not None:
        clauses.append("ABS(CAST(amount AS NUMERIC)) = %s")
        params.append(amount)

    return "(" + " OR ".join(clauses) + ")", params


# Text columns a document/receipt search tests with substring arms. `id` and
# `total` are numeric and get their own exact arms in
# _build_doc_search_predicate.
_DOC_SEARCH_COLUMNS = (
    "vendor",
    "original_filename",
    "currency",
    "document_date::text",
)


# Fuzzy-search tuning. A document's searchable text is whatever the vision
# model read off the image, and handwriting is where it slips — take a
# photographed cheque payable to Jordan Avery read as "KORDAN AVERY": a search
# for the name printed on the document returns nothing and the receipt looks
# like it was never uploaded. Short terms are excluded because a one-edit window
# on three characters matches nearly everything.
_FUZZY_MIN_LEN = 4
_FUZZY_MAX_PATTERNS = 24
_FUZZY_COLUMNS = ("vendor", "original_filename")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _one_edit_patterns(term: str) -> list[str]:
    """LIKE bodies matching ``term`` with at most one character of error.

    Uses only standard LIKE — ``_`` matches exactly one character — so this
    needs no pg_trgm/fuzzystrmatch extension. Covers all three edit kinds:
    substitution (wildcard one position), deletion (the stored text is missing
    a character the user typed) and insertion (the stored text has one extra).
    """
    term = term.strip()
    if len(term) < _FUZZY_MIN_LEN:
        return []
    chars = list(term)
    n = len(chars)
    variants: list[str] = []

    def _join(prefix: list[str], suffix: list[str], wildcard: bool) -> str:
        return _escape_like("".join(prefix)) + ("_" if wildcard else "") + _escape_like("".join(suffix))

    for i in range(n):                       # substitution
        variants.append(_join(chars[:i], chars[i + 1:], True))
    for i in range(n):                       # deletion
        variants.append(_join(chars[:i], chars[i + 1:], False))
    for i in range(n + 1):                   # insertion
        variants.append(_join(chars[:i], chars[i:], True))

    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= _FUZZY_MAX_PATTERNS:
            break
    return out


def _build_doc_search_predicate(search: str, alias: str = "") -> tuple[str, list]:
    """WHERE fragment OR-ing every way ``search`` might match a document.

    The receipt/"match receipt" search mirrors the transaction search's
    exhaust-every-criterion contract:

      1. Exact doc id   — ``id = N``               (digits, '#' tolerated)
      2. Text substring — vendor, filename, currency, date
      3. Near-miss text — vendor/filename within one character of the query
      4. Exact amount   — ``ABS(total) = N``       (money-parseable input)

    ``alias`` prefixes every column (e.g. "d." → "d.total") for queries that
    join the documents table under an alias. Unlike transactions.amount,
    documents.total is nullable and extraction-sourced, so its numeric arm is
    regex-guarded: a non-numeric/NULL total evaluates to false instead of
    aborting the whole query with a cast error.
    """
    a = alias
    safe = _escape_like(search)
    like_param = f"%{safe}%"
    clauses: list[str] = []
    params: list = []

    doc_id = _search_id(search)
    if doc_id is not None:
        clauses.append(f"{a}id = %s")
        params.append(doc_id)

    for col in _DOC_SEARCH_COLUMNS:
        clauses.append(f"{a}{col} ILIKE %s ESCAPE '\\'")
        params.append(like_param)

    # Only the free-text, extraction-sourced columns get the fuzzy arm —
    # currency and date are structured and a near-miss there is noise.
    for pattern in _one_edit_patterns(search):
        for col in _FUZZY_COLUMNS:
            clauses.append(f"{a}{col} ILIKE %s ESCAPE '\\'")
            params.append(f"%{pattern}%")

    amount = _search_amount(search)
    if amount is not None:
        clauses.append(
            f"(CASE WHEN {a}total ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN ABS(CAST({a}total AS NUMERIC)) = %s ELSE false END)"
        )
        params.append(amount)

    return "(" + " OR ".join(clauses) + ")", params


class DatabasePg:
    """Multi-tenant PostgreSQL database layer with connection pooling.

    Every instance is bound to a single user_id. All queries are scoped
    to that user, providing application-level tenant isolation on top of
    the RLS policies defined in schema_pg.sql.
    """

    def __init__(self, pool: psycopg2.pool.ThreadedConnectionPool, user_id: str) -> None:
        self._pool = pool
        self.user_id = user_id

    @contextmanager
    def _conn(self) -> Generator:
        """Acquire a connection from the pool, yield it, then return it.

        Defense-in-depth: PostgreSQL RLS is enforced **in addition to** the
        application-layer ``WHERE user_id = %s`` in every query. This method
        arms RLS by, in order:

            1. Storing the JWT claims (`request.jwt.claims`, `.sub`) as
               transaction-local GUCs so the ``auth.uid()`` SQL function
               returns ``self.user_id``.
            2. Switching the session role to ``authenticated`` via
               ``SET LOCAL ROLE``. This downgrades the connection from the
               pool's superuser-capable role for the lifetime of the
               transaction, so every read + write is subject to the RLS
               policies defined in ``db/schema_pg.sql``.

        If ``SET LOCAL ROLE authenticated`` fails (e.g. the pool DSN belongs
        to a role that can't become ``authenticated``), the transaction is
        rolled back and the exception propagates: it fails closed rather than
        proceeding as a privileged role. The only code paths that intentionally
        bypass this context are the unauthenticated public reads - the share-link
        and OAuth-callback lookups - which have no signed-in user to scope to.

        Any change that removes or weakens the ``SET LOCAL ROLE`` line breaks
        multi-tenant isolation — covered by the regression test in
        ``tests/test_rls_enforcement.py``.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                # Tell RLS who the current user is.  set_config
                # with is_local=true scopes the value to this transaction.
                claims = json.dumps({"sub": self.user_id, "role": "authenticated"})
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (claims,),
                )
                cur.execute(
                    "SELECT set_config('request.jwt.claim.sub', %s, true)",
                    (self.user_id,),
                )
                # Switch to the authenticated role so RLS policies apply
                # using the claims just set.
                cur.execute("SET LOCAL ROLE authenticated")
                # Defensive verification: confirm the role actually switched.
                # If it didn't (shouldn't ever happen), fail closed rather than
                # silently run RLS-bypassing queries as the pool's privileged role.
                cur.execute("SELECT current_user")
                effective_role = cur.fetchone()[0]
                if effective_role != "authenticated":
                    raise RuntimeError(
                        f"DatabasePg: expected session role 'authenticated' after "
                        f"SET LOCAL ROLE, got {effective_role!r}. Refusing to execute "
                        f"queries because RLS would be bypassed."
                    )
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Reset — empty one account's books
    # ------------------------------------------------------------------

    # Every user-scoped table a reset clears, ordered children-before-parents.
    #
    # Both the order and the completeness are load-bearing. The foreign keys
    # that constrain the order:
    #
    #   ledger_entries              → reconciliation_matches   (NO ACTION)
    #   reconciliation_matches      → transactions, documents  (NO ACTION)
    #   transaction_links           → transactions ×2          (NO ACTION)
    #   note_attachments            → transactions             (CASCADE)
    #   reconciliation_adjudications→ transactions, documents  (CASCADE)
    #   extraction_log              → documents                (CASCADE)
    #   transactions                → statements               (SET NULL)
    #
    # NO ACTION blocks the parent's delete, so a child left off this list makes
    # the whole reset fail. The CASCADE ones would not block it, but they are
    # this account's rows and their counts belong in the answer, so they are
    # deleted explicitly rather than swept away. `statements` comes after
    # `transactions` because the dependency runs that way.
    RESET_TABLES: tuple[str, ...] = (
        # 1. Rows that point at matches.
        "ledger_entries",
        "audit_log",
        # 2. Rows that point at transactions or documents.
        "reconciliation_matches",
        "transaction_links",
        "note_attachments",
        "reconciliation_adjudications",
        "extraction_log",
        # 3. The two parents.
        "documents",
        "transactions",
        # 4. What transactions pointed at, plus everything independent.
        "statements",
        "processing_jobs",
        "categorization_run",
        "processed_files",
        "vendor_rules",
        "vendor_category_cache",
        "api_calls",
        "gather_log",
        "gather_sources",
        "scan_runs",
        "connection_health",
        "onboarding_steps",
        "business_profile",
        "shared_reports",
        "month_status",
        "reasonableness_dismissed_flags",
        "gmail_oauth_states",
    )

    def reset_all_tables(self) -> dict[str, int]:
        """Delete every row this user owns, in dependency order, atomically.

        One transaction: either the account is empty afterwards or nothing was
        deleted at all. A failure raises with the table named rather than being
        recorded as a count of zero, because a reset that reports success and
        deletes nothing is worse than one that fails.

        A table that does not exist in this database is skipped (it is checked
        against the catalog first, not discovered by catching an error), so a
        checkout that has not applied every migration is still resettable.

        Returns {table: deleted_count} for the tables that were present.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "AND table_name = ANY(%s)",
                    (list(self.RESET_TABLES),),
                )
                present = {r[0] for r in cur.fetchall()}

                counts: dict[str, int] = {}
                for table in self.RESET_TABLES:
                    if table not in present:
                        continue
                    try:
                        cur.execute(
                            f"DELETE FROM {table} WHERE user_id = %s",  # noqa: S608
                            (self.user_id,),
                        )
                        counts[table] = cur.rowcount
                    except Exception as exc:
                        # No savepoint, no swallow. The whole reset rolls back
                        # (``_conn`` does that on the way out) and the caller
                        # learns which table stopped it.
                        raise RuntimeError(
                            f"reset failed on table {table!r}: {exc}. Nothing was "
                            f"deleted — the reset is atomic."
                        ) from exc

                # The account must actually be empty of the things a reset is
                # about. Checked inside the same transaction, so a foreign key
                # that silently kept rows alive fails the reset instead of
                # being reported as success.
                for table in ("transactions", "documents", "reconciliation_matches"):
                    if table not in present:
                        continue
                    cur.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE user_id = %s",  # noqa: S608
                        (self.user_id,),
                    )
                    remaining = cur.fetchone()[0]
                    if remaining:
                        raise RuntimeError(
                            f"reset left {remaining} row(s) in {table!r}. Nothing "
                            f"was deleted — the reset is atomic."
                        )
        return counts

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def insert_transaction(
        self,
        txn: Transaction,
        *,
        institution: str | None = None,
        account_type: str | None = None,
        external_id: str | None = None,
        import_confidence: str | None = None,
        import_flags: list[str] | None = None,
        statement_id: str | None = None,
    ) -> int:
        # Guard: account may be an AccountType enum (legacy) or a plain string (new).
        account_val = txn.account.value if hasattr(txn.account, "value") else (txn.account or "")
        status_val = txn.status.value if hasattr(txn.status, "value") else (txn.status or "UNMATCHED")
        # Prefer explicit kwargs; fall back to fields set on the model itself.
        eff_institution = institution if institution is not None else txn.institution
        eff_account_type = account_type if account_type is not None else txn.account_type
        eff_external_id = external_id if external_id is not None else txn.external_id
        eff_import_confidence = import_confidence if import_confidence is not None else (
            txn.import_confidence.value if txn.import_confidence is not None else None
        )
        eff_import_flags = import_flags if import_flags is not None else txn.import_flags
        eff_statement_id = statement_id if statement_id is not None else txn.statement_id

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO transactions
                       (user_id, account, transaction_type, date_posted, amount,
                        currency, description, source_file, source_row, status,
                        institution, account_type, external_id,
                        import_confidence, import_flags, statement_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id,
                        account_val,
                        txn.transaction_type,
                        txn.date_posted.isoformat(),
                        str(txn.amount),
                        txn.currency,
                        txn.description,
                        txn.source_file,
                        txn.source_row,
                        status_val,
                        eff_institution,
                        eff_account_type,
                        eff_external_id,
                        eff_import_confidence,
                        json.dumps(eff_import_flags) if eff_import_flags else "[]",
                        eff_statement_id,
                    ),
                )
                return cur.fetchone()[0]

    def update_transaction_status(self, txn_id: int, status: TransactionStatus | str) -> None:
        status_val = status.value if hasattr(status, "value") else status
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE transactions SET status = %s WHERE id = %s AND user_id = %s",
                    (status_val, txn_id, self.user_id),
                )

    def count_transactions_by_status(self, status: str) -> int:
        """Return the count of transactions with a given status for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM transactions WHERE user_id = %s AND status = %s",
                    (self.user_id, status),
                )
                return cur.fetchone()[0]

    def get_unmatched_transactions(
        self,
        since: str | None = None,
        until: str | None = None,
        source_file: str | None = None,
    ) -> list[Transaction]:
        """Return UNMATCHED transactions for the tenant, optionally date-bounded.

        `since` / `until` are inclusive ISO `YYYY-MM-DD` strings compared lexically
        against `date_posted` (which is stored as ISO text, so string ordering is
        date ordering). Both default to None for backwards-compatible behaviour.
        Reconciliation runs that scope to a statement period pass both; legacy
        callers that want the full pool pass neither.

        `source_file` scopes the pool to a single statement tab (e.g. the
        "BMO CAD · May 2026" bubble). When supplied, only transactions belonging
        to that statement are returned — the per-statement reconcile shortlists
        transactions to one statement while still matching against the full
        unmatched-receipt pool.
        """
        clauses = ["user_id = %s", "status = 'UNMATCHED'"]
        params: list[object] = [self.user_id]
        if since is not None:
            clauses.append("date_posted >= %s")
            params.append(since)
        if until is not None:
            clauses.append("date_posted <= %s")
            params.append(until)
        if source_file is not None:
            clauses.append("source_file = %s")
            params.append(source_file)
        sql = (
            "SELECT id, account, transaction_type, date_posted, amount, "
            "currency, description, source_file, source_row, status, "
            "created_at, category, note FROM transactions WHERE "
            + " AND ".join(clauses)
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    def get_linkable_transactions(self) -> list[Transaction]:
        """Every row the deterministic transfer linker may still pair up.

        Deliberately NOT ``get_unmatched_transactions`` semantics, and
        deliberately not date- or statement-scoped:

        * **IGNORED counts.** A row is stamped IGNORED the moment the rule
          pre-pass recognises it as self-evident — and "Credit card payment" is
          one of those rules, so both legs of a card payment are IGNORED before
          the linker ever sees them. Feeding the linker only UNMATCHED rows
          would take the first statement's leg out of the pool, and the second
          statement's run could then never find a counterpart for it.
        * **The whole book, not the statement.** A transfer's two legs live on
          two different statements by definition; scoping the pool to the
          statement being reconciled guarantees at most one leg is present.
        * **Rows that already have an active link are out.** One link per
          transaction (fan-out groups are grown through the explicit group
          verbs, not by re-linking here). A USER_REJECTED link row does not
          count — a rejected pair frees both rows to link to something else,
          and the rejected pair itself is remembered separately by
          :meth:`get_user_rejected_link_pairs`.
        * **MATCHED / LINKED rows are out** — they are already explained.

        Ordered by (date, id) so a run's output depends on the books, not on the
        order statements happened to be imported in.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT t.id, t.account, t.transaction_type, t.date_posted,
                              t.amount, t.currency, t.description, t.source_file,
                              t.source_row, t.status, t.created_at, t.category, t.note
                         FROM transactions t
                        WHERE t.user_id = %s
                          AND t.status IN ('UNMATCHED', 'IGNORED')
                          AND NOT EXISTS (
                              SELECT 1 FROM transaction_links l
                               WHERE l.user_id = t.user_id
                                 AND l.status <> 'USER_REJECTED'
                                 AND (l.source_transaction_id = t.id
                                      OR l.target_transaction_id = t.id)
                          )
                        ORDER BY t.date_posted, t.id""",
                    (self.user_id,),
                )
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    def get_user_rejected_link_pairs(self) -> set[tuple[int, int]]:
        """Transaction pairs the user has rejected as a link, direction-free.

        A rejection is remembered as a surviving ``transaction_links`` row with
        status ``USER_REJECTED`` (``update_link_status``). The linker must never
        propose that pair again, in either direction — so each pair is returned
        with its ids in ascending order and compared the same way.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source_transaction_id, target_transaction_id
                         FROM transaction_links
                        WHERE user_id = %s AND status = 'USER_REJECTED'""",
                    (self.user_id,),
                )
                return {
                    (a, b) if a <= b else (b, a)
                    for a, b in cur.fetchall()
                    if a is not None and b is not None
                }

    def get_non_matched_transactions(self) -> list[Transaction]:
        """Fetch all transactions whose status is anything other than MATCHED.

        Legacy method — prefer get_all_transactions_for_linking() which
        returns ALL transactions regardless of status.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, account, transaction_type, date_posted, amount,
                              currency, description, source_file, source_row,
                              status, created_at, category, note
                       FROM transactions
                       WHERE user_id = %s AND (status IS NULL OR status != 'MATCHED')""",
                    (self.user_id,),
                )
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    def get_all_transactions_for_linking(self) -> list[Transaction]:
        """Fetch ALL transactions regardless of status for 4th pass linking.

        Linkage is independent of matching — a MATCHED transaction can still
        have a cross-statement linkage (e.g. CC payment appears both as a
        bank debit and a credit card credit, both matched to receipts).
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, account, transaction_type, date_posted, amount,
                              currency, description, source_file, source_row,
                              status, created_at, category, note
                       FROM transactions
                       WHERE user_id = %s""",
                    (self.user_id,),
                )
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    def get_actively_linked_transaction_ids(self) -> set[int]:
        """Return all transaction IDs that participate in an active link.

        Used by the 4th pass linker to skip transactions that already have
        a 1:1 linkage (only one link per transaction is allowed).
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source_transaction_id FROM transaction_links
                       WHERE user_id = %s AND status != 'USER_REJECTED'
                       UNION
                       SELECT target_transaction_id FROM transaction_links
                       WHERE user_id = %s AND status != 'USER_REJECTED'""",
                    (self.user_id, self.user_id),
                )
                return {row[0] for row in cur.fetchall()}

    def get_transactions_by_month(self, year: int, month: int) -> list[Transaction]:
        """Rows for a month, for every reporting surface.

        This single query feeds the month report, the PDF, the audit-binder ZIP,
        the XLSX ledger and the QBO/Xero exports. EXCLUDED rows (rejected at
        import as not-real) are dropped here so no phantom row can reach a CRA
        artifact. IGNORED rows are kept: they are real money that simply needs
        no receipt — see core.transfer_exclusion.NON_COUNTABLE_STATUSES.
        """
        from core.transfer_exclusion import countable_sql

        countable, cparams = countable_sql(alias="transactions")
        pattern = f"{year:04d}-{month:02d}-%"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT id, account, transaction_type, date_posted, amount,
                               currency, description, source_file, source_row,
                               status, created_at, category, note
                        FROM transactions
                        WHERE user_id = %s AND date_posted LIKE %s
                          AND {countable}""",
                    (self.user_id, pattern, *cparams),
                )
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_transaction(row: tuple) -> Transaction:
        # account: try to coerce to AccountType enum for backward compat;
        # fall through to plain string for new institution-agnostic rows.
        account_raw = row[1]
        try:
            account_val = AccountType(account_raw)
        except ValueError:
            account_val = account_raw or ""

        # status: try enum; fall back gracefully.
        status_raw = row[9]
        try:
            status_val = TransactionStatus(status_raw)
        except ValueError:
            status_val = TransactionStatus.UNMATCHED

        return Transaction(
            id=row[0],
            account=account_val,
            transaction_type=row[2],
            date_posted=date.fromisoformat(row[3]) if isinstance(row[3], str) else row[3],
            amount=Decimal(row[4]),
            currency=row[5],
            description=row[6],
            source_file=row[7],
            source_row=row[8],
            status=status_val,
            created_at=_datetime_or_none(row[10]),
            category=row[11] if len(row) > 11 else None,
            note=row[12] if len(row) > 12 else None,
        )

    @staticmethod
    def _row_to_transaction_extended(row: tuple) -> Transaction:
        """Deserialize a 19-column SELECT that includes the migration-009 columns.

        Column order (0-based):
          0  id
          1  account
          2  transaction_type
          3  date_posted
          4  amount
          5  currency
          6  description
          7  source_file
          8  source_row
          9  status
          10 created_at
          11 category
          12 note
          13 institution
          14 account_type
          15 external_id
          16 import_confidence
          17 import_flags   (JSONB — psycopg2 returns as list/dict)
          18 statement_id
        """
        account_raw = row[1]
        try:
            account_val = AccountType(account_raw)
        except ValueError:
            account_val = account_raw or ""

        status_raw = row[9]
        try:
            status_val = TransactionStatus(status_raw)
        except ValueError:
            status_val = TransactionStatus.UNMATCHED

        conf_raw = row[16] if len(row) > 16 else None
        try:
            import_confidence_val = ImportConfidence(conf_raw) if conf_raw else None
        except ValueError:
            import_confidence_val = None

        raw_flags = row[17] if len(row) > 17 else None
        if isinstance(raw_flags, list):
            flags_val: list[str] = raw_flags
        elif isinstance(raw_flags, str):
            try:
                flags_val = json.loads(raw_flags)
            except (ValueError, TypeError):
                flags_val = []
        else:
            flags_val = []

        return Transaction(
            id=row[0],
            account=account_val,
            transaction_type=row[2],
            date_posted=date.fromisoformat(row[3]) if isinstance(row[3], str) else row[3],
            amount=Decimal(row[4]),
            currency=row[5],
            description=row[6],
            source_file=row[7],
            source_row=row[8],
            status=status_val,
            created_at=_datetime_or_none(row[10]),
            category=row[11] if len(row) > 11 else None,
            note=row[12] if len(row) > 12 else None,
            institution=row[13] if len(row) > 13 else None,
            account_type=row[14] if len(row) > 14 else None,
            external_id=row[15] if len(row) > 15 else None,
            import_confidence=import_confidence_val,
            import_flags=flags_val,
            statement_id=str(row[18]) if len(row) > 18 and row[18] is not None else None,
        )

    def update_transaction_note(self, transaction_id: int, note: str | None) -> bool:
        """Update the note on a transaction. Returns True if found and updated."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions SET note = %s
                       WHERE id = %s AND user_id = %s""",
                    (note, transaction_id, self.user_id),
                )
                return cur.rowcount > 0

    def update_transaction_category(self, transaction_id: int, category: str | None) -> bool:
        """Update the category on a transaction. Returns True if found and updated."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions SET category = %s
                       WHERE id = %s AND user_id = %s""",
                    (category, transaction_id, self.user_id),
                )
                return cur.rowcount > 0

    def clear_all_transaction_categories(self) -> int:
        """Wipe category + vendor_key for every transaction belonging to this user.

        Rows where ``category_confirmed_by_user = TRUE`` are preserved —
        those are hard-locked user decisions that the categorization agent
        must never overwrite.

        ``vendor_key`` is also cleared so a full recategorize rebuilds vendor
        clusters from scratch. Without this, a stale vendor_key from a prior
        run (with older, less-precise normalization rules) would short-circuit
        the clustering phase and lock in the previous — potentially wrong —
        grouping forever.

        Returns the number of rows cleared.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions
                       SET category = NULL,
                           category_source = NULL,
                           category_confidence = NULL,
                           category_reasoning = NULL,
                           vendor_key = NULL
                       WHERE user_id = %s
                         AND COALESCE(category_confirmed_by_user, FALSE) = FALSE""",
                    (self.user_id,),
                )
                return cur.rowcount

    def get_transaction_date_range(self) -> tuple[date | None, date | None]:
        """Return (earliest_date, latest_date) across all user transactions."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MIN(date_posted), MAX(date_posted)
                       FROM transactions
                       WHERE user_id = %s""",
                    (self.user_id,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return (None, None)
                min_date = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
                max_date = row[1] if isinstance(row[1], date) else date.fromisoformat(str(row[1]))
                return (min_date, max_date)

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def insert_document(self, doc: Document) -> int:
        line_items_json = None
        if doc.line_items is not None:
            line_items_json = json.dumps([li.model_dump(mode="json") for li in doc.line_items])

        # Handle new optional fields (field_confidence, review_status)
        field_confidence_json = None
        if hasattr(doc, "field_confidence") and doc.field_confidence is not None:
            field_confidence_json = json.dumps(doc.field_confidence)
        review_status = getattr(doc, "review_status", "pending") or "pending"

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO documents
                       (user_id, original_filename, stored_path, file_hash, vendor,
                        document_date, currency, subtotal, tax_gst, tax_hst,
                        tax_pst, tax_other, total, payment_method, invoice_number,
                        line_items_json, extraction_confidence, extraction_raw_json,
                        status, field_confidence, review_status,
                        currency_source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s)
                       ON CONFLICT (user_id, file_hash) DO UPDATE SET
                           updated_at = NOW()
                       RETURNING id""",
                    (
                        self.user_id,
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
                        field_confidence_json,
                        review_status,
                        getattr(doc, "currency_source", None),
                    ),
                )
                return cur.fetchone()[0]

    def get_unmatched_documents(
        self,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Document]:
        """Return EXTRACTED documents for the tenant, optionally date-bounded.

        `since` / `until` are inclusive ISO `YYYY-MM-DD` strings compared lexically
        against `document_date`. Documents with NULL `document_date` are included
        only when no bounds are supplied — date-scoped reconciliation runs
        implicitly require a dated document. Both default to None for
        backwards-compatible behaviour.
        """
        # DISTINCT ON file_hash keeps only one record per unique file,
        # preferring the earliest upload (smallest id).  This prevents
        # duplicate receipts from inflating the reconciliation candidate
        # list when the same file was uploaded more than once.
        clauses = ["user_id = %s", "status = 'EXTRACTED'"]
        params: list[object] = [self.user_id]
        if since is not None:
            clauses.append("document_date >= %s")
            params.append(since)
        if until is not None:
            clauses.append("document_date <= %s")
            params.append(until)
        sql = (
            "SELECT DISTINCT ON (file_hash) "
            "id, original_filename, stored_path, file_hash, vendor, "
            "document_date, currency, subtotal, tax_gst, tax_hst, "
            "tax_pst, tax_other, total, payment_method, invoice_number, "
            "line_items_json, extraction_confidence, extraction_raw_json, "
            "status, created_at, currency_source "
            "FROM documents WHERE "
            + " AND ".join(clauses)
            + " ORDER BY file_hash, id"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [self._row_to_document(row) for row in cur.fetchall()]

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
            document_date=_date_or_none(row[5]) if isinstance(row[5], str) else row[5],
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
            # Appended at the end so a shorter legacy SELECT still maps: a row
            # without it resolves as a pre-provenance row, which is correct.
            currency_source=row[20] if len(row) > 20 else None,
        )

    # ------------------------------------------------------------------
    # Reconciliation matches
    # ------------------------------------------------------------------

    def insert_match(self, match: ReconciliationMatch) -> int:
        explanation = getattr(match, "explanation", None)
        group_id = getattr(match, "group_id", None)
        covered_amount = getattr(match, "covered_amount", None)
        group_id_expr = "%s::uuid" if group_id else "uuid_generate_v4()"

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO reconciliation_matches
                       (user_id, transaction_id, document_id, confidence_score,
                        amount_score, date_score, vendor_score, match_type, status,
                        explanation, covered_amount, group_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {group_id_expr})
                       RETURNING id""",
                    (
                        self.user_id,
                        match.transaction_id,
                        match.document_id,
                        match.confidence_score,
                        match.amount_score,
                        match.date_score,
                        match.vendor_score,
                        match.match_type.value,
                        match.status.value,
                        explanation,
                        covered_amount,
                    ) + ((group_id,) if group_id else ()),
                )
                return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Manual matching
    # ------------------------------------------------------------------

    def create_manual_match(
        self,
        transaction_id: int,
        document_id: int,
        confidence_score: float,
        amount_score: float,
        date_score: float,
        vendor_score: float,
        explanation: str | None = None,
        match_type: str = "ONE_TO_ONE",
    ) -> dict:
        """Create a manual match atomically: insert match + update txn + update doc + audit log.

        When `match_type='ONE_TO_MANY'` (split payment — one receipt covers
        multiple transactions), the document's status is left unchanged so it
        remains splittable across additional transactions. The schema's
        idx_matches_doc_one_to_one partial unique index permits ONE_TO_MANY
        rows to share a document even when an existing match exists.
        """
        if match_type not in ("ONE_TO_ONE", "ONE_TO_MANY"):
            raise ValueError(f"Unsupported match_type: {match_type}")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO reconciliation_matches
                       (user_id, transaction_id, document_id, confidence_score,
                        amount_score, date_score, vendor_score, match_type, status,
                        match_source, reviewed_by, reviewed_at, user_action, actioned_at, explanation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'USER_APPROVED',
                               'MANUAL', %s, NOW(), 'manual_match', NOW(), %s)
                       RETURNING id""",
                    (self.user_id, transaction_id, document_id,
                     confidence_score, amount_score, date_score, vendor_score,
                     match_type, self.user_id, explanation),
                )
                match_id = cur.fetchone()[0]

                # Both sides are derived from the row just inserted rather than
                # asserted: the USER_APPROVED match makes them MATCHED, and if
                # this transaction is IGNORED or EXCLUDED that human statement
                # about the row survives (see _resync_transaction_statuses).
                # ONE_TO_MANY splits need no special case any more — the
                # document is MATCHED because a match points at it.
                self._resync_transaction_statuses(cur, [transaction_id])
                self._resync_document_statuses(cur, [document_id])
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, 'match', %s, 'MANUAL_MATCH', NULL, %s, %s)""",
                    (self.user_id, match_id,
                     json.dumps({"transaction_id": transaction_id,
                                 "document_id": document_id,
                                 "confidence_score": confidence_score,
                                 "match_type": match_type}),
                     self.user_id),
                )

        return {"match_id": match_id, "confidence_score": confidence_score,
                "amount_score": amount_score, "date_score": date_score,
                "vendor_score": vendor_score, "match_type": match_type}

    def reassign_match(
        self,
        match_id: int,
        new_document_id: int,
        confidence_score: float,
        amount_score: float,
        date_score: float,
        vendor_score: float,
        explanation: str | None = None,
    ) -> dict:
        """Atomically unmatch old match and create new match to different document."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Get old match details
                cur.execute(
                    """SELECT transaction_id, document_id, status
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (match_id, self.user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Match not found")
                old_txn_id, old_doc_id, old_status = row

                if old_status == "USER_REJECTED":
                    raise ValueError("Match is already rejected")

                # Reject old match
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_REJECTED', user_action = 'reassigned',
                           actioned_at = NOW(), reviewed_by = %s, reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (self.user_id, match_id, self.user_id),
                )

                # Insert new match
                cur.execute(
                    """INSERT INTO reconciliation_matches
                       (user_id, transaction_id, document_id, confidence_score,
                        amount_score, date_score, vendor_score, match_type, status,
                        match_source, reviewed_by, reviewed_at, user_action, actioned_at, explanation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 'ONE_TO_ONE', 'USER_APPROVED',
                               'MANUAL', %s, NOW(), 'manual_match', NOW(), %s)
                       RETURNING id""",
                    (self.user_id, old_txn_id, new_document_id,
                     confidence_score, amount_score, date_score, vendor_score,
                     self.user_id, explanation),
                )
                new_match_id = cur.fetchone()[0]

                # Re-derive all three rows the reassign touched: the old
                # receipt is released only if nothing else still proves with
                # it (a split receipt may prove another transaction), the new
                # receipt becomes MATCHED, and the transaction is re-derived
                # rather than assumed to stay MATCHED.
                self._resync_document_statuses(cur, [old_doc_id, new_document_id])
                self._resync_transaction_statuses(cur, [old_txn_id])

                # Audit log
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, 'match', %s, 'REASSIGN', %s, %s, %s)""",
                    (self.user_id, new_match_id,
                     json.dumps({"old_match_id": match_id, "old_document_id": old_doc_id}),
                     json.dumps({"new_document_id": new_document_id,
                                 "confidence_score": confidence_score}),
                     self.user_id),
                )

        return {"old_match_id": match_id, "new_match_id": new_match_id,
                "transaction_id": old_txn_id}

    # ------------------------------------------------------------------
    # Multi-proof matching
    # ------------------------------------------------------------------

    def add_proof_to_transaction(
        self,
        txn_id: int,
        doc_id: int,
        match_type: str,
        confidence_score: float,
        amount_score: float,
        date_score: float,
        vendor_score: float,
        group_id: str | None = None,
        covered_amount: str | None = None,
        explanation: str | None = None,
        match_source: str = "MANUAL",
    ) -> dict:
        """Add a proof document to a transaction as an additional match row.

        Returns {"proof_id": int, "group_id": str, "transaction_id": int,
                 "document_id": int, "covered_amount": str | None}
        """
        status = "USER_APPROVED" if match_source == "MANUAL" else "PENDING_REVIEW"
        group_id_expr = "%s::uuid" if group_id else "uuid_generate_v4()"

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO reconciliation_matches
                       (user_id, transaction_id, document_id, confidence_score,
                        amount_score, date_score, vendor_score, match_type, status,
                        match_source, reviewed_by, reviewed_at, user_action, actioned_at,
                        covered_amount, explanation, group_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, NOW(), 'add_proof', NOW(),
                               %s, %s, {group_id_expr})
                       RETURNING id, group_id""",
                    (
                        self.user_id, txn_id, doc_id,
                        confidence_score, amount_score, date_score, vendor_score,
                        match_type, status, match_source,
                        self.user_id,
                        covered_amount, explanation,
                    ) + ((group_id,) if group_id else ()),
                )
                proof_id, resolved_group_id = cur.fetchone()

                self._resync_transaction_statuses(cur, [txn_id])
                self._resync_document_statuses(cur, [doc_id])

                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, new_value, performed_by)
                       VALUES (%s, 'match', %s, 'ADD_PROOF', %s, %s)""",
                    (
                        self.user_id, proof_id,
                        json.dumps({
                            "transaction_id": txn_id, "document_id": doc_id,
                            "group_id": str(resolved_group_id),
                            "covered_amount": covered_amount,
                        }),
                        self.user_id,
                    ),
                )

        return {
            "proof_id": proof_id,
            "group_id": str(resolved_group_id),
            "transaction_id": txn_id,
            "document_id": doc_id,
            "covered_amount": covered_amount,
        }

    def remove_proof(self, proof_id: int) -> dict:
        """Soft-delete a proof by setting status to USER_REJECTED.

        Returns {"proof_id": int, "transaction_id": int, "document_id": int,
                 "proofs_remaining": int, "transaction_reverted": bool}
        Raises ValueError if proof not found or already removed.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT transaction_id, document_id, status
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (proof_id, self.user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Proof {proof_id} not found")
                txn_id, doc_id, current_status = row
                if current_status == "USER_REJECTED":
                    raise ValueError(f"Proof {proof_id} is already removed")

                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_REJECTED', user_action = 'remove_proof',
                           actioned_at = NOW(), reviewed_by = %s, reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (self.user_id, proof_id, self.user_id),
                )

                # Release the receipt only if nothing else still proves with it
                # — a split receipt can cover a second transaction.
                self._resync_document_statuses(cur, [doc_id])

                cur.execute(
                    """SELECT COUNT(*) FROM reconciliation_matches
                       WHERE user_id = %s AND transaction_id = %s
                         AND status != 'USER_REJECTED'""",
                    (self.user_id, txn_id),
                )
                proofs_remaining = cur.fetchone()[0]

                # Re-derive the transaction from what survives: no proofs left
                # means UNMATCHED (or LINKED if a cross-statement link still
                # explains it), and the caller is told which way it went.
                self._resync_transaction_statuses(cur, [txn_id])
                cur.execute(
                    "SELECT status FROM transactions WHERE id = %s AND user_id = %s",
                    (txn_id, self.user_id),
                )
                txn_row = cur.fetchone()
                transaction_reverted = bool(txn_row) and txn_row[0] != "MATCHED"

                # If only 1 proof remains in a MANY_TO_ONE bundle, downgrade to ONE_TO_ONE
                if proofs_remaining == 1:
                    cur.execute(
                        """UPDATE reconciliation_matches
                           SET match_type = 'ONE_TO_ONE', group_id = uuid_generate_v4()
                           WHERE user_id = %s AND transaction_id = %s
                             AND status != 'USER_REJECTED'""",
                        (self.user_id, txn_id),
                    )

                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, performed_by)
                       VALUES (%s, 'match', %s, 'REMOVE_PROOF', %s, %s)""",
                    (
                        self.user_id, proof_id,
                        json.dumps({"transaction_id": txn_id, "document_id": doc_id,
                                    "proofs_remaining": proofs_remaining}),
                        self.user_id,
                    ),
                )

        return {
            "proof_id": proof_id, "transaction_id": txn_id,
            "document_id": doc_id, "proofs_remaining": proofs_remaining,
            "transaction_reverted": transaction_reverted,
        }

    def get_transaction_proofs(self, txn_id: int) -> dict:
        """Return all active proofs for a transaction with coverage statistics.

        Returns {"transaction_id": int, "proofs": list[dict], "coverage": dict}
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                           rm.id AS proof_id, rm.document_id, rm.group_id,
                           rm.match_type, rm.status, rm.match_source,
                           rm.confidence_score, rm.amount_score, rm.date_score,
                           rm.vendor_score, rm.covered_amount, rm.explanation,
                           rm.created_at,
                           d.original_filename, d.vendor, d.document_date,
                           d.total, d.currency, d.stored_path
                       FROM reconciliation_matches rm
                       JOIN documents d ON rm.document_id = d.id
                       WHERE rm.user_id = %s AND rm.transaction_id = %s
                         AND rm.status != 'USER_REJECTED'
                       ORDER BY rm.created_at ASC""",
                    (self.user_id, txn_id),
                )
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

        proofs = []
        for row in rows:
            item = dict(zip(cols, row))
            item["group_id"] = str(item["group_id"]) if item["group_id"] else None
            item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
            if item["document_date"]:
                item["document_date"] = str(item["document_date"])
            proofs.append(item)

        from decimal import Decimal, InvalidOperation
        proof_count = len(proofs)
        has_full_coverage = any(p["covered_amount"] is None for p in proofs)
        group_ids = list({p["group_id"] for p in proofs if p["group_id"]})

        total_covered = None
        if not has_full_coverage and proof_count > 0:
            total_covered = Decimal("0")
            for p in proofs:
                if p["covered_amount"] is not None:
                    try:
                        total_covered += Decimal(p["covered_amount"])
                    except InvalidOperation:
                        pass

        return {
            "transaction_id": txn_id,
            "proofs": proofs,
            "coverage": {
                "total_covered": str(total_covered) if total_covered is not None else None,
                "proof_count": proof_count,
                "has_full_coverage": has_full_coverage,
                "group_ids": group_ids,
            },
        }

    def get_available_matches_for_transaction(
        self,
        transaction_id: int,
        search: str | None = None,
        limit: int = 20,
        include_matched: bool = True,
    ) -> list[dict]:
        """Get documents available for manual matching against a transaction.

        Returns unmatched (status='EXTRACTED') documents plus, when
        `include_matched=True`, already-MATCHED documents with their existing
        match count. Already-matched documents are eligible for split-payment
        linking (one receipt covering multiple transactions, ONE_TO_MANY).
        Each row includes:
          - existing_match_count: number of active matches (0 = unmatched)
          - is_already_matched: bool convenience flag

        `search` exhausts every criterion (see doc_search_predicate): the
        document id (exact), vendor/filename/currency/date (substring), and the
        document total (exact dollar value) — so a receipt can be found by its
        amount, not just its name or ID.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Subquery: count active matches per document (excluding the current
                # transaction so re-selecting an already-attached doc is filtered out
                # at the API/UI layer).
                statuses_filter = "('EXTRACTED', 'MATCHED')" if include_matched else "('EXTRACTED')"
                sql = f"""
                    SELECT d.id, d.original_filename, d.vendor, d.document_date,
                           d.total, d.currency, d.extraction_confidence, d.stored_path,
                           d.status,
                           -- What the manual picker needs to resolve the
                           -- receipt's currency (core/document_currency.py):
                           -- a receipt that states nothing is read in the
                           -- transaction's currency, not scored zero.
                           d.currency_source, d.subtotal, d.tax_gst, d.tax_hst,
                           d.tax_pst, d.tax_other,
                           COALESCE((
                               SELECT COUNT(*)
                               FROM reconciliation_matches rm
                               WHERE rm.document_id = d.id
                                 AND rm.user_id = d.user_id
                                 AND rm.status IN ('AUTO_APPROVED','USER_APPROVED','PENDING_REVIEW')
                                 AND rm.transaction_id != %s
                           ), 0) AS existing_match_count
                    FROM documents d
                    WHERE d.user_id = %s AND d.status IN {statuses_filter}
                """
                params: list = [transaction_id, self.user_id]

                if search and search.strip():
                    predicate, p_params = _build_doc_search_predicate(search, alias="d.")
                    sql += " AND " + predicate
                    params.extend(p_params)

                # Unmatched first, then already-matched (visually deprioritised).
                sql += " ORDER BY (d.status = 'EXTRACTED') DESC, d.created_at DESC LIMIT %s"
                params.append(limit)

                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                # A pair the user rejected is still offered here — the picker
                # is the one place a human may overrule their own earlier
                # decision — but it is labelled, never silently re-suggested.
                # Same cursor, so no second pooled connection.
                rejected_doc_ids = self._rejected_document_ids(cur, transaction_id)
                for r in rows:
                    r["existing_match_count"] = int(r.get("existing_match_count") or 0)
                    r["is_already_matched"] = r["existing_match_count"] > 0
                    r["previously_rejected"] = int(r["id"]) in rejected_doc_ids
                return rows

    def get_matched_document_info(self) -> dict[int, list[dict]]:
        """Return dict mapping transaction_id -> list of active match info dicts.

        Supports multi-proof: a transaction can have multiple matched documents.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rm.transaction_id, rm.id AS match_id,
                              d.id AS doc_id, d.original_filename,
                              rm.group_id, rm.covered_amount, rm.confidence_score
                       FROM reconciliation_matches rm
                       JOIN documents d ON rm.document_id = d.id
                       WHERE rm.user_id = %s
                         AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED', 'PENDING_REVIEW')
                       ORDER BY rm.transaction_id, rm.created_at ASC""",
                    (self.user_id,),
                )
                result: dict[int, list[dict]] = {}
                for row in cur.fetchall():
                    txn_id = row[0]
                    entry = {
                        "match_id": row[1],
                        "doc_id": row[2],
                        "filename": row[3],
                        "group_id": str(row[4]) if row[4] else None,
                        "covered_amount": row[5],
                        "confidence_score": float(row[6]) if row[6] is not None else None,
                    }
                    result.setdefault(txn_id, []).append(entry)
                return result

    # ------------------------------------------------------------------
    # Transaction links (cross-statement linking)
    # ------------------------------------------------------------------

    @staticmethod
    def _link_status_for(match_source: str, confidence_score: float) -> str:
        """Review status a new link lands in.

        MANUAL is the user's own assertion, so it is approved outright;
        AUTO clears only above the canonical auto-approve threshold
        (config/settings.py RECONCILIATION_AUTO_APPROVE_THRESHOLD) and
        otherwise queues for human review.
        """
        if match_source == "MANUAL":
            return "USER_APPROVED"
        if confidence_score >= _recon_cfg.auto_approve_threshold:
            return "AUTO_APPROVED"
        return "PENDING_REVIEW"

    def _insert_link(
        self,
        cur,
        source_txn_id: int,
        target_txn_id: int,
        link_type: str,
        confidence_score: float,
        status: str,
        explanation: str | None,
        match_source: str,
        chain_id: str | None,
        chain_position: int,
        exchange_rate: str | None,
        amount_variance: str | None,
    ) -> tuple[int, str]:
        """Insert one link row + flip both sides to LINKED + audit, on ``cur``.

        Cursor-scoped so a whole link GROUP can be written inside a single
        transaction — a half-persisted refund group would misstate the books.
        Returns (link_id, chain_id).
        """
        cur.execute(
            """INSERT INTO transaction_links
                (user_id, source_transaction_id, target_transaction_id,
                 link_type, confidence_score, explanation, match_source,
                 chain_id, chain_position, exchange_rate, amount_variance, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::uuid, uuid_generate_v4()), %s, %s, %s, %s)
                RETURNING id, chain_id""",
            (
                self.user_id, source_txn_id, target_txn_id,
                link_type, confidence_score, explanation, match_source,
                chain_id, chain_position, exchange_rate, amount_variance, status,
            ),
        )
        link_id, resolved_chain_id = cur.fetchone()

        # Both sides become LINKED (only from UNMATCHED or IGNORED — never
        # downgrade a MATCHED transaction that already has receipt proof).
        for txn_id in (source_txn_id, target_txn_id):
            cur.execute(
                """UPDATE transactions SET status = 'LINKED'
                   WHERE id = %s AND user_id = %s
                     AND status IN ('UNMATCHED', 'IGNORED')""",
                (txn_id, self.user_id),
            )

        cur.execute(
            """INSERT INTO audit_log
               (user_id, entity_type, entity_id, action, new_value, performed_by)
               VALUES (%s, 'transaction_link', %s, 'LINK', %s, %s)""",
            (self.user_id, link_id,
             json.dumps({"source_txn_id": source_txn_id,
                         "target_txn_id": target_txn_id,
                         "link_type": link_type,
                         "confidence_score": confidence_score}),
             self.user_id),
        )
        return link_id, str(resolved_chain_id)

    def create_transaction_link(
        self,
        source_txn_id: int,
        target_txn_id: int,
        link_type: str,
        confidence_score: float,
        explanation: str | None = None,
        match_source: str = "AUTO",
        chain_id: str | None = None,
        chain_position: int = 0,
        exchange_rate: str | None = None,
        amount_variance: str | None = None,
    ) -> dict:
        """Create a cross-statement transaction link atomically.

        Inserts the link, updates both transaction statuses to LINKED
        (only if currently UNMATCHED or IGNORED), and logs the action.
        Auto-approves if match_source is MANUAL or confidence >= auto_approve_threshold
        (canonical value from config/settings.py RECONCILIATION_AUTO_APPROVE_THRESHOLD).
        """
        status = self._link_status_for(match_source, confidence_score)

        with self._conn() as conn:
            with conn.cursor() as cur:
                link_id, resolved_chain_id = self._insert_link(
                    cur,
                    source_txn_id=source_txn_id,
                    target_txn_id=target_txn_id,
                    link_type=link_type,
                    confidence_score=confidence_score,
                    status=status,
                    explanation=explanation,
                    match_source=match_source,
                    chain_id=chain_id,
                    chain_position=chain_position,
                    exchange_rate=exchange_rate,
                    amount_variance=amount_variance,
                )

        return {
            "link_id": link_id,
            "chain_id": resolved_chain_id,
            "source_transaction_id": source_txn_id,
            "target_transaction_id": target_txn_id,
            "link_type": link_type,
            "status": status,
        }

    def create_transaction_link_group(
        self,
        source_txn_id: int,
        target_txn_ids: list[int],
        link_type: str,
        confidence_score: float = 1.0,
        explanation: str | None = None,
        match_source: str = "MANUAL",
        chain_id: str | None = None,
        exchange_rate: str | None = None,
        amount_variance: str | None = None,
    ) -> dict:
        """Link one anchor transaction to MANY counterparts in one transaction.

        This is the split-refund shape: a single bulk charge fanned out to every
        credit the merchant issued as it returned items one at a time. All legs
        share one ``chain_id`` so the group can be read back — and unlinked —
        as a unit, and carry ascending ``chain_position`` in the order given.

        Passing a single target is equivalent to :meth:`create_transaction_link`.
        Either every leg is written or none is: a partially-persisted refund
        group would show the user a charge that looks half-refunded.
        """
        if not target_txn_ids:
            raise ValueError("target_txn_ids must not be empty")
        if source_txn_id in target_txn_ids:
            raise ValueError("Cannot link a transaction to itself")
        if len(set(target_txn_ids)) != len(target_txn_ids):
            raise ValueError("target_txn_ids contains duplicates")

        status = self._link_status_for(match_source, confidence_score)
        links: list[dict] = []

        with self._conn() as conn:
            with conn.cursor() as cur:
                resolved_chain_id = chain_id
                for position, target_txn_id in enumerate(target_txn_ids):
                    link_id, resolved_chain_id = self._insert_link(
                        cur,
                        source_txn_id=source_txn_id,
                        target_txn_id=target_txn_id,
                        link_type=link_type,
                        confidence_score=confidence_score,
                        status=status,
                        explanation=explanation,
                        match_source=match_source,
                        # First leg mints the chain_id; the rest join it.
                        chain_id=resolved_chain_id,
                        chain_position=position,
                        exchange_rate=exchange_rate,
                        amount_variance=amount_variance,
                    )
                    links.append({
                        "link_id": link_id,
                        "source_transaction_id": source_txn_id,
                        "target_transaction_id": target_txn_id,
                        "chain_position": position,
                    })

        return {
            "chain_id": resolved_chain_id,
            "link_type": link_type,
            "status": status,
            "source_transaction_id": source_txn_id,
            "links": links,
            "created": len(links),
        }

    def get_transaction_links(self, transaction_id: int) -> list[dict]:
        """Get all active links for a transaction (both as source and target)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT tl.id, tl.link_type, tl.confidence_score, tl.status,
                              tl.match_source, tl.explanation, tl.chain_id,
                              tl.chain_position, tl.exchange_rate, tl.amount_variance,
                              tl.created_at,
                              CASE WHEN tl.source_transaction_id = %s THEN 'source' ELSE 'target' END AS direction,
                              CASE WHEN tl.source_transaction_id = %s
                                   THEN tl.target_transaction_id
                                   ELSE tl.source_transaction_id END AS linked_txn_id,
                              t.account, t.description, t.amount, t.currency,
                              t.date_posted, t.source_file, t.status AS txn_status,
                              t.transaction_type, tl.source_transaction_id
                       FROM transaction_links tl
                       JOIN transactions t ON t.id = CASE
                           WHEN tl.source_transaction_id = %s
                           THEN tl.target_transaction_id
                           ELSE tl.source_transaction_id END
                       WHERE tl.user_id = %s
                         AND (tl.source_transaction_id = %s OR tl.target_transaction_id = %s)
                         AND tl.status != 'USER_REJECTED'
                       ORDER BY tl.created_at ASC""",
                    (transaction_id, transaction_id, transaction_id,
                     self.user_id, transaction_id, transaction_id),
                )
                return [
                    {
                        "id": row[0],
                        "link_type": row[1],
                        "confidence_score": row[2],
                        "status": row[3],
                        "match_source": row[4],
                        "explanation": row[5],
                        "chain_id": str(row[6]) if row[6] else None,
                        "chain_position": row[7],
                        "exchange_rate": row[8],
                        "amount_variance": row[9],
                        "created_at": row[10].isoformat() if row[10] else None,
                        "direction": row[11],
                        "linked_txn_id": row[12],
                        "linked_account": row[13],
                        "linked_description": row[14],
                        "linked_amount": str(row[15]),
                        "linked_currency": row[16],
                        "linked_date": str(row[17]),
                        "linked_source_file": row[18],
                        "linked_status": row[19],
                        "linked_transaction_type": row[20],
                        "anchor_transaction_id": row[21],
                    }
                    for row in cur.fetchall()
                ]

    def get_transaction_link_summary(self, transaction_id: int) -> dict:
        """Links for a transaction plus how much of its amount they offset.

        A split refund is only readable if the user can see the arithmetic:
        a -$480.00 charge with three credits linked to it is 56% refunded with
        $210.00 still standing as expense. ``offset_total`` therefore sums only
        counterparts moving the OPPOSITE way (credits against a debit) and only
        in the transaction's own currency — a US$ leg against a C$ charge is
        real linkage but not a computable offset without an FX rate, so it is
        counted separately rather than silently distorting the ratio.

        Same-direction legs (a debit linked to another debit — an internal
        transfer, a re-billed charge) are counted but never netted.

        The arithmetic only makes sense from the ANCHOR's side, so only links
        this transaction anchors are netted. Read from a leg it would invert:
        the +$120.00 credit would report "$480.00 of $120.00 offset, $0.00
        net" — 400% covered and fully settled, which is nonsense on its face.
        A leg still lists its links; it just doesn't claim to net them.
        """
        links = self.get_transaction_links(transaction_id)

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT amount, currency, transaction_type
                       FROM transactions WHERE id = %s AND user_id = %s""",
                    (transaction_id, self.user_id),
                )
                row = cur.fetchone()
        if not row:
            raise ValueError("Transaction not found")

        txn_amount = abs(Decimal(str(row[0] or 0)))
        txn_currency = (row[1] or "CAD").upper()
        txn_type = row[2]

        offset_total = Decimal("0")
        cross_currency_count = 0
        same_direction_count = 0
        offset_leg_count = 0

        anchored = [
            link for link in links
            if link.get("anchor_transaction_id") == transaction_id
        ]

        for link in anchored:
            leg_currency = (link.get("linked_currency") or txn_currency).upper()
            if leg_currency != txn_currency:
                cross_currency_count += 1
                continue
            if link.get("linked_transaction_type") == txn_type:
                same_direction_count += 1
                continue
            try:
                offset_total += abs(Decimal(str(link["linked_amount"])))
            except (InvalidOperation, TypeError, KeyError):
                continue
            offset_leg_count += 1

        if txn_amount and offset_total:
            offset_ratio = round(float(offset_total / txn_amount), 4)
        elif cross_currency_count and not offset_total:
            # Nothing nettable in-currency — report unknown, not "0% refunded".
            offset_ratio = None
        else:
            offset_ratio = 0.0

        # Clamp at zero: refunds exceeding the original charge (an overpayment
        # reversal, a duplicate credit) leave nothing outstanding, not a
        # negative expense.
        net_remaining = max(txn_amount - offset_total, Decimal("0"))

        chain_ids = list(dict.fromkeys(
            link["chain_id"] for link in links if link.get("chain_id")
        ))

        return {
            "transaction_id": transaction_id,
            "links": links,
            "summary": {
                "link_count": len(links),
                # How many legs actually contributed to offset_total — the
                # banner must attribute the sum to these, not to every link.
                "offset_leg_count": offset_leg_count,
                "chain_ids": chain_ids,
                "transaction_amount": str(txn_amount),
                "transaction_currency": txn_currency,
                "offset_total": str(offset_total),
                "net_remaining": str(net_remaining),
                "offset_ratio": offset_ratio,
                "is_fully_offset": (
                    None if offset_ratio is None else offset_ratio >= 0.99
                ),
                "cross_currency_count": cross_currency_count,
                "same_direction_count": same_direction_count,
            },
        }

    def get_transaction_chain(self, transaction_id: int) -> list[dict]:
        """Walk the link graph from a transaction, returning ordered chain nodes."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH RECURSIVE chain_walk AS (
                        -- Seed: the starting transaction
                        SELECT t.id AS txn_id, 0 AS depth,
                               ARRAY[t.id] AS visited
                        FROM transactions t
                        WHERE t.id = %s AND t.user_id = %s

                        UNION ALL

                        SELECT
                            CASE WHEN tl.source_transaction_id = cw.txn_id
                                 THEN tl.target_transaction_id
                                 ELSE tl.source_transaction_id END AS txn_id,
                            cw.depth + 1,
                            cw.visited || CASE WHEN tl.source_transaction_id = cw.txn_id
                                 THEN tl.target_transaction_id
                                 ELSE tl.source_transaction_id END
                        FROM transaction_links tl
                        JOIN chain_walk cw ON (
                            tl.source_transaction_id = cw.txn_id
                            OR tl.target_transaction_id = cw.txn_id
                        )
                        WHERE tl.user_id = %s
                          AND tl.status != 'USER_REJECTED'
                          AND NOT (CASE WHEN tl.source_transaction_id = cw.txn_id
                                        THEN tl.target_transaction_id
                                        ELSE tl.source_transaction_id END = ANY(cw.visited))
                          AND cw.depth < 20
                    )
                    SELECT DISTINCT ON (cw.txn_id)
                        cw.txn_id, cw.depth,
                        t.account, t.amount, t.currency, t.description,
                        t.date_posted, t.source_file, t.status,
                        EXISTS(SELECT 1 FROM reconciliation_matches rm
                               WHERE rm.transaction_id = cw.txn_id
                                 AND rm.user_id = %s
                                 AND rm.status != 'USER_REJECTED') AS has_doc_match,
                        (SELECT d.original_filename FROM reconciliation_matches rm
                         JOIN documents d ON d.id = rm.document_id
                         WHERE rm.transaction_id = cw.txn_id
                           AND rm.user_id = %s
                           AND rm.status != 'USER_REJECTED'
                         LIMIT 1) AS matched_doc_filename
                    FROM chain_walk cw
                    JOIN transactions t ON t.id = cw.txn_id
                    ORDER BY cw.txn_id, cw.depth ASC""",
                    (transaction_id, self.user_id, self.user_id,
                     self.user_id, self.user_id),
                )
                nodes = []
                for row in cur.fetchall():
                    nodes.append({
                        "transaction_id": row[0],
                        "depth": row[1],
                        "account": row[2],
                        "amount": str(row[3]),
                        "currency": row[4],
                        "description": row[5],
                        "date": str(row[6]),
                        "source_file": row[7],
                        "status": row[8],
                        "has_document_match": row[9],
                        "matched_doc_filename": row[10],
                    })
                # Sort by depth to get chain order
                nodes.sort(key=lambda n: n["depth"])
                return nodes

    def get_unified_proof(self, transaction_id: int) -> list[dict]:
        """Get combined document + transaction link proofs for a transaction."""
        proofs: list[dict] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Document proofs
                cur.execute(
                    """SELECT rm.id, d.original_filename, rm.confidence_score,
                              rm.status, rm.match_source
                       FROM reconciliation_matches rm
                       JOIN documents d ON rm.document_id = d.id
                       WHERE rm.transaction_id = %s AND rm.user_id = %s
                         AND rm.status != 'USER_REJECTED'""",
                    (transaction_id, self.user_id),
                )
                for row in cur.fetchall():
                    proofs.append({
                        "proof_type": "document",
                        "proof_id": row[0],
                        "proof_label": row[1],
                        "confidence_score": row[2],
                        "status": row[3],
                        "match_source": row[4],
                    })

                # Transaction link proofs (both directions)
                cur.execute(
                    """SELECT tl.id, tl.confidence_score, tl.status, tl.match_source,
                              t.account, t.amount, t.currency, t.description
                       FROM transaction_links tl
                       JOIN transactions t ON t.id = CASE
                           WHEN tl.source_transaction_id = %s
                           THEN tl.target_transaction_id
                           ELSE tl.source_transaction_id END
                       WHERE tl.user_id = %s
                         AND (tl.source_transaction_id = %s OR tl.target_transaction_id = %s)
                         AND tl.status != 'USER_REJECTED'""",
                    (transaction_id, self.user_id, transaction_id, transaction_id),
                )
                for row in cur.fetchall():
                    amount_str = str(row[5])
                    proofs.append({
                        "proof_type": "transaction_link",
                        "proof_id": row[0],
                        "proof_label": f"-> {row[4]}: ${amount_str} {row[6]}",
                        "confidence_score": row[1],
                        "status": row[2],
                        "match_source": row[3],
                    })
        return proofs

    def txn_search_predicate(self, search: str) -> tuple[str, list]:
        """All-columns substring+regex search predicate for the transactions table.

        The regex arm is included only when the pattern is valid in BOTH
        Python and Postgres (dialects differ slightly); otherwise the search
        silently degrades to substring-only rather than erroring the query.
        """
        pattern = _regex_candidate(search)
        if pattern is not None and not self._pg_regex_valid(pattern):
            pattern = None
        return _build_txn_search_predicate(search, pattern)

    def doc_search_predicate(self, search: str, alias: str = "") -> tuple[str, list]:
        """All-criteria (id / text / exact amount) search predicate for documents.

        ``alias`` prefixes every column so callers that join the documents
        table under an alias (e.g. "d.") get the right column references. See
        _build_doc_search_predicate for the exhaust-every-criterion contract.
        """
        return _build_doc_search_predicate(search, alias)

    def _pg_regex_valid(self, pattern: str) -> bool:
        """Probe whether Postgres accepts ``pattern`` without erroring a real query."""
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT '' ~* %s", (pattern,))
                    cur.fetchone()
            return True
        except psycopg2.Error:
            return False

    def get_transaction_page(
        self,
        transaction_id: int,
        per_page: int = 50,
        source_file: str | None = None,
        account: str | None = None,
        status: str | None = None,
        month: str | None = None,
        search: str | None = None,
    ) -> dict:
        """Calculate which page a transaction is on given current filters."""
        conditions = ["user_id = %s"]
        params: list = [self.user_id]

        if source_file is not None:
            conditions.append("source_file = %s")
            params.append(source_file)
        if account is not None:
            conditions.append("account = %s")
            params.append(account)
        if status is not None:
            conditions.append("status = %s")
            params.append(status)
        if month is not None:
            conditions.append("date_posted LIKE %s")
            params.append(f"{month}-%")
        if search is not None:
            # Same all-columns predicate as get_transactions — the two MUST
            # agree or the computed row number points at the wrong page.
            predicate, p_params = self.txn_search_predicate(search)
            conditions.append(predicate)
            params.extend(p_params)

        where_clause = " AND ".join(conditions)

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""WITH numbered AS (
                            SELECT id, ROW_NUMBER() OVER (ORDER BY date_posted ASC, id ASC) AS rn
                            FROM transactions
                            WHERE {where_clause}
                        )
                        SELECT rn,
                               (SELECT COUNT(*) FROM numbered) AS total
                        FROM numbered
                        WHERE id = %s""",
                    params + [transaction_id],
                )
                row = cur.fetchone()
                if not row:
                    return {"page": 1, "row_number": 0, "total_count": 0, "per_page": per_page}
                rn, total = row[0], row[1]
                page = ((rn - 1) // per_page) + 1
                return {"page": page, "row_number": rn, "total_count": total, "per_page": per_page}

    def _revert_orphaned_link_status(self, cur, txn_ids) -> None:
        """Send transactions back to UNMATCHED once nothing proves them any more.

        A transaction stays LINKED while ANY active link or receipt match
        survives — which is the whole point under one-to-many: dropping one
        refund leg from a group must not un-link the charge that still has
        two other legs attached.

        There is one rule for this, and it lives in
        :meth:`_resync_transaction_statuses`: MATCHED if an active match proves
        it, LINKED if an active link explains it, otherwise UNMATCHED. This
        name is kept because the link verbs read better with it.
        """
        self._resync_transaction_statuses(cur, txn_ids)

    def reject_transaction_link(
        self, link_id: int, reviewed_by: str | None = None
    ) -> dict:
        """Unlink at the user's request — and remember that they did.

        Deleting the row forgot the decision. The deterministic linker feeds
        every UNMATCHED/IGNORED row without an active link back in on *every*
        run, so the next Match re-created the exact link the user had just
        removed. The row therefore survives, stamped ``USER_REJECTED``: that is
        precisely what :meth:`get_user_rejected_link_pairs` reads back and what
        both linkers drop at candidate generation.

        AUTO and MANUAL links get the same treatment. A rejection marker on a
        link the user made themselves is harmless — it only stops the *auto*
        linker re-proposing that pair — and the dedup index
        (``idx_transaction_links_dedup``) is partial on
        ``status != 'USER_REJECTED'``, so the same pair can still be linked
        again by hand afterwards, as many times as the user likes.

        The rejected leg keeps its ``chain_id`` and ``chain_position``: every
        chain and link read already filters ``USER_REJECTED`` out
        (``get_transaction_chain``, ``get_transaction_links``,
        ``get_linked_transaction_info``), so the surviving legs of a fan-out
        group read exactly as before — one leg fewer, positions untouched.

        Both sides are re-derived in the same DB transaction
        (:meth:`_resync_transaction_statuses`), so a row whose only link is now
        rejected leaves LINKED and lands on UNMATCHED; a transfer leg that
        needs no receipt is stamped back to IGNORED by the next run's
        non-reconcilable rule.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Get link details
                cur.execute(
                    """SELECT source_transaction_id, target_transaction_id,
                              link_type, status
                       FROM transaction_links
                       WHERE id = %s AND user_id = %s""",
                    (link_id, self.user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Link not found")
                src_id, tgt_id, link_type, old_status = row

                # Reject, never DELETE — the surviving row IS the memory.
                cur.execute(
                    """UPDATE transaction_links
                       SET status = 'USER_REJECTED', reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (reviewed_by or self.user_id, link_id, self.user_id),
                )

                self._revert_orphaned_link_status(cur, (src_id, tgt_id))

                # Audit log
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value,
                        new_value, performed_by)
                       VALUES (%s, 'transaction_link', %s, 'UNLINK', %s, %s, %s)""",
                    (self.user_id, link_id,
                     json.dumps({"source_txn_id": src_id, "target_txn_id": tgt_id,
                                 "link_type": link_type, "status": old_status}),
                     json.dumps({"status": "USER_REJECTED"}),
                     reviewed_by or self.user_id),
                )

        return {
            "unlinked": True,
            "link_id": link_id,
            "status": "USER_REJECTED",
            "remembered": True,
            "source_transaction_id": src_id,
            "target_transaction_id": tgt_id,
        }

    def reject_transaction_link_group(
        self, chain_id: str, reviewed_by: str | None = None
    ) -> dict:
        """Unlink every leg of a link group in one go, remembering each.

        Removing a split refund one leg at a time leaves the charge looking
        partially refunded at each intermediate step; callers that mean
        "undo this whole grouping" get an atomic path here. Like the
        single-leg verb, the rows survive stamped USER_REJECTED so the linker
        does not rebuild the group on the next run.

        Legs already rejected are left alone — a group with nothing live in it
        raises, so a second call reads as "nothing to unlink" rather than
        silently re-stamping an old decision with today's date.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source_transaction_id, target_transaction_id,
                              link_type, status
                       FROM transaction_links
                       WHERE chain_id = %s::uuid AND user_id = %s
                         AND status != 'USER_REJECTED'""",
                    (chain_id, self.user_id),
                )
                rows = cur.fetchall()
                if not rows:
                    raise ValueError("Link group not found")

                link_ids = [r[0] for r in rows]
                touched: list[int] = []
                for _, src_id, tgt_id, _, _ in rows:
                    touched.extend((src_id, tgt_id))

                cur.execute(
                    """UPDATE transaction_links
                       SET status = 'USER_REJECTED', reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE chain_id = %s::uuid AND user_id = %s
                         AND status != 'USER_REJECTED'""",
                    (reviewed_by or self.user_id, chain_id, self.user_id),
                )

                self._revert_orphaned_link_status(cur, touched)

                for link_id, src_id, tgt_id, link_type, old_status in rows:
                    cur.execute(
                        """INSERT INTO audit_log
                           (user_id, entity_type, entity_id, action, old_value,
                            new_value, performed_by)
                           VALUES (%s, 'transaction_link', %s, 'UNLINK', %s, %s, %s)""",
                        (self.user_id, link_id,
                         json.dumps({"source_txn_id": src_id, "target_txn_id": tgt_id,
                                     "link_type": link_type, "chain_id": chain_id,
                                     "status": old_status}),
                         json.dumps({"status": "USER_REJECTED"}),
                         reviewed_by or self.user_id),
                    )

        return {
            "unlinked": True,
            "chain_id": chain_id,
            "link_ids": link_ids,
            "status": "USER_REJECTED",
            "remembered": True,
        }

    def update_link_status(
        self, link_id: int, status: str, reviewed_by: str | None = None
    ) -> dict:
        """Update a transaction link's review status (approve/reject)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, source_transaction_id, target_transaction_id
                       FROM transaction_links
                       WHERE id = %s AND user_id = %s""",
                    (link_id, self.user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Link not found")
                old_status, src_id, tgt_id = row

                cur.execute(
                    """UPDATE transaction_links
                       SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (status, reviewed_by or self.user_id, link_id, self.user_id),
                )

                # On rejection, revert transaction statuses if no other links/matches.
                # The UPDATE above already stamped this link USER_REJECTED, so it
                # is excluded from the remaining-link count by status alone.
                if status == "USER_REJECTED":
                    self._revert_orphaned_link_status(cur, (src_id, tgt_id))

                action = "APPROVE" if status in ("USER_APPROVED", "AUTO_APPROVED") else "REJECT"
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, 'transaction_link', %s, %s, %s, %s, %s)""",
                    (self.user_id, link_id, action,
                     json.dumps({"old_status": old_status}),
                     json.dumps({"new_status": status}),
                     reviewed_by or self.user_id),
                )

        return {"link_id": link_id, "old_status": old_status, "new_status": status}

    def get_linked_transaction_info(self) -> dict[int, list[dict]]:
        """Bulk query: for all active links, return a dict mapping txn_id -> list of link info.

        Used by the transactions list endpoint to include link data inline.
        """
        result: dict[int, list[dict]] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT tl.id AS link_id,
                              tl.source_transaction_id, tl.target_transaction_id,
                              tl.link_type, tl.confidence_score, tl.status,
                              src.account AS src_account, src.description AS src_desc,
                              src.amount AS src_amount, src.currency AS src_currency,
                              src.source_file AS src_source_file,
                              tgt.account AS tgt_account, tgt.description AS tgt_desc,
                              tgt.amount AS tgt_amount, tgt.currency AS tgt_currency,
                              tgt.source_file AS tgt_source_file
                       FROM transaction_links tl
                       JOIN transactions src ON src.id = tl.source_transaction_id
                       JOIN transactions tgt ON tgt.id = tl.target_transaction_id
                       WHERE tl.user_id = %s
                         AND tl.status != 'USER_REJECTED'""",
                    (self.user_id,),
                )
                for row in cur.fetchall():
                    link_id = row[0]
                    src_txn_id, tgt_txn_id = row[1], row[2]
                    link_type, confidence, link_status = row[3], row[4], row[5]

                    # Add entry for source transaction (pointing to target)
                    entry_for_source = {
                        "link_id": link_id,
                        "linked_txn_id": tgt_txn_id,
                        "linked_account": row[11],
                        "linked_description": row[12],
                        "linked_amount": str(row[13]),
                        "linked_currency": row[14],
                        "linked_source_file": row[15],
                        "link_type": link_type,
                        "confidence_score": confidence,
                        "link_status": link_status,
                    }
                    result.setdefault(src_txn_id, []).append(entry_for_source)

                    # Add entry for target transaction (pointing to source)
                    entry_for_target = {
                        "link_id": link_id,
                        "linked_txn_id": src_txn_id,
                        "linked_account": row[6],
                        "linked_description": row[7],
                        "linked_amount": str(row[8]),
                        "linked_currency": row[9],
                        "linked_source_file": row[10],
                        "link_type": link_type,
                        "confidence_score": confidence,
                        "link_status": link_status,
                    }
                    result.setdefault(tgt_txn_id, []).append(entry_for_target)

        return result

    def get_all_transaction_links(self) -> list[dict]:
        """Get all active transaction links for this user.

        Ordered by id so the reports that group these into per-transaction
        counterpart lists render the legs of a group the same way every time —
        without it, a charge with three refund legs listed them in whatever
        order the heap scan happened to produce.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source_transaction_id, target_transaction_id,
                              link_type, confidence_score, status, chain_id,
                              chain_position
                       FROM transaction_links
                       WHERE user_id = %s AND status != 'USER_REJECTED'
                       ORDER BY id""",
                    (self.user_id,),
                )
                return [
                    {
                        "id": row[0],
                        "source_transaction_id": row[1],
                        "target_transaction_id": row[2],
                        "link_type": row[3],
                        "confidence_score": row[4],
                        "status": row[5],
                        "chain_id": str(row[6]) if row[6] else None,
                        "chain_position": row[7],
                    }
                    for row in cur.fetchall()
                ]

    # ------------------------------------------------------------------
    # Vendor rules
    # ------------------------------------------------------------------

    def insert_vendor_rule(self, rule: VendorRule) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO vendor_rules
                       (user_id, vendor_pattern, category, priority, source)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id,
                        rule.vendor_pattern,
                        rule.category,
                        rule.priority,
                        rule.source,
                    ),
                )
                return cur.fetchone()[0]

    def get_vendor_rules(self) -> list[VendorRule]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, vendor_pattern, category, priority, source, created_at
                       FROM vendor_rules
                       WHERE user_id = %s
                       ORDER BY priority DESC""",
                    (self.user_id,),
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
                    for row in cur.fetchall()
                ]

    # ------------------------------------------------------------------
    # Processed files
    # ------------------------------------------------------------------

    def insert_processed_file(
        self,
        file_hash: str,
        original_filename: str,
        stored_path: str | None = None,
    ) -> int:
        """Record a file in the cross-endpoint dedup table, idempotently.

        ``idx_processed_files_dedup`` is UNIQUE on (user_id, file_hash), and the
        same bytes legitimately arrive here twice: two uploads of one file in
        flight together, or a re-upload of a file whose extraction produced no
        ``documents`` row at all (a non-financial document — the early
        ``get_document_by_hash`` guard has nothing to find, so the upload runs
        again and lands on this INSERT). Without ON CONFLICT that surfaces as an
        unhandled ``UniqueViolation`` → HTTP 500 *after* a full LLM extraction.
        ON CONFLICT returns the id of the row already there instead.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO processed_files
                       (user_id, file_hash, original_filename, stored_path)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, file_hash) DO UPDATE
                          SET stored_path = COALESCE(EXCLUDED.stored_path,
                                                     processed_files.stored_path)
                       RETURNING id""",
                    (self.user_id, file_hash, original_filename, stored_path),
                )
                return cur.fetchone()[0]

    def get_processed_file_stored_path(self, original_filename: str) -> str | None:
        """Return the stored_path for a processed file by original filename, or None."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT stored_path FROM processed_files
                       WHERE user_id = %s AND original_filename = %s
                         AND stored_path IS NOT NULL
                       ORDER BY created_at DESC LIMIT 1""",
                    (self.user_id, original_filename),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def update_processed_file_stored_path(
        self, original_filename: str, stored_path: str
    ) -> None:
        """Attach stored_path to the most recent processed_files row for a user.

        The ingest path records the row before it knows where the file landed,
        so the location is written back here once the file store has it.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE processed_files
                       SET stored_path = %s
                       WHERE id = (
                           SELECT id FROM processed_files
                           WHERE user_id = %s AND original_filename = %s
                           ORDER BY created_at DESC LIMIT 1
                       )""",
                    (stored_path, self.user_id, original_filename),
                )

    def is_file_processed(self, file_hash: str) -> bool:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM processed_files WHERE user_id = %s AND file_hash = %s",
                    (self.user_id, file_hash),
                )
                return cur.fetchone() is not None

    def get_processed_file_info(self, file_hash: str) -> tuple[str, datetime | None] | None:
        """Return (original_filename, created_at) if this hash was already processed, else None."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT original_filename, created_at
                       FROM processed_files
                       WHERE user_id = %s AND file_hash = %s""",
                    (self.user_id, file_hash),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else None

    def delete_transactions_by_source_file(self, source_file: str) -> int:
        """Delete all transactions for this user that came from the given source file.

        Returns the number of deleted rows. Cleans up everything that hangs
        off those transactions, in FK-safe order:
        ledger entries -> reconciliation matches (+ reset matched documents
        back to EXTRACTED so receipts can re-match) -> transaction links
        (+ revert partner rows from LINKED when nothing else resolves them)
        -> the transactions themselves.

        Without the link cleanup, re-uploading a statement whose rows were
        linked (USD<->CAD transfer, credit-card payment) hits the FK
        RESTRICT on transaction_links and 500s.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM transactions WHERE user_id = %s AND source_file = %s",
                    (self.user_id, source_file),
                )
                txn_ids = [row[0] for row in cur.fetchall()]
                if not txn_ids:
                    return 0

                # Ledger entries reference reconciliation_matches (RESTRICT)
                cur.execute(
                    """DELETE FROM ledger_entries
                       WHERE user_id = %s AND match_id IN (
                           SELECT id FROM reconciliation_matches
                           WHERE user_id = %s AND transaction_id = ANY(%s)
                       )""",
                    (self.user_id, self.user_id, txn_ids),
                )

                # Remember which receipts these transactions were matched to, drop
                # the matches, then release only the receipts left with no surviving
                # match. A split-payment receipt can prove transactions in two
                # statements — resetting it unconditionally would return it to the
                # unmatched pool while it is still proving the other statement's
                # charge, letting reconciliation bind it to a second transaction.
                cur.execute(
                    """SELECT DISTINCT document_id FROM reconciliation_matches
                       WHERE user_id = %s AND transaction_id = ANY(%s)
                         AND document_id IS NOT NULL""",
                    (self.user_id, txn_ids),
                )
                doc_ids = [r[0] for r in cur.fetchall()]

                cur.execute(
                    """DELETE FROM reconciliation_matches
                       WHERE user_id = %s AND transaction_id = ANY(%s)""",
                    (self.user_id, txn_ids),
                )

                # Release only the receipts left with no surviving match.
                self._resync_document_statuses(cur, doc_ids)

                # Transaction links: note the surviving partners, delete every
                # link row, THEN re-derive those partners' statuses.
                #
                # Order matters under one-to-many. Judging a partner while
                # its other links are still on the table, excluding only the
                # link being processed, is wrong: an anchor with two legs in
                # the deleted statement sees a sibling each time and stays
                # LINKED, and then both links go. It would be left LINKED with
                # zero links — never reverted to UNMATCHED, and so silently no
                # longer asking for the receipt it still needs.
                cur.execute(
                    """SELECT source_transaction_id, target_transaction_id
                       FROM transaction_links
                       WHERE user_id = %s
                         AND (source_transaction_id = ANY(%s)
                              OR target_transaction_id = ANY(%s))""",
                    (self.user_id, txn_ids, txn_ids),
                )
                deleted_set = set(txn_ids)
                partners = {
                    (tgt_id if src_id in deleted_set else src_id)
                    for src_id, tgt_id in cur.fetchall()
                } - deleted_set

                cur.execute(
                    """DELETE FROM transaction_links
                       WHERE user_id = %s
                         AND (source_transaction_id = ANY(%s)
                              OR target_transaction_id = ANY(%s))""",
                    (self.user_id, txn_ids, txn_ids),
                )

                self._revert_orphaned_link_status(cur, partners)

                # Now delete the transactions themselves
                cur.execute(
                    "DELETE FROM transactions WHERE user_id = %s AND source_file = %s",
                    (self.user_id, source_file),
                )
                return cur.rowcount

    def get_source_files_by_prefix(self, prefix: str) -> list[str]:
        """Return distinct source_file values for this user matching the given prefix.

        Args:
            prefix: The prefix to match, e.g. "wise_USD_". Uses SQL LIKE.

        Returns:
            List of source_file strings, e.g. ["wise_USD_202601", "wise_USD_202602"].
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT source_file
                       FROM transactions
                       WHERE user_id = %s AND source_file LIKE %s
                       ORDER BY source_file""",
                    (self.user_id, f"{prefix}%"),
                )
                return [row[0] for row in cur.fetchall()]

    def delete_processed_file_by_filename(self, filename: str) -> int:
        """Delete processed_files entries for this user with the given original filename.

        Returns the number of deleted rows.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM processed_files WHERE user_id = %s AND original_filename = %s",
                    (self.user_id, filename),
                )
                return cur.rowcount

    def is_document_hash_exists(self, file_hash: str) -> bool:
        """Check if a document with this file_hash already exists for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM documents WHERE user_id = %s AND file_hash = %s LIMIT 1",
                    (self.user_id, file_hash),
                )
                return cur.fetchone() is not None

    def get_document_by_hash(self, file_hash: str) -> dict | None:
        """Return the document row for this hash, or None."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, original_filename, vendor, document_date,
                              total, currency, extraction_confidence, status
                       FROM documents
                       WHERE user_id = %s AND file_hash = %s
                       LIMIT 1""",
                    (self.user_id, file_hash),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0],
                    "original_filename": row[1],
                    "vendor": row[2],
                    "document_date": row[3],
                    "total": str(row[4]) if row[4] else None,
                    "currency": row[5],
                    "extraction_confidence": row[6],
                    "status": row[7],
                }

    def purge_document(self, doc_id: int) -> bool:
        """Completely remove a document and all its traces from every table.

        Deletes from: reconciliation_matches, ledger_entries, documents,
        processed_files. Returns True if a document was found and deleted.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Get the file_hash before deleting
                cur.execute(
                    "SELECT file_hash FROM documents WHERE id = %s AND user_id = %s",
                    (doc_id, self.user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                file_hash = row[0]

                # Delete from ledger_entries (FK to matches)
                cur.execute(
                    """DELETE FROM ledger_entries
                       WHERE user_id = %s AND match_id IN (
                           SELECT id FROM reconciliation_matches
                           WHERE user_id = %s AND document_id = %s
                       )""",
                    (self.user_id, self.user_id, doc_id),
                )

                # Which transactions this document was proving — needed AFTER
                # the matches are gone. Deleting the rows without re-deriving
                # is how a transaction ends up MATCHED with no match row and no
                # proofs at all.
                cur.execute(
                    """SELECT DISTINCT transaction_id FROM reconciliation_matches
                       WHERE user_id = %s AND document_id = %s
                         AND transaction_id IS NOT NULL""",
                    (self.user_id, doc_id),
                )
                affected_txn_ids = [r[0] for r in cur.fetchall()]

                # Delete reconciliation matches
                cur.execute(
                    "DELETE FROM reconciliation_matches WHERE user_id = %s AND document_id = %s",
                    (self.user_id, doc_id),
                )

                self._resync_transaction_statuses(cur, affected_txn_ids)

                # Delete the document
                cur.execute(
                    "DELETE FROM documents WHERE id = %s AND user_id = %s",
                    (doc_id, self.user_id),
                )

                # Delete from processed_files by hash
                cur.execute(
                    "DELETE FROM processed_files WHERE user_id = %s AND file_hash = %s",
                    (self.user_id, file_hash),
                )

                conn.commit()
                return True

    def purge_document_by_hash(self, file_hash: str) -> bool:
        """Completely remove a document by file hash from every table."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Get doc id
                cur.execute(
                    "SELECT id FROM documents WHERE user_id = %s AND file_hash = %s",
                    (self.user_id, file_hash),
                )
                row = cur.fetchone()

                if row:
                    doc_id = row[0]
                    # Delete from ledger_entries
                    cur.execute(
                        """DELETE FROM ledger_entries
                           WHERE user_id = %s AND match_id IN (
                               SELECT id FROM reconciliation_matches
                               WHERE user_id = %s AND document_id = %s
                           )""",
                        (self.user_id, self.user_id, doc_id),
                    )
                    # Remember what this document was proving, so the
                    # transactions can be re-derived once the rows are gone.
                    cur.execute(
                        """SELECT DISTINCT transaction_id FROM reconciliation_matches
                           WHERE user_id = %s AND document_id = %s
                             AND transaction_id IS NOT NULL""",
                        (self.user_id, doc_id),
                    )
                    affected_txn_ids = [r[0] for r in cur.fetchall()]
                    # Delete reconciliation matches
                    cur.execute(
                        "DELETE FROM reconciliation_matches WHERE user_id = %s AND document_id = %s",
                        (self.user_id, doc_id),
                    )
                    self._resync_transaction_statuses(cur, affected_txn_ids)
                    # Delete document
                    cur.execute(
                        "DELETE FROM documents WHERE id = %s AND user_id = %s",
                        (doc_id, self.user_id),
                    )

                # Always delete from processed_files by hash
                cur.execute(
                    "DELETE FROM processed_files WHERE user_id = %s AND file_hash = %s",
                    (self.user_id, file_hash),
                )

                conn.commit()
                return True

    # ------------------------------------------------------------------
    # API call logging
    # ------------------------------------------------------------------

    def log_api_call(
        self, endpoint: str, model: str, tokens_in: int, tokens_out: int, estimated_cost: float
    ) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO api_calls
                       (user_id, endpoint, model, tokens_in, tokens_out, estimated_cost)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (self.user_id, endpoint, model, tokens_in, tokens_out, estimated_cost),
                )

    def get_cost_summary(self, start_date: str, end_date: str) -> dict:
        """Get LLM cost summary for dashboard widget."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(estimated_cost), 0) as total_cost,
                        COUNT(*) as total_calls,
                        COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                        COALESCE(SUM(tokens_out), 0) as total_tokens_out
                    FROM api_calls
                    WHERE user_id = %s AND created_at >= %s AND created_at < %s
                    """,
                    (self.user_id, start_date, end_date),
                )
                row = cur.fetchone()
                total = {
                    "total_cost": float(row[0]),
                    "total_calls": row[1],
                    "total_tokens_in": row[2],
                    "total_tokens_out": row[3],
                }

                # Per-endpoint/model breakdown
                cur.execute(
                    """
                    SELECT endpoint, model, COUNT(*) as calls,
                           COALESCE(SUM(estimated_cost), 0) as cost
                    FROM api_calls
                    WHERE user_id = %s AND created_at >= %s AND created_at < %s
                    GROUP BY endpoint, model
                    ORDER BY cost DESC
                    """,
                    (self.user_id, start_date, end_date),
                )
                breakdown = [
                    {"endpoint": r[0], "model": r[1], "calls": r[2], "cost": float(r[3])}
                    for r in cur.fetchall()
                ]

                return {**total, "breakdown": breakdown}

    # ------------------------------------------------------------------
    # Ledger entries
    # ------------------------------------------------------------------

    def insert_ledger_entry(self, entry: LedgerEntry) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ledger_entries
                       (user_id, account, month, transaction_type, date_posted,
                        amount, currency, description, category, document_link,
                        note, match_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id,
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
                return cur.fetchone()[0]

    def get_ledger_entries_by_month(self, month: str) -> list[LedgerEntry]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, account, month, transaction_type, date_posted,
                              amount, currency, description, category, document_link,
                              note, match_id, created_at
                       FROM ledger_entries
                       WHERE user_id = %s AND month = %s""",
                    (self.user_id, month),
                )
                return [
                    LedgerEntry(
                        id=row[0],
                        account=AccountType(row[1]),
                        month=row[2],
                        transaction_type=row[3],
                        date_posted=date.fromisoformat(row[4]) if isinstance(row[4], str) else row[4],
                        amount=Decimal(row[5]),
                        currency=row[6],
                        description=row[7],
                        category=row[8],
                        document_link=row[9],
                        note=row[10],
                        match_id=row[11],
                        created_at=_datetime_or_none(row[12]),
                    )
                    for row in cur.fetchall()
                ]

    def update_document_status(self, doc_id: int, status: str) -> None:
        """Update the status for a document."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET status = %s WHERE id = %s AND user_id = %s",
                    (status, doc_id, self.user_id),
                )

    def update_document_path(self, doc_id: int, new_path: str) -> None:
        """Update the stored_path for a document (used when refiling after reconciliation)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET stored_path = %s WHERE id = %s AND user_id = %s",
                    (new_path, doc_id, self.user_id),
                )

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def get_latest_transaction_month(self) -> tuple[int, int] | None:
        """Return (year, month) of the most recent transaction, or None."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT date_posted FROM transactions
                       WHERE user_id = %s
                       ORDER BY date_posted DESC LIMIT 1""",
                    (self.user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                d = date.fromisoformat(row[0]) if isinstance(row[0], str) else row[0]
                return d.year, d.month

    def get_transfer_linked_txn_ids(self) -> set[int]:
        """Transaction ids that are internal transfers by EITHER signal:
        transfer-type active link, transfer-like category (normalized), or
        a category whose taxonomy kind is 'transfer'.

        Used by binder/report/ledger generators that aggregate Python-side.
        """
        from core.transfer_exclusion import transfer_exclusion_sql

        predicate, params = transfer_exclusion_sql(alias="t")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT t.id FROM transactions t
                        WHERE t.user_id = %s AND {predicate}""",
                    [self.user_id, *params],
                )
                return {row[0] for row in cur.fetchall()}

    def get_category_breakdown(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """Aggregate transactions by (category, currency) within [start_date, end_date].

        NULL category rows are bucketed as 'Uncategorized'. Internal
        transfers (transfer-like category, taxonomy kind='transfer', or
        transfer-type link — see core.transfer_exclusion) are omitted.
        Results are ordered by absolute net descending so the most impactful
        categories surface first.
        """
        from core.transfer_exclusion import countable_sql, transfer_exclusion_sql

        predicate, xparams = transfer_exclusion_sql(alias="t")
        countable, cparams = countable_sql(alias="t")
        with self._conn() as conn:
            with conn.cursor() as cur:
                # ABS() on BOTH transaction types: stored signs vary by
                # ingest path (the credit-card parser stores CREDIT refunds
                # NEGATIVE, the LLM parser stores positives). Summing raw
                # CREDITs made a refund SUBTRACT from money-in — the wrong
                # direction — and made Analytics disagree with the dashboard,
                # binder, and XLSX, which all use per-row ABS.
                cur.execute(
                    f"""
                    SELECT
                        COALESCE(t.category, 'Uncategorized') AS category,
                        t.currency,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS income,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS expenses,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC))
                                          WHEN t.transaction_type = 'DEBIT'
                                          THEN -ABS(CAST(t.amount AS NUMERIC))
                                          ELSE 0 END), 0) AS net,
                        COUNT(*) AS transaction_count
                    FROM transactions t
                    WHERE t.user_id = %s
                      AND t.date_posted BETWEEN %s AND %s
                      AND NOT {predicate}
                      AND {countable}
                    GROUP BY COALESCE(t.category, 'Uncategorized'), t.currency
                    ORDER BY ABS(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC))
                                          WHEN t.transaction_type = 'DEBIT'
                                          THEN -ABS(CAST(t.amount AS NUMERIC))
                                          ELSE 0 END)) DESC
                    """,
                    [
                        self.user_id,
                        start_date,
                        end_date,
                        *xparams,
                        *cparams,
                    ],
                )
                rows = cur.fetchall()
                return [
                    {
                        "category": r[0],
                        "currency": r[1],
                        "income": float(r[2]),
                        "expenses": float(r[3]),
                        "net": float(r[4]),
                        "transaction_count": int(r[5]),
                    }
                    for r in rows
                ]

    def get_monthly_trend(self, year: int) -> list[dict]:
        """Per-month, per-currency income/expense totals for a year.

        Same inclusion rules as get_category_breakdown (internal transfers
        omitted, no status filter); currencies are never numerically mixed —
        the frontend converts with a live FX rate before charting.
        """
        from core.transfer_exclusion import countable_sql, transfer_exclusion_sql

        predicate, xparams = transfer_exclusion_sql(alias="t")
        countable, cparams = countable_sql(alias="t")
        with self._conn() as conn:
            with conn.cursor() as cur:
                # ABS() on CREDIT too — see get_category_breakdown: raw
                # signed sums flip refunds stored as negative CREDITs.
                cur.execute(
                    f"""
                    SELECT
                        CAST(SUBSTRING(t.date_posted, 6, 2) AS INTEGER) AS month,
                        t.currency,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS income,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                          THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS expenses
                    FROM transactions t
                    WHERE t.user_id = %s
                      AND t.date_posted LIKE %s
                      AND NOT {predicate}
                      AND {countable}
                    GROUP BY 1, t.currency
                    ORDER BY 1, t.currency
                    """,
                    [self.user_id, f"{year:04d}-%", *xparams, *cparams],
                )
                return [
                    {
                        "month": int(r[0]),
                        "currency": r[1],
                        "income": float(r[2]),
                        "expenses": float(r[3]),
                    }
                    for r in cur.fetchall()
                ]

    def get_matched_doc_paths_and_vendors(
        self,
    ) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
        """Return matched doc paths and vendor names, supporting multi-proof.

        Returns (paths_dict, vendor_dict) where:
            paths_dict: transaction_id -> list of document stored_paths
            vendor_dict: transaction_id -> list of document vendor names
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rm.transaction_id, d.stored_path, d.vendor
                       FROM reconciliation_matches rm
                       JOIN documents d ON rm.document_id = d.id
                       WHERE rm.user_id = %s
                         AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')
                       ORDER BY rm.transaction_id, rm.created_at ASC""",
                    (self.user_id,),
                )
                paths: dict[int, list[str]] = {}
                vendors: dict[int, list[str]] = {}
                for row in cur.fetchall():
                    txn_id = row[0]
                    paths.setdefault(txn_id, []).append(row[1] or "")
                    vendors.setdefault(txn_id, []).append(row[2] or "")
                return paths, vendors

    def get_matched_document_paths(self) -> dict[int, str]:
        """Return a dict mapping transaction_id -> document stored_path for matched transactions."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rm.transaction_id, d.stored_path
                       FROM reconciliation_matches rm
                       JOIN documents d ON rm.document_id = d.id
                       WHERE rm.user_id = %s
                         AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')
                         AND d.stored_path IS NOT NULL""",
                    (self.user_id,),
                )
                return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Review helpers
    # ------------------------------------------------------------------

    def get_pending_reviews(self) -> list[ReconciliationMatch]:
        """Return all reconciliation matches needing review (PENDING_REVIEW or NEEDS_REVIEW)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, transaction_id, document_id, confidence_score,
                              amount_score, date_score, vendor_score,
                              match_type, status, reviewed_by, reviewed_at, created_at
                       FROM reconciliation_matches
                       WHERE user_id = %s AND status IN ('PENDING_REVIEW', 'NEEDS_REVIEW')
                       ORDER BY created_at ASC""",
                    (self.user_id,),
                )
                results = []
                for row in cur.fetchall():
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
                        reviewed_at=_datetime_or_none(row[10]),
                    ))
                return results

    def update_match_status(
        self, match_id: int, status: str, reviewed_by: str | None = None
    ) -> None:
        """Update a reconciliation match's status and reviewer info.

        Approving a match is also what settles the transaction and the
        document. A low-confidence match is stored FLAGGED with its transaction
        left UNMATCHED and in the candidate pool, so without that promotion an
        approved review item would stay unmatched.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (status, reviewed_by, match_id, self.user_id),
                )
                # Re-derive both sides for EVERY status, not just the
                # approvals: a rejection that changed only the match row would
                # leave the transaction MATCHED with nothing proving it.
                cur.execute(
                    """SELECT transaction_id, document_id
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (match_id, self.user_id),
                )
                row = cur.fetchone()
                if row:
                    self._resync_transaction_statuses(cur, [row[0]])
                    self._resync_document_statuses(cur, [row[1]])

    # ------------------------------------------------------------------
    # Provisional (flagged) matches
    # ------------------------------------------------------------------

    #: Match rows that still hold a transaction or a document.
    ACTIVE_MATCH_STATUSES = ("AUTO_APPROVED", "PENDING_REVIEW", "USER_APPROVED")

    def get_active_matches_for(
        self, transaction_ids: list[int], document_ids: list[int],
    ) -> list[dict]:
        """Return live match rows touching any of these transactions/documents.

        Read before storing a fresh run's matches, to decide whether a new pair
        may displace an existing weak one.
        """
        if not transaction_ids and not document_ids:
            return []
        clauses = []
        params: list[object] = [self.user_id, list(self.ACTIVE_MATCH_STATUSES)]
        if transaction_ids:
            clauses.append("transaction_id = ANY(%s)")
            params.append(list(transaction_ids))
        if document_ids:
            clauses.append("document_id = ANY(%s)")
            params.append(list(document_ids))
        sql = (
            "SELECT id, transaction_id, document_id, confidence_score, status, "
            "match_source, match_type FROM reconciliation_matches "
            "WHERE user_id = %s AND status = ANY(%s) AND ("
            + " OR ".join(clauses) + ")"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [
                    {
                        "id": r[0],
                        "transaction_id": r[1],
                        "document_id": r[2],
                        "confidence_score": float(r[3]) if r[3] is not None else 0.0,
                        "status": r[4],
                        "match_source": r[5],
                        "match_type": r[6],
                    }
                    for r in cur.fetchall()
                ]

    # ------------------------------------------------------------------
    # A rejected pair stays rejected
    # ------------------------------------------------------------------

    #: ``user_action`` values on a USER_REJECTED row that mean a *person*
    #: judged this exact (transaction, document) pair and said no.
    #:
    #: ``superseded`` is deliberately absent: that is the engine withdrawing
    #: its own weak match in favour of a better one (see ``supersede_match``),
    #: not a human verdict, and freezing those pairs out would stop the
    #: displaced receipt from ever being tried again.
    USER_REJECTION_ACTIONS = ("rejected", "unmatched", "reassigned", "remove_proof")

    def _rejected_pair_rows(
        self, cur, transaction_id: int | None = None,
    ) -> list[tuple[int, int]]:
        """Raw (transaction_id, document_id) pairs a human rejected."""
        sql = (
            "SELECT DISTINCT transaction_id, document_id "
            "FROM reconciliation_matches "
            "WHERE user_id = %s AND status = 'USER_REJECTED' "
            "  AND user_action = ANY(%s) "
            "  AND transaction_id IS NOT NULL AND document_id IS NOT NULL"
        )
        params: list[object] = [self.user_id, list(self.USER_REJECTION_ACTIONS)]
        if transaction_id is not None:
            sql += " AND transaction_id = %s"
            params.append(transaction_id)
        cur.execute(sql, tuple(params))
        return [(int(r[0]), int(r[1])) for r in cur.fetchall()]

    def _document_twin_map(self, cur) -> dict[int, frozenset[int]]:
        """doc_id -> every document row that is the same piece of paper."""
        cur.execute(
            """SELECT id, file_hash, vendor, total, currency, document_date
               FROM documents WHERE user_id = %s""",
            (self.user_id,),
        )
        groups: dict[tuple, list[int]] = {}
        for doc_id, file_hash, vendor, total, currency, doc_date in cur.fetchall():
            for key in _document_identity_keys(
                file_hash, vendor, total, currency, doc_date
            ):
                groups.setdefault(key, []).append(int(doc_id))
        twins: dict[int, set[int]] = {}
        for ids in groups.values():
            if len(ids) < 2:
                continue
            for doc_id in ids:
                twins.setdefault(doc_id, set()).update(ids)
        return {doc_id: frozenset(ids) for doc_id, ids in twins.items()}

    def get_user_rejected_pairs(self) -> set[tuple[int, int]]:
        """Every (transaction_id, document_id) pair the user has rejected.

        Loaded once per reconciliation run and applied at *candidate
        generation* — not as a filter after scoring — so a rejected pair can
        neither be re-proposed nor claim the transaction and block a better
        candidate. Filtering after scoring is not equivalent: the rejected pair
        would be written again, with a new id and the identical (transaction,
        document) pair, by the very next run.

        A rejection is expanded over the document's twins: the same receipt
        uploaded twice is one decision, so rejecting a transaction against one
        copy also rules out the other copies of that receipt. See
        ``_document_identity_keys``.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                rejected = self._rejected_pair_rows(cur)
                if not rejected:
                    return set()
                twins = self._document_twin_map(cur)
        pairs: set[tuple[int, int]] = set()
        for txn_id, doc_id in rejected:
            for twin_id in twins.get(doc_id, frozenset({doc_id})):
                pairs.add((txn_id, twin_id))
        return pairs

    # ── Remembered agent verdicts ─────────────────────────────────────────

    def get_adjudication_memory(self, document_ids: list[int] | None = None) -> list[dict]:
        """Every agent verdict stored for this user, for one run to load once.

        Read in a single query before the matcher's thread pool starts: the
        pool's workers each looking one row up is a worse bargain than holding
        the whole small table in memory. The companion of
        :meth:`get_user_rejected_pairs`: that one remembers what the *user*
        decided about a pair, this one what the *model* decided.

        Returns ``[]`` rather than raising when the table is missing, so a
        database that has not applied every migration still reconciles — it
        just asks the model everything.
        """
        sql = (
            "SELECT document_id, transaction_id, agent, inputs_hash, verdict, "
            "       confidence, reasoning, model "
            "FROM reconciliation_adjudications WHERE user_id = %s"
        )
        params: list[object] = [self.user_id]
        if document_ids:
            sql += " AND document_id = ANY(%s)"
            params.append(list(document_ids))
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    rows = cur.fetchall()
        except Exception:
            logger.warning(
                "Could not read remembered adjudications; this run will ask the "
                "model every question", exc_info=True,
            )
            return []
        return [
            {
                "document_id": r[0],
                "transaction_id": r[1],
                "agent": r[2],
                "inputs_hash": r[3],
                "verdict": r[4],
                "confidence": r[5],
                "reasoning": r[6],
                "model": r[7],
            }
            for r in rows
        ]

    def save_adjudication_memory(self, entries: list[dict]) -> int:
        """Persist verdicts a run got from the model. Returns rows written.

        Called once, after the thread pool has drained. ``ON CONFLICT DO
        NOTHING`` on the (user, document, agent, inputs_hash) index: the same
        question can legitimately be asked by two documents' pipelines in the
        same run, and the answer is the same either way.

        Never raises. A verdict that fails to store costs the next run some
        time; it must not cost this run its matches.
        """
        if not entries:
            return 0
        rows = [
            (
                self.user_id,
                int(e["document_id"]),
                e.get("transaction_id"),
                e["agent"],
                e["inputs_hash"],
                json.dumps(e["verdict"]),
                e.get("confidence"),
                e.get("reasoning"),
                e["model"],
            )
            for e in entries
        ]
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO reconciliation_adjudications
                               (user_id, document_id, transaction_id, agent,
                                inputs_hash, verdict, confidence, reasoning, model)
                           VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                           ON CONFLICT (user_id, document_id, agent, inputs_hash)
                           DO UPDATE SET used_at = NOW()""",
                        rows,
                    )
                conn.commit()
            return len(rows)
        except Exception:
            logger.warning(
                "Could not store %d remembered adjudication(s); the next run will "
                "re-ask those questions", len(rows), exc_info=True,
            )
            return 0

    def _rejected_document_ids(self, cur, transaction_id: int) -> set[int]:
        """Documents rejected for this transaction (twins too), on one cursor."""
        rejected = self._rejected_pair_rows(cur, transaction_id)
        if not rejected:
            return set()
        twins = self._document_twin_map(cur)
        doc_ids: set[int] = set()
        for _txn_id, doc_id in rejected:
            doc_ids.update(twins.get(doc_id, frozenset({doc_id})))
        return doc_ids

    def get_user_rejected_document_ids(self, transaction_id: int) -> set[int]:
        """Documents the user has rejected for this transaction (twins too).

        The manual picker still offers them — a rejection is not a ban, it is
        a fact the user should see before choosing again.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                return self._rejected_document_ids(cur, transaction_id)

    # ------------------------------------------------------------------
    # Status re-derivation — a status is never asserted, always derived
    # ------------------------------------------------------------------

    #: Transaction statuses a re-derivation must not overwrite. IGNORED and
    #: EXCLUDED are statements about the row itself ("needs no receipt", "isn't
    #: real") that no amount of match bookkeeping may undo; PENDING_REVIEW
    #: belongs to statement-import review, before reconciliation is in play.
    #: One exception, in _resync_transaction_statuses: IGNORED loses to an
    #: active cross-statement link, which explains the row better.
    PRESERVED_TXN_STATUSES = ("IGNORED", "EXCLUDED", "PENDING_REVIEW")

    #: Match rows that settle a pair on their own.
    SETTLING_MATCH_STATUSES = ("AUTO_APPROVED", "USER_APPROVED")

    def _resync_transaction_statuses(self, cur, txn_ids) -> list[dict]:
        """Re-derive each transaction's status from the rows that survive.

        The rule, in order:

        1. an **approved** match row (AUTO_APPROVED / USER_APPROVED) proves the
           transaction → MATCHED;
        2. a **PENDING_REVIEW** match row is *neutral* — it neither proves nor
           disproves. A pair above the confidence floor is marked MATCHED when
           it is stored, and a pair below it is deliberately left UNMATCHED and
           in the candidate pool; this function must not overturn either
           decision, so the status is left exactly as it is;
        3. an active cross-statement link explains it → LINKED;
        4. nothing at all → UNMATCHED.

        Called in the SAME database transaction as whatever changed the match
        rows, so a transaction can never be left MATCHED with nothing behind
        it — status MATCHED, zero proofs and a null ``matched_document`` is a
        state no caller can observe.

        Returns one dict per row actually changed.
        """
        changed: list[dict] = []
        for txn_id in dict.fromkeys(t for t in txn_ids if t):
            cur.execute(
                """SELECT t.status,
                          EXISTS (SELECT 1 FROM reconciliation_matches m
                                   WHERE m.user_id = t.user_id
                                     AND m.transaction_id = t.id
                                     AND m.status = ANY(%s)),
                          EXISTS (SELECT 1 FROM reconciliation_matches m
                                   WHERE m.user_id = t.user_id
                                     AND m.transaction_id = t.id
                                     AND m.status = 'PENDING_REVIEW'),
                          EXISTS (SELECT 1 FROM transaction_links l
                                   WHERE l.user_id = t.user_id
                                     AND l.status <> 'USER_REJECTED'
                                     AND (l.source_transaction_id = t.id
                                          OR l.target_transaction_id = t.id))
                     FROM transactions t
                    WHERE t.id = %s AND t.user_id = %s""",
                (list(self.SETTLING_MATCH_STATUSES), txn_id, self.user_id),
            )
            row = cur.fetchone()
            if row is None:
                continue
            current = row[0]
            has_approved, has_pending, has_link = bool(row[1]), bool(row[2]), bool(row[3])
            if current in self.PRESERVED_TXN_STATUSES:
                # One exception, and only one: IGNORED means "self-evident, no
                # receipt needed" — a guess made from the description alone. An
                # active cross-statement link is a better explanation of the same
                # row (it names the counterpart), and the link-insert path
                # already promotes IGNORED -> LINKED, so a re-derivation that
                # left it IGNORED would quietly contradict the insert and hide
                # the link badge in the grid. EXCLUDED ("isn't real") and
                # PENDING_REVIEW (import review) are never overwritten.
                if current == "IGNORED" and has_link:
                    cur.execute(
                        "UPDATE transactions SET status = 'LINKED' "
                        "WHERE id = %s AND user_id = %s",
                        (txn_id, self.user_id),
                    )
                    changed.append(
                        {"transaction_id": txn_id, "from": current, "to": "LINKED"}
                    )
                continue
            if has_approved:
                derived = "MATCHED"
            elif has_pending:
                derived = current  # neutral — the storing decision stands
            elif has_link:
                derived = "LINKED"
            else:
                derived = "UNMATCHED"
            if derived == current:
                continue
            cur.execute(
                "UPDATE transactions SET status = %s WHERE id = %s AND user_id = %s",
                (derived, txn_id, self.user_id),
            )
            changed.append({
                "transaction_id": txn_id, "from": current, "to": derived,
            })
        return changed

    def _resync_document_statuses(self, cur, doc_ids) -> list[dict]:
        """Re-derive each document's status from its surviving match rows.

        Same three-way rule as the transaction side: an approved match makes
        the receipt MATCHED, a PENDING_REVIEW match is neutral (the storage
        rule already decided whether that pair took the receipt out of the
        pool), and nothing at all sends a receipt still marked MATCHED back to
        EXTRACTED so it re-enters the candidate pool. FLAGGED (the extraction
        needs a human) is left alone — that is a statement about the
        extraction, not about matching.
        """
        changed: list[dict] = []
        for doc_id in dict.fromkeys(d for d in doc_ids if d):
            cur.execute(
                """SELECT d.status,
                          EXISTS (SELECT 1 FROM reconciliation_matches m
                                   WHERE m.user_id = d.user_id
                                     AND m.document_id = d.id
                                     AND m.status = ANY(%s)),
                          EXISTS (SELECT 1 FROM reconciliation_matches m
                                   WHERE m.user_id = d.user_id
                                     AND m.document_id = d.id
                                     AND m.status = 'PENDING_REVIEW')
                     FROM documents d
                    WHERE d.id = %s AND d.user_id = %s""",
                (list(self.SETTLING_MATCH_STATUSES), doc_id, self.user_id),
            )
            row = cur.fetchone()
            if row is None:
                continue
            current = row[0]
            has_approved, has_pending = bool(row[1]), bool(row[2])
            if has_approved:
                derived = "MATCHED" if current in ("EXTRACTED", "MATCHED") else current
            elif has_pending:
                derived = current  # neutral
            else:
                derived = "EXTRACTED" if current == "MATCHED" else current
            if derived == current:
                continue
            cur.execute(
                """UPDATE documents SET status = %s, updated_at = NOW()
                   WHERE id = %s AND user_id = %s""",
                (derived, doc_id, self.user_id),
            )
            changed.append({"document_id": doc_id, "from": current, "to": derived})
        return changed

    def resync_statuses_in_cursor(
        self, cur, transaction_ids=(), document_ids=(),
    ) -> dict[str, list[dict]]:
        """Re-derive statuses on a cursor the caller already holds.

        For the API handlers that do their own SQL on ``db._conn()`` — the
        re-derivation has to land in the SAME database transaction as the match
        status change, or a crash in between leaves the pair half-updated.
        """
        return {
            "transactions": self._resync_transaction_statuses(cur, transaction_ids),
            "documents": self._resync_document_statuses(cur, document_ids),
        }

    def resync_statuses(
        self, transaction_ids=(), document_ids=(),
    ) -> dict[str, list[dict]]:
        """Re-derive transaction and document statuses in one transaction.

        The entry point for callers that are not already holding a cursor.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                txns = self._resync_transaction_statuses(cur, transaction_ids)
                docs = self._resync_document_statuses(cur, document_ids)
        if txns or docs:
            logger.info(
                "Status re-derivation corrected %d transaction(s) and %d document(s): %s %s",
                len(txns), len(docs), txns, docs,
            )
        return {"transactions": txns, "documents": docs}

    def supersede_match(self, match_id: int, reason: str) -> dict | None:
        """Withdraw a weak match in favour of a better one.

        The row is kept for the audit trail (status USER_REJECTED, action
        "superseded") and its transaction and document go back to unmatched, so
        the displaced receipt is available again, rather than stranded behind
        a low-confidence match that was never approved.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, transaction_id, document_id
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (match_id, self.user_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                old_status, txn_id, doc_id = row
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_REJECTED',
                           user_action = 'superseded',
                           actioned_at = NOW(),
                           explanation = %s
                       WHERE id = %s AND user_id = %s""",
                    (reason[:1000], match_id, self.user_id),
                )
                # Both sides go back to whatever the surviving rows say — not
                # blindly to unmatched: the transaction may still hold another
                # active match, and the receipt may still prove a second
                # transaction in a split.
                self._resync_transaction_statuses(cur, [txn_id])
                self._resync_document_statuses(cur, [doc_id])
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value,
                        new_value, performed_by)
                       VALUES (%s, 'match', %s, 'SUPERSEDE', %s, 'USER_REJECTED',
                               'system')""",
                    (self.user_id, match_id, old_status),
                )
                return {
                    "match_id": match_id,
                    "transaction_id": txn_id,
                    "document_id": doc_id,
                    "previous_status": old_status,
                }

    def refresh_match_scores(
        self,
        match_id: int,
        confidence_score: float,
        amount_score: float,
        date_score: float,
        vendor_score: float,
        explanation: str | None,
        status: str,
    ) -> None:
        """Re-score an existing pending match instead of inserting a twin.

        A flagged pair stays in the candidate pool, so the next run proposes it
        again; without this the review queue grows a duplicate row per run.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET confidence_score = %s, amount_score = %s,
                           date_score = %s, vendor_score = %s,
                           explanation = %s, status = %s
                       WHERE id = %s AND user_id = %s""",
                    (confidence_score, amount_score, date_score, vendor_score,
                     explanation, status, match_id, self.user_id),
                )

    # ------------------------------------------------------------------
    # Business profile (onboarding)
    # ------------------------------------------------------------------

    def get_business_profile(self) -> dict | None:
        """Return the business profile for this user, or None if not set."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, company_name, fiscal_year, base_currency,
                              tax_jurisdiction, onboarding_complete, config_json,
                              created_at, updated_at,
                              onboarding_state_json, setup_checklist_state,
                              province, business_number,
                              industry, business_summary, naics_code
                       FROM business_profile
                       WHERE user_id = %s
                       LIMIT 1""",
                    (self.user_id,),
                )
                row = cur.fetchone()
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
                    "onboarding_state": row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
                    "setup_checklist": row[10] if isinstance(row[10], dict) else (json.loads(row[10]) if row[10] else {}),
                    "province": row[11],
                    "business_number": row[12],
                    "industry": row[13],
                    "business_summary": row[14],
                    "naics_code": row[15],
                }

    def upsert_business_profile(self, data: dict) -> None:
        """Insert or update the business profile for this user.

        Only fields present in ``data`` are updated. Missing fields are
        preserved via COALESCE (NULL in the EXCLUDED row falls through to
        the existing value). ``onboarding_complete`` is only set when
        explicitly provided — otherwise it is passed as NULL so the
        existing value is kept.

        Every text column is guarded with ``NULLIF(…, '')`` as well, so a
        caller that sends an empty string for a field it has no value for is
        treated as not having sent it. Province and industry select the GST/HST
        rate and the peer-benchmark cohort, and a form that submits before it
        has loaded would otherwise blank them. The cost is that this call
        cannot clear a stored text value back to empty; it is an upsert of what
        is known, not a replace of the whole row.
        """
        config_json = json.dumps(data.get("config_json")) if data.get("config_json") else None
        # Only set onboarding_complete when explicitly provided in data
        onboarding_val = int(data["onboarding_complete"]) if "onboarding_complete" in data else None
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO business_profile
                       (user_id, company_name, fiscal_year, base_currency,
                        tax_jurisdiction, province, business_number,
                        onboarding_complete, config_json,
                        industry, business_summary, naics_code)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                           company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), business_profile.company_name),
                           fiscal_year = COALESCE(EXCLUDED.fiscal_year, business_profile.fiscal_year),
                           base_currency = COALESCE(NULLIF(EXCLUDED.base_currency, ''), business_profile.base_currency),
                           tax_jurisdiction = COALESCE(NULLIF(EXCLUDED.tax_jurisdiction, ''), business_profile.tax_jurisdiction),
                           province = COALESCE(NULLIF(EXCLUDED.province, ''), business_profile.province),
                           business_number = COALESCE(NULLIF(EXCLUDED.business_number, ''), business_profile.business_number),
                           onboarding_complete = COALESCE(EXCLUDED.onboarding_complete, business_profile.onboarding_complete),
                           config_json = COALESCE(EXCLUDED.config_json, business_profile.config_json),
                           industry = COALESCE(NULLIF(EXCLUDED.industry, ''), business_profile.industry),
                           business_summary = COALESCE(NULLIF(EXCLUDED.business_summary, ''), business_profile.business_summary),
                           naics_code = COALESCE(NULLIF(EXCLUDED.naics_code, ''), business_profile.naics_code),
                           updated_at = NOW()""",
                    (
                        self.user_id,
                        data.get("company_name"),
                        data.get("fiscal_year"),
                        data.get("base_currency", "CAD"),
                        data.get("tax_jurisdiction", "CA"),
                        data.get("province"),
                        data.get("business_number"),
                        onboarding_val,
                        config_json,
                        data.get("industry"),
                        data.get("business_summary"),
                        data.get("naics_code"),
                    ),
                )

    def is_onboarding_complete(self) -> bool:
        """Check if onboarding has been completed for this user."""
        profile = self.get_business_profile()
        return profile is not None and profile["onboarding_complete"]

    # ------------------------------------------------------------------
    # Reasonableness / peer-benchmark dismissed flags
    # ------------------------------------------------------------------

    def get_dismissed_reasonableness_flags(self) -> list[dict]:
        """Return this user's dismissed reasonableness flags (suppression set)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT flag_key, note, naics_code, dismissed_at
                       FROM reasonableness_dismissed_flags
                       WHERE user_id = %s ORDER BY dismissed_at DESC""",
                    (self.user_id,),
                )
                return [
                    {"flag_key": r[0], "note": r[1], "naics_code": r[2],
                     "dismissed_at": r[3]}
                    for r in cur.fetchall()
                ]

    def upsert_dismissed_reasonableness_flag(
        self, flag_key: str, note: str | None = None, naics_code: str | None = None
    ) -> None:
        """Mark a reasonableness flag as dismissed ("correct / expected") for
        this user. Idempotent — re-dismissing refreshes the note + timestamp."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO reasonableness_dismissed_flags
                           (user_id, flag_key, note, naics_code)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, flag_key) DO UPDATE SET
                           note = EXCLUDED.note,
                           naics_code = COALESCE(EXCLUDED.naics_code,
                                                 reasonableness_dismissed_flags.naics_code),
                           dismissed_at = NOW()""",
                    (self.user_id, flag_key, note, naics_code),
                )

    def delete_dismissed_reasonableness_flag(self, flag_key: str) -> bool:
        """Un-dismiss a reasonableness flag. Returns True if a row was removed."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM reasonableness_dismissed_flags
                       WHERE user_id = %s AND flag_key = %s""",
                    (self.user_id, flag_key),
                )
                return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Gather sources
    # ------------------------------------------------------------------

    def get_gather_sources(self) -> list[dict]:
        """Return all configured gather sources for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source_type, enabled, config_json, last_gathered,
                              created_at, updated_at
                       FROM gather_sources
                       WHERE user_id = %s
                       ORDER BY source_type""",
                    (self.user_id,),
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
                    for row in cur.fetchall()
                ]

    def upsert_gather_source(
        self, source_type: str, enabled: bool = True, config_json: dict | None = None
    ) -> None:
        """Insert or update a gather source configuration."""
        config_str = json.dumps(config_json) if config_json else None
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gather_sources
                       (user_id, source_type, enabled, config_json)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, source_type) DO UPDATE SET
                           enabled = EXCLUDED.enabled,
                           config_json = EXCLUDED.config_json,
                           updated_at = NOW()""",
                    (self.user_id, source_type, int(enabled), config_str),
                )

    def update_gather_source_last_gathered(self, source_type: str) -> None:
        """Update the last_gathered timestamp for a source."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE gather_sources
                       SET last_gathered = NOW(), updated_at = NOW()
                       WHERE user_id = %s AND source_type = %s""",
                    (self.user_id, source_type),
                )

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
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gather_log
                       (user_id, source, source_id, filename, file_hash,
                        file_size, stored_path, metadata_json)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id, source, source_id, filename,
                        file_hash, file_size, stored_path, meta_str,
                    ),
                )
                return cur.fetchone()[0]

    def get_gather_log_by_source_id(self, source: str, source_id: str) -> dict | None:
        """Look up a gather log entry by source + source_id (for dedup)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, source_id, filename, file_hash, file_size,
                              stored_path, gathered_at, status, metadata_json
                       FROM gather_log
                       WHERE user_id = %s AND source = %s AND source_id = %s""",
                    (self.user_id, source, source_id),
                )
                row = cur.fetchone()
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
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, source_id, filename, file_hash, file_size,
                              stored_path, gathered_at, status, metadata_json
                       FROM gather_log
                       WHERE user_id = %s AND file_hash = %s
                       LIMIT 1""",
                    (self.user_id, file_hash),
                )
                row = cur.fetchone()
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
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE gather_log SET status = %s WHERE id = %s AND user_id = %s",
                    (status, log_id, self.user_id),
                )

    def get_gather_log_stats(self) -> dict:
        """Return summary counts of gather log entries by source and status."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source, status, COUNT(*) as cnt
                       FROM gather_log
                       WHERE user_id = %s
                       GROUP BY source, status""",
                    (self.user_id,),
                )
                stats: dict[str, dict[str, int]] = {}
                for row in cur.fetchall():
                    stats.setdefault(row[0], {})[row[1]] = row[2]
                return stats

    # ------------------------------------------------------------------
    # Shared reports
    # ------------------------------------------------------------------

    def create_shared_report(
        self, year: int, month: int, token: str, expires_at: datetime
    ) -> int:
        """Create a shareable report link. Returns the new row ID."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO shared_reports
                       (user_id, token, year, month, expires_at)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id""",
                    (self.user_id, token, year, month, expires_at),
                )
                return cur.fetchone()[0]

    def get_shared_report(self, token: str) -> dict | None:
        """Look up a shared report by its token (not user-scoped — public access).

        Returns None if the token does not exist or has expired.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, user_id, token, year, month, expires_at, created_at
                       FROM shared_reports
                       WHERE token = %s AND expires_at > NOW()""",
                    (token,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0],
                    "user_id": str(row[1]),
                    "token": row[2],
                    "year": row[3],
                    "month": row[4],
                    "expires_at": row[5],
                    "created_at": row[6],
                }

    # ------------------------------------------------------------------
    # Dashboard & reporting
    # ------------------------------------------------------------------

    def get_dashboard_summary(self) -> dict:
        """Return a summary for the user's dashboard.

        A transaction is **resolved** if it has at least one active receipt
        match (``reconciliation_matches``), at least one active cross-
        statement link (``transaction_links``), **or** its status is
        IGNORED (the user has explicitly dismissed it — e.g. bank fees,
        internal transfers). IGNORED counts as resolved in both the
        numerator and denominator so the user can reach 100% by ignoring
        the last few non-reconcilable rows.

        Returns dict with:
            transaction_count: total transactions
            resolved_count: txns with active match/link OR status=IGNORED
            unresolved_count: total - resolved_count
            matched_count: txns with any active receipt match (informational)
            linked_count: txns with any active link (informational)
            ignored_count: status=IGNORED (informational breakdown)
            reconcilable_count: total (all transactions count)
            resolution_rate: resolved / total * 100
        """
        from core.transfer_exclusion import countable_sql

        countable, cparams = countable_sql(alias="t")
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Single query: count total, ignored, and resolved (has match OR link).
                cur.execute(
                    f"""SELECT
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE t.status = 'IGNORED') AS ignored,
                           COUNT(*) FILTER (
                               WHERE t.status = 'IGNORED'
                                  OR EXISTS (
                                         SELECT 1 FROM reconciliation_matches m
                                         WHERE m.user_id = t.user_id
                                           AND m.transaction_id = t.id
                                           AND m.status != 'USER_REJECTED'
                                     )
                                  OR EXISTS (
                                         SELECT 1 FROM transaction_links l
                                         WHERE l.user_id = t.user_id
                                           AND l.status != 'USER_REJECTED'
                                           AND (l.source_transaction_id = t.id
                                                OR l.target_transaction_id = t.id)
                                     )
                           ) AS resolved,
                           COUNT(*) FILTER (
                               WHERE EXISTS (
                                     SELECT 1 FROM reconciliation_matches m
                                     WHERE m.user_id = t.user_id
                                       AND m.transaction_id = t.id
                                       AND m.status != 'USER_REJECTED'
                                 )
                           ) AS matched,
                           COUNT(*) FILTER (
                               WHERE EXISTS (
                                     SELECT 1 FROM transaction_links l
                                     WHERE l.user_id = t.user_id
                                       AND l.status != 'USER_REJECTED'
                                       AND (l.source_transaction_id = t.id
                                            OR l.target_transaction_id = t.id)
                                 )
                           ) AS linked
                       FROM transactions t
                       WHERE t.user_id = %s
                         AND {countable}""",
                    (self.user_id, *cparams),
                )
                row = cur.fetchone()
                total = row[0] or 0
                ignored = row[1] or 0
                resolved = row[2] or 0
                matched = row[3] or 0
                linked = row[4] or 0
                reconcilable = total
                unresolved = total - resolved
                resolution_rate = (resolved / total * 100.0) if total > 0 else 0.0

                # Last month income/expenses — find the most recent month with data
                cur.execute(
                    """SELECT SUBSTRING(date_posted FROM 1 FOR 7) AS ym
                       FROM transactions WHERE user_id = %s
                       ORDER BY date_posted DESC LIMIT 1""",
                    (self.user_id,),
                )
                latest_row = cur.fetchone()
                if latest_row and latest_row[0]:
                    # Use the most recent month that has transactions
                    month_prefix = latest_row[0] + "-%"
                else:
                    # Fallback to last calendar month
                    now = datetime.now()
                    if now.month == 1:
                        last_month_year, last_month = now.year - 1, 12
                    else:
                        last_month_year, last_month = now.year, now.month - 1
                    month_prefix = f"{last_month_year:04d}-{last_month:02d}-%"

                from core.transfer_exclusion import countable_sql, transfer_exclusion_sql

                excl, xparams = transfer_exclusion_sql(alias="t")
                countable, cparams = countable_sql(alias="t")
                cur.execute(
                    f"""SELECT
                           COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                             AND NOT {excl} THEN CAST(t.amount AS NUMERIC)
                                             ELSE 0 END), 0) AS income,
                           COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                             AND NOT {excl} THEN ABS(CAST(t.amount AS NUMERIC))
                                             ELSE 0 END), 0) AS expenses
                       FROM transactions t
                       WHERE t.user_id = %s AND t.date_posted LIKE %s
                         AND {countable}""",
                    [*xparams, *xparams, self.user_id, month_prefix, *cparams],
                )
                fin_row = cur.fetchone()
                income = Decimal(str(fin_row[0]))
                expenses = Decimal(str(fin_row[1]))
                net = income - expenses

                # Document count
                cur.execute(
                    "SELECT COUNT(*) FROM documents WHERE user_id = %s",
                    (self.user_id,),
                )
                doc_count = cur.fetchone()[0]

                return {
                    "total_transactions": total,
                    "transaction_count": total,  # backward-compat alias
                    "resolved_count": resolved,          # has match OR link (primary KPI)
                    "unresolved_count": unresolved,      # reconcilable but neither matched nor linked
                    "matched_count": matched,            # has active receipt match (breakdown)
                    "linked_count": linked,              # has active link (breakdown)
                    "ignored_count": ignored,            # non-reconcilable
                    "reconcilable_count": reconcilable,  # = total (IGNORED counts as resolved, so it stays in the denominator)
                    "resolution_rate": round(resolution_rate, 1),
                    "income_this_month": round(float(income), 2),
                    "expenses_this_month": round(float(expenses), 2),
                    "net_this_month": round(float(net), 2),
                    "document_count": doc_count,
                }

    def get_monthly_status(self, year: int) -> list[dict]:
        """Return per-month status for a given year.

        Resolution semantics: a transaction is **resolved** if it has any
        active receipt match OR any active cross-statement link (see
        ``get_dashboard_summary`` for rationale).

        Each dict matches the frontend MonthInfo interface:
            month, transaction_count, resolved_count, unresolved_count,
            matched_count, linked_count, ignored_count,
            income, expenses, status, closed
        """
        from core.transfer_exclusion import countable_sql, transfer_exclusion_sql

        excl, xparams = transfer_exclusion_sql(alias="t")
        countable, cparams = countable_sql(alias="t")
        results = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Per-month per-currency money figures in one grouped query.
                # Amounts in different currencies must never be numerically
                # summed — the legacy scalar income/expenses keys remain (as
                # cross-currency sums) for backward compatibility only.
                cur.execute(
                    f"""SELECT
                           CAST(SUBSTRING(t.date_posted, 6, 2) AS INTEGER) AS month,
                           t.currency,
                           COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                             AND NOT {excl} THEN ABS(CAST(t.amount AS NUMERIC))
                                             ELSE 0 END), 0) AS income,
                           COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                             AND NOT {excl} THEN ABS(CAST(t.amount AS NUMERIC))
                                             ELSE 0 END), 0) AS expenses
                       FROM transactions t
                       WHERE t.user_id = %s AND t.date_posted LIKE %s
                         AND {countable}
                       GROUP BY 1, t.currency
                       ORDER BY 1, t.currency""",
                    [*xparams, *xparams, self.user_id, f"{year:04d}-%", *cparams],
                )
                by_currency_by_month: dict[int, list[dict]] = {}
                for r in cur.fetchall():
                    income = float(r[2])
                    expenses = float(r[3])
                    by_currency_by_month.setdefault(int(r[0]), []).append({
                        "currency": r[1] or "CAD",
                        "income": round(income, 2),
                        "expenses": round(expenses, 2),
                        "net": round(income - expenses, 2),
                    })
                for lst in by_currency_by_month.values():
                    lst.sort(key=lambda c: (
                        c["currency"] != "CAD",
                        c["currency"] != "USD",
                        -(abs(c["income"]) + abs(c["expenses"])),
                        c["currency"],
                    ))

                for m in range(1, 13):
                    month_prefix = f"{year:04d}-{m:02d}-%"

                    cur.execute(
                        f"""SELECT
                               COUNT(*) AS total,
                               COUNT(*) FILTER (WHERE t.status = 'IGNORED') AS ignored,
                               COUNT(*) FILTER (
                                   WHERE t.status = 'IGNORED'
                                      OR EXISTS (
                                             SELECT 1 FROM reconciliation_matches rm
                                             WHERE rm.user_id = t.user_id
                                               AND rm.transaction_id = t.id
                                               AND rm.status != 'USER_REJECTED'
                                         )
                                      OR EXISTS (
                                             SELECT 1 FROM transaction_links tl
                                             WHERE tl.user_id = t.user_id
                                               AND tl.status != 'USER_REJECTED'
                                               AND (tl.source_transaction_id = t.id
                                                    OR tl.target_transaction_id = t.id)
                                         )
                               ) AS resolved,
                               COUNT(*) FILTER (
                                   WHERE EXISTS (
                                         SELECT 1 FROM reconciliation_matches rm
                                         WHERE rm.user_id = t.user_id
                                           AND rm.transaction_id = t.id
                                           AND rm.status != 'USER_REJECTED'
                                     )
                               ) AS matched,
                               COUNT(*) FILTER (
                                   WHERE EXISTS (
                                         SELECT 1 FROM transaction_links tl
                                         WHERE tl.user_id = t.user_id
                                           AND tl.status != 'USER_REJECTED'
                                           AND (tl.source_transaction_id = t.id
                                                OR tl.target_transaction_id = t.id)
                                     )
                               ) AS linked,
                               COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                                 AND NOT {excl} THEN CAST(t.amount AS NUMERIC)
                                                 ELSE 0 END), 0) AS income,
                               COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                                 AND NOT {excl} THEN ABS(CAST(t.amount AS NUMERIC))
                                                 ELSE 0 END), 0) AS expenses
                           FROM transactions t
                           WHERE t.user_id = %s AND t.date_posted LIKE %s
                             AND {countable}""",
                        [*xparams, *xparams, self.user_id, month_prefix, *cparams],
                    )
                    row = cur.fetchone()
                    total = row[0] or 0
                    ignored = row[1] or 0
                    resolved = row[2] or 0
                    matched = row[3] or 0
                    linked = row[4] or 0
                    reconcilable = total
                    unresolved = total - resolved

                    # Check month close status
                    cur.execute(
                        """SELECT status FROM month_status
                           WHERE user_id = %s AND year = %s AND month = %s""",
                        (self.user_id, year, m),
                    )
                    status_row = cur.fetchone()
                    is_closed = status_row and status_row[0] == "CLOSED"

                    results.append({
                        "month": m,
                        "transaction_count": total,
                        "resolved_count": resolved,
                        "unresolved_count": unresolved,
                        "matched_count": matched,
                        "linked_count": linked,
                        "ignored_count": ignored,
                        "reconcilable_count": reconcilable,
                        "income": round(float(row[5]), 2),
                        "expenses": round(float(row[6]), 2),
                        "by_currency": by_currency_by_month.get(m, []),
                        "status": status_row[0] if status_row else "OPEN",
                        "closed": bool(is_closed),
                    })
        return results

    # Tax columns are TEXT decimal strings written by the extraction pipeline.
    # The regex guard makes a single malformed value degrade to 0 instead of
    # failing the whole aggregate with a cast error.
    _TAX_NUM_SQL = (
        "COALESCE(CASE WHEN d.{col} ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "THEN CAST(d.{col} AS NUMERIC) END, 0)"
    )

    def get_tax_summary(self, year: int) -> dict:
        """Aggregate extracted sales-tax amounts (GST/HST/PST/other) per month.

        Sums documents.tax_* for documents dated in ``year``, grouped by
        currency. Documents whose document_date is missing or not ISO-formatted
        are aggregated separately under ``undated`` so extracted tax is never
        silently dropped from the summary.
        """
        gst = self._TAX_NUM_SQL.format(col="tax_gst")
        hst = self._TAX_NUM_SQL.format(col="tax_hst")
        pst = self._TAX_NUM_SQL.format(col="tax_pst")
        other = self._TAX_NUM_SQL.format(col="tax_other")
        any_tax = f"({gst} <> 0 OR {hst} <> 0 OR {pst} <> 0 OR {other} <> 0)"

        currencies: dict[str, dict] = {}
        undated: list[dict] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT CAST(substring(d.document_date, 6, 2) AS INTEGER) AS month,
                               COALESCE(NULLIF(d.currency, ''), 'CAD') AS currency,
                               SUM({gst}) AS gst,
                               SUM({hst}) AS hst,
                               SUM({pst}) AS pst,
                               SUM({other}) AS other,
                               COUNT(*) FILTER (WHERE {any_tax}) AS taxed_receipts,
                               COUNT(*) AS receipts
                        FROM documents d
                        WHERE d.user_id = %s
                          AND d.document_date ~ '^[0-9]{{4}}-[0-9]{{2}}'
                          AND substring(d.document_date, 1, 4) = %s
                        GROUP BY 1, 2
                        ORDER BY 2, 1""",
                    (self.user_id, f"{year:04d}"),
                )
                for row in cur.fetchall():
                    month, currency = int(row[0]), row[1]
                    if not 1 <= month <= 12:
                        continue
                    cur_entry = currencies.setdefault(currency, {
                        "currency": currency,
                        "months": [
                            {"month": m, "gst": 0.0, "hst": 0.0, "pst": 0.0,
                             "other": 0.0, "total_tax": 0.0,
                             "taxed_receipt_count": 0, "receipt_count": 0}
                            for m in range(1, 13)
                        ],
                    })
                    entry = cur_entry["months"][month - 1]
                    entry["gst"] = round(float(row[2]), 2)
                    entry["hst"] = round(float(row[3]), 2)
                    entry["pst"] = round(float(row[4]), 2)
                    entry["other"] = round(float(row[5]), 2)
                    entry["total_tax"] = round(
                        float(row[2]) + float(row[3]) + float(row[4]) + float(row[5]), 2
                    )
                    entry["taxed_receipt_count"] = int(row[6])
                    entry["receipt_count"] = int(row[7])

                cur.execute(
                    f"""SELECT COALESCE(NULLIF(d.currency, ''), 'CAD') AS currency,
                               SUM({gst}) AS gst,
                               SUM({hst}) AS hst,
                               SUM({pst}) AS pst,
                               SUM({other}) AS other,
                               COUNT(*) AS receipts
                        FROM documents d
                        WHERE d.user_id = %s
                          AND (d.document_date IS NULL
                               OR d.document_date !~ '^[0-9]{{4}}-[0-9]{{2}}')
                          AND {any_tax}
                        GROUP BY 1
                        ORDER BY 1""",
                    (self.user_id,),
                )
                for row in cur.fetchall():
                    undated.append({
                        "currency": row[0],
                        "gst": round(float(row[1]), 2),
                        "hst": round(float(row[2]), 2),
                        "pst": round(float(row[3]), 2),
                        "other": round(float(row[4]), 2),
                        "total_tax": round(
                            float(row[1]) + float(row[2]) + float(row[3]) + float(row[4]), 2
                        ),
                        "receipt_count": int(row[5]),
                    })

        def _sum_rows(rows: list[dict], label_key: str, label_val: int) -> dict:
            out = {label_key: label_val, "gst": 0.0, "hst": 0.0, "pst": 0.0,
                   "other": 0.0, "total_tax": 0.0,
                   "taxed_receipt_count": 0, "receipt_count": 0}
            for r in rows:
                for k in ("gst", "hst", "pst", "other", "total_tax"):
                    out[k] = round(out[k] + r[k], 2)
                out["taxed_receipt_count"] += r["taxed_receipt_count"]
                out["receipt_count"] += r["receipt_count"]
            return out

        for cur_entry in currencies.values():
            months = cur_entry["months"]
            cur_entry["quarters"] = [
                _sum_rows(months[q * 3:q * 3 + 3], "quarter", q + 1)
                for q in range(4)
            ]
            cur_entry["total"] = _sum_rows(months, "year", year)

        return {
            "year": year,
            "currencies": sorted(currencies.values(), key=lambda c: c["currency"]),
            "undated": undated,
        }

    def get_recent_transactions(self, limit: int = 10) -> list[Transaction]:
        """Return the most recent transactions for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, account, transaction_type, date_posted, amount,
                              currency, description, source_file, source_row,
                              status, created_at, category, note
                       FROM transactions
                       WHERE user_id = %s
                       ORDER BY date_posted DESC, id DESC
                       LIMIT %s""",
                    (self.user_id, limit),
                )
                return [self._row_to_transaction(row) for row in cur.fetchall()]

    def get_transactions(
        self,
        page: int = 1,
        per_page: int = 50,
        account: str | None = None,
        status: str | None = None,
        month: str | None = None,
        search: str | None = None,
        source_file: str | None = None,
        category: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
    ) -> tuple[list[Transaction], int]:
        """Return paginated, filterable transactions.

        Args:
            page: 1-based page number.
            per_page: Items per page (max 200).
            account: Filter by account type (e.g. 'CAD', 'USD').
            status: Filter by status ('UNMATCHED', 'MATCHED', 'IGNORED').
            month: Filter by month prefix (e.g. '2026-03').
            search: Exhaust-every-criterion search (see
                _build_txn_search_predicate): exact TXN id (digits, '#'
                tolerated), case-insensitive substring + regex across the text
                columns (description, category, account, note, date, currency,
                status, source_file), and — when the input parses as money
                ("$1,234.50", "12.50", "50") — an exact ABS(amount) match.
            min_amount: Keep rows with ABS(amount) >= this value.
            max_amount: Keep rows with ABS(amount) <= this value.

        Returns:
            (list of Transaction, total count matching filters)
        """
        per_page = min(per_page, 200)
        offset = (max(page, 1) - 1) * per_page

        # Validate status against allowed set; account is now free-form (any bank name).
        _ALLOWED_STATUSES = {"UNMATCHED", "MATCHED", "IGNORED", "LINKED",
                             "PENDING_REVIEW", "EXCLUDED"}
        if account is not None and (not account or len(account.strip()) == 0):
            raise ValueError(f"Invalid account filter: {account!r}")
        if status is not None and status not in _ALLOWED_STATUSES:
            raise ValueError(f"Invalid status filter: {status!r}")

        # SAFETY: All conditions below are hardcoded SQL fragments with %s
        # placeholders — no user input is interpolated into the SQL structure.
        conditions = ["user_id = %s"]
        params: list = [self.user_id]

        if account is not None:
            conditions.append("account = %s")
            params.append(account)
        if status is not None:
            conditions.append("status = %s")
            params.append(status)
        if month is not None:
            conditions.append("date_posted LIKE %s")
            params.append(f"{month}-%")
        if search is not None:
            # All-columns substring+regex predicate (id/description/category/
            # account/note/amount/date/currency/status/source_file) — must stay
            # in lockstep with get_transaction_page or jump-to-row breaks.
            predicate, p_params = self.txn_search_predicate(search)
            conditions.append(predicate)
            params.extend(p_params)
        if min_amount is not None:
            conditions.append("ABS(CAST(amount AS NUMERIC)) >= %s")
            params.append(min_amount)
        if max_amount is not None:
            conditions.append("ABS(CAST(amount AS NUMERIC)) <= %s")
            params.append(max_amount)
        if source_file is not None:
            conditions.append("source_file = %s")
            params.append(source_file)
        if category is not None:
            if category == "Uncategorized":
                conditions.append("category IS NULL")
            else:
                conditions.append("category = %s")
                params.append(category)
        if start_date is not None:
            conditions.append("date_posted >= %s")
            params.append(start_date)
        if end_date is not None:
            conditions.append("date_posted <= %s")
            params.append(end_date)

        where_clause = " AND ".join(conditions)

        # Sort support — whitelist allowed columns to prevent SQL injection
        _SORT_COLUMNS = {
            "date": "date_posted",
            "amount": "amount",
            "description": "description",
            "category": "category",
            "status": "status",
            "account": "account",
        }
        sort_col = _SORT_COLUMNS.get(sort_by or "", "date_posted")
        sort_direction = "DESC" if (sort_dir or "").upper() == "DESC" else "ASC"
        order_clause = f"{sort_col} {sort_direction}, id ASC"

        with self._conn() as conn:
            with conn.cursor() as cur:
                # Total count
                cur.execute(
                    f"SELECT COUNT(*) FROM transactions WHERE {where_clause}",  # noqa: S608
                    params,
                )
                total = cur.fetchone()[0]

                # Paginated results
                cur.execute(
                    f"""SELECT id, account, transaction_type, date_posted, amount,
                               currency, description, source_file, source_row,
                               status, created_at, category, note
                        FROM transactions
                        WHERE {where_clause}
                        ORDER BY {order_clause}
                        LIMIT %s OFFSET %s""",  # noqa: S608
                    params + [per_page, offset],
                )
                items = [self._row_to_transaction(row) for row in cur.fetchall()]

                return items, total

    # ------------------------------------------------------------------
    # Gmail OAuth states (CSRF protection for redirect flow)
    # ------------------------------------------------------------------

    def upsert_gmail_oauth_state(self, state: str) -> None:
        """Store a Gmail OAuth state nonce. Replaces any existing state for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Delete any existing pending state for this user
                cur.execute(
                    "DELETE FROM gmail_oauth_states WHERE user_id = %s",
                    (self.user_id,),
                )
                cur.execute(
                    """INSERT INTO gmail_oauth_states (user_id, state)
                       VALUES (%s, %s)""",
                    (self.user_id, state),
                )

    @staticmethod
    def validate_gmail_oauth_state(
        pool: "psycopg2.pool.ThreadedConnectionPool", state: str
    ) -> str | None:
        """Validate a Gmail OAuth state and return the user_id if valid.

        This is a STATIC method — not scoped to self.user_id — because the
        GET callback from Google has no JWT (no authenticated user context).

        Returns user_id if state exists and is < 10 minutes old, then deletes it.
        Returns None if not found or expired.
        """
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT user_id FROM gmail_oauth_states
                       WHERE state = %s
                         AND created_at > NOW() - INTERVAL '10 minutes'""",
                    (state,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None
                user_id = str(row[0])
                cur.execute(
                    "DELETE FROM gmail_oauth_states WHERE state = %s",
                    (state,),
                )
            conn.commit()
            return user_id
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    # ------------------------------------------------------------------
    # Setup checklist (derived from existing data)
    # ------------------------------------------------------------------

    def get_checklist_state(self) -> dict:
        """Derive setup checklist state from existing data. No dedicated table needed."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                        EXISTS (SELECT 1 FROM business_profile WHERE user_id = %s AND company_name IS NOT NULL),
                        EXISTS (SELECT 1 FROM transactions WHERE user_id = %s),
                        EXISTS (SELECT 1 FROM gather_sources WHERE user_id = %s AND source_type = 'gmail' AND enabled = 1),
                        EXISTS (SELECT 1 FROM gather_sources WHERE user_id = %s AND enabled = 1),
                        EXISTS (SELECT 1 FROM documents WHERE user_id = %s),
                        EXISTS (SELECT 1 FROM reconciliation_matches WHERE user_id = %s),
                        (SELECT COUNT(*) FROM transactions WHERE user_id = %s),
                        (SELECT COUNT(*) FROM transactions WHERE user_id = %s AND status = 'MATCHED'),
                        (SELECT COUNT(*) FROM documents WHERE user_id = %s),
                        (SELECT COUNT(*) FROM documents WHERE user_id = %s AND status = 'MATCHED'),
                        (SELECT COUNT(*) FROM reconciliation_matches WHERE user_id = %s AND status = 'PENDING_REVIEW'),
                        (SELECT COUNT(*) FROM gather_sources WHERE user_id = %s AND enabled = 1)
                    """,
                    (self.user_id,) * 12,
                )
                row = cur.fetchone()
                profile = self.get_business_profile()
                config = profile.get("config_json") or {} if profile else {}
                dismissed = config.get("checklist_dismissed", False)
                return {
                    "profile_complete": row[0],
                    "bank_connected": row[1],
                    "email_connected": row[2],
                    "any_source_connected": row[3],
                    "first_receipt_uploaded": row[4],
                    "first_reconciliation_done": row[5],
                    "dismissed": dismissed,
                    "total_transactions": row[6],
                    "matched_transactions": row[7],
                    "total_documents": row[8],
                    "matched_documents": row[9],
                    "pending_reviews": row[10],
                    "enabled_sources": row[11],
                }

    def dismiss_checklist(self) -> None:
        """Mark the setup checklist as dismissed. Stored in config_json."""
        profile = self.get_business_profile()
        config = profile.get("config_json") or {} if profile else {}
        config["checklist_dismissed"] = True
        self.upsert_business_profile({"config_json": config})

    # ------------------------------------------------------------------
    # Month close/reopen
    # ------------------------------------------------------------------

    def close_month(self, year: int, month: int, closed_by: str, notes: str | None = None) -> None:
        """Close a month for reporting. Idempotent."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO month_status (user_id, year, month, status, closed_at, closed_by, notes)
                       VALUES (%s, %s, %s, 'CLOSED', NOW(), %s, %s)
                       ON CONFLICT (user_id, year, month)
                       DO UPDATE SET
                           status = 'CLOSED',
                           closed_at = NOW(),
                           closed_by = EXCLUDED.closed_by,
                           notes = COALESCE(EXCLUDED.notes, month_status.notes),
                           updated_at = NOW()""",
                    (self.user_id, year, month, closed_by, notes),
                )

    def reopen_month(self, year: int, month: int) -> None:
        """Reopen a previously closed month."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE month_status
                       SET status = 'OPEN', closed_at = NULL, closed_by = NULL, updated_at = NOW()
                       WHERE user_id = %s AND year = %s AND month = %s""",
                    (self.user_id, year, month),
                )

    def get_month_statuses(self, year: int) -> dict[int, dict]:
        """Return month close statuses for a year. Returns {month: {status, closed_at, ...}}."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT month, status, closed_at, closed_by, notes
                       FROM month_status
                       WHERE user_id = %s AND year = %s""",
                    (self.user_id, year),
                )
                result = {}
                for row in cur.fetchall():
                    result[row[0]] = {
                        "status": row[1],
                        "closed_at": row[2],
                        "closed_by": row[3],
                        "notes": row[4],
                    }
                return result

    def is_month_closed(self, year: int, month: int) -> bool:
        """Check if a month is closed. Returns False if no row exists (implicit OPEN)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status FROM month_status
                       WHERE user_id = %s AND year = %s AND month = %s""",
                    (self.user_id, year, month),
                )
                row = cur.fetchone()
                return row is not None and row[0] == 'CLOSED'

    # ------------------------------------------------------------------
    # Audit log (CRA audit trail)
    # ------------------------------------------------------------------

    def insert_audit_log(
        self,
        entity_type: str,
        entity_id: int,
        action: str,
        performed_by: str | None = None,
        old_value: dict | str | None = None,
        new_value: dict | str | None = None,
    ) -> int:
        """Insert an audit log entry. Returns the new row ID.

        ``old_value``/``new_value`` are TEXT columns rendered straight to the
        user on /activity as "From: X → To: Y". A dict is stored as JSON (what
        every existing caller passes); a plain string is stored verbatim, which
        is what a human-readable before → after wants — an accountant reading
        the trail should see ``Office expenses``, not ``{"category": "Office
        expenses"}``.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id,
                        entity_type,
                        entity_id,
                        action,
                        _audit_value(old_value),
                        _audit_value(new_value),
                        performed_by,
                    ),
                )
                return cur.fetchone()[0]

    def get_audit_log(
        self,
        entity_type: str | None = None,
        entity_id: int | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict], int]:
        """Return paginated audit log entries. Returns (entries, total_count)."""
        _ALLOWED_ENTITY_TYPES = {
            "match", "document", "transaction", "proof_bundle",
            "onboarding", "connection", "transaction_link",
            "export", "report", "settings", "session", "system",
        }
        if entity_type and entity_type not in _ALLOWED_ENTITY_TYPES:
            raise ValueError(f"Invalid entity_type filter: {entity_type!r}")

        with self._conn() as conn:
            with conn.cursor() as cur:
                # SAFETY: All conditions below are hardcoded SQL fragments with
                # %s placeholders — no user input is interpolated into SQL structure.
                where_clauses = ["user_id = %s"]
                params: list = [self.user_id]

                if entity_type:
                    where_clauses.append("entity_type = %s")
                    params.append(entity_type)
                if entity_id is not None:
                    where_clauses.append("entity_id = %s")
                    params.append(entity_id)

                where_sql = " AND ".join(where_clauses)

                cur.execute(
                    f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}",  # noqa: S608
                    params,
                )
                total = cur.fetchone()[0]

                offset = (page - 1) * per_page
                params.extend([per_page, offset])
                cur.execute(
                    f"""SELECT id, entity_type, entity_id, action, old_value, new_value,
                               performed_by, created_at
                        FROM audit_log WHERE {where_sql}
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s""",  # noqa: S608
                    params,
                )

                entries = []
                for row in cur.fetchall():
                    entries.append({
                        "id": row[0],
                        "entity_type": row[1],
                        "entity_id": row[2],
                        "action": row[3],
                        "old_value": json.loads(row[4]) if row[4] else None,
                        "new_value": json.loads(row[5]) if row[5] else None,
                        "performed_by": row[6],
                        "created_at": str(row[7]) if row[7] else None,
                    })

                return entries, total

    # ------------------------------------------------------------------
    # Reconciliation match lookups and mutations
    # ------------------------------------------------------------------

    def get_match_by_id(self, match_id: int) -> ReconciliationMatch | None:
        """Return a single reconciliation match by ID, or None."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, transaction_id, document_id, confidence_score,
                              amount_score, date_score, vendor_score,
                              match_type, status, reviewed_by, reviewed_at, created_at,
                              explanation, user_action, actioned_at
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (match_id, self.user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                match = ReconciliationMatch(
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
                    reviewed_at=_datetime_or_none(row[10]),
                    created_at=_datetime_or_none(row[11]),
                )
                # Attach new fields if the model supports them
                if hasattr(match, "explanation"):
                    match.explanation = row[12]
                if hasattr(match, "user_action"):
                    match.user_action = row[13]
                if hasattr(match, "actioned_at"):
                    match.actioned_at = _datetime_or_none(row[14])
                return match

    def delete_match(self, match_id: int) -> bool:
        """Delete a reconciliation match. Returns True if a row was deleted.

        Re-derives both sides in the same transaction — a hard-deleted match
        that leaves its transaction MATCHED is exactly the inconsistency the
        startup repair has to clean up (server/jobs/status_repair.py).
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s
                       RETURNING id, transaction_id, document_id""",
                    (match_id, self.user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                self._resync_transaction_statuses(cur, [row[1]])
                self._resync_document_statuses(cur, [row[2]])
                return True

    def delete_ledger_entries_by_match(self, match_id: int) -> int:
        """Delete ledger entries associated with a match. Returns count deleted."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ledger_entries WHERE match_id = %s AND user_id = %s",
                    (match_id, self.user_id),
                )
                return cur.rowcount

    def update_match_explanation(self, match_id: int, explanation: str) -> None:
        """Set the natural language explanation for why a match was made.

        Called by the reconciliation engine after matching. Example:
        "Matched a $45.99 receipt dated Jan 15 to a $45.99 debit posted Jan 17 (2-day clearing delay)"
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET explanation = %s
                       WHERE id = %s AND user_id = %s""",
                    (explanation, match_id, self.user_id),
                )

    def get_matches_paginated(
        self,
        status: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict], int]:
        """Return paginated matches with transaction and document summaries.

        Returns (matches_list, total_count).
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                where_clauses = ["rm.user_id = %s"]
                params: list = [self.user_id]

                if status:
                    where_clauses.append("rm.status = %s")
                    params.append(status)

                where_sql = " AND ".join(where_clauses)

                # Count
                cur.execute(
                    f"SELECT COUNT(*) FROM reconciliation_matches rm WHERE {where_sql}",  # noqa: S608
                    params,
                )
                total = cur.fetchone()[0]

                # Paginated with joins
                offset = (page - 1) * per_page
                query_params = list(params) + [per_page, offset]
                cur.execute(
                    f"""SELECT rm.id, rm.transaction_id, rm.document_id, rm.confidence_score,
                               rm.amount_score, rm.date_score, rm.vendor_score,
                               rm.match_type, rm.status, rm.explanation, rm.created_at,
                               t.date_posted, t.amount, t.currency, t.description,
                               d.vendor, d.document_date, d.total, d.original_filename
                        FROM reconciliation_matches rm
                        JOIN transactions t ON rm.transaction_id = t.id
                        JOIN documents d ON rm.document_id = d.id
                        WHERE {where_sql}
                        ORDER BY rm.created_at DESC
                        LIMIT %s OFFSET %s""",  # noqa: S608
                    query_params,
                )

                matches = []
                for row in cur.fetchall():
                    matches.append({
                        "id": row[0],
                        "transaction_id": row[1],
                        "document_id": row[2],
                        "confidence_score": row[3],
                        "amount_score": row[4],
                        "date_score": row[5],
                        "vendor_score": row[6],
                        "match_type": row[7],
                        "status": row[8],
                        "explanation": row[9],
                        "created_at": str(row[10]) if row[10] else None,
                        "transaction_summary": {
                            "date": row[11],
                            "amount": row[12],
                            "currency": row[13],
                            "description": row[14],
                        },
                        "document_summary": {
                            "vendor": row[15],
                            "date": row[16],
                            "total": row[17],
                            "filename": row[18],
                        },
                    })

                return matches, total

    def get_match_queue_counts(self) -> dict[str, int]:
        """Return count of matches grouped by status."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM reconciliation_matches
                       WHERE user_id = %s GROUP BY status""",
                    (self.user_id,),
                )
                return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Connection health
    # ------------------------------------------------------------------

    def upsert_connection_health(
        self,
        source: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Insert or update connection health for a source."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO connection_health
                       (user_id, source, status, last_checked, last_success, error_message)
                       VALUES (%s, %s, %s, NOW(),
                               CASE WHEN %s = 'HEALTHY' THEN NOW() ELSE NULL END,
                               %s)
                       ON CONFLICT (user_id, source) DO UPDATE SET
                           status = EXCLUDED.status,
                           last_checked = NOW(),
                           last_success = CASE WHEN EXCLUDED.status = 'HEALTHY'
                                               THEN NOW()
                                               ELSE connection_health.last_success END,
                           error_message = EXCLUDED.error_message,
                           updated_at = NOW()""",
                    (self.user_id, source, status, status, error_message),
                )

    def get_connection_health(self, source: str | None = None) -> list[dict]:
        """Return connection health records. If source specified, filter to that source."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                if source:
                    cur.execute(
                        """SELECT source, status, last_checked, last_success, error_message
                           FROM connection_health
                           WHERE user_id = %s AND source = %s""",
                        (self.user_id, source),
                    )
                else:
                    cur.execute(
                        """SELECT source, status, last_checked, last_success, error_message
                           FROM connection_health
                           WHERE user_id = %s
                           ORDER BY source""",
                        (self.user_id,),
                    )
                return [
                    {
                        "source": row[0],
                        "status": row[1],
                        "last_checked": str(row[2]) if row[2] else None,
                        "last_success": str(row[3]) if row[3] else None,
                        "error_message": row[4],
                    }
                    for row in cur.fetchall()
                ]

    # ------------------------------------------------------------------
    # Onboarding steps
    # ------------------------------------------------------------------

    def get_onboarding_steps(self) -> dict[str, str]:
        """Return onboarding step statuses as {step: status} dict.

        Steps not yet in the table default to 'PENDING'.
        """
        all_steps = ["profile", "bank_connect", "receipt_upload", "first_reconciliation", "export"]
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT step, status FROM onboarding_steps WHERE user_id = %s",
                    (self.user_id,),
                )
                found = {row[0]: row[1] for row in cur.fetchall()}
        return {step: found.get(step, "PENDING") for step in all_steps}

    def upsert_onboarding_step(self, step: str, status: str) -> None:
        """Insert or update an onboarding step status."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO onboarding_steps (user_id, step, status, completed_at)
                       VALUES (%s, %s, %s, CASE WHEN %s != 'PENDING' THEN NOW() ELSE NULL END)
                       ON CONFLICT (user_id, step) DO UPDATE SET
                           status = EXCLUDED.status,
                           completed_at = CASE WHEN EXCLUDED.status != 'PENDING'
                                               THEN NOW()
                                               ELSE onboarding_steps.completed_at END""",
                    (self.user_id, step, status, status),
                )

    # ------------------------------------------------------------------
    # Document field updates (correction workflow)
    # ------------------------------------------------------------------

    def update_document_fields(self, doc_id: int, updates: dict) -> dict | None:
        """Update specific fields on a document. Returns previous values of updated fields, or None if doc not found.

        ``updates`` keys must be valid column names from: vendor, document_date, total, currency,
        subtotal, tax_gst, tax_hst, tax_pst, payment_method, invoice_number, review_status.
        """
        allowed_columns = {
            "vendor", "document_date", "total", "currency", "subtotal",
            "tax_gst", "tax_hst", "tax_pst", "payment_method", "invoice_number", "review_status",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed_columns and v is not None}
        if not filtered:
            return None

        with self._conn() as conn:
            with conn.cursor() as cur:
                # Fetch old values — use psql.Identifier for column names to
                # prevent SQL injection even though keys are already whitelist-filtered.
                cols_sql = psql.SQL(", ").join(psql.Identifier(c) for c in filtered.keys())
                query = psql.SQL("SELECT {} FROM documents WHERE id = %s AND user_id = %s").format(cols_sql)
                cur.execute(query, (doc_id, self.user_id))
                row = cur.fetchone()
                if row is None:
                    return None
                old_values = dict(zip(filtered.keys(), row))

                # Build SET clause with safe identifier composition
                set_sql = psql.SQL(", ").join(
                    psql.SQL("{} = %s").format(psql.Identifier(col)) for col in filtered.keys()
                )
                params = list(filtered.values()) + [doc_id, self.user_id]

                query = psql.SQL("UPDATE documents SET {} WHERE id = %s AND user_id = %s").format(set_sql)
                cur.execute(query, params)
                return old_values

    # ------------------------------------------------------------------
    # Scan runs (Gmail scan history)
    # ------------------------------------------------------------------

    def insert_scan_run(
        self,
        source: str = "gmail",
        since_date: datetime | None = None,
        until_date: datetime | None = None,
    ) -> int:
        """Create a new scan run. Returns the run ID."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scan_runs
                       (user_id, source, status, since_date, until_date)
                       VALUES (%s, %s, 'RUNNING', %s, %s)
                       RETURNING id""",
                    (self.user_id, source, since_date, until_date),
                )
                return cur.fetchone()[0]

    def update_scan_run(
        self,
        run_id: int,
        status: str | None = None,
        emails_scanned: int | None = None,
        documents_found: int | None = None,
        documents_added: int | None = None,
        documents_skipped: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update a scan run with progress or completion data."""
        updates = []
        params = []
        if status is not None:
            updates.append("status = %s")
            params.append(status)
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                updates.append("completed_at = NOW()")
        if emails_scanned is not None:
            updates.append("emails_scanned = %s")
            params.append(emails_scanned)
        if documents_found is not None:
            updates.append("documents_found = %s")
            params.append(documents_found)
        if documents_added is not None:
            updates.append("documents_added = %s")
            params.append(documents_added)
        if documents_skipped is not None:
            updates.append("documents_skipped = %s")
            params.append(documents_skipped)
        if error_message is not None:
            updates.append("error_message = %s")
            params.append(error_message)
        if not updates:
            return
        params.extend([run_id, self.user_id])
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE scan_runs SET {', '.join(updates)} "  # noqa: S608
                    "WHERE id = %s AND user_id = %s",
                    params,
                )

    def get_scan_run(self, run_id: int) -> dict | None:
        """Get a single scan run by ID."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, status, since_date, until_date,
                              emails_scanned, documents_found, documents_added,
                              documents_skipped, error_message, started_at,
                              completed_at
                       FROM scan_runs
                       WHERE id = %s AND user_id = %s""",
                    (run_id, self.user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0], "source": row[1], "status": row[2],
                    "since_date": str(row[3]) if row[3] else None,
                    "until_date": str(row[4]) if row[4] else None,
                    "emails_scanned": row[5], "documents_found": row[6],
                    "documents_added": row[7], "documents_skipped": row[8],
                    "error_message": row[9],
                    "started_at": str(row[10]) if row[10] else None,
                    "completed_at": str(row[11]) if row[11] else None,
                }

    def get_scan_runs(self, limit: int = 20) -> list[dict]:
        """List recent scan runs."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, status, since_date, until_date,
                              emails_scanned, documents_found, documents_added,
                              documents_skipped, error_message, started_at,
                              completed_at
                       FROM scan_runs
                       WHERE user_id = %s
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (self.user_id, limit),
                )
                return [
                    {
                        "id": row[0], "source": row[1], "status": row[2],
                        "since_date": str(row[3]) if row[3] else None,
                        "until_date": str(row[4]) if row[4] else None,
                        "emails_scanned": row[5], "documents_found": row[6],
                        "documents_added": row[7], "documents_skipped": row[8],
                        "error_message": row[9],
                        "started_at": str(row[10]) if row[10] else None,
                        "completed_at": str(row[11]) if row[11] else None,
                    }
                    for row in cur.fetchall()
                ]

    def get_active_scan_run(self) -> dict | None:
        """Get the currently running scan, if any."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, status, since_date, until_date,
                              emails_scanned, documents_found, documents_added,
                              documents_skipped, error_message, started_at,
                              completed_at
                       FROM scan_runs
                       WHERE user_id = %s AND status = 'RUNNING'
                       ORDER BY started_at DESC
                       LIMIT 1""",
                    (self.user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0], "source": row[1], "status": row[2],
                    "since_date": str(row[3]) if row[3] else None,
                    "until_date": str(row[4]) if row[4] else None,
                    "emails_scanned": row[5], "documents_found": row[6],
                    "documents_added": row[7], "documents_skipped": row[8],
                    "error_message": row[9],
                    "started_at": str(row[10]) if row[10] else None,
                    "completed_at": str(row[11]) if row[11] else None,
                }

    # ------------------------------------------------------------------
    # Gmail OAuth consents
    # ------------------------------------------------------------------

    def record_gmail_consent(self) -> None:
        """Record that this user granted Gmail OAuth consent."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gmail_oauth_consents (user_id, granted_at)
                       VALUES (%s, NOW())
                       ON CONFLICT (user_id) DO UPDATE SET
                           granted_at = NOW(),
                           revoked_at = NULL""",
                    (self.user_id,),
                )

    def revoke_gmail_consent(self) -> None:
        """Mark this user's Gmail OAuth consent as revoked."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE gmail_oauth_consents
                       SET revoked_at = NOW()
                       WHERE user_id = %s AND revoked_at IS NULL""",
                    (self.user_id,),
                )

    def update_gather_source_account_email(self, source_type: str, email: str) -> None:
        """Store the connected account email on a gather source."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE gather_sources
                       SET account_email = %s, updated_at = NOW()
                       WHERE user_id = %s AND source_type = %s""",
                    (email, self.user_id, source_type),
                )

    def get_gather_source_account_email(self, source_type: str) -> str | None:
        """Get the connected account email for a gather source."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT account_email FROM gather_sources
                       WHERE user_id = %s AND source_type = %s""",
                    (self.user_id, source_type),
                )
                row = cur.fetchone()
                return row[0] if row else None

    # ------------------------------------------------------------------
    # Categorization agent — taxonomy, vendor cache, run audit
    # ------------------------------------------------------------------

    def get_user_categories(self) -> list[dict]:
        """Return the per-user category taxonomy."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, name, definition, umbrella_for, example_vendors,
                              kind, is_system
                       FROM user_categories
                       WHERE user_id = %s
                       ORDER BY name""",
                    (self.user_id,),
                )
                rows = cur.fetchall()
        result = []
        for row in rows:
            example_vendors = []
            if row[4]:
                try:
                    parsed = json.loads(row[4])
                    if isinstance(parsed, list):
                        example_vendors = parsed
                except (ValueError, TypeError):
                    pass
            result.append({
                "id": row[0],
                "name": row[1],
                "definition": row[2],
                "umbrella_for": row[3],
                "example_vendors": example_vendors,
                "kind": row[5],
                "is_system": bool(row[6]),
            })
        return result

    def upsert_user_categories(self, categories: list[dict]) -> None:
        """Insert or update a list of category taxonomy rows for this user.

        ``categories`` is a list of dicts with keys: name, definition,
        umbrella_for, example_vendors (list), kind, is_system (optional).
        """
        if not categories:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                for c in categories:
                    name = c.get("name")
                    if not name:
                        continue
                    example_json = json.dumps(c.get("example_vendors") or [])
                    cur.execute(
                        """INSERT INTO user_categories
                           (user_id, name, definition, umbrella_for,
                            example_vendors, kind, is_system, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                           ON CONFLICT (user_id, name) DO UPDATE SET
                               definition = COALESCE(EXCLUDED.definition, user_categories.definition),
                               umbrella_for = COALESCE(EXCLUDED.umbrella_for, user_categories.umbrella_for),
                               example_vendors = EXCLUDED.example_vendors,
                               kind = COALESCE(EXCLUDED.kind, user_categories.kind),
                               is_system = EXCLUDED.is_system,
                               updated_at = NOW()""",
                        (
                            self.user_id,
                            name,
                            c.get("definition"),
                            c.get("umbrella_for"),
                            example_json,
                            c.get("kind"),
                            bool(c.get("is_system", False)),
                        ),
                    )

    def seed_fixed_taxonomy(self) -> None:
        """Seed/upgrade the 27 fixed Canada ISED/GIFI system categories for this user.

        Idempotent upsert, carrying each category's accounting ``kind`` and
        ``is_system=TRUE``. Provides the per-user category rows the
        ``vendor_category_cache.category_id`` FK and agent persistence depend
        on for a brand-new user. The agent's ``persist_run``
        also self-seeds via ``upsert_user_categories`` on every run, so this is a
        lazy safety net. "Uncategorized" is NOT seeded (it is the NULL sentinel).
        """
        from config.ised_categories import taxonomy_upsert_rows

        self.upsert_user_categories(taxonomy_upsert_rows())

    def get_distinct_unmapped_confirmed_categories(self) -> list[str]:
        """Confirmed categories the migration could not map (for the LLM backfill pass)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT category FROM transactions
                       WHERE user_id = %s
                         AND category_reasoning = 'legacy_unmapped_confirmed'
                         AND category IS NOT NULL""",
                    (self.user_id,),
                )
                return [row[0] for row in cur.fetchall()]

    def remap_category(self, old: str, new: str) -> int:
        """Rewrite every row of this user with category == ``old`` to ``new`` (llm_backfill)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions
                       SET category = %s, category_source = 'llm_backfill'
                       WHERE user_id = %s AND category = %s""",
                    (new, self.user_id, old),
                )
                return cur.rowcount or 0

    def rename_user_category(self, from_name: str, to_name: str) -> None:
        """Rename a taxonomy entry in-place (preserves vendor_category_cache FK)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_categories SET name = %s, updated_at = NOW()
                       WHERE user_id = %s AND name = %s""",
                    (to_name, self.user_id, from_name),
                )
                cur.execute(
                    """UPDATE transactions SET category = %s
                       WHERE user_id = %s
                         AND category = %s
                         AND COALESCE(category_confirmed_by_user, FALSE) = FALSE""",
                    (to_name, self.user_id, from_name),
                )

    def delete_user_category(self, name: str) -> None:
        """Remove a taxonomy entry. Caller must have already reassigned any
        transactions and vendor cache entries that point to it."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_categories WHERE user_id = %s AND name = %s",
                    (self.user_id, name),
                )

    def clear_vendor_cache(self) -> int:
        """Delete every vendor_category_cache row for this user.

        Called from the "Recategorize All" flow so a stale cached decision
        from a prior run (e.g. a vendor_key that was incorrectly clustered
        and assigned the wrong category) can never short-circuit the fresh
        LLM decision pass.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vendor_category_cache WHERE user_id = %s",
                    (self.user_id,),
                )
                return cur.rowcount or 0

    def get_vendor_cache(self) -> dict[str, dict]:
        """Return the vendor_key -> cached decision map for this user."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT vc.vendor_key, uc.name, vc.confidence,
                              vc.reasoning, vc.sample_desc, vc.updated_at
                       FROM vendor_category_cache vc
                       LEFT JOIN user_categories uc
                              ON uc.id = vc.category_id AND uc.user_id = vc.user_id
                       WHERE vc.user_id = %s""",
                    (self.user_id,),
                )
                rows = cur.fetchall()
        return {
            row[0]: {
                "vendor_key": row[0],
                "category_name": row[1],
                "confidence": row[2],
                "reasoning": row[3],
                "sample_desc": row[4],
                "updated_at": row[5],
            }
            for row in rows
        }

    def upsert_vendor_cache(self, entries: list[dict]) -> None:
        """Insert or update vendor->category cache rows.

        ``entries`` is a list of dicts with keys: vendor_key, category_name,
        confidence, reasoning, sample_desc. Category is resolved by name
        against user_categories; unknown category names map to NULL.
        """
        if not entries:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT name, id FROM user_categories
                       WHERE user_id = %s""",
                    (self.user_id,),
                )
                name_to_id = {row[0]: row[1] for row in cur.fetchall()}
                for e in entries:
                    vendor_key = e.get("vendor_key")
                    if not vendor_key:
                        continue
                    cat_id = name_to_id.get(e.get("category_name") or "")
                    cur.execute(
                        """INSERT INTO vendor_category_cache
                           (user_id, vendor_key, category_id, confidence,
                            reasoning, sample_desc, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, NOW())
                           ON CONFLICT (user_id, vendor_key) DO UPDATE SET
                               category_id = EXCLUDED.category_id,
                               confidence = EXCLUDED.confidence,
                               reasoning = EXCLUDED.reasoning,
                               sample_desc = EXCLUDED.sample_desc,
                               updated_at = NOW()""",
                        (
                            self.user_id,
                            vendor_key,
                            cat_id,
                            e.get("confidence"),
                            e.get("reasoning"),
                            e.get("sample_desc"),
                        ),
                    )

    def get_confirmed_categorizations(self) -> list[dict]:
        """Return the user's locked (confirmed) category assignments.

        These are the single most valuable signal for taxonomy synthesis —
        treat them as ground truth and never overwrite them.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, description, amount, currency, category
                       FROM transactions
                       WHERE user_id = %s
                         AND category_confirmed_by_user = TRUE
                         AND category IS NOT NULL""",
                    (self.user_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "txn_id": row[0],
                "description": row[1],
                "amount": float(row[2]) if row[2] is not None else 0.0,
                "currency": row[3],
                "category": row[4],
            }
            for row in rows
        ]

    def load_categorization_proof_context(self) -> dict[int, dict]:
        """Bulk-load matched-document + link-chain proof context for every txn.

        For every transaction owned by this user, return the proof evidence
        (vendor, total, line items, invoice number) that categorization
        should use as its primary signal. A transaction gets a proof if
        either:

        (a) it has a direct ``reconciliation_matches`` row joining to a
            ``documents`` row **that has been approved** (AUTO_APPROVED or
            USER_APPROVED), or
        (b) it is reachable via an **approved** ``transaction_links`` chain
            from another transaction that does.

        Approved, not merely "not rejected". A PENDING_REVIEW match is the
        engine's guess awaiting a human — exactly the case the matcher was
        unsure about — and treating it as proof would let one weak, unreviewed
        pair rewrite a transaction's identity: the transaction would take the
        vendor of a receipt it had only been tentatively paired with, and be
        categorized off the back of it. Rejecting the match afterwards would
        not undo that, because the vendor key has already been written. Proof
        means a decision someone stands behind.

        Walks the link graph breadth-first from every matched transaction, so
        a row that is linked but unmatched — a deposit feeding an FX
        conversion that does match a receipt — inherits the proof from the
        nearest matched sibling. Only one proof is kept per transaction: the
        direct match if there is one, else the shortest-distance linked match.
        """
        from collections import deque

        matches_by_txn: dict[int, dict] = {}
        adj: dict[int, set[int]] = {}

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rm.transaction_id, d.vendor, d.total, d.currency,
                              d.line_items_json, d.invoice_number, d.document_date,
                              d.original_filename, rm.confidence_score
                       FROM reconciliation_matches rm
                       JOIN documents d ON d.id = rm.document_id
                       WHERE rm.user_id = %s
                         AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')
                       ORDER BY rm.confidence_score DESC NULLS LAST""",
                    (self.user_id,),
                )
                for row in cur.fetchall():
                    txn_id = row[0]
                    if txn_id in matches_by_txn:
                        continue  # Keep the highest-confidence match only.
                    matches_by_txn[txn_id] = {
                        "vendor": (row[1] or "").strip() or None,
                        "total": str(row[2]) if row[2] is not None else None,
                        "currency": row[3],
                        "line_items_json": row[4],
                        "invoice_number": row[5],
                        "document_date": row[6],
                        "filename": row[7],
                    }

                cur.execute(
                    """SELECT source_transaction_id, target_transaction_id
                       FROM transaction_links
                       WHERE user_id = %s
                         AND status IN ('AUTO_APPROVED', 'USER_APPROVED')""",
                    (self.user_id,),
                )
                for src, tgt in cur.fetchall():
                    adj.setdefault(src, set()).add(tgt)
                    adj.setdefault(tgt, set()).add(src)

        result: dict[int, dict] = {
            txn_id: {**doc, "source": "direct", "link_depth": 0}
            for txn_id, doc in matches_by_txn.items()
        }

        # BFS from every matched seed so linked rows inherit the nearest
        # direct proof. Seeds are scanned in descending match confidence
        # (already sorted above) so ties go to the more trusted match.
        for seed, doc in matches_by_txn.items():
            visited = {seed}
            queue = deque([(seed, 0)])
            while queue:
                node, depth = queue.popleft()
                if depth >= 20:
                    continue
                for neighbor in adj.get(node, ()):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    if neighbor in matches_by_txn:
                        continue  # Has its own direct proof; don't overwrite.
                    existing = result.get(neighbor)
                    if existing and existing.get("link_depth", 0) <= depth + 1:
                        continue
                    result[neighbor] = {
                        **doc,
                        "source": "linked",
                        "link_depth": depth + 1,
                    }

        return result

    def get_transactions_for_categorization(self, scope: str) -> list[dict]:
        """Fetch all in-scope transactions for the categorization agent.

        ``scope`` is either ``"all"`` (every transaction this user owns) or
        ``"uncategorized"`` (only those with NULL / empty category).
        """
        where = "WHERE user_id = %s"
        if scope == "uncategorized":
            where += " AND (category IS NULL OR category = '')"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT id, date_posted, amount, currency, account,
                               description, category,
                               COALESCE(category_confirmed_by_user, FALSE),
                               vendor_key, note
                        FROM transactions
                        {where}
                        ORDER BY date_posted""",
                    (self.user_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "date_posted": row[1],
                "amount": float(row[2]) if row[2] is not None else 0.0,
                "currency": row[3],
                "account": row[4],
                "description": row[5],
                "category": row[6],
                "category_confirmed_by_user": bool(row[7]),
                "vendor_key": row[8],
                "note": row[9],
            }
            for row in rows
        ]

    def update_transaction_categorizations(self, updates: list[dict]) -> int:
        """Bulk update categorization fields for transactions.

        Each item must include ``id`` and may include ``category``,
        ``category_source``, ``category_confidence``, ``category_reasoning``,
        and ``vendor_key``. Rows where ``category_confirmed_by_user = TRUE``
        are NEVER touched — the hard lock is enforced at the WHERE clause.

        Returns the number of rows actually updated.
        """
        if not updates:
            return 0
        updated = 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                for u in updates:
                    txn_id = u.get("id")
                    if txn_id is None:
                        continue
                    cur.execute(
                        """UPDATE transactions
                           SET category = COALESCE(%s, category),
                               category_source = COALESCE(%s, category_source),
                               category_confidence = COALESCE(%s, category_confidence),
                               category_reasoning = COALESCE(%s, category_reasoning),
                               vendor_key = COALESCE(%s, vendor_key)
                           WHERE id = %s AND user_id = %s
                             AND COALESCE(category_confirmed_by_user, FALSE) = FALSE""",
                        (
                            u.get("category"),
                            u.get("category_source"),
                            u.get("category_confidence"),
                            u.get("category_reasoning"),
                            u.get("vendor_key"),
                            txn_id,
                            self.user_id,
                        ),
                    )
                    updated += cur.rowcount
        return updated

    def confirm_transaction_category(self, txn_id: int, category: str) -> bool:
        """Set the category on a transaction and hard-lock it.

        Confirmed categorizations are authoritative; the agent will never
        overwrite them and will treat them as ground truth in Phase 3.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions
                       SET category = %s,
                           category_confirmed_by_user = TRUE,
                           category_source = 'user'
                       WHERE id = %s AND user_id = %s""",
                    (category, txn_id, self.user_id),
                )
                return cur.rowcount > 0

    def insert_categorization_run(self, run: dict) -> int:
        """Insert a new categorization_run audit row and return its id."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO categorization_run
                       (user_id, started_at, completed_at, scope, total,
                        categorized, rule_locked, llm_calls, status, error,
                        summary_json)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.user_id,
                        run.get("started_at"),
                        run.get("completed_at"),
                        run.get("scope"),
                        run.get("total"),
                        run.get("categorized"),
                        run.get("rule_locked"),
                        run.get("llm_calls"),
                        run.get("status"),
                        run.get("error"),
                        json.dumps(run.get("summary")) if run.get("summary") else None,
                    ),
                )
                return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Statements (Migration 009 — LLM generic bank statement parser)
    # ------------------------------------------------------------------

    def insert_statement(
        self,
        *,
        user_id: str,
        source_file: str,
        source_file_hash: str | None,
        institution: str,
        account_type: str,
        account_number: str | None,
        currency: str,
        statement_period_start: date,
        statement_period_end: date,
        opening_balance: Decimal | None,
        closing_balance: Decimal | None,
        balance_matches: bool | None,
        balance_difference: Decimal | None,
        page_count: int | None,
        row_count: int | None,
        import_confidence: str | None,
        import_flags: list[str] | None,
        parse_method: str | None,
        llm_model: str | None,
        llm_tokens_in: int,
        llm_tokens_out: int,
        llm_cost_usd: float,
    ) -> str:
        """Insert a statement metadata row and return the UUID.

        Raises psycopg2.errors.UniqueViolation on duplicate (user_id, source_file_hash).
        """
        flags_json = json.dumps(import_flags) if import_flags else "[]"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO statements
                       (user_id, source_file, source_file_hash, institution,
                        account_type, account_number, currency,
                        statement_period_start, statement_period_end,
                        opening_balance, closing_balance,
                        balance_matches, balance_difference,
                        page_count, row_count,
                        import_confidence, import_flags,
                        parse_method, llm_model,
                        llm_tokens_in, llm_tokens_out, llm_cost_usd)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        user_id,
                        source_file,
                        source_file_hash,
                        institution,
                        account_type,
                        account_number,
                        currency,
                        statement_period_start.isoformat() if hasattr(statement_period_start, "isoformat") else str(statement_period_start),
                        statement_period_end.isoformat() if hasattr(statement_period_end, "isoformat") else str(statement_period_end),
                        str(opening_balance) if opening_balance is not None else None,
                        str(closing_balance) if closing_balance is not None else None,
                        balance_matches,
                        str(balance_difference) if balance_difference is not None else None,
                        page_count,
                        row_count,
                        import_confidence,
                        flags_json,
                        parse_method,
                        llm_model,
                        llm_tokens_in,
                        llm_tokens_out,
                        llm_cost_usd,
                    ),
                )
                return str(cur.fetchone()[0])

    def insert_statement_processing(
        self,
        *,
        user_id: str,
        source_file: str,
        source_file_hash: str | None,
        page_count: int | None = None,
    ) -> str:
        """Insert a placeholder statements row in the ``processing`` state.

        Called by the upload endpoint before the LLM parse starts, so the
        browser gets an id to track while the parse runs in the background.
        Everything descriptive (institution, period, balances, token counts)
        is still unknown and stays NULL until ``update_statement_parsed`` fills
        it in, which is why those columns are nullable.

        Raises psycopg2.errors.UniqueViolation on duplicate
        (user_id, source_file_hash): the content-hash dedupe fires here, before
        the model is asked to read the file rather than after.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO statements
                       (user_id, source_file, source_file_hash, page_count, status)
                       VALUES (%s, %s, %s, %s, 'processing')
                       RETURNING id""",
                    (user_id, source_file, source_file_hash, page_count),
                )
                return str(cur.fetchone()[0])

    def update_statement_parsed(
        self,
        statement_id: str,
        user_id: str,
        *,
        institution: str,
        account_type: str,
        account_number: str | None,
        currency: str,
        statement_period_start: date,
        statement_period_end: date,
        opening_balance: Decimal | None,
        closing_balance: Decimal | None,
        balance_matches: bool | None,
        balance_difference: Decimal | None,
        page_count: int | None,
        row_count: int | None,
        import_confidence: str | None,
        import_flags: list[str] | None,
        parse_method: str | None,
        llm_model: str | None,
        llm_tokens_in: int,
        llm_tokens_out: int,
        llm_cost_usd: float,
    ) -> int:
        """Fill in a ``processing`` row with the parse result and mark it imported.

        The async twin of ``insert_statement``: same columns, same values, same
        telemetry — an UPDATE instead of an INSERT because the row already
        exists by the time the model answers. Returns the number of rows
        updated (0 if the row was deleted meanwhile, e.g. by a re-upload of the
        same filename, which is not an error).
        """
        flags_json = json.dumps(import_flags) if import_flags else "[]"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE statements SET
                           institution = %s,
                           account_type = %s,
                           account_number = %s,
                           currency = %s,
                           statement_period_start = %s,
                           statement_period_end = %s,
                           opening_balance = %s,
                           closing_balance = %s,
                           balance_matches = %s,
                           balance_difference = %s,
                           page_count = %s,
                           row_count = %s,
                           import_confidence = %s,
                           import_flags = %s,
                           parse_method = %s,
                           llm_model = %s,
                           llm_tokens_in = %s,
                           llm_tokens_out = %s,
                           llm_cost_usd = %s,
                           status = 'imported',
                           status_message = NULL
                       WHERE id = %s AND user_id = %s""",
                    (
                        institution,
                        account_type,
                        account_number,
                        currency,
                        statement_period_start.isoformat() if hasattr(statement_period_start, "isoformat") else str(statement_period_start),
                        statement_period_end.isoformat() if hasattr(statement_period_end, "isoformat") else str(statement_period_end),
                        str(opening_balance) if opening_balance is not None else None,
                        str(closing_balance) if closing_balance is not None else None,
                        balance_matches,
                        str(balance_difference) if balance_difference is not None else None,
                        page_count,
                        row_count,
                        import_confidence,
                        flags_json,
                        parse_method,
                        llm_model,
                        llm_tokens_in,
                        llm_tokens_out,
                        llm_cost_usd,
                        statement_id,
                        user_id,
                    ),
                )
                return cur.rowcount

    def update_statement_status(
        self,
        statement_id: str,
        user_id: str,
        *,
        status: str,
        status_message: str | None = None,
        row_count: int | None = None,
        parse_method: str | None = None,
    ) -> int:
        """Move a statement to a terminal state (``imported`` / ``failed``).

        Used by the background parse for the legacy-fallback success (no LLM
        metadata to write) and for every failure, so no upload is ever left
        sitting in ``processing``. Returns rows updated.
        """
        sets = ["status = %s", "status_message = %s"]
        params: list = [status, status_message]
        if row_count is not None:
            sets.append("row_count = %s")
            params.append(row_count)
        if parse_method is not None:
            sets.append("parse_method = %s")
            params.append(parse_method)
        params.extend([statement_id, user_id])
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE statements SET {", ".join(sets)}
                        WHERE id = %s AND user_id = %s""",  # noqa: S608
                    params,
                )
                return cur.rowcount

    def find_statement_by_hash(self, source_file_hash: str) -> dict | None:
        """The statement these exact bytes were already imported as, if any.

        The ``statements_unique_hash`` UNIQUE constraint catches this on the
        INSERT, but by then the upload has already cleared the prior data for
        this filename. Asking first is what lets a duplicate be refused before
        that happens.

        Returns the row (id, source_file, status, created_at) or None.
        """
        if not source_file_hash:
            return None
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, source_file, status, row_count, created_at
                         FROM statements
                        WHERE user_id = %s AND source_file_hash = %s
                        ORDER BY created_at ASC
                        LIMIT 1""",
                    (self.user_id, source_file_hash),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def source_file_holding_period(
        self, account: str, date_from: date | str, date_to: date | str
    ) -> str | None:
        """Which statement tab holds this user's rows for that account and span.

        Used for one thing: a statement whose every row was already in the books
        imports nothing of its own, so it has no tab on the transactions page —
        and the queue's "View" link, which selects a tab by ``source_file``, used
        to silently land on whichever tab happened to be first. This answers
        "where did those rows actually go" so the link can go there instead.

        A navigation hint, deliberately: the heaviest-populated source file over
        the account and date span the duplicate rows cover. It is never used for
        a number the user reads or a ledger decision.

        ``date_posted`` is **TEXT** holding ISO ``YYYY-MM-DD``, not a DATE, so the
        bounds are bound as strings — handing psycopg2 a ``date`` makes Postgres
        look for a ``text >= date`` operator, find none, and raise. ISO strings
        sort lexicographically in date order, so BETWEEN over text is exact.
        """
        low = date_from.isoformat() if hasattr(date_from, "isoformat") else str(date_from)
        high = date_to.isoformat() if hasattr(date_to, "isoformat") else str(date_to)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source_file, COUNT(*) AS n
                         FROM transactions
                        WHERE user_id = %s AND account = %s
                          AND date_posted BETWEEN %s AND %s
                          AND source_file IS NOT NULL
                        GROUP BY source_file
                        ORDER BY n DESC, source_file ASC
                        LIMIT 1""",
                    (self.user_id, account, low, high),
                )
                row = cur.fetchone()
                return str(row[0]) if row else None

    def delete_statements_by_source_file(self, source_file: str) -> int:
        """Delete all ``statements`` rows for this user that came from the given source file.

        Needed by the replace-on-re-upload flow so that a re-uploaded statement does
        not collide with the ``statements_unique_hash`` UNIQUE constraint after the
        matching transactions have already been deleted.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM statements WHERE user_id = %s AND source_file = %s",
                    (self.user_id, source_file),
                )
                return cur.rowcount

    def get_statement_by_id(self, statement_id: str, user_id: str) -> dict | None:
        """Return a statement row as a dict, or None if not found / not owned by user."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT * FROM statements
                       WHERE id = %s AND user_id = %s""",
                    (statement_id, user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return dict(row)

    def list_statements(
        self,
        user_id: str,
        *,
        institution: str | None = None,
        confidence: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List statements for a user with optional filters."""
        conditions = ["user_id = %s"]
        params: list = [user_id]
        if institution is not None:
            conditions.append("institution = %s")
            params.append(institution)
        if confidence is not None:
            conditions.append("import_confidence = %s")
            params.append(confidence)
        where = " AND ".join(conditions)
        limit = min(limit, 200)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT * FROM statements
                        WHERE {where}
                        ORDER BY statement_period_end DESC, created_at DESC
                        LIMIT %s OFFSET %s""",  # noqa: S608
                    params + [limit, offset],
                )
                return [dict(row) for row in cur.fetchall()]

    def get_pending_review_transactions(
        self,
        user_id: str,
        statement_id: str | None = None,
    ) -> list[Transaction]:
        """Return all PENDING_REVIEW transactions for a user (optionally filtered to one statement)."""
        conditions = ["user_id = %s", "status = 'PENDING_REVIEW'"]
        params: list = [user_id]
        if statement_id is not None:
            conditions.append("statement_id = %s")
            params.append(statement_id)
        where = " AND ".join(conditions)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT id, account, transaction_type, date_posted, amount,
                               currency, description, source_file, source_row,
                               status, created_at, category, note,
                               institution, account_type, external_id,
                               import_confidence, import_flags, statement_id
                        FROM transactions
                        WHERE {where}
                        ORDER BY date_posted ASC, id ASC""",  # noqa: S608
                    params,
                )
                return [self._row_to_transaction_extended(row) for row in cur.fetchall()]

    def promote_transactions_to_imported(
        self,
        user_id: str,
        transaction_ids: list[int],
    ) -> int:
        """Change status PENDING_REVIEW → UNMATCHED for the given transaction IDs.

        Only rows owned by user_id are updated (RLS-equivalent guard).
        Returns the count of rows actually updated.
        """
        if not transaction_ids:
            return 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE transactions
                       SET status = 'UNMATCHED'
                       WHERE user_id = %s
                         AND id = ANY(%s)
                         AND status = 'PENDING_REVIEW'""",
                    (user_id, transaction_ids),
                )
                return cur.rowcount

    def update_statement_confidence(
        self,
        statement_id: str,
        user_id: str,
        *,
        new_confidence: str,
        new_flags: list[str] | None = None,
    ) -> None:
        """Update the import_confidence (and optionally import_flags) on a statement row."""
        flags_json = json.dumps(new_flags) if new_flags is not None else None
        with self._conn() as conn:
            with conn.cursor() as cur:
                if flags_json is not None:
                    cur.execute(
                        """UPDATE statements
                           SET import_confidence = %s, import_flags = %s
                           WHERE id = %s AND user_id = %s""",
                        (new_confidence, flags_json, statement_id, user_id),
                    )
                else:
                    cur.execute(
                        """UPDATE statements
                           SET import_confidence = %s
                           WHERE id = %s AND user_id = %s""",
                        (new_confidence, statement_id, user_id),
                    )

    # ------------------------------------------------------------------
    # processing_jobs — the document queue
    #
    # These are the user-facing half: every one runs inside a request, so it
    # goes through _conn() and is enforced by RLS on top of the explicit
    # user_id filter. The worker's half (claiming a job, moving it through
    # states, the startup re-queue) is system bookkeeping across all users and
    # lives in server/jobs/document_queue.py against the pool's owner role.
    # ------------------------------------------------------------------

    _JOB_COLUMNS = (
        "id, user_id, kind, filename, stored_path, file_hash, status, position, "
        "attempts, max_attempts, transient_waits, next_attempt_at, pages_total, "
        "pages_done, error_code, error_message, result, created_at, started_at, "
        "finished_at, updated_at"
    )

    # Queue depth for one row: how many of this user's uploads are still queued
    # or waiting in front of it, in the order the worker drains (created_at,
    # position, id). `position` cannot answer this — it is a submission sequence
    # that never shrinks, so it keeps its value after the rows ahead of it have
    # finished and cannot stand in for "how many are still ahead". NULL once the
    # row is no longer waiting. Appended to every user-facing read and RETURNING
    # so the 202, the poll and the SSE event all say the same number.
    _JOB_AHEAD_SQL = """,
                   CASE WHEN processing_jobs.status
                             IN ('queued', 'waiting_for_model', 'running')
                        THEN (SELECT COUNT(*) FROM processing_jobs a
                               WHERE a.user_id = processing_jobs.user_id
                                 AND a.status IN ('queued', 'waiting_for_model')
                                 AND (a.created_at, a.position, a.id)
                                     < (processing_jobs.created_at,
                                        processing_jobs.position,
                                        processing_jobs.id))
                        ELSE NULL END AS ahead"""

    def insert_processing_job(
        self,
        *,
        kind: str,
        filename: str,
        stored_path: str | None,
        file_hash: str | None,
        status: str = "queued",
        max_attempts: int = 3,
        pages_total: int | None = None,
        result: dict | None = None,
        finished: bool = False,
    ) -> dict:
        """Enqueue one document and return the row the API answers 202 with.

        ``position`` is MAX(position)+1 for this user, so a user's uploads drain
        in the order they were dropped. Two simultaneous uploads can land on the
        same number; the worker orders by (created_at, position, id), so the
        FIFO promise holds either way.

        ``finished=True`` writes an already-complete job (the CSV/XLSX statement
        path parses in milliseconds and stays synchronous, but still has to
        produce a job object so the UI has one shape).
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""INSERT INTO processing_jobs
                           (user_id, kind, filename, stored_path, file_hash, status,
                            position, max_attempts, pages_total, result,
                            started_at, finished_at)
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            COALESCE((SELECT MAX(position) FROM processing_jobs
                                       WHERE user_id = %s), 0) + 1,
                            %s, %s, %s,
                            CASE WHEN %s THEN NOW() ELSE NULL END,
                            CASE WHEN %s THEN NOW() ELSE NULL END
                        )
                        RETURNING {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}""",  # noqa: S608 - literal column list
                    (
                        self.user_id, kind, filename, stored_path, file_hash, status,
                        self.user_id, max_attempts, pages_total,
                        json.dumps(result) if result is not None else None,
                        finished, finished,
                    ),
                )
                return dict(cur.fetchone())

    def get_processing_job(self, job_id: str) -> dict | None:
        """One job, or None when it is not this user's."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                try:
                    cur.execute(
                        f"""SELECT {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}
                              FROM processing_jobs
                            WHERE id = %s AND user_id = %s""",  # noqa: S608
                        (job_id, self.user_id),
                    )
                except psycopg2.errors.InvalidTextRepresentation:
                    # Not a UUID at all — the same answer as "not yours".
                    return None
                row = cur.fetchone()
                return dict(row) if row else None

    def list_processing_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """This user's jobs: pending ones in queue order, then finished ones.

        Pending (queued / running / waiting_for_model) first in queue order —
        that is the list a user watching an import cares about — then everything
        finished, most recent first.
        """
        conditions = ["user_id = %s"]
        params: list = [self.user_id]
        if status:
            conditions.append("status = %s")
            params.append(status)
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}
                          FROM processing_jobs
                        WHERE {" AND ".join(conditions)}
                        ORDER BY
                            CASE WHEN status IN ('queued', 'running', 'waiting_for_model')
                                 THEN 0 ELSE 1 END,
                            CASE WHEN status IN ('queued', 'running', 'waiting_for_model')
                                 THEN position END ASC,
                            COALESCE(finished_at, created_at) DESC,
                            position DESC
                        LIMIT %s OFFSET %s""",  # noqa: S608
                    params,
                )
                return [dict(r) for r in cur.fetchall()]

    def count_processing_jobs(self) -> dict[str, int]:
        """{status: count} for this user, every status present even at zero."""
        counts = {
            "queued": 0, "running": 0, "waiting_for_model": 0,
            "done": 0, "failed": 0, "cancelled": 0,
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM processing_jobs
                       WHERE user_id = %s GROUP BY status""",
                    (self.user_id,),
                )
                for status, count in cur.fetchall():
                    counts[status] = int(count)
        return counts

    def count_pending_processing_jobs_by_kind(self) -> dict[str, int]:
        """Pending work per kind — the multiplier in the ETA."""
        out = {"receipt": 0, "statement": 0}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT kind, COUNT(*) FROM processing_jobs
                       WHERE user_id = %s
                         AND status IN ('queued', 'running', 'waiting_for_model')
                       GROUP BY kind""",
                    (self.user_id,),
                )
                for kind, count in cur.fetchall():
                    out[kind] = int(count)
        return out

    def recent_processing_job_seconds(self, kind: str, limit: int = 20) -> list[float]:
        """Wall-clock seconds for this user's last ``limit`` finished jobs of a kind.

        Feeds the rolling-average ETA. Measured started_at → finished_at rather
        than created_at, so time a job spent queued behind others (or waiting on
        a dead local LLM server) does not poison the estimate for the next import.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT EXTRACT(EPOCH FROM (finished_at - started_at))
                         FROM processing_jobs
                        WHERE user_id = %s AND kind = %s AND status = 'done'
                          AND started_at IS NOT NULL AND finished_at IS NOT NULL
                        ORDER BY finished_at DESC
                        LIMIT %s""",
                    (self.user_id, kind, max(1, min(limit, 100))),
                )
                return [
                    float(r[0]) for r in cur.fetchall()
                    if r[0] is not None and float(r[0]) >= 0
                ]

    def find_active_processing_job_by_hash(self, kind: str, file_hash: str) -> dict | None:
        """A job for these exact bytes that has not finished yet.

        Dropping the same file twice in one go should not run the extraction
        twice: the second upload is answered with the job the first one
        created.
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}
                          FROM processing_jobs
                        WHERE user_id = %s AND kind = %s AND file_hash = %s
                          AND status IN ('queued', 'running', 'waiting_for_model')
                        ORDER BY position ASC LIMIT 1""",  # noqa: S608
                    (self.user_id, kind, file_hash),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def retry_processing_job(self, job_id: str) -> dict | None:
        """Put a failed job back in the queue, attempts reset.

        Keeps its original position: a retry is the same upload, not a new one.
        Returns the updated row, or None when the job is not this user's or is
        not in a retryable state.
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""UPDATE processing_jobs
                           SET status = 'queued',
                               attempts = 0,
                               transient_waits = 0,
                               next_attempt_at = NULL,
                               error_code = NULL,
                               error_message = NULL,
                               pages_done = 0,
                               started_at = NULL,
                               finished_at = NULL,
                               updated_at = NOW()
                         WHERE id = %s AND user_id = %s
                           AND status IN ('failed', 'cancelled')
                        RETURNING {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}""",  # noqa: S608
                    (job_id, self.user_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def cancel_processing_job(self, job_id: str) -> dict | None:
        """Cancel a job that has not started. Returns None if it is past that.

        Deliberately queued-only: a running job holds an LLM call and half a
        document's worth of writes, and there is no safe point to stop it.
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""UPDATE processing_jobs
                           SET status = 'cancelled',
                               finished_at = NOW(),
                               updated_at = NOW()
                         WHERE id = %s AND user_id = %s AND status = 'queued'
                        RETURNING {self._JOB_COLUMNS}{self._JOB_AHEAD_SQL}""",  # noqa: S608
                    (job_id, self.user_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def delete_processing_job(self, job_id: str) -> bool:
        """Remove one not-yet-running job. True when a row actually went.

        Used by ``POST /api/jobs/{id}/reroute``, which re-files an auto-detected
        statement as a receipt: the file stays in the store and a new receipt job
        takes over, so the old row is deleted rather than cancelled. Leaving a
        cancelled twin behind would make one dropped file read as two in the
        queue panel and in the batch counter ("1 of 2" for a single upload).

        Scoped to this user and to ``REROUTABLE_JOB_STATUSES``; anything else
        answers False and the caller turns that into a 409 rather than silently
        doing nothing.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """DELETE FROM processing_jobs
                            WHERE id = %s AND user_id = %s
                              AND status = ANY(%s)""",
                        (job_id, self.user_id, list(REROUTABLE_JOB_STATUSES)),
                    )
                except psycopg2.errors.InvalidTextRepresentation:
                    # Not a UUID at all — the same answer as "not yours".
                    return False
                return cur.rowcount > 0

    def processing_job_batch(self) -> dict:
        """The batch this user is watching: ``{total, finished, started_at}``.

        ``PROCESSING_JOB_BATCH_SQL`` says what counts as a batch and why it is
        the server that decides. Empty queue answers zeros rather than None, so
        the caller never has to special-case it.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(PROCESSING_JOB_BATCH_SQL, (self.user_id, self.user_id))
                row = cur.fetchone()
        if not row or row[1] is None:
            return {"total": 0, "finished": 0, "started_at": None}
        started = row[0]
        return {
            "started_at": started.isoformat() if hasattr(started, "isoformat") else started,
            "total": int(row[1]),
            "finished": int(row[2]),
        }

    def delete_processing_jobs(self, status: str = "done") -> int:
        """Clear finished jobs off the list. Never touches pending work."""
        if status not in {"done", "failed", "cancelled"}:
            raise ValueError(f"refusing to clear jobs with status {status!r}")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM processing_jobs WHERE user_id = %s AND status = %s",
                    (self.user_id, status),
                )
                return cur.rowcount

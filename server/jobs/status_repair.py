"""Startup repair for transactions and documents whose status is a lie.

A transaction's status is supposed to be *derived*: MATCHED because an approved
match row proves it, LINKED because an active cross-statement link explains it,
otherwise UNMATCHED. Every verb that changes a match row now re-derives both
sides in the same database transaction (``DatabasePg._resync_transaction_statuses``
/ ``_resync_document_statuses``), but rows written before that — and any future
path that forgets — leave drift behind, and drift is not cosmetic:

* a transaction stranded at MATCHED with no match row shows the user a
  reconciled row with **no receipt behind it**, counts toward the readiness
  figure, and is gone from every later candidate pool because the matcher only
  considers UNMATCHED rows — so the receipt that would prove it can never be
  found;
* a receipt stranded at EXTRACTED while it still proves a transaction can be
  handed to a *second* transaction by the next run.

So on every startup this sweep re-derives the two statuses across all tenants
and logs every correction it makes. It reads and writes with the pool's own
role, deliberately: this is a maintenance pass across every user, not a
request, and there is no user session to borrow.

The three-way rule (approved proves / PENDING_REVIEW is neutral / nothing means
unmatched) is the same one the request path uses — a PENDING_REVIEW match is
left alone because the storage rule already decided whether that pair was
settled (at or above the confidence floor) or flagged and left in the pool.

One case is deliberately **not** a correction. A transaction MATCHED to a
document whose stored file is no longer on disk stays MATCHED: the match is
real — a human or the matcher approved that pair, the extracted figures are still
on the document row — and only the proof bytes are gone. Demoting it would throw
away a real reconciliation because of a missing file, and would hand the receipt
to a second transaction on the next run. So the sweep logs it at WARNING and
reports it separately, which is the same thing the audit binder's "Matched, proof
file missing" list says about the same row: re-upload the receipt.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Match rows that settle a pair on their own.
SETTLING_MATCH_STATUSES = ("AUTO_APPROVED", "USER_APPROVED")

#: Transaction statuses that are statements about the row itself, not about
#: match bookkeeping, and are not overwritten by a re-derivation. One exception,
#: handled by _TXN_LINK_PROMOTE_SQL below: an IGNORED row that has an active
#: cross-statement link becomes LINKED, because the link explains the row better
#: than the description-only guess that stamped it IGNORED.
PRESERVED_TXN_STATUSES = ("IGNORED", "EXCLUDED", "PENDING_REVIEW")

# A transaction is MATCHED only if an approved match row proves it. Rows with
# nothing but a PENDING_REVIEW match are excluded by the NOT EXISTS below, so
# the review queue's normal state is never disturbed.
_TXN_DEMOTE_SQL = """
UPDATE transactions t
   SET status = CASE
           WHEN EXISTS (
               SELECT 1 FROM transaction_links l
                WHERE l.user_id = t.user_id
                  AND l.status <> 'USER_REJECTED'
                  AND (l.source_transaction_id = t.id
                       OR l.target_transaction_id = t.id)
           ) THEN 'LINKED'
           ELSE 'UNMATCHED'
       END
 WHERE t.status = 'MATCHED'{user_clause_t}
   AND NOT EXISTS (
       SELECT 1 FROM reconciliation_matches m
        WHERE m.user_id = t.user_id AND m.transaction_id = t.id
          AND m.status <> 'USER_REJECTED'
   )
RETURNING t.id, t.user_id, t.status
"""

_TXN_LINKED_SQL = """
UPDATE transactions t
   SET status = 'UNMATCHED'
 WHERE t.status = 'LINKED'{user_clause_t}
   AND NOT EXISTS (
       SELECT 1 FROM transaction_links l
        WHERE l.user_id = t.user_id
          AND l.status <> 'USER_REJECTED'
          AND (l.source_transaction_id = t.id OR l.target_transaction_id = t.id)
   )
   AND NOT EXISTS (
       SELECT 1 FROM reconciliation_matches m
        WHERE m.user_id = t.user_id AND m.transaction_id = t.id
          AND m.status <> 'USER_REJECTED'
   )
RETURNING t.id, t.user_id, t.status
"""

_TXN_PROMOTE_SQL = """
UPDATE transactions t
   SET status = 'MATCHED'
 WHERE t.status NOT IN %(preserved)s{user_clause_t}
   AND t.status <> 'MATCHED'
   AND EXISTS (
       SELECT 1 FROM reconciliation_matches m
        WHERE m.user_id = t.user_id AND m.transaction_id = t.id
          AND m.status = ANY(%(settling)s)
   )
RETURNING t.id, t.user_id, t.status
"""

# IGNORED is a guess made from the description alone ("self-evident, needs no
# receipt"). An active cross-statement link is a better explanation of the same
# row — it names the counterpart — and the link-insert path already promotes
# IGNORED -> LINKED. This catches the rows that were stamped IGNORED before a
# link existed, or by a run that stamped after linking: LINKED wins over
# IGNORED. EXCLUDED ("isn't real") and PENDING_REVIEW (import review) are left
# exactly as they are, and a row with an approved match is left to
# _TXN_PROMOTE_SQL above.
_TXN_LINK_PROMOTE_SQL = """
UPDATE transactions t
   SET status = 'LINKED'
 WHERE t.status = 'IGNORED'{user_clause_t}
   AND EXISTS (
       SELECT 1 FROM transaction_links l
        WHERE l.user_id = t.user_id
          AND l.status <> 'USER_REJECTED'
          AND (l.source_transaction_id = t.id OR l.target_transaction_id = t.id)
   )
RETURNING t.id, t.user_id, t.status
"""

_DOC_DEMOTE_SQL = """
UPDATE documents d
   SET status = 'EXTRACTED', updated_at = NOW()
 WHERE d.status = 'MATCHED'{user_clause_d}
   AND NOT EXISTS (
       SELECT 1 FROM reconciliation_matches m
        WHERE m.user_id = d.user_id AND m.document_id = d.id
          AND m.status <> 'USER_REJECTED'
   )
RETURNING d.id, d.user_id, d.status
"""

_DOC_PROMOTE_SQL = """
UPDATE documents d
   SET status = 'MATCHED', updated_at = NOW()
 WHERE d.status = 'EXTRACTED'{user_clause_d}
   AND EXISTS (
       SELECT 1 FROM reconciliation_matches m
        WHERE m.user_id = d.user_id AND m.document_id = d.id
          AND m.status = ANY(%(settling)s)
   )
RETURNING d.id, d.user_id, d.status
"""


# Every MATCHED transaction whose approved proof names a stored file. The join is
# the same predicate as DatabasePg.get_matched_document_paths, which is where the
# audit binder gets the paths it tries to archive — so the binder's missing-proof
# list and this sweep can only ever disagree about what is on the disk, never
# about which rows to look at.
_MATCHED_PROOF_FILES_SQL = """
SELECT t.id, d.id, d.user_id, d.stored_path, d.original_filename
  FROM transactions t
  JOIN reconciliation_matches m
    ON m.user_id = t.user_id AND m.transaction_id = t.id
   AND m.status = ANY(%(settling)s)
  JOIN documents d
    ON d.user_id = m.user_id AND d.id = m.document_id
 WHERE t.status = 'MATCHED'{user_clause_t}
   AND d.stored_path IS NOT NULL
   AND btrim(d.stored_path) <> ''
"""

#: Column on ``documents`` that records "the stored file is gone", when the
#: schema has one. The shipped schema does not: a document's only free-form
#: field is ``extraction_raw_json``, which holds a Python repr of the extraction
#: and cannot carry a JSON flag without corrupting it. So the rule degrades to a
#: log line, and adding the column later starts filling it in with no change
#: here.
_FILE_MISSING_COLUMN = "file_missing"

_FILE_MISSING_COLUMN_SQL = """
SELECT 1 FROM information_schema.columns
 WHERE table_schema = current_schema()
   AND table_name = 'documents'
   AND column_name = %(column)s
"""


def _proof_file_exists(stored_path: str) -> bool:
    """True when the bytes a document points at are really on this disk.

    Uses the binder's own list of spellings a stored path may have (an
    ``uploads/`` prefix, Windows backslashes, an absolute path that already
    includes the storage root), so "the file is gone" means the same thing in
    this log as in the binder's "Matched, proof file missing" list. A path the
    resolver refuses — empty, or climbing out of the root — counts as missing,
    because nothing will ever read it.

    Returns True if the storage layer itself cannot be consulted: a repair sweep
    that cannot see the disk must not report every receipt as lost.
    """
    try:
        from core.export_binder import _storage_path_candidates
        from core.file_storage import resolve_path
    except Exception:  # pragma: no cover - storage/deps misconfigured
        logger.warning("Proof-file check skipped — storage layer unavailable", exc_info=True)
        return True
    for candidate in _storage_path_candidates(stored_path):
        try:
            if resolve_path(candidate).is_file():
                return True
        except Exception:
            continue
    return False


def _report_missing_proof_files(cur, clauses: dict, params: dict) -> list[dict]:
    """Find MATCHED rows whose proof file is gone. Log them; never demote them.

    Returns one entry per (transaction, document) pair, and marks the document
    with ``documents.file_missing`` when that column exists.
    """
    cur.execute(_MATCHED_PROOF_FILES_SQL.format(**clauses), params)
    rows = cur.fetchall() or []
    if not rows:
        return []

    on_disk: dict[str, bool] = {}
    missing: list[dict] = []
    for txn_id, doc_id, owner, stored_path, filename in rows:
        present = on_disk.get(stored_path)
        if present is None:
            present = _proof_file_exists(stored_path)
            on_disk[stored_path] = present
        if present:
            continue
        missing.append({
            "transaction_id": txn_id,
            "document_id": doc_id,
            "user_id": str(owner),
            "stored_path": stored_path,
            "filename": filename,
        })
        logger.warning(
            "Transaction %s is MATCHED to document %s (%s) whose stored file is gone "
            "(%s) — left MATCHED, the match is real and only the proof file is "
            "missing. Re-upload the receipt.",
            txn_id, doc_id, filename, stored_path,
        )

    if not missing:
        return []

    cur.execute(_FILE_MISSING_COLUMN_SQL, {"column": _FILE_MISSING_COLUMN})
    if cur.fetchone() is None:
        logger.info(
            "documents.%s does not exist — %d missing proof file(s) recorded "
            "in the log only",
            _FILE_MISSING_COLUMN, len(missing),
        )
        return missing

    cur.execute(
        f'UPDATE documents SET "{_FILE_MISSING_COLUMN}" = TRUE, updated_at = NOW() '
        "WHERE id = ANY(%(ids)s) AND user_id::text = ANY(%(owners)s)",
        {
            "ids": [entry["document_id"] for entry in missing],
            "owners": list({entry["user_id"] for entry in missing}),
        },
    )
    for entry in missing:
        entry["flagged"] = True
    return missing


def repair_status_drift(pool=None, user_id: str | None = None) -> dict:
    """Re-derive transaction and document statuses across every tenant.

    ``user_id`` narrows the sweep to one tenant. The startup call passes
    nothing — drift is drift whoever owns it — and the tests pass their own
    synthetic tenant so a test run only ever touches its own rows.

    Returns a dict of what was corrected, e.g.::

        {"transactions_unproven": [...], "transactions_orphaned_link": [...],
         "transactions_promoted": [...], "transactions_linked": [...],
         "documents_released": [...], "documents_promoted": [...],
         "documents_file_missing": [...], "total": 7}

    ``documents_file_missing`` is reported but **not** counted in ``total``:
    nothing was changed for those rows, they are MATCHED and they stay MATCHED.

    Never raises: a repair failure must not stop the server from booting.
    """
    empty = {
        "transactions_unproven": [],
        "transactions_orphaned_link": [],
        "transactions_promoted": [],
        "transactions_linked": [],
        "documents_released": [],
        "documents_promoted": [],
        "documents_file_missing": [],
        "total": 0,
    }
    if pool is None:
        try:
            from db.connection import get_pool
            pool = get_pool()
        except Exception:
            logger.warning("Status repair skipped — no database pool", exc_info=True)
            return empty

    conn = pool.getconn()
    try:
        params = {
            "settling": list(SETTLING_MATCH_STATUSES),
            "preserved": tuple(PRESERVED_TXN_STATUSES),
        }
        if user_id is None:
            clauses = {"user_clause_t": "", "user_clause_d": ""}
        else:
            clauses = {
                "user_clause_t": " AND t.user_id = %(user_id)s",
                "user_clause_d": " AND d.user_id = %(user_id)s",
            }
            params["user_id"] = user_id
        result = dict(empty)
        with conn.cursor() as cur:
            for key, sql in (
                ("transactions_unproven", _TXN_DEMOTE_SQL),
                ("transactions_orphaned_link", _TXN_LINKED_SQL),
                ("transactions_promoted", _TXN_PROMOTE_SQL),
                ("transactions_linked", _TXN_LINK_PROMOTE_SQL),
                ("documents_released", _DOC_DEMOTE_SQL),
                ("documents_promoted", _DOC_PROMOTE_SQL),
            ):
                cur.execute(sql.format(**clauses), params)
                rows = cur.fetchall() or []
                result[key] = [
                    {"id": r[0], "user_id": str(r[1]), "new_status": r[2]}
                    for r in rows
                ]
            # Runs last, and after the demotions above: a row that just lost its
            # MATCHED status is no longer a row with a missing proof.
            result["documents_file_missing"] = _report_missing_proof_files(
                cur, clauses, params
            )
        conn.commit()
        result["total"] = sum(
            len(result[k]) for k in empty
            if k not in ("total", "documents_file_missing")
        )
        if result["total"]:
            logger.warning(
                "Status repair corrected %d row(s): %d transaction(s) MATCHED with "
                "nothing proving them %s, %d LINKED with no link %s, %d promoted to "
                "MATCHED %s, %d IGNORED row(s) promoted to LINKED %s, "
                "%d receipt(s) released %s, %d receipt(s) promoted %s",
                result["total"],
                len(result["transactions_unproven"]), result["transactions_unproven"],
                len(result["transactions_orphaned_link"]),
                result["transactions_orphaned_link"],
                len(result["transactions_promoted"]), result["transactions_promoted"],
                len(result["transactions_linked"]), result["transactions_linked"],
                len(result["documents_released"]), result["documents_released"],
                len(result["documents_promoted"]), result["documents_promoted"],
            )
        else:
            logger.info("Status repair: every transaction and receipt agrees with its rows")
        if result["documents_file_missing"]:
            logger.warning(
                "Status repair: %d matched receipt(s) have no proof file on this disk "
                "and were left MATCHED %s",
                len(result["documents_file_missing"]), result["documents_file_missing"],
            )
        return result
    except Exception:
        logger.warning("Status repair failed (non-fatal)", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return empty
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass

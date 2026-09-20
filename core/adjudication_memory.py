"""Remembered agent verdicts — so a re-run does not re-ask an answered question.

Every LLM call the matcher makes is a pure function of what its prompt contains.
Temperature is 0, thinking is disabled, and the prompt renders exactly six
fields of the document and five of each candidate transaction. Ask the same
question of the same model and the same answer comes back, at the cost of
another round of inference.

A reconciliation run over books that have not changed therefore re-derives
decisions it has already made. This module is the memory that stops that: a
content-addressed store of
`(document, agent, inputs) → verdict`.

What makes it safe:

* **The key is the question.** :func:`inputs_hash` hashes precisely the fields
  the prompt renders, plus the model name. Edit the receipt's total, re-parse
  the statement, add or remove a candidate, or point ``LOCAL_LLM_MODEL``
  somewhere else, and the hash changes — the stored row is simply not found and
  the agent is asked again. There is no expiry to tune and no `updated_at` to
  keep in sync, because there is no way for a stored answer to belong to a
  question that has changed.
* **A remembered verdict is not a remembered match.** The verdict is what the
  model *said*; it is fed back into the same deterministic scoring,
  ``classify_match`` and claim arbitration as a fresh reply. A cache hit can
  never store a match a fresh run would not have stored.
* **No database access from worker threads.** A run loads every row it might
  need in one query before the thread pool starts, and collects new verdicts in
  memory for the caller to persist after the pool has drained. The matcher
  itself stays database-free, and the psycopg2 pool never sees the worker
  threads at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Bump when a prompt changes in a way that changes what a correct answer is.
# The hash covers the *values* a prompt renders, not its wording, so editing an
# instruction line would otherwise silently reuse verdicts formed under the old
# instructions. This is the one invalidation that cannot be derived from data.
# Editing this string retires every verdict stored under the previous value:
# a verdict formed under other instructions is a different reply, so none of
# them is reused. The value is an opaque sequence number; only inequality
# between two values means anything.
PROMPT_REVISION = "2"

# The agents whose verdicts are worth remembering, and what each answers.
AGENT_AMOUNT = "amount"   # shortlist candidates by monetary match
AGENT_DATE = "date"       # narrow that shortlist by date plausibility
AGENT_FINAL = "final"     # pick exactly one, with a confidence label

VALID_AGENTS = frozenset({AGENT_AMOUNT, AGENT_DATE, AGENT_FINAL})


def _doc_fingerprint(doc: Any) -> list[str]:
    """The document fields `_format_doc_for_llm` renders, as stable strings.

    The currency is the *resolved* one, because that is what the prompt
    renders: correct a receipt's tax line and the currency it is read in can
    change, which must invalidate the remembered verdict. A plainly-stated code
    hashes to the bare code, so no remembered verdict is thrown away for a
    document whose prompt line did not move.
    """
    from core.document_currency import STATED, resolve_document_currency
    from core.reconciliation import _get_best_date

    code, source = resolve_document_currency(doc)
    currency_token = (code or "") if source == STATED else f"{code or ''}/{source}"
    return [
        str(doc.vendor or ""),
        str(_get_best_date(doc) or ""),
        str(doc.document_date or ""),
        str(doc.total if doc.total is not None else ""),
        currency_token,
        str(doc.original_filename or ""),
    ]


def _txn_fingerprint(txn: Any) -> list[str]:
    """The transaction fields `_format_txn_for_llm` renders, as stable strings.

    The description is truncated to the same 80 characters the prompt shows, so
    an edit past that point — which the model never saw — does not invalidate a
    verdict that could not have depended on it.
    """
    from models.transaction import account_str

    return [
        str(txn.id),
        str(txn.date_posted or ""),
        account_str(txn.account),
        str(txn.amount),
        str(txn.currency or ""),
        (txn.description or "")[:80],
    ]


def inputs_hash(
    agent: str,
    doc: Any,
    txns: list,
    model: str,
    extra: dict | None = None,
) -> str:
    """A SHA-256 over everything the agent's prompt will render.

    ``txns`` is fingerprinted in the order the prompt lists them, because the
    order is part of the question a language model is asked. ``extra`` carries
    any agent-specific parameter that appears in the prompt text — the date
    agent's ``max_date_days`` is the only one today.
    """
    payload = {
        "revision": PROMPT_REVISION,
        "agent": agent,
        "model": model,
        "doc": _doc_fingerprint(doc),
        "txns": [_txn_fingerprint(t) for t in txns],
        "extra": extra or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AdjudicationMemory:
    """Verdicts already formed, and verdicts formed this run.

    Thread-safe: the document workers read and write it concurrently. Loading
    and persisting are the caller's job (see
    ``server/api/reconciliation.py``) — this object never touches a database.
    """

    def __init__(self, rows: list[dict] | None = None, enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._known: dict[tuple[int, str, str], dict] = {}
        self._pending: list[dict] = []
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        for row in rows or ():
            try:
                key = (int(row["document_id"]), str(row["agent"]), str(row["inputs_hash"]))
            except (KeyError, TypeError, ValueError):
                continue
            verdict = row.get("verdict")
            if isinstance(verdict, str):
                try:
                    verdict = json.loads(verdict)
                except ValueError:
                    continue
            if isinstance(verdict, dict):
                self._known[key] = verdict

    def get(self, agent: str, doc_id: int, digest: str) -> dict | None:
        """The stored reply for this exact question, or None."""
        if not self.enabled:
            return None
        with self._lock:
            verdict = self._known.get((int(doc_id), agent, digest))
            if verdict is None:
                self.misses += 1
                return None
            self.hits += 1
            return dict(verdict)

    def put(
        self,
        agent: str,
        doc_id: int,
        digest: str,
        verdict: dict,
        model: str,
        transaction_id: int | None = None,
        confidence: float | None = None,
        reasoning: str | None = None,
    ) -> None:
        """Record a fresh reply for persisting after the run."""
        if not self.enabled or agent not in VALID_AGENTS or not isinstance(verdict, dict):
            return
        with self._lock:
            key = (int(doc_id), agent, digest)
            if key in self._known:
                return
            self._known[key] = dict(verdict)
            self._pending.append({
                "document_id": int(doc_id),
                "transaction_id": transaction_id,
                "agent": agent,
                "inputs_hash": digest,
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": (reasoning or "")[:2000] or None,
                "model": model,
            })

    def pending(self) -> list[dict]:
        """Verdicts formed this run that are not yet on disk."""
        with self._lock:
            return list(self._pending)

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "known": len(self._known),
                "hits": self.hits,
                "misses": self.misses,
                "new": len(self._pending),
            }


# A disabled memory: every lookup misses, every write is dropped. This is what
# the engine uses when a caller wires no store in (every unit test, for one), so
# `find_matches` adjudicates every question afresh.
DISABLED = AdjudicationMemory(enabled=False)

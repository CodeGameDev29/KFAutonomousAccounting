"""The document processing queue: one worker, nothing lost to a restart.

Why this exists
---------------
Doing "process this upload" inside the request that carried the file has two
problems: extraction takes longer than a request timeout allows, and nothing
survives a restart. A request also has no way to pace itself, so a dropped
folder becomes dozens of simultaneous calls against one model endpoint.

So every upload becomes a row in ``processing_jobs`` and the HTTP request ends
there, quickly. This module is what drains that table: a single asyncio loop in
the uvicorn process, running ``local_llm_config.max_concurrent`` jobs at a time
— read live, so raising ``LOCAL_LLM_MAX_CONCURRENT`` in ``.env`` and restarting
is the only throughput knob — in FIFO order across both kinds and all users.

The four properties that matter
-------------------------------
* **Nothing times out.** The HTTP request no longer waits for the model, and a
  job has no deadline of its own: the per-call timeout inside the engine
  (``LOCAL_LLM_TIMEOUT_S``) is the only clock. A job runs until it is done or
  it fails.
* **Nothing is lost to a restart.** ``requeue_running_jobs()`` runs at startup
  and puts every ``running`` row back to ``queued`` with its position intact,
  so a restart mid-import is a pause rather than a loss.
* **A dead LLM endpoint costs time, not work.** A transport failure parks the job in
  ``waiting_for_model`` without consuming an attempt, backs off 10s → 30s →
  60s → every 60s, and resumes by itself the moment ``GET {base_url}/models``
  answers again. Uploads keep being accepted while it is down.
* **Every transition is visible.** Each one writes the row and pushes SSE
  ``job_updated`` (the whole job, including how many uploads are still ``ahead``
  of it) plus ``jobs_summary`` (that user's counts and the ``worker`` block, so
  the ETA and the model-unreachable banner change on the event rather than on the
  next poll), on top of the ``statement_parsed`` / ``receipts_updated`` /
  ``transactions_updated`` events the existing UI already listens for.

Where the SQL lives
-------------------
Split deliberately in two:

* The **user-facing** half (enqueue, list, retry, cancel, clear) is on
  ``DatabasePg`` — it runs inside a request, so it goes through ``_conn()`` and
  RLS enforces the tenant boundary on top of the explicit ``user_id`` filter.
* The **worker's** half — claiming the next job for *any* user, moving it
  through states, the startup re-queue — is system bookkeeping that by
  definition spans users, so it runs on the pool's owner role here, exactly as
  ``status_repair`` and the old statement sweep do. Every statement still names
  the job id or ``user_id`` it touches.

The ``progress_cb`` contract
----------------------------
The engine entry points accept an optional keyword
``progress_cb: Callable[[int, int], None] | None``, called as
``progress_cb(pages_done, pages_total)`` after each page (or page batch) of a
multi-page document, and - because this module's callback declares it - once up
front as ``progress_cb(0, pages_total, mode=...)`` where ``mode`` is
``"one_pass"`` (the whole document is a single LLM call) or ``"paged"`` (real
page batches). It is synchronous, must never raise, and may be called from a
worker thread. This module passes one only when the engine's signature declares
it, so it is safe for the engine to gain the parameter later.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

# Columns every job read returns, in one place so the worker and the API agree.
JOB_COLUMNS = (
    "id, user_id, kind, filename, stored_path, file_hash, status, position, "
    "attempts, max_attempts, transient_waits, next_attempt_at, pages_total, "
    "pages_done, error_code, error_message, result, created_at, started_at, "
    "finished_at, updated_at"
)

PENDING_STATUSES = ("queued", "running", "waiting_for_model")
TERMINAL_STATUSES = ("done", "failed", "cancelled")
ALL_STATUSES = ("queued", "running", "waiting_for_model", *TERMINAL_STATUSES)

# How long the loop sleeps when nothing woke it. An enqueue wakes it instantly,
# so this only sets the resolution of the backoff ladder below.
_POLL_SECONDS = 3.0

# The model-unreachable ladder, then every 60s for as long as it takes.
_BACKOFF_SECONDS = (10, 30, 60)

# A read timeout is treated as transient — the endpoint may be loading a model
# or swapping weights — but only a few times. A model that answers GET /models
# while every generation times out is a real failure, and parking the job
# forever is the "looks like a hang" outcome this queue exists to avoid.
#
# A full ladder of timeouts at a long LOCAL_LLM_TIMEOUT_S is a long stretch of a
# row that would otherwise say nothing but "Waiting for the extraction model",
# which reads as a hang even though a ladder is being climbed. So every
# timed-out attempt writes ``_timeout_progress_message`` onto the row: the panel
# shows movement through the ladder instead of a static label, and a user can
# see that the model answered and ran out of time rather than never answering.
_TIMEOUT_WAIT_LIMIT = 3

# How long the supervisor waits before putting the drain loop back after it died
# for a reason other than shutdown, then every 60s. A dead loop is the "uploads
# are accepted but nothing runs" failure, which otherwise needs a restart nobody
# knows to perform.
_RESTART_BACKOFF_SECONDS = (1, 5, 15, 60)

# Reachability probe cache. Much shorter than /health's 60s: this one decides
# how long a user waits after the LLM endpoint comes back.
_PROBE_TTL_SECONDS = 10.0
_PROBE_TIMEOUT_SECONDS = 5.0

# Seeds for the ETA before a user has finished anything — deliberately
# pessimistic, so the first estimate errs long rather than short. Replaced by
# that user's own rolling average as soon as there is one.
DEFAULT_SECONDS_PER_KIND = {"receipt": 25.0, "statement": 90.0}

# Message written onto a statements row whose parse a restart killed and whose
# job is gone.
RESTART_MESSAGE = "Server restarted while parsing; upload again."


# ---------------------------------------------------------------------------
# Job serialization
# ---------------------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def job_public(row: dict | None, *, ahead: int | None = None) -> dict | None:
    """The JSON shape of a job — the SSE payload and every API body.

    ``job_id`` duplicates ``id`` on purpose: the upload endpoints answered with
    ``job_id`` first and it is the name the frontend reads.

    ``position`` and ``ahead`` answer two different questions and both are sent.
    ``position`` is the submission sequence for this user — monotonic and never
    reused, so it keeps its value long after everything before it has finished
    and cannot be read as "how many are left". ``ahead`` is queue depth: how
    many of this user's uploads are still queued or waiting in front of this one
    right now. ``None`` means "not applicable / not computed" (a finished job,
    or a caller that did not ask the database for it).
    """
    if row is None:
        return None
    if ahead is None:
        ahead = row.get("ahead")
    result = row.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            result = None
    return {
        "job_id": str(row["id"]),
        "id": str(row["id"]),
        "kind": row["kind"],
        "filename": row["filename"],
        "status": row["status"],
        "position": int(row["position"]) if row.get("position") is not None else None,
        "ahead": int(ahead) if ahead is not None else None,
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "pages_total": row.get("pages_total"),
        "pages_done": int(row.get("pages_done") or 0),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "result": result,
        "next_attempt_at": _jsonable(row.get("next_attempt_at")),
        "created_at": _jsonable(row.get("created_at")),
        "started_at": _jsonable(row.get("started_at")),
        "finished_at": _jsonable(row.get("finished_at")),
        "updated_at": _jsonable(row.get("updated_at")),
    }


def _job_result(row: dict) -> dict:
    """A job's ``result`` as a dict, whatever the driver handed back."""
    result = row.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return {}
    return dict(result) if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

class JobFailure(RuntimeError):
    """A failure the engine already classified for the user.

    ``transient`` means nothing was wrong with the document: the LLM endpoint
    did not answer. Those park the job instead of spending one of its attempts.
    """

    def __init__(self, *, error_code: str, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.transient = transient


def classify_job_failure(exc: BaseException, filename: str) -> tuple[bool, str, str]:
    """Return ``(transient, error_code, user_message)`` for a failed job.

    One vocabulary, reused: ``core/llm_errors.py`` decides what "the LLM
    endpoint is down" looks like and ``server/api/_extraction_errors.py`` turns
    everything else into the sentence the user reads.
    """
    from core.llm_errors import is_local_endpoint_down
    from server.api._extraction_errors import classify_extraction_error

    if isinstance(exc, JobFailure):
        return exc.transient, exc.error_code, exc.message

    if is_local_endpoint_down(exc):
        from core.llm_errors import (
            LOCAL_LLM_UNREACHABLE_CODE,
            LOCAL_LLM_UNREACHABLE_MESSAGE,
        )
        return True, LOCAL_LLM_UNREACHABLE_CODE, LOCAL_LLM_UNREACHABLE_MESSAGE

    if isinstance(exc, Exception):
        _status, code, message = classify_extraction_error(exc, filename)
    else:  # pragma: no cover - BaseException that is not an Exception
        code, message = "EXTRACTION_ERROR", str(exc) or exc.__class__.__name__
    # A slow answer is treated as transient, but only _TIMEOUT_WAIT_LIMIT times.
    return code == "TIMEOUT", code, message


def _llm_timeout_seconds() -> int | None:
    """``LOCAL_LLM_TIMEOUT_S``, for the message on a timed-out attempt."""
    try:
        from config.settings import local_llm_config

        return int(round(float(local_llm_config.timeout_s)))
    except Exception:  # pragma: no cover - config always imports
        return None


def _timeout_progress_message(attempt: int, limit: int = _TIMEOUT_WAIT_LIMIT) -> str:
    """What the job row says while it is climbing the timeout ladder.

    A sentence that moves — which attempt, out of how many, and after how long.
    Repeating the engine's generic "the AI service stopped answering part-way
    through" on every attempt, while the status stays ``waiting_for_model``,
    is indistinguishable from a job that is simply parked.
    """
    seconds = _llm_timeout_seconds()
    after = f" after {seconds} s" if seconds else ""
    return f"Attempt {attempt} of {limit} timed out{after}; retrying"


def _timeout_exhausted_message(limit: int = _TIMEOUT_WAIT_LIMIT) -> str:
    """The row's last word once the timeout ladder is spent."""
    seconds = _llm_timeout_seconds()
    each = f" after {seconds} s each" if seconds else ""
    return (
        f"Timed out {limit} times{each}. The model is answering but not finishing "
        "this document, so it is being treated as a failure rather than waited on "
        "again."
    )


# ---------------------------------------------------------------------------
# The store: the worker's own SQL, on the pool's owner role
# ---------------------------------------------------------------------------

@contextmanager
def _system_conn() -> Iterator[Any]:
    """A pooled connection for cross-user queue bookkeeping.

    Not RLS-armed, deliberately: the worker's job is to find the next job for
    *any* user. Every statement below names either a job id or an explicit
    user_id. Same posture as ``server/jobs/status_repair.py``.
    """
    from db.connection import get_pool

    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


class PgJobStore:
    """Every state transition the worker makes, as one SQL statement each."""

    def _one(self, sql: str, params: tuple) -> dict | None:
        import psycopg2.extras

        with _system_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None

    def claim_next(self, *, include_waiting: bool = True) -> dict | None:
        """Take the oldest runnable job and mark it ``running``, atomically.

        ``FOR UPDATE SKIP LOCKED`` so this stays correct if the app is ever
        served by more than one worker process. Ordering is ``created_at`` first (FIFO
        across users and kinds) with the per-user ``position`` as the tiebreak,
        so two uploads that landed in the same microsecond still drain in the
        order the user dropped them.
        """
        statuses = ("queued", "waiting_for_model") if include_waiting else ("queued",)
        return self._one(
            f"""UPDATE processing_jobs SET
                    status = 'running',
                    started_at = NOW(),
                    updated_at = NOW()
                WHERE id = (
                    SELECT id FROM processing_jobs
                     WHERE status = ANY(%s)
                       AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                     ORDER BY created_at ASC, position ASC, id ASC
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                RETURNING {JOB_COLUMNS}""",  # noqa: S608 - literal column list
            (list(statuses),),
        )

    def mark_done(self, job_id: str, result: dict | None) -> dict | None:
        return self._one(
            f"""UPDATE processing_jobs SET
                    status = 'done',
                    result = COALESCE(result, '{{}}'::jsonb) || %s::jsonb,
                    error_code = NULL,
                    error_message = NULL,
                    next_attempt_at = NULL,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {JOB_COLUMNS}""",  # noqa: S608
            (json.dumps(result or {}, default=str), job_id),
        )

    def mark_waiting(
        self,
        job_id: str,
        *,
        transient_waits: int,
        delay_seconds: int,
        error_code: str,
        error_message: str,
    ) -> dict | None:
        return self._one(
            f"""UPDATE processing_jobs SET
                    status = 'waiting_for_model',
                    transient_waits = %s,
                    next_attempt_at = NOW() + (%s || ' seconds')::interval,
                    error_code = %s,
                    error_message = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {JOB_COLUMNS}""",  # noqa: S608
            (transient_waits, str(delay_seconds), error_code, error_message, job_id),
        )

    def mark_queued_for_retry(
        self,
        job_id: str,
        *,
        attempts: int,
        delay_seconds: int,
        error_code: str,
        error_message: str,
    ) -> dict | None:
        return self._one(
            f"""UPDATE processing_jobs SET
                    status = 'queued',
                    attempts = %s,
                    next_attempt_at = NOW() + (%s || ' seconds')::interval,
                    error_code = %s,
                    error_message = %s,
                    started_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {JOB_COLUMNS}""",  # noqa: S608
            (attempts, str(delay_seconds), error_code, error_message, job_id),
        )

    def mark_failed(
        self, job_id: str, *, attempts: int, error_code: str, error_message: str
    ) -> dict | None:
        return self._one(
            f"""UPDATE processing_jobs SET
                    status = 'failed',
                    attempts = %s,
                    error_code = %s,
                    error_message = %s,
                    next_attempt_at = NULL,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {JOB_COLUMNS}""",  # noqa: S608
            (attempts, error_code, error_message, job_id),
        )

    def set_progress(
        self,
        job_id: str,
        pages_done: int,
        pages_total: int | None,
        mode: str | None = None,
    ) -> dict | None:
        """Page progress, and how the engine is reading this document.

        ``mode`` is merged into ``result`` rather than given a column of its own:
        it is a fact about this one run that only the progress display reads, and
        ``result`` is already the JSONB every job carries to the browser.
        """
        return self._one(
            f"""UPDATE processing_jobs SET
                    pages_done = %s,
                    pages_total = COALESCE(%s, pages_total),
                    result = CASE WHEN %s::text IS NULL THEN result
                                  ELSE COALESCE(result, '{{}}'::jsonb)
                                       || jsonb_build_object('mode', %s::text) END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {JOB_COLUMNS}""",  # noqa: S608
            (pages_done, pages_total, mode, mode, job_id),
        )

    def resume_waiting(self) -> int:
        """The LLM endpoint answered: let every parked job run on the next tick."""
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE processing_jobs
                          SET next_attempt_at = NOW(), updated_at = NOW()
                        WHERE status = 'waiting_for_model'
                          AND (next_attempt_at IS NULL OR next_attempt_at > NOW())"""
                )
                return cur.rowcount or 0

    def park_queued(self, *, error_code: str, message: str, delay_seconds: int = 10) -> list[dict]:
        """Move queued jobs to ``waiting_for_model`` because the model is down.

        Not a failure and not an attempt: the work is untouched, the user is
        simply told why nothing is moving instead of watching "queued" forever.
        Uploads carry on landing in ``queued`` and get parked on the next tick.
        """
        import psycopg2.extras

        with _system_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""UPDATE processing_jobs SET
                            status = 'waiting_for_model',
                            next_attempt_at = NOW() + (%s || ' seconds')::interval,
                            error_code = %s,
                            error_message = %s,
                            updated_at = NOW()
                        WHERE status = 'queued'
                        RETURNING {JOB_COLUMNS}""",  # noqa: S608
                    (str(delay_seconds), error_code, message),
                )
                return [dict(r) for r in cur.fetchall()]

    def counts(self, user_id: str) -> dict[str, int]:
        counts = {status: 0 for status in ALL_STATUSES}
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM processing_jobs
                        WHERE user_id = %s GROUP BY status""",
                    (user_id,),
                )
                for status, count in cur.fetchall():
                    counts[status] = int(count)
        return counts

    def batch(self, user_id: str) -> dict:
        """This user's current batch: everything dropped since the queue was last idle.

        Defined server-side on purpose. A browser can only count the rows *it*
        watched go by, so a finished-of-total derived there resets on a reload
        and disagrees between two tabs watching the same import.

        The anchor is the most recent job that arrived when nothing of this
        user's was still unfinished - the start of the current run. Everything
        created at or after it is the batch. "Clear finished" needs no special
        case: deleting the finished rows moves the anchor forward on its own, to
        the oldest job still on the list, which is exactly the batch the user is
        looking at afterwards.
        """
        from db.database_pg import PROCESSING_JOB_BATCH_SQL

        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(PROCESSING_JOB_BATCH_SQL, (user_id, user_id))
                row = cur.fetchone()
        if not row or row[1] is None:
            return {"total": 0, "finished": 0, "started_at": None}
        return {
            "started_at": _jsonable(row[0]),
            "total": int(row[1]),
            "finished": int(row[2]),
        }

    def pending_count(self) -> int:
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status = ANY(%s)",
                    (["queued", "waiting_for_model"],),
                )
                return int(cur.fetchone()[0])

    def ahead_for(self, job: dict) -> int | None:
        """How many of this user's uploads are still waiting in front of this one.

        Counted in the order the worker actually drains — ``(created_at,
        position, id)`` — over ``queued`` and ``waiting_for_model`` only. A job
        already running is not "ahead" of anyone: it is being worked on, which
        is why the first file in line reads "Next up" while four others extract.
        ``None`` for a job that is no longer waiting at all.
        """
        if job.get("status") not in PENDING_STATUSES:
            return None
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM processing_jobs
                        WHERE user_id = %s
                          AND status = ANY(%s)
                          AND (created_at, position, id)
                              < (%s::timestamptz, %s::bigint, %s::uuid)""",
                    (
                        str(job["user_id"]),
                        ["queued", "waiting_for_model"],
                        job["created_at"],
                        int(job.get("position") or 0),
                        str(job["id"]),
                    ),
                )
                return int(cur.fetchone()[0])

    def pending_by_kind(self, user_id: str) -> dict[str, int]:
        """This user's pending work per kind — the ETA's multiplier."""
        out = {"receipt": 0, "statement": 0}
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT kind, COUNT(*) FROM processing_jobs
                        WHERE user_id = %s AND status = ANY(%s)
                        GROUP BY kind""",
                    (user_id, list(PENDING_STATUSES)),
                )
                for kind, count in cur.fetchall():
                    out[kind] = int(count)
        return out

    def recent_seconds(self, user_id: str, kind: str, limit: int = 20) -> list[float]:
        """started_at → finished_at for this user's last finished jobs of a kind."""
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT EXTRACT(EPOCH FROM (finished_at - started_at))
                         FROM processing_jobs
                        WHERE user_id = %s AND kind = %s AND status = 'done'
                          AND started_at IS NOT NULL AND finished_at IS NOT NULL
                        ORDER BY finished_at DESC
                        LIMIT %s""",
                    (user_id, kind, max(1, min(limit, 100))),
                )
                return [
                    float(r[0]) for r in cur.fetchall()
                    if r[0] is not None and float(r[0]) >= 0
                ]


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

def requeue_running_jobs() -> int:
    """Put every ``running`` job back to ``queued``, keeping its position.

    A restart kills the asyncio tasks, not the rows. This is what makes a
    restart mid-import a pause rather than a loss. ``attempts`` is not touched:
    a restart is not the document's fault.

    Never raises — a recovery failure must not stop the server from booting.
    """
    try:
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE processing_jobs
                          SET status = 'queued',
                              started_at = NULL,
                              next_attempt_at = NULL,
                              updated_at = NOW()
                        WHERE status = 'running'"""
                )
                requeued = cur.rowcount or 0
        if requeued:
            logger.info(
                "Document queue: re-queued %d job(s) a restart interrupted", requeued
            )
        return requeued
    except Exception:
        logger.warning("Document queue re-queue failed (non-fatal)", exc_info=True)
        return 0


def fail_orphaned_processing_statements() -> int:
    """Fail ``statements`` rows in ``processing`` that no live job will finish.

    The statement row is created when the upload lands, so it outlives its job
    if the job row was cleared, cancelled or failed outright. Anything with a
    pending job is left alone — that one is simply waiting its turn, which is
    the whole point of the queue, and no age-based rule may override it.

    Must run *after* ``requeue_running_jobs()`` so re-queued work counts as live.
    """
    try:
        with _system_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE statements s
                          SET status = 'failed',
                              status_message = %s
                        WHERE s.status = 'processing'
                          AND NOT EXISTS (
                              SELECT 1 FROM processing_jobs j
                               WHERE j.user_id = s.user_id
                                 AND j.kind = 'statement'
                                 AND j.status = ANY(%s)
                                 AND j.result->>'statement_id' = s.id::text
                          )""",
                    (RESTART_MESSAGE, list(PENDING_STATUSES)),
                )
                failed = cur.rowcount or 0
        if failed:
            logger.info(
                "Document queue: failed %d statement(s) in 'processing' with no live job",
                failed,
            )
        return failed
    except Exception:
        logger.warning("Orphaned statement check failed (non-fatal)", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Is the LLM endpoint answering?
# ---------------------------------------------------------------------------

_probe: dict = {"at": 0.0, "url": None, "reachable": True}


async def probe_model_reachable(*, force: bool = False) -> bool:
    """True when ``GET {base_url}/models`` answers.

    This is the resume condition for every job parked in ``waiting_for_model``,
    so it is cached for seconds rather than /health's minute. With no local
    endpoint configured — a test run, or a configuration that uses only a hosted
    key — there is nothing to probe and the answer is "go ahead"; the engine's
    own error is then the accurate one.
    """
    from config.settings import local_llm_config

    base_url = (local_llm_config.base_url or "").rstrip("/")
    if not base_url:
        return True

    url = f"{base_url}/models"
    now = time.monotonic()
    if (
        not force
        and _probe["url"] == url
        and now - float(_probe["at"]) < _PROBE_TTL_SECONDS
    ):
        return bool(_probe["reachable"])

    reachable = False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        # Any answer means the server process is alive, which is the question.
        reachable = resp.status_code < 500
    except Exception:
        reachable = False

    was = bool(_probe["reachable"]) if _probe["url"] == url else None
    _probe.update({"at": now, "url": url, "reachable": reachable})
    if was is not None and was != reachable:
        logger.info(
            "Document queue: local model at %s is now %s",
            url, "reachable" if reachable else "UNREACHABLE",
        )
    return reachable


def model_reachable_cached() -> bool | None:
    """The last probe verdict, without making a new one. None if never probed."""
    if _probe["url"] is None:
        return None
    return bool(_probe["reachable"])


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def emit_job_event(job: dict | None, *, store: PgJobStore | None = None) -> None:
    """Push ``job_updated`` + ``jobs_summary`` for one job's owner.

    Both payloads carry everything the panel needs to redraw without a fetch:
    the job includes its live ``ahead`` count, and the summary carries the
    ``worker`` block — concurrency, whether the LLM endpoint is answering, the
    rolling averages and the ETA. Without the worker block, the "model is not
    reachable" banner and the estimate could only change on the next poll, while
    every other state changes on the event.

    Best-effort by design: the row is the truth and the jobs API is pollable, so
    an SSE failure must never turn into a failed job.
    """
    if not job:
        return
    store = store or PgJobStore()
    user_id = str(job["user_id"])
    try:
        from server.api.events import send_event
    except Exception:  # pragma: no cover - events always imports
        return

    ahead = None
    try:
        ahead = store.ahead_for(job)
    except Exception:
        logger.debug("ahead count failed for job %s", job.get("id"), exc_info=True)
    try:
        send_event(user_id, "job_updated", job_public(job, ahead=ahead) or {})
    except Exception:
        logger.debug("job_updated SSE failed for job %s", job.get("id"), exc_info=True)

    try:
        payload = dict(store.counts(user_id))
        payload["worker"] = worker_snapshot(user_id, store=store)
        # The batch bar moves on the event, not only on the next 3s poll.
        payload["batch"] = store.batch(user_id)
        send_event(user_id, "jobs_summary", payload)
    except Exception:
        logger.debug("jobs_summary SSE failed for job %s", job.get("id"), exc_info=True)


# ---------------------------------------------------------------------------
# The engine calls
# ---------------------------------------------------------------------------

def engine_accepts_progress_cb(func: Callable) -> bool:
    """True when the engine entry point declares the ``progress_cb`` keyword.

    The engine entry points may or may not support page-batched progress, so
    the queue asks rather than assumes: it passes the callback when the
    signature takes it and stays silent otherwise.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False
    if "progress_cb" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _Progress:
    """Writes ``pages_done`` / ``pages_total`` onto the job as the engine works.

    Throttled to one write per ``_MIN_INTERVAL`` seconds (the last page always
    lands) because a long statement should cost one small UPDATE per page at
    most, not an UPDATE plus an SSE fan-out several times a second. Never
    raises: a progress bar is not worth failing an extraction over.
    """

    _MIN_INTERVAL = 1.5

    def __init__(self, store: PgJobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id
        self._last_at = 0.0
        self._last_done = -1
        self._last_mode: str | None = None

    def __call__(
        self,
        pages_done: int,
        pages_total: int | None = None,
        mode: str | None = None,
    ) -> None:
        try:
            done = int(pages_done)
            total = int(pages_total) if pages_total else None
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        final = total is not None and done >= total
        # A new mode is the one thing worth an immediate write even when the page
        # count has not moved: it is what the row's sentence is built from, and
        # the engine reports it before the first (possibly very long) call.
        mode_changed = mode is not None and mode != self._last_mode
        if done == self._last_done and not mode_changed:
            return
        if not final and not mode_changed and now - self._last_at < self._MIN_INTERVAL:
            return
        self._last_at = now
        self._last_done = done
        if mode is not None:
            self._last_mode = mode
        try:
            row = self._store.set_progress(self._job_id, done, total, mode=mode)
            emit_job_event(row, store=self._store)
        except Exception:
            logger.debug("progress write failed for job %s", self._job_id, exc_info=True)


def _source_path(job: dict):
    """Absolute path of the job's durable copy, or a JobFailure if it is gone."""
    from pathlib import Path

    stored_path = job.get("stored_path")
    if not stored_path:
        raise JobFailure(
            error_code="FILE_MISSING",
            message="The uploaded file was not stored, so it cannot be processed. "
                    "Upload it again.",
        )
    try:
        from core.file_storage import resolve_path

        path = Path(resolve_path(str(stored_path)))
    except Exception as exc:
        raise JobFailure(
            error_code="FILE_MISSING",
            message="The uploaded file could not be read back from storage. "
                    "Upload it again.",
        ) from exc
    if not path.is_file():
        raise JobFailure(
            error_code="FILE_MISSING",
            message="The uploaded file is no longer in storage. Upload it again.",
        )
    return path


async def _execute_receipt(job: dict, store: PgJobStore) -> dict:
    """Run the receipt pipeline for one queued job."""
    from db.connection import get_database
    from server.api.receipts import process_receipt_document

    path = _source_path(job)
    db = get_database(str(job["user_id"]))
    return await process_receipt_document(
        db=db,
        user_id=str(job["user_id"]),
        filename=job["filename"],
        file_hash=job.get("file_hash") or "",
        file_path=path,
        stored_path=str(job["stored_path"]),
        progress_cb=_Progress(store, str(job["id"])),
    )


async def _execute_statement(job: dict, store: PgJobStore) -> dict:
    """Run the statement parse for one queued job."""
    from db.connection import get_database
    from server.api.transactions import run_statement_parse_job

    path = _source_path(job)
    seeded = _job_result(job)
    statement_id = seeded.get("statement_id")
    if not statement_id:
        raise JobFailure(
            error_code="STATEMENT_ROW_MISSING",
            message="This statement upload lost its tracking row. Upload it again.",
        )
    db = get_database(str(job["user_id"]))
    return await run_statement_parse_job(
        db=db,
        user_id=str(job["user_id"]),
        statement_id=str(statement_id),
        filename=job["filename"],
        file_hash=job.get("file_hash") or "",
        source_path=path,
        replaced_count=int(seeded.get("replaced") or 0),
        progress_cb=_Progress(store, str(job["id"])),
    )


async def execute_job(job: dict, store: PgJobStore | None = None) -> dict:
    """Run one job through the engine. Raises on failure; the worker classifies."""
    store = store or PgJobStore()
    kind = job["kind"]
    if kind == "receipt":
        return await _execute_receipt(job, store)
    if kind == "statement":
        return await _execute_statement(job, store)
    raise JobFailure(
        error_code="UNKNOWN_KIND", message=f"Unknown job kind {kind!r}."
    )


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------

def average_seconds(samples: list[float], kind: str) -> float:
    """Rolling average of the last finished jobs of a kind, or the seed."""
    usable = [s for s in samples if s and s > 0]
    if not usable:
        return DEFAULT_SECONDS_PER_KIND.get(kind, 60.0)
    return sum(usable) / len(usable)


def compute_eta_seconds(
    *,
    avg_receipt: float,
    avg_statement: float,
    pending_receipts: int,
    pending_statements: int,
    concurrency: int,
) -> int:
    """How long the work still on this user's queue should take.

    Rolling average per kind × how many of that kind are pending, divided by how
    many run at once. Deliberately simple, and deliberately explicit about what
    it ignores: another user's queue, and an LLM endpoint that is down (a parked
    job counts as pending, so the estimate means "once the model is back").
    """
    slots = max(1, int(concurrency or 1))
    total = avg_receipt * max(0, pending_receipts) + avg_statement * max(0, pending_statements)
    return int(round(total / slots))


def current_concurrency() -> int:
    """How many jobs run at once — read live from ``local_llm_config``.

    One endpoint serves every generation, so this is the single throughput knob:
    ``LOCAL_LLM_MAX_CONCURRENT`` in ``.env``. Read on every tick rather than
    captured at boot, so a change to the default takes effect on the next
    restart without any change here.
    """
    try:
        from config.settings import local_llm_config

        return max(1, int(local_llm_config.max_concurrent))
    except Exception:  # pragma: no cover - config always imports
        return 1


def worker_alive() -> bool:
    """Whether this process's drain loop is running, for the ``worker`` block.

    ``False`` also covers "there is no worker at all" — a process that never
    started one drains nothing either, and the browser needs the honest answer,
    not a reassuring one.
    """
    worker = get_worker()
    return bool(worker is not None and worker.alive)


def worker_restarts() -> int:
    """How many times the supervisor has had to put the drain loop back."""
    worker = get_worker()
    return int(getattr(worker, "restarts", 0) or 0)


def worker_snapshot(user_id: str, *, store: PgJobStore | None = None) -> dict:
    """The ``worker`` block, built synchronously for the SSE ``jobs_summary``.

    Same shape and same arithmetic as ``worker_status``, with one deliberate
    difference: it never probes the LLM endpoint. The probe is a 5s-timeout HTTP
    call and this runs inside every state transition, so it reads the cached
    verdict the worker loop refreshes instead. Never having probed (no local
    endpoint configured) reports reachable, which the engine's own error then
    contradicts if it is wrong.
    """
    store = store or PgJobStore()
    pending = store.pending_by_kind(user_id)
    avg_receipt = average_seconds(store.recent_seconds(user_id, "receipt", 20), "receipt")
    avg_statement = average_seconds(
        store.recent_seconds(user_id, "statement", 20), "statement"
    )
    concurrency = current_concurrency()
    cached = model_reachable_cached()
    return {
        "concurrency": concurrency,
        "alive": worker_alive(),
        "restarts": worker_restarts(),
        "model_reachable": True if cached is None else cached,
        "avg_seconds_receipt": round(avg_receipt, 1),
        "avg_seconds_statement": round(avg_statement, 1),
        "eta_seconds": compute_eta_seconds(
            avg_receipt=avg_receipt,
            avg_statement=avg_statement,
            pending_receipts=pending["receipt"],
            pending_statements=pending["statement"],
            concurrency=concurrency,
        ),
        "pending": pending,
    }


async def worker_status(db) -> dict:
    """The ``worker`` block of ``GET /api/jobs`` for one signed-in user."""
    pending = db.count_pending_processing_jobs_by_kind()
    avg_receipt = average_seconds(db.recent_processing_job_seconds("receipt", 20), "receipt")
    avg_statement = average_seconds(
        db.recent_processing_job_seconds("statement", 20), "statement"
    )
    concurrency = current_concurrency()
    return {
        "concurrency": concurrency,
        # The panel reports a stopped processing worker off this, rather than
        # showing an idle queue that is silently accepting uploads nothing will
        # ever run.
        "alive": worker_alive(),
        "restarts": worker_restarts(),
        "model_reachable": await probe_model_reachable(),
        "avg_seconds_receipt": round(avg_receipt, 1),
        "avg_seconds_statement": round(avg_statement, 1),
        "eta_seconds": compute_eta_seconds(
            avg_receipt=avg_receipt,
            avg_statement=avg_statement,
            pending_receipts=pending["receipt"],
            pending_statements=pending["statement"],
            concurrency=concurrency,
        ),
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

class DocumentQueueWorker:
    """One asyncio loop draining ``processing_jobs`` for every user.

    ``store`` and ``executor`` are injectable so the queue's behaviour — FIFO,
    concurrency, the backoff ladder, restart recovery — can be tested without a
    database or an LLM, which is the only way those properties get asserted at
    all.
    """

    def __init__(
        self,
        *,
        store: PgJobStore | None = None,
        executor: Callable[[dict], Awaitable[dict]] | None = None,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self.store = store or PgJobStore()
        self._executor = executor
        self._poll_seconds = poll_seconds
        self._running: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_reachable: bool | None = None
        self.restarts = 0
        self.last_loop_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._loop_task = asyncio.create_task(self._run(), name="document-queue-worker")
        self._loop_task.add_done_callback(self._loop_ended)
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="document-queue-supervisor"
            )
        logger.info(
            "Document queue worker started (concurrency=%d, poll=%.1fs)",
            current_concurrency(), self._poll_seconds,
        )

    @property
    def alive(self) -> bool:
        """Is the drain loop actually running right now?

        This is what ``GET /api/jobs`` reports as ``worker.alive``. A queue that
        accepts uploads and never runs them looks, from the browser, exactly like
        a queue with nothing in it — which is why the panel gets to report a
        stopped worker instead of guessing.
        """
        task = self._loop_task
        return not self._stopping and task is not None and not task.done()

    def _loop_ended(self, task: asyncio.Task) -> None:
        """Say so loudly if the drain loop dies while jobs still need draining.

        Without this, a loop that ended for any reason other than shutdown looks
        exactly like an empty queue: uploads keep being accepted and nothing ever
        runs them. ``_supervise`` puts it back; this is the line in
        the server error log that says it happened at all.
        """
        if self._stopping or task.cancelled():
            return
        exc = task.exception() if not task.cancelled() else None
        self.last_loop_error = repr(exc) if exc is not None else "exited with no exception"
        logger.error(
            "Document queue worker loop exited unexpectedly (%s) — nothing will "
            "drain the queue until it is back",
            exc or "no exception",
            exc_info=exc,
        )

    async def _supervise(self) -> None:
        """Put the drain loop back when it dies for any reason but shutdown.

        The loop already catches everything a tick can raise, so reaching here
        means something outside that — a bug in the loop's own await, a
        ``MemoryError``, a cancellation from somewhere that is not ``stop()`` —
        ended the only thing that runs uploads. Without a supervisor that is one
        ERROR line and then permanent silence: every upload accepted, none
        processed.

        Backoff, and an ERROR per restart, because a loop that dies immediately
        and forever must not turn into a hot spin that buries the log.
        """
        while not self._stopping:
            task = self._loop_task
            if task is None:  # pragma: no cover - start() always sets one
                return
            # Waits for the task without owning it: a cancellation delivered here
            # (shutdown) must not cancel the drain loop out from under stop().
            await asyncio.wait({task})
            if self._stopping:
                return
            self.restarts += 1
            delay = _RESTART_BACKOFF_SECONDS[
                min(self.restarts, len(_RESTART_BACKOFF_SECONDS)) - 1
            ]
            logger.error(
                "Document queue worker loop is down (%s) — restarting in %ds "
                "(restart #%d)",
                self.last_loop_error or "unknown", delay, self.restarts,
            )
            await asyncio.sleep(delay)
            if self._stopping:
                return
            self._loop_task = asyncio.create_task(
                self._run(), name="document-queue-worker"
            )
            self._loop_task.add_done_callback(self._loop_ended)
            self._wake.set()
            logger.error(
                "Document queue worker loop restarted (restart #%d)", self.restarts
            )

    async def stop(self) -> None:
        """Stop draining and hand anything in flight back to the queue.

        A job cancelled here stays ``running`` in the database only for as long
        as it takes this method to put it back to ``queued``; if the process is
        killed before that, the startup re-queue catches it instead. Either way
        the work is not lost and the position is kept.
        """
        self._stopping = True
        self._wake.set()
        # The supervisor goes first: it exists to put the loop back, and it must
        # not do that while the loop is being taken down on purpose.
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        requeue_running_jobs()
        logger.info("Document queue worker stopped")

    def wake(self) -> None:
        """Ask the loop to look for work now (an upload just landed)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._wake.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
        else:
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:  # pragma: no cover - loop died between checks
                pass

    @property
    def running_count(self) -> int:
        return len(self._running)

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Document queue tick failed — retrying")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            self._wake.clear()

    async def tick(self) -> int:
        """Fill every free slot with the next runnable job. Returns how many started."""
        free = current_concurrency() - len(self._running)
        if free <= 0:
            return 0

        # Nothing waiting means nothing to decide, so do not probe the endpoint
        # either. An idle queue would otherwise write an httpx line into the
        # server error log every few seconds forever, and that log has to stay
        # readable.
        if self.store.pending_count() == 0:
            self._last_reachable = None
            return 0

        reachable = await probe_model_reachable()
        if reachable and self._last_reachable is not True:
            # The endpoint is up (or came back): let everything parked run on
            # this same tick rather than sitting out the rest of its backoff.
            resumed = self.store.resume_waiting()
            if resumed:
                logger.info(
                    "Document queue: local model is reachable — resuming %d waiting job(s)",
                    resumed,
                )
        was_reachable = self._last_reachable
        self._last_reachable = reachable

        if not reachable:
            # Nothing to gain by claiming — every job would die on the same dead
            # socket. Park what is queued so the user is told *why* nothing is
            # moving, then try again on the next tick. No attempt is consumed.
            from core.llm_errors import (
                LOCAL_LLM_UNREACHABLE_CODE,
                LOCAL_LLM_UNREACHABLE_MESSAGE,
            )
            parked = self.store.park_queued(
                error_code=LOCAL_LLM_UNREACHABLE_CODE,
                message=LOCAL_LLM_UNREACHABLE_MESSAGE,
            )
            for row in parked:
                emit_job_event(row, store=self.store)
            if parked or was_reachable is not False:
                logger.warning(
                    "Document queue: local model unreachable — %d job(s) waiting",
                    len(parked),
                )
            return 0

        started = 0
        while free > 0 and not self._stopping:
            job = self.store.claim_next(include_waiting=True)
            if job is None:
                break
            self._spawn(job)
            started += 1
            free -= 1
        return started

    def _spawn(self, job: dict) -> None:
        job_id = str(job["id"])
        logger.info(
            "Job %s started: %s %s (position %s, attempt %d/%d)",
            job_id[:8], job["kind"], job["filename"], job["position"],
            int(job.get("attempts") or 0) + 1, int(job.get("max_attempts") or 3),
        )
        emit_job_event(job, store=self.store)
        task = asyncio.create_task(self._execute(job), name=f"job-{job_id[:8]}")
        self._running[job_id] = task

        def _done(_task: asyncio.Task, jid: str = job_id) -> None:
            self._running.pop(jid, None)
            self._wake.set()

        task.add_done_callback(_done)

    async def _execute(self, job: dict) -> None:
        job_id = str(job["id"])
        started = time.monotonic()
        try:
            executor = self._executor or (lambda j: execute_job(j, self.store))
            result = await executor(job)
        except asyncio.CancelledError:
            # Shutdown. Leave the row ``running`` — stop()/startup re-queues it.
            logger.info("Job %s cancelled by shutdown — will be re-queued", job_id[:8])
            raise
        except BaseException as exc:  # noqa: BLE001 - nothing may escape a task
            self._handle_failure(job, exc, time.monotonic() - started)
            return

        row = self.store.mark_done(job_id, result if isinstance(result, dict) else {})
        emit_job_event(row, store=self.store)
        summary = (result or {}).get("summary") if isinstance(result, dict) else None
        logger.info(
            "Job %s done: %s %s in %.1fs — %s",
            job_id[:8], job["kind"], job["filename"], time.monotonic() - started,
            (summary or {}).get("text") if isinstance(summary, dict) else (summary or ""),
        )

    def _handle_failure(self, job: dict, exc: BaseException, elapsed: float) -> None:
        job_id = str(job["id"])
        transient, error_code, message = classify_job_failure(exc, job["filename"])
        waits = int(job.get("transient_waits") or 0)
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 3)

        timed_out = error_code == "TIMEOUT"
        if transient and timed_out and waits >= _TIMEOUT_WAIT_LIMIT:
            # It is answering and still not finishing — stop calling that a wait.
            transient = False
            message = _timeout_exhausted_message()

        if transient:
            waits += 1
            if timed_out:
                # Each rung of the ladder, on the row, so the panel shows a job
                # working through its attempts rather than one static "Waiting
                # for the extraction model".
                message = _timeout_progress_message(waits)
            delay = _BACKOFF_SECONDS[min(waits, len(_BACKOFF_SECONDS)) - 1]
            row = self.store.mark_waiting(
                job_id,
                transient_waits=waits,
                delay_seconds=delay,
                error_code=error_code,
                error_message=message,
            )
            emit_job_event(row, store=self.store)
            logger.warning(
                "Job %s waiting for the model: %s %s (%s) — retry in %ds, attempt "
                "not consumed (%d/%d)",
                job_id[:8], job["kind"], job["filename"], error_code, delay,
                attempts, max_attempts,
            )
            return

        attempts += 1
        # A corrupt PDF, an unsupported format or a file that is gone from
        # storage will read exactly the same on the second and third attempt.
        # Retrying reaches the same verdict and delays the one sentence the user
        # can act on, so these fail on the first attempt.
        from server.api._extraction_errors import PERMANENT_ERROR_CODES

        permanent = error_code in PERMANENT_ERROR_CODES
        if attempts < max_attempts and not permanent:
            delay = _BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS)) - 1]
            row = self.store.mark_queued_for_retry(
                job_id,
                attempts=attempts,
                delay_seconds=delay,
                error_code=error_code,
                error_message=message,
            )
            emit_job_event(row, store=self.store)
            logger.warning(
                "Job %s failed after %.1fs (%s: %s) — retry %d/%d in %ds",
                job_id[:8], elapsed, error_code, message, attempts, max_attempts, delay,
            )
            return

        row = self.store.mark_failed(
            job_id, attempts=attempts, error_code=error_code, error_message=message
        )
        emit_job_event(row, store=self.store)
        self._announce_final_failure(job, error_code, message)
        logger.error(
            "Job %s FAILED permanently after %d attempt(s)%s: %s %s (%s: %s)",
            job_id[:8], attempts,
            " (not retryable)" if permanent else "",
            job["kind"], job["filename"], error_code, message,
        )

    def _announce_final_failure(self, job: dict, error_code: str, message: str) -> None:
        """Tell the existing UI surfaces a statement is not coming.

        The ``statements`` row is left in ``processing`` while a job still has
        retries, so this is the one place it is failed — with the
        ``statement_failed`` / ``statement_parse_error`` events the parsing card
        listens for.
        """
        if job["kind"] != "statement":
            return
        statement_id = _job_result(job).get("statement_id")
        if not statement_id:
            return
        try:
            from db.connection import get_database
            from server.api.transactions import fail_statement_row

            fail_statement_row(
                get_database(str(job["user_id"])),
                str(job["user_id"]),
                str(statement_id),
                job["filename"],
                error_code=error_code,
                message=message,
            )
        except Exception:
            logger.warning(
                "Could not mark statement %s failed for job %s",
                statement_id, str(job["id"])[:8], exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level worker, owned by the app lifespan
# ---------------------------------------------------------------------------

_worker: DocumentQueueWorker | None = None


async def start_worker() -> DocumentQueueWorker:
    """Start the one worker this process runs. Idempotent."""
    global _worker
    if _worker is None:
        _worker = DocumentQueueWorker()
    await _worker.start()
    return _worker


async def stop_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None


def get_worker() -> DocumentQueueWorker | None:
    return _worker


def wake_worker() -> None:
    """Nudge the worker after an enqueue. Safe when there is no worker."""
    worker = _worker
    if worker is None:
        return
    try:
        worker.wake()
    except Exception:
        logger.debug("wake_worker failed", exc_info=True)

"""The document queue: FIFO, concurrency, backoff, restart recovery, ETA.

The properties pinned here are the ones that let a user drop months of
statements and receipts in at once and walk away, while only a small, fixed
number of documents are being extracted at any moment:

  * both upload endpoints answer **202** with a job, in the same shape;
  * jobs drain in the order they were dropped, no more than
    ``LOCAL_LLM_MAX_CONCURRENT`` at a time;
  * the local LLM server being down parks a job in ``waiting_for_model`` and costs it no
    attempt, and it resumes by itself once ``GET /models`` answers;
  * a real error is retried up to ``max_attempts`` and then fails with the
    classified message a user can act on;
  * a restart re-queues whatever was running instead of losing it;
  * every transition is pushed over SSE, and the ETA is honest arithmetic.

The worker's ``store`` and ``executor`` are injectable, which is what lets all
of that be asserted without a Postgres or an LLM in the loop. The SQL those
transitions compile to is asserted separately, in ``TestRestartRecovery``.
"""

from __future__ import annotations

import asyncio
import io
import itertools
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.llm_errors import (
    LOCAL_LLM_UNREACHABLE_CODE,
    LOCAL_LLM_UNREACHABLE_MESSAGE,
    LocalLLMUnreachableError,
)
from server.access import require_verified_email
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db
from server.jobs import document_queue as dq

FAKE_USER = AuthUser(
    id="user-queue", email="queue@example.com", role="authenticated", email_verified=True
)

PDF_BYTES = b"%PDF-1.4 a-receipt-for-the-queue-tests"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# An in-memory stand-in for the queue's SQL
# ---------------------------------------------------------------------------

class FakeStore:
    """The six transitions the worker makes, over a list of dicts.

    Faithful to the parts the worker's behaviour depends on: claim takes the
    oldest runnable job and marks it running, ``next_attempt_at`` gates a
    retry, and every mutation returns the row the way ``RETURNING`` does.
    """

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self._clock = itertools.count()

    # -- test helpers ------------------------------------------------------
    def add(
        self,
        *,
        kind: str = "receipt",
        filename: str | None = None,
        status: str = "queued",
        attempts: int = 0,
        max_attempts: int = 3,
        transient_waits: int = 0,
        next_attempt_at: datetime | None = None,
        result: dict | None = None,
        user_id: str = FAKE_USER.id,
    ) -> dict:
        seq = next(self._clock)
        job = {
            "id": f"job-{next(self._ids):04d}",
            "user_id": user_id,
            "kind": kind,
            "filename": filename or f"{kind}-{seq}.pdf",
            "stored_path": f"{user_id}/202609/{seq}_{kind}.pdf",
            "file_hash": f"hash-{seq}",
            "status": status,
            "position": seq + 1,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "transient_waits": transient_waits,
            "next_attempt_at": next_attempt_at,
            "pages_total": None,
            "pages_done": 0,
            "error_code": None,
            "error_message": None,
            "result": result,
            "created_at": _now() + timedelta(milliseconds=seq),
            "started_at": None,
            "finished_at": None,
            "updated_at": _now(),
        }
        self.jobs.append(job)
        return job

    def get(self, job_id: str) -> dict:
        return next(j for j in self.jobs if j["id"] == job_id)

    def by_status(self, status: str) -> list[dict]:
        return [j for j in self.jobs if j["status"] == status]

    # -- the store interface ----------------------------------------------
    def claim_next(self, *, include_waiting: bool = True) -> dict | None:
        statuses = {"queued", "waiting_for_model"} if include_waiting else {"queued"}
        now = _now()
        runnable = [
            j for j in self.jobs
            if j["status"] in statuses
            and (j["next_attempt_at"] is None or j["next_attempt_at"] <= now)
        ]
        runnable.sort(key=lambda j: (j["created_at"], j["position"], str(j["id"])))
        if not runnable:
            return None
        job = runnable[0]
        job["status"] = "running"
        job["started_at"] = now
        return dict(job)

    def mark_done(self, job_id: str, result: dict | None) -> dict:
        job = self.get(job_id)
        merged = dict(job.get("result") or {})
        merged.update(result or {})
        job.update({
            "status": "done", "result": merged, "error_code": None,
            "error_message": None, "next_attempt_at": None, "finished_at": _now(),
        })
        return dict(job)

    def mark_waiting(self, job_id, *, transient_waits, delay_seconds,
                     error_code, error_message) -> dict:
        job = self.get(job_id)
        job.update({
            "status": "waiting_for_model",
            "transient_waits": transient_waits,
            "next_attempt_at": _now() + timedelta(seconds=delay_seconds),
            "error_code": error_code, "error_message": error_message,
        })
        return dict(job)

    def mark_queued_for_retry(self, job_id, *, attempts, delay_seconds,
                              error_code, error_message) -> dict:
        job = self.get(job_id)
        job.update({
            "status": "queued", "attempts": attempts,
            "next_attempt_at": _now() + timedelta(seconds=delay_seconds),
            "error_code": error_code, "error_message": error_message,
            "started_at": None,
        })
        return dict(job)

    def mark_failed(self, job_id, *, attempts, error_code, error_message) -> dict:
        job = self.get(job_id)
        job.update({
            "status": "failed", "attempts": attempts, "error_code": error_code,
            "error_message": error_message, "next_attempt_at": None,
            "finished_at": _now(),
        })
        return dict(job)

    def set_progress(self, job_id, pages_done, pages_total, mode=None) -> dict:
        job = self.get(job_id)
        job["pages_done"] = pages_done
        if pages_total:
            job["pages_total"] = pages_total
        if mode is not None:
            result = dict(job.get("result") or {})
            result["mode"] = mode
            job["result"] = result
        return dict(job)

    def batch(self, user_id) -> dict:
        """Everything since this user's queue was last empty.

        Same definition as PROCESSING_JOB_BATCH_SQL, in Python: the anchor is the
        most recent job that arrived while nothing of that user's was still
        unfinished, and the batch is everything from there on.
        """
        mine = sorted(
            (j for j in self.jobs if str(j["user_id"]) == str(user_id)),
            key=lambda j: (j["created_at"], j["position"], j["id"]),
        )
        anchor = None
        for i, job in enumerate(mine):
            busy = any(
                (p["finished_at"] is None or p["finished_at"] > job["created_at"])
                for p in mine[:i]
            )
            if not busy:
                anchor = i
        if anchor is None:
            return {"total": 0, "finished": 0, "started_at": None}
        window = mine[anchor:]
        return {
            "total": len(window),
            "finished": sum(
                1 for j in window if j["status"] in ("done", "failed", "cancelled")
            ),
            "started_at": window[0]["created_at"],
        }

    def resume_waiting(self) -> int:
        now = _now()
        resumed = 0
        for job in self.jobs:
            if job["status"] == "waiting_for_model" and (
                job["next_attempt_at"] is None or job["next_attempt_at"] > now
            ):
                job["next_attempt_at"] = now
                resumed += 1
        return resumed

    def park_queued(self, *, error_code, message, delay_seconds=10) -> list[dict]:
        parked = []
        for job in self.jobs:
            if job["status"] == "queued":
                job.update({
                    "status": "waiting_for_model",
                    "next_attempt_at": _now() + timedelta(seconds=delay_seconds),
                    "error_code": error_code, "error_message": message,
                })
                parked.append(dict(job))
        return parked

    def pending_count(self) -> int:
        return len([
            j for j in self.jobs
            if j["status"] in ("queued", "waiting_for_model")
        ])

    def counts(self, user_id: str) -> dict[str, int]:
        counts = {status: 0 for status in dq.ALL_STATUSES}
        for job in self.jobs:
            if job["user_id"] == user_id:
                counts[job["status"]] += 1
        return counts

    def ahead_for(self, job: dict) -> int | None:
        if job["status"] not in dq.PENDING_STATUSES:
            return None
        key = (job["created_at"], job["position"], str(job["id"]))
        return len([
            j for j in self.jobs
            if j["user_id"] == job["user_id"]
            and j["status"] in ("queued", "waiting_for_model")
            and (j["created_at"], j["position"], str(j["id"])) < key
        ])

    def pending_by_kind(self, user_id: str) -> dict[str, int]:
        out = {"receipt": 0, "statement": 0}
        for job in self.jobs:
            if job["user_id"] == user_id and job["status"] in dq.PENDING_STATUSES:
                out[job["kind"]] += 1
        return out

    def recent_seconds(self, user_id: str, kind: str, limit: int = 20) -> list[float]:
        return []


def _rewind(job: dict) -> None:
    """Pretend the job's backoff has elapsed."""
    job["next_attempt_at"] = _now() - timedelta(seconds=1)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def reachable():
    """The local LLM server answers, unless a test flips this."""
    state = {"up": True}

    async def _probe(*_args, **_kwargs):
        return state["up"]

    with patch.object(dq, "probe_model_reachable", _probe):
        yield state


async def _drain(worker: dq.DocumentQueueWorker, rounds: int = 40) -> None:
    """Tick until nothing is left to start and nothing is still running."""
    for _ in range(rounds):
        started = await worker.tick()
        if worker._running:
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)
            await asyncio.sleep(0)
            continue
        if started == 0:
            return


# ---------------------------------------------------------------------------
# FIFO and concurrency
# ---------------------------------------------------------------------------

class TestDraining:
    @pytest.mark.asyncio
    async def test_jobs_drain_in_the_order_they_were_dropped(self, store, reachable):
        """Across kinds, too: one line, oldest first."""
        for kind in ("receipt", "statement", "receipt", "statement", "receipt"):
            store.add(kind=kind)
        expected = [j["filename"] for j in store.jobs]
        ran: list[str] = []

        async def _executor(job):
            ran.append(job["filename"])
            return {}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await _drain(worker)

        assert ran == expected
        assert len(store.by_status("done")) == 5

    @pytest.mark.asyncio
    async def test_no_more_than_the_configured_concurrency_runs_at_once(
        self, store, reachable
    ):
        """One model server behind the queue: this ceiling is the whole point."""
        for _ in range(6):
            store.add()
        release = asyncio.Event()
        peak = {"value": 0}
        live = {"value": 0}

        async def _executor(job):
            live["value"] += 1
            peak["value"] = max(peak["value"], live["value"])
            await release.wait()
            live["value"] -= 1
            return {}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=2):
            assert await worker.tick() == 2
            assert worker.running_count == 2
            # Let both tasks reach the LLM call they are standing in for.
            for _ in range(4):
                await asyncio.sleep(0)
            assert live["value"] == 2, "both slots should be in flight"
            # A third job must not start while both slots are full.
            assert await worker.tick() == 0
            assert len(store.by_status("queued")) == 4
            release.set()
            await _drain(worker)

        assert peak["value"] == 2, f"peak concurrency was {peak['value']}"
        assert len(store.by_status("done")) == 6

    @pytest.mark.asyncio
    async def test_an_idle_queue_does_not_probe_the_model(self, store):
        """An empty queue is silent: no probe, so no noise in the server log."""
        probes = []

        async def _probe(*_a, **_k):
            probes.append(1)
            return True

        worker = dq.DocumentQueueWorker(store=store, executor=None)
        with patch.object(dq, "probe_model_reachable", _probe),              patch.object(dq, "current_concurrency", return_value=2):
            assert await worker.tick() == 0
            assert probes == []
            store.add()
            assert await worker.tick() == 1
            assert len(probes) == 1
        await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_concurrency_is_read_live_not_captured_at_boot(self, store, reachable):
        """Raising LOCAL_LLM_MAX_CONCURRENT is the only throughput knob."""
        for _ in range(4):
            store.add()
        release = asyncio.Event()

        async def _executor(job):
            await release.wait()
            return {}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            assert await worker.tick() == 1
        with patch.object(dq, "current_concurrency", return_value=3):
            assert await worker.tick() == 2  # fills up to the new ceiling
        release.set()
        await _drain(worker)


# ---------------------------------------------------------------------------
# The local LLM server is down
# ---------------------------------------------------------------------------

class TestWaitingForModel:
    @pytest.mark.asyncio
    async def test_a_transient_failure_parks_the_job_without_spending_an_attempt(
        self, store, reachable
    ):
        job = store.add()

        async def _executor(_job):
            raise LocalLLMUnreachableError()

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        row = store.get(job["id"])
        assert row["status"] == "waiting_for_model"
        assert row["attempts"] == 0, "an outage is not the document's fault"
        assert row["transient_waits"] == 1
        assert row["error_code"] == LOCAL_LLM_UNREACHABLE_CODE
        assert row["error_message"] == LOCAL_LLM_UNREACHABLE_MESSAGE
        assert row["next_attempt_at"] > _now()

    @pytest.mark.asyncio
    async def test_the_backoff_ladder_is_10_30_60_then_60(self, store, reachable):
        job = store.add()

        async def _executor(_job):
            raise LocalLLMUnreachableError()

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        delays: list[int] = []
        with patch.object(dq, "current_concurrency", return_value=1):
            for _ in range(4):
                _rewind(store.get(job["id"]))
                before = _now()
                await worker.tick()
                await asyncio.gather(
                    *list(worker._running.values()), return_exceptions=True
                )
                delays.append(round((store.get(job["id"])["next_attempt_at"] - before).total_seconds()))

        assert delays == [10, 30, 60, 60]
        assert store.get(job["id"])["attempts"] == 0

    @pytest.mark.asyncio
    async def test_it_resumes_by_itself_once_the_model_answers(self, store, reachable):
        job = store.add()
        outcome = {"fail": True}

        async def _executor(_job):
            if outcome["fail"]:
                raise LocalLLMUnreachableError()
            return {"document_id": 9, "summary": {"text": "extracted"}}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)
            assert store.get(job["id"])["status"] == "waiting_for_model"

            # The local LLM server goes away entirely: nothing is claimed, nothing is failed.
            reachable["up"] = False
            assert await worker.tick() == 0
            assert store.get(job["id"])["status"] == "waiting_for_model"

            # …and comes back. The parked job runs on that same tick, without
            # anyone having to wait out the rest of its backoff.
            reachable["up"] = True
            outcome["fail"] = False
            assert await worker.tick() == 1
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        row = store.get(job["id"])
        assert row["status"] == "done"
        assert row["attempts"] == 0
        assert row["result"]["document_id"] == 9

    @pytest.mark.asyncio
    async def test_a_queued_job_is_parked_so_the_user_is_told_why(self, store, reachable):
        """Nothing is moving: say "waiting for the model", not a silent "queued"."""
        store.add()
        store.add()
        reachable["up"] = False

        worker = dq.DocumentQueueWorker(store=store, executor=None)
        with patch.object(dq, "current_concurrency", return_value=2):
            assert await worker.tick() == 0

        assert len(store.by_status("waiting_for_model")) == 2
        for row in store.by_status("waiting_for_model"):
            assert row["error_message"] == LOCAL_LLM_UNREACHABLE_MESSAGE
            assert row["attempts"] == 0

    @pytest.mark.asyncio
    async def test_a_model_that_answers_but_never_finishes_eventually_fails(
        self, store, reachable
    ):
        """The one bounded wait: a job parked forever is an invisible hang."""
        job = store.add()

        async def _executor(_job):
            raise asyncio.TimeoutError()

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            for _ in range(dq._TIMEOUT_WAIT_LIMIT + 4):
                _rewind(store.get(job["id"]))
                await worker.tick()
                await asyncio.gather(
                    *list(worker._running.values()), return_exceptions=True
                )
                if store.get(job["id"])["status"] == "failed":
                    break

        row = store.get(job["id"])
        assert row["status"] == "failed"
        assert row["error_code"] == "TIMEOUT"
        assert row["transient_waits"] == dq._TIMEOUT_WAIT_LIMIT


# ---------------------------------------------------------------------------
# Real failures
# ---------------------------------------------------------------------------

class TestHardFailures:
    @pytest.mark.asyncio
    async def test_it_retries_up_to_max_attempts_then_fails_with_the_classified_message(
        self, store, reachable
    ):
        job = store.add(max_attempts=3)

        async def _executor(_job):
            raise RuntimeError("the model answered with something unusable")

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        seen: list[tuple[str, int]] = []
        with patch.object(dq, "current_concurrency", return_value=1):
            for _ in range(3):
                _rewind(store.get(job["id"]))
                await worker.tick()
                await asyncio.gather(
                    *list(worker._running.values()), return_exceptions=True
                )
                row = store.get(job["id"])
                seen.append((row["status"], row["attempts"]))

        assert seen == [("queued", 1), ("queued", 2), ("failed", 3)]
        row = store.get(job["id"])
        assert row["error_code"] == "EXTRACTION_ERROR"
        assert row["error_message"]

    @pytest.mark.asyncio
    async def test_an_unreadable_file_fails_on_the_first_attempt(self, store, reachable):
        """A corrupt PDF is not transient and not retryable.

        PyMuPDF's own error ("Failed to open file '...'") has to be classified
        as FILE_CORRUPTED rather than falling through to a generic
        EXTRACTION_ERROR: a file the reader cannot open will not open on the
        second or third try either, so retrying only makes the user wait for a
        verdict that cannot change.
        """
        job = store.add(max_attempts=3, filename="shredded.pdf")

        async def _executor(_job):
            raise RuntimeError("Failed to open file 'shredded.pdf'.")

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        row = store.get(job["id"])
        assert (row["status"], row["attempts"]) == ("failed", 1)
        assert row["error_code"] == "FILE_CORRUPTED"
        assert row["error_message"] == "This file could not be read as a PDF."

    @pytest.mark.asyncio
    async def test_a_failed_statement_job_finally_fails_its_statements_row(
        self, store, reachable
    ):
        """The statements row is only failed once the queue has given up on it."""
        job = store.add(
            kind="statement", max_attempts=1, result={"statement_id": "stmt-9"}
        )

        async def _executor(_job):
            raise RuntimeError("model returned no transactions")

        failed: list[tuple] = []
        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch("db.connection.get_database", return_value=MagicMock()), \
             patch("server.api.transactions.fail_statement_row",
                   side_effect=lambda *a, **k: failed.append((a, k))):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        assert store.get(job["id"])["status"] == "failed"
        assert len(failed) == 1
        assert failed[0][0][2] == "stmt-9"

    @pytest.mark.asyncio
    async def test_a_retry_does_not_touch_the_statements_row(self, store, reachable):
        job = store.add(
            kind="statement", max_attempts=3, result={"statement_id": "stmt-9"}
        )

        async def _executor(_job):
            raise RuntimeError("transient-looking but not")

        failed: list = []
        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch("server.api.transactions.fail_statement_row",
                   side_effect=lambda *a, **k: failed.append(a)):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        assert store.get(job["id"])["status"] == "queued"
        assert failed == [], "a statement with retries left is still 'processing'"

    @pytest.mark.asyncio
    async def test_a_missing_stored_file_fails_with_something_a_user_can_act_on(
        self, store
    ):
        job = store.add()
        job["stored_path"] = None
        with pytest.raises(dq.JobFailure) as exc_info:
            dq._source_path(job)
        assert exc_info.value.error_code == "FILE_MISSING"
        assert "again" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    def _fake_pool(self, rowcount: int):
        cur = MagicMock()
        cur.rowcount = rowcount
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool = MagicMock()
        pool.getconn.return_value = conn
        return pool, conn, cur

    def test_running_jobs_go_back_to_queued_with_their_position(self):
        pool, conn, cur = self._fake_pool(3)
        with patch("db.connection.get_pool", return_value=pool):
            assert dq.requeue_running_jobs() == 3

        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "SET status = 'queued'" in sql
        assert "WHERE status = 'running'" in sql
        assert "position" not in sql, "a restart must not move anyone in the line"
        assert "attempts" not in sql, "a restart is not the document's fault"
        conn.commit.assert_called_once()
        pool.putconn.assert_called_once_with(conn)

    def test_a_recovery_failure_never_stops_the_server_booting(self):
        pool, conn, cur = self._fake_pool(0)
        cur.execute.side_effect = RuntimeError("no database")
        with patch("db.connection.get_pool", return_value=pool):
            assert dq.requeue_running_jobs() == 0
        conn.rollback.assert_called_once()

    def test_a_statement_with_no_live_job_is_failed(self):
        pool, conn, cur = self._fake_pool(2)
        with patch("db.connection.get_pool", return_value=pool):
            assert dq.fail_orphaned_processing_statements() == 2

        sql, params = cur.execute.call_args[0]
        flat = " ".join(sql.split())
        assert "status = 'failed'" in flat
        assert "s.status = 'processing'" in flat
        assert "NOT EXISTS" in flat, "a statement whose job is queued is not stale"
        assert params[0] == dq.RESTART_MESSAGE
        assert set(params[1]) == set(dq.PENDING_STATUSES)

    def test_an_orphan_check_failure_is_also_non_fatal(self):
        pool, conn, cur = self._fake_pool(0)
        cur.execute.side_effect = RuntimeError("no database")
        with patch("db.connection.get_pool", return_value=pool):
            assert dq.fail_orphaned_processing_statements() == 0
        conn.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_cancelled_job_is_left_running_for_the_requeue_to_find(
        self, store, reachable
    ):
        """Shutdown must not fail a job — the restart picks it back up."""
        job = store.add()
        started = asyncio.Event()

        async def _executor(_job):
            started.set()
            await asyncio.sleep(30)
            return {}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await worker.tick()
        await asyncio.wait_for(started.wait(), timeout=1)
        for task in list(worker._running.values()):
            task.cancel()
        await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        assert store.get(job["id"])["status"] == "running"
        assert store.get(job["id"])["error_code"] is None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class TestEvents:
    @pytest.mark.asyncio
    async def test_every_transition_pushes_job_updated_and_jobs_summary(
        self, store, reachable
    ):
        job = store.add()
        events: list[tuple[str, dict]] = []

        async def _executor(_job):
            return {"document_id": 5, "summary": {"text": "Vendor — CAD 10.00"}}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch("server.api.events.send_event",
                   side_effect=lambda uid, t, d=None: events.append((t, d or {}))):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        names = [name for name, _ in events]
        assert names == [
            "job_updated", "jobs_summary",   # claimed → running
            "job_updated", "jobs_summary",   # → done
        ]
        first = dict(events)["job_updated"]
        assert first["job_id"] == job["id"]
        assert events[0][1]["status"] == "running"
        assert events[2][1]["status"] == "done"
        assert events[2][1]["result"]["document_id"] == 5
        summary = events[3][1]
        assert {k: v for k, v in summary.items() if k not in ("worker", "batch")} == {
            "queued": 0, "running": 0, "waiting_for_model": 0,
            "done": 1, "failed": 0, "cancelled": 0,
        }
        # The worker block rides along so the ETA and the model-unreachable
        # banner change on the event instead of on the next 3s poll.
        assert summary["worker"]["model_reachable"] is True
        assert summary["worker"]["concurrency"] == 1
        assert summary["worker"]["eta_seconds"] == 0
        # And so does the batch, so the "finished of total" counter moves on the
        # event rather than waiting for the poll — and means the same thing in
        # every tab.
        assert summary["batch"] == {
            "total": 1, "finished": 1, "started_at": job["created_at"],
        }

    @pytest.mark.asyncio
    async def test_page_progress_is_written_and_pushed(self, store, reachable):
        job = store.add()
        events: list[tuple[str, dict]] = []

        async def _executor(j):
            progress = dq._Progress(store, j["id"])
            # How it is reading the document rides along with the page count.
            progress(3, 12, mode="paged")
            return {}

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch("server.api.events.send_event",
                   side_effect=lambda uid, t, d=None: events.append((t, d or {}))):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        row = store.get(job["id"])
        assert (row["pages_done"], row["pages_total"]) == (3, 12)
        progress_events = [
            d for name, d in events if name == "job_updated" and d["pages_done"] == 3
        ]
        assert progress_events, "the progress bar has to be pushed, not polled for"
        # The mode is what the row's sentence is built from — "Extracting page N
        # of M" for batched pages, "Reading M pages in one pass" for a single
        # call — so it has to reach the browser with the progress.
        assert row["result"]["mode"] == "paged"
        assert progress_events[-1]["result"]["mode"] == "paged"

    def test_a_new_mode_is_pushed_immediately_even_without_page_movement(self, store):
        """The mode arrives before the first (possibly very long) LLM call.

        Throttling it behind the 1.5s page-write interval would leave a
        multi-page statement claiming to be on its first page for the whole
        call, which is the lie this exists to remove.
        """
        job = store.add()
        progress = dq._Progress(store, job["id"])
        with patch("server.api.events.send_event"):
            progress(0, 6, mode="one_pass")
        assert store.get(job["id"])["result"]["mode"] == "one_pass"

    def test_a_progress_callback_never_raises(self, store):
        """A progress bar is not worth failing an extraction over."""
        job = store.add()
        progress = dq._Progress(store, job["id"])
        broken = MagicMock(side_effect=RuntimeError("db gone"))
        with patch.object(store, "set_progress", broken):
            progress(1, 2)  # must not raise
        progress("not", "numbers")  # must not raise


# ---------------------------------------------------------------------------
# The job object and the ETA
# ---------------------------------------------------------------------------

class TestJobShape:
    def test_job_public_is_json_ready_and_carries_the_queue_position(self, store):
        job = store.add(kind="statement", result={"statement_id": "stmt-1"})
        public = dq.job_public(job)

        assert public["job_id"] == public["id"] == job["id"]
        assert public["kind"] == "statement"
        assert public["status"] == "queued"
        assert public["position"] == job["position"]
        assert public["result"] == {"statement_id": "stmt-1"}
        assert isinstance(public["created_at"], str)
        import json
        json.dumps(public)  # must survive the wire

    def test_a_queued_job_says_how_many_are_ahead_of_it_not_its_position(self, store):
        """Queue depth, not the submission sequence: 0 ahead means "Next up"."""
        first, second, third = store.add(), store.add(), store.add()
        for expected, job in enumerate((first, second, third)):
            public = dq.job_public(job, ahead=store.ahead_for(job))
            assert public["ahead"] == expected
            assert public["position"] == job["position"]  # still in the payload

        # The one in front finishes: everyone behind moves up.
        store.mark_done(first["id"], {})
        assert dq.job_public(second, ahead=store.ahead_for(second))["ahead"] == 0
        # A finished job has no queue depth at all.
        done = store.get(first["id"])
        assert dq.job_public(done, ahead=store.ahead_for(done))["ahead"] is None

    def test_a_jsonb_result_that_came_back_as_text_is_still_parsed(self, store):
        job = store.add()
        job["result"] = '{"document_id": 7}'
        assert dq.job_public(job)["result"] == {"document_id": 7}


class TestEta:
    def test_the_average_is_the_rolling_one_with_a_default_seed(self):
        assert dq.average_seconds([10.0, 20.0, 30.0], "receipt") == 20.0
        # No history yet: the built-in seed estimates, one per kind.
        assert dq.average_seconds([], "receipt") == 25.0
        assert dq.average_seconds([], "statement") == 90.0
        # Junk is ignored rather than dragging the estimate to zero.
        assert dq.average_seconds([0.0, -1.0, 40.0], "statement") == 40.0

    def test_eta_is_work_over_slots(self):
        # 6 receipts at 20s + 2 statements at 90s = 300s of work, two at a time.
        assert dq.compute_eta_seconds(
            avg_receipt=20.0, avg_statement=90.0,
            pending_receipts=6, pending_statements=2, concurrency=2,
        ) == 150
        # Nothing pending is nothing to wait for.
        assert dq.compute_eta_seconds(
            avg_receipt=20.0, avg_statement=90.0,
            pending_receipts=0, pending_statements=0, concurrency=2,
        ) == 0
        # A nonsense concurrency must not divide by zero.
        assert dq.compute_eta_seconds(
            avg_receipt=10.0, avg_statement=10.0,
            pending_receipts=1, pending_statements=1, concurrency=0,
        ) == 20

    @pytest.mark.asyncio
    async def test_worker_status_reports_what_the_ui_needs(self, reachable):
        db = MagicMock()
        db.count_pending_processing_jobs_by_kind.return_value = {
            "receipt": 4, "statement": 1,
        }
        db.recent_processing_job_seconds.side_effect = (
            lambda kind, limit=20: [20.0] if kind == "receipt" else [80.0]
        )
        with patch.object(dq, "current_concurrency", return_value=2):
            status = await dq.worker_status(db)

        assert status["concurrency"] == 2
        assert status["model_reachable"] is True
        assert status["avg_seconds_receipt"] == 20.0
        assert status["avg_seconds_statement"] == 80.0
        assert status["eta_seconds"] == 80  # (4*20 + 1*80) / 2
        assert status["pending"] == {"receipt": 4, "statement": 1}


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_an_outage_is_transient_and_says_so_honestly(self):
        transient, code, message = dq.classify_job_failure(
            LocalLLMUnreachableError(), "receipt.pdf"
        )
        assert transient is True
        assert code == LOCAL_LLM_UNREACHABLE_CODE
        assert message == LOCAL_LLM_UNREACHABLE_MESSAGE

    def test_a_wrapped_connection_error_is_also_transient(self):
        exc = RuntimeError("Connection error.")
        transient, code, _ = dq.classify_job_failure(exc, "receipt.pdf")
        assert transient is True
        assert code == LOCAL_LLM_UNREACHABLE_CODE

    def test_a_bad_file_is_not_transient(self):
        transient, code, _ = dq.classify_job_failure(
            ValueError("format is not supported"), "notes.txt"
        )
        assert transient is False
        assert code == "UNSUPPORTED_FORMAT"

    def test_a_corrupt_pdf_is_named_by_the_error_pymupdf_actually_raises(self):
        import pymupdf

        transient, code, message = dq.classify_job_failure(
            pymupdf.FileDataError("Failed to open file 'x.pdf'."), "x.pdf"
        )
        assert (transient, code) == (False, "FILE_CORRUPTED")
        assert message == "This file could not be read as a PDF."

        # Zero bytes, and the planner's own verdict, land in the same place.
        assert dq.classify_job_failure(
            pymupdf.EmptyFileError("Cannot open empty file"), "x.pdf"
        )[1] == "FILE_CORRUPTED"
        assert dq.classify_job_failure(
            ValueError("no page of this document could be rendered for extraction"),
            "x.pdf",
        )[1] == "FILE_CORRUPTED"

    def test_a_job_failure_carries_its_own_verdict(self):
        failure = dq.JobFailure(
            error_code="unable_to_parse", message="could not read it", transient=False
        )
        assert dq.classify_job_failure(failure, "x.pdf") == (
            False, "unable_to_parse", "could not read it",
        )

    def test_the_progress_kwarg_is_only_passed_when_the_engine_takes_it(self):
        def without(path, db=None):
            ...

        def with_it(path, db=None, progress_cb=None):
            ...

        def with_kwargs(path, **kwargs):
            ...

        assert dq.engine_accepts_progress_cb(without) is False
        assert dq.engine_accepts_progress_cb(with_it) is True
        assert dq.engine_accepts_progress_cb(with_kwargs) is True


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def api(store):
    """A TestClient whose DatabasePg is backed by the FakeStore above."""
    db = MagicMock()
    db.user_id = FAKE_USER.id
    db.get_document_by_hash.return_value = None
    db.find_active_processing_job_by_hash.return_value = None
    db.count_processing_jobs.side_effect = lambda: store.counts(FAKE_USER.id)
    db.count_pending_processing_jobs_by_kind.return_value = {"receipt": 0, "statement": 0}
    db.recent_processing_job_seconds.return_value = []
    db.list_processing_jobs.side_effect = lambda **kw: [
        j for j in store.jobs
        if kw.get("status") in (None, j["status"])
    ]
    db.get_processing_job.side_effect = lambda jid: next(
        (j for j in store.jobs if j["id"] == jid), None
    )

    def _insert(**kwargs):
        return store.add(
            kind=kwargs["kind"],
            filename=kwargs["filename"],
            status=kwargs.get("status", "queued"),
            result=kwargs.get("result"),
        )

    db.insert_processing_job.side_effect = _insert

    def _retry(jid):
        job = next((j for j in store.jobs if j["id"] == jid), None)
        if job is None or job["status"] not in ("failed", "cancelled"):
            return None
        job.update({"status": "queued", "attempts": 0, "transient_waits": 0,
                    "error_code": None, "error_message": None})
        return dict(job)

    def _cancel(jid):
        job = next((j for j in store.jobs if j["id"] == jid), None)
        if job is None or job["status"] != "queued":
            return None
        job.update({"status": "cancelled", "finished_at": _now()})
        return dict(job)

    def _clear(status):
        keep = [j for j in store.jobs if j["status"] != status]
        removed = len(store.jobs) - len(keep)
        store.jobs[:] = keep
        return removed

    db.retry_processing_job.side_effect = _retry
    db.cancel_processing_job.side_effect = _cancel
    db.delete_processing_jobs.side_effect = _clear

    async def _user():
        return FAKE_USER


    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_verified_email] = _user
    app.dependency_overrides[get_db] = lambda: db
    with patch("core.file_storage.upload_file", return_value="user-queue/202609/x.pdf"), \
         patch("server.jobs.document_queue.emit_job_event"), \
         patch("server.jobs.document_queue.wake_worker"), \
         patch.object(dq, "probe_model_reachable", lambda *a, **k: _true()):
        yield TestClient(app, raise_server_exceptions=False), db
    app.dependency_overrides.clear()


async def _true() -> bool:
    return True


class TestUploadAnswers202:
    def test_a_receipt_upload_answers_202_with_a_queued_job(self, api, store):
        client, db = api
        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("lunch.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["kind"] == "receipt"
        assert body["filename"] == "lunch.pdf"
        assert body["job_id"]
        assert body["position"] == 1
        # Nothing was extracted in the request — that is the whole point.
        db.insert_document.assert_not_called()
        assert db.insert_processing_job.call_args.kwargs["stored_path"]

    def test_an_unsupported_file_is_still_rejected_before_anything_is_queued(self, api):
        client, db = api
        resp = client.post(
            "/api/receipts/upload",
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert resp.status_code == 400
        db.insert_processing_job.assert_not_called()


class TestJobsApi:
    def test_list_returns_jobs_counts_and_the_worker_block(self, api, store):
        client, _db = api
        store.add(kind="receipt")
        store.add(kind="statement", status="done")

        resp = client.get("/api/jobs")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["jobs"]) == 2
        assert body["counts"]["queued"] == 1
        assert body["counts"]["done"] == 1
        assert body["worker"]["concurrency"] >= 1
        assert body["worker"]["model_reachable"] is True
        assert body["worker"]["eta_seconds"] == 0

    def test_a_status_filter_narrows_the_list_but_not_the_counts(self, api, store):
        client, _db = api
        store.add(status="queued")
        store.add(status="done")

        body = client.get("/api/jobs", params={"status": "done"}).json()
        assert [j["status"] for j in body["jobs"]] == ["done"]
        assert body["counts"]["queued"] == 1

    def test_an_unknown_status_is_a_400(self, api):
        client, _db = api
        assert client.get("/api/jobs", params={"status": "sideways"}).status_code == 400

    def test_one_job_by_id(self, api, store):
        client, _db = api
        job = store.add()
        body = client.get(f"/api/jobs/{job['id']}").json()
        assert body["job_id"] == job["id"]
        assert client.get("/api/jobs/job-does-not-exist").status_code == 404

    def test_retry_resets_a_failed_job(self, api, store):
        client, _db = api
        job = store.add(status="failed", attempts=3)
        body = client.post(f"/api/jobs/{job['id']}/retry").json()
        assert body["status"] == "queued"
        assert body["attempts"] == 0
        assert store.get(job["id"])["error_message"] is None

    def test_retry_on_a_running_job_is_a_409(self, api, store):
        client, _db = api
        job = store.add(status="running")
        resp = client.post(f"/api/jobs/{job['id']}/retry")
        assert resp.status_code == 409
        assert "running" in resp.json()["detail"]

    def test_cancel_only_works_before_a_job_starts(self, api, store):
        client, _db = api
        queued = store.add(status="queued")
        running = store.add(status="running")

        assert client.post(f"/api/jobs/{queued['id']}/cancel").json()["status"] == (
            "cancelled"
        )
        resp = client.post(f"/api/jobs/{running['id']}/cancel")
        assert resp.status_code == 409

    def test_cancelling_a_statement_closes_its_statements_row(self, api, store):
        """Otherwise the transactions page spins until the next restart."""
        client, _db = api
        job = store.add(
            kind="statement", status="queued", result={"statement_id": "stmt-77"}
        )
        closed: list = []
        with patch("server.api.transactions.fail_statement_row",
                   side_effect=lambda *a, **k: closed.append((a, k))):
            assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200

        assert len(closed) == 1
        assert closed[0][0][2] == "stmt-77"
        assert closed[0][1]["error_code"] == "CANCELLED"

    def test_clear_removes_finished_jobs_only(self, api, store):
        client, _db = api
        store.add(status="done")
        store.add(status="done")
        store.add(status="queued")

        body = client.request("DELETE", "/api/jobs", params={"status": "done"}).json()
        assert body["deleted"] == 2
        assert [j["status"] for j in store.jobs] == ["queued"]

        resp = client.request("DELETE", "/api/jobs", params={"status": "queued"})
        assert resp.status_code == 400, "pending work is never cleared"

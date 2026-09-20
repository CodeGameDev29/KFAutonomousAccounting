"""Two ways the queue could read as a hang, and what says otherwise.

**A document that times out every attempt.** A read timeout is transient — the
local LLM server may be loading weights — so the job is parked in
``waiting_for_model`` without spending an attempt, up to ``_TIMEOUT_WAIT_LIMIT``
times. A row whose only sentence is "Waiting for the extraction model" is
indistinguishable from a job nothing is happening to, so each timed-out attempt
writes its own line — "Attempt 2 of 3 timed out after 600 s; retrying" — and the
panel shows a ladder being climbed.

**A drain loop that dies.** The loop is the only thing that runs uploads, and it
dying looks, from the browser, exactly like an empty queue: uploads keep being
accepted and nothing ever runs. A supervisor puts it back with backoff and logs
an ERROR per restart, and ``GET /api/jobs`` reports ``worker.alive`` so the panel
can say the worker stopped instead of showing a calm, idle queue.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from server.jobs import document_queue as dq
from tests.test_document_queue import FakeStore, _rewind


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def reachable():
    """The local LLM server answers ``GET /models``; it is the generation that times out."""
    state = {"up": True}

    async def _probe(*, force: bool = False):
        return state["up"]

    with patch.object(dq, "probe_model_reachable", _probe):
        yield state


# ---------------------------------------------------------------------------
# The timeout ladder, on the row
# ---------------------------------------------------------------------------

class TestTimeoutsAreVisible:
    @pytest.mark.asyncio
    async def test_each_timed_out_attempt_names_itself_on_the_job_row(
        self, store, reachable
    ):
        job = store.add(filename="a-very-long-statement.pdf", kind="statement")

        async def _executor(_job):
            raise asyncio.TimeoutError()

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        messages: list[str] = []
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch.object(dq, "_llm_timeout_seconds", return_value=600):
            for _ in range(dq._TIMEOUT_WAIT_LIMIT):
                _rewind(store.get(job["id"]))
                await worker.tick()
                await asyncio.gather(
                    *list(worker._running.values()), return_exceptions=True
                )
                messages.append(store.get(job["id"])["error_message"])

        assert messages == [
            "Attempt 1 of 3 timed out after 600 s; retrying",
            "Attempt 2 of 3 timed out after 600 s; retrying",
            "Attempt 3 of 3 timed out after 600 s; retrying",
        ]
        # Still a wait, not a failure: the attempt counter is untouched.
        assert store.get(job["id"])["status"] == "waiting_for_model"
        assert store.get(job["id"])["attempts"] == 0

    @pytest.mark.asyncio
    async def test_the_last_word_says_why_it_stopped_waiting(self, store, reachable):
        job = store.add(kind="statement")

        async def _executor(_job):
            raise asyncio.TimeoutError()

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1), \
             patch.object(dq, "_llm_timeout_seconds", return_value=600):
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
        assert "Timed out 3 times after 600 s each" in row["error_message"]
        assert "treated as a failure" in row["error_message"]

    @pytest.mark.asyncio
    async def test_a_dead_model_keeps_its_own_message(self, store, reachable):
        """Only a timeout gets the ladder wording. "The local LLM server is unreachable" is a
        different sentence and must not be overwritten by a counter."""
        from core.llm_errors import LOCAL_LLM_UNREACHABLE_MESSAGE
        from core.llm_statement_parser import StatementLocalEndpointDown

        job = store.add()

        async def _executor(_job):
            raise StatementLocalEndpointDown("connection refused")

        worker = dq.DocumentQueueWorker(store=store, executor=_executor)
        with patch.object(dq, "current_concurrency", return_value=1):
            await worker.tick()
            await asyncio.gather(*list(worker._running.values()), return_exceptions=True)

        assert store.get(job["id"])["error_message"] == LOCAL_LLM_UNREACHABLE_MESSAGE

    def test_the_message_degrades_without_a_configured_timeout(self):
        with patch.object(dq, "_llm_timeout_seconds", return_value=None):
            assert dq._timeout_progress_message(2) == "Attempt 2 of 3 timed out; retrying"


# ---------------------------------------------------------------------------
# The worker comes back
# ---------------------------------------------------------------------------

class TestTheSupervisorRestartsTheLoop:
    @pytest.mark.asyncio
    async def test_a_loop_that_dies_is_put_back(self, store, reachable):
        worker = dq.DocumentQueueWorker(store=store, poll_seconds=0.01)
        crash = {"pending": True}
        real_tick = worker.tick

        async def _tick():
            if crash["pending"]:
                crash["pending"] = False
                # Not a tick failure (the loop catches those) - this stands in for
                # whatever ends the loop itself.
                raise BaseException("the loop's own await blew up")  # noqa: TRY002
            return await real_tick()

        with patch.object(worker, "tick", _tick), \
             patch.object(dq, "_RESTART_BACKOFF_SECONDS", (0.01, 0.01, 0.01, 0.01)):
            await worker.start()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if worker.restarts and worker.alive:
                    break
            assert worker.restarts >= 1, "the loop was never restarted"
            assert worker.alive is True
            await worker.stop()

    @pytest.mark.asyncio
    async def test_stopping_does_not_get_the_loop_restarted(self, store, reachable):
        worker = dq.DocumentQueueWorker(store=store, poll_seconds=0.01)
        await worker.start()
        assert worker.alive is True
        await worker.stop()
        await asyncio.sleep(0.05)
        assert worker.restarts == 0
        assert worker.alive is False

    @pytest.mark.asyncio
    async def test_alive_is_false_the_moment_the_loop_is_gone(self, store, reachable):
        """Without the supervisor in the way, `alive` has to be honest on its own."""
        worker = dq.DocumentQueueWorker(store=store, poll_seconds=0.01)
        await worker.start()
        # Take the supervisor out first, then the loop: the state the panel must
        # be able to describe.
        worker._supervisor_task.cancel()
        try:
            await worker._supervisor_task
        except asyncio.CancelledError:
            pass
        worker._loop_task.cancel()
        try:
            await worker._loop_task
        except asyncio.CancelledError:
            pass
        assert worker.alive is False
        await worker.stop()


class TestTheApiSaysWhetherTheWorkerIsAlive:
    @pytest.mark.asyncio
    async def test_worker_status_reports_alive_and_restarts(self):
        db = MagicMock()
        db.count_pending_processing_jobs_by_kind.return_value = {
            "receipt": 0, "statement": 0,
        }
        db.recent_processing_job_seconds.return_value = []

        fake = MagicMock()
        fake.alive = True
        fake.restarts = 2

        async def _probe(*, force: bool = False):
            return True

        with patch.object(dq, "get_worker", return_value=fake), \
             patch.object(dq, "probe_model_reachable", _probe):
            status = await dq.worker_status(db)

        assert status["alive"] is True
        assert status["restarts"] == 2

    @pytest.mark.asyncio
    async def test_no_worker_at_all_is_reported_as_not_alive(self):
        """A process that never started one drains nothing either. The browser
        needs the honest answer, not a reassuring one."""
        db = MagicMock()
        db.count_pending_processing_jobs_by_kind.return_value = {
            "receipt": 3, "statement": 1,
        }
        db.recent_processing_job_seconds.return_value = []

        async def _probe(*, force: bool = False):
            return True

        with patch.object(dq, "get_worker", return_value=None), \
             patch.object(dq, "probe_model_reachable", _probe):
            status = await dq.worker_status(db)

        assert status["alive"] is False
        assert status["restarts"] == 0

    def test_the_sse_snapshot_carries_the_same_field(self, store):
        """``jobs_summary`` and ``GET /api/jobs`` must not disagree about it."""
        fake = MagicMock()
        fake.alive = False
        fake.restarts = 4
        with patch.object(dq, "get_worker", return_value=fake), \
             patch.object(dq, "model_reachable_cached", return_value=True):
            snapshot = dq.worker_snapshot(store.add()["user_id"], store=store)

        assert snapshot["alive"] is False
        assert snapshot["restarts"] == 4

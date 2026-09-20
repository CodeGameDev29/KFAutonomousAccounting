"""Progress reporters — one engine, different sinks.

Every phase in the agent calls ``progress.log(...)`` to stream a line to
whatever UI is hosting the run. The reconciliation pipeline passes an
adapter that maps into ``_update_progress``; the Recategorize endpoint
passes an adapter that writes into ``_recategorize_progress``.
"""

from __future__ import annotations

from typing import Callable, Protocol


class ProgressReporter(Protocol):
    def log(self, phase: str, percent: int, message: str) -> None: ...
    def set_total(self, total: int) -> None: ...
    def set_done(self, done: int) -> None: ...


class NullProgressReporter:
    """Used by tests and by callers that don't care about progress."""

    def __init__(self) -> None:
        self.logs: list[tuple[str, int, str]] = []

    def log(self, phase: str, percent: int, message: str) -> None:
        self.logs.append((phase, percent, message))

    def set_total(self, total: int) -> None:
        pass

    def set_done(self, done: int) -> None:
        pass


class RecategorizeProgressReporter:
    """Writes to ``server.api.transactions._recategorize_progress`` via setter.

    The progress dict shape includes ``phase``, ``percent``, ``logs``,
    ``total``, and ``done`` — the frontend polls it every 1.5s through
    ``GET /api/transactions/recategorize-all/progress``.
    """

    def __init__(self, setter: Callable[..., None], user_id: str) -> None:
        self._setter = setter
        self._user_id = user_id
        self._logs: list[str] = []

    def log(self, phase: str, percent: int, message: str) -> None:
        self._logs.append(message)
        if len(self._logs) > 200:
            self._logs = self._logs[-200:]
        self._setter(
            self._user_id,
            phase=phase,
            percent=percent,
            logs=list(self._logs),
        )

    def set_total(self, total: int) -> None:
        self._setter(self._user_id, total=total)

    def set_done(self, done: int) -> None:
        self._setter(self._user_id, done=done)


class ReconciliationProgressReporter:
    """Writes to ``server.api.reconciliation._update_progress``.

    The reconciliation progress bar gives the agent a small percentage
    span (default 90%..95%) within the larger reconciliation timeline.
    ``percent`` passed into ``log`` is the agent's internal 0..100, which
    is mapped into that reserved slice.
    """

    def __init__(self, user_id: str, base_percent: int = 90, percent_span: int = 5) -> None:
        self._user_id = user_id
        self._base = base_percent
        self._span = percent_span

    def log(self, phase: str, percent: int, message: str) -> None:
        from server.api.reconciliation import _update_progress

        clamped = max(0, min(100, percent))
        mapped = self._base + int(clamped / 100 * self._span)
        _update_progress(
            self._user_id,
            phase,
            percent=mapped,
            log=message,
        )

    def set_total(self, total: int) -> None:
        pass

    def set_done(self, done: int) -> None:
        pass

"""Safe-execution helper: a logged alternative to `try/except Exception: pass`.

A bare pass:

    try:
        db.upsert_connection_health("wise", "HEALTHY")
    except Exception:
        pass

silently swallows every error — including audit-log failures, connection-pool
exhaustion, and schema drift that someone needs to see. Use `safe_call` or the
`safe` context manager instead so the failure at least reaches the log.

Typical usage:

    from server.safe_exec import safe_call, safe

    safe_call("wise.health_upsert", db.upsert_connection_health, "wise", "HEALTHY")

    with safe("gather.wise_sync"):
        wise_client.sync_transactions()

Both variants catch `Exception`, log at WARNING with `exc_info` so the stack
trace is captured, and return `None` / continue execution. Neither catches
`BaseException` (KeyboardInterrupt / SystemExit), so the process can still be
interrupted. The `label` is mandatory — it is what identifies the call site in
the log, so pick something searchable (e.g. `gather.wise_sync`).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

_logger = logging.getLogger("server.safe_exec")

_T = TypeVar("_T")


def safe_call(
    label: str,
    fn: Callable[..., _T],
    /,
    *args: Any,
    default: _T | None = None,
    **kwargs: Any,
) -> _T | None:
    """Invoke `fn(*args, **kwargs)`; return `default` on Exception and log."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        _logger.warning("safe_call(%s) suppressed %s: %s", label, type(e).__name__, e,
                        exc_info=True)
        return default


@contextmanager
def safe(label: str) -> Iterator[None]:
    """Context-manager form. Use when you have multi-statement cleanup to protect."""
    try:
        yield
    except Exception as e:
        _logger.warning("safe(%s) suppressed %s: %s", label, type(e).__name__, e,
                        exc_info=True)

"""The slowapi limiter, owned by a module no router has to import the app for.

Why this module exists
----------------------
A route module needs the limiter **while its own routes are being defined** —
``server/local_auth.py`` needs it inside ``_rate_limit()``, which runs at
decoration time, and the routers under ``server/api/`` need it at module scope.
Reaching for it through ``server.app`` would mean importing a half-initialised
module whenever a router is imported first, and the ``except Exception`` such
code needs then hands back a no-op decorator: rate limiting silently off, or —
depending on which module wins the race — a router that finishes importing with
no routes on it at all, every path under it answering 405 with nothing in the
log to say why.

A limiter has no business knowing about the application object, so it does not:
it is created here, ``server/app.py`` imports it from here to register the 429
handler, and every route module imports :func:`rate_limit` from here. Nothing in
this module imports ``server.app``, ``server.api`` or anything that does, which
is the property that makes import order stop mattering.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Per-IP: a 60/minute default, with tighter per-route limits declared at the
# routes themselves.
DEFAULT_LIMITS = ["60/minute"]

limiter: Any | None
RateLimitExceeded: type[Exception] | None

try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded as _RateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address, default_limits=DEFAULT_LIMITS)
    RateLimitExceeded = _RateLimitExceeded
except ImportError:  # pragma: no cover - slowapi is a hard dependency in cloud mode
    limiter = None
    RateLimitExceeded = None
    logger.warning("slowapi not installed - rate limiting disabled")


def rate_limit(spec: str) -> Callable[[F], F]:
    """Return slowapi's ``@limit(spec)``, or a no-op when slowapi is missing.

    Always per-IP (the limiter's default key function). Safe to use as a
    decorator at module import time from anywhere: this module has no
    application-level imports, so there is no ordering in which it can fail and
    silently drop the limit.
    """
    if limiter is None:
        def _noop(fn: F) -> F:
            return fn
        return _noop
    return limiter.limit(spec)

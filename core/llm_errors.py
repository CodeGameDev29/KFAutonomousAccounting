"""One vocabulary for "the local model did not answer".

The primary extraction provider is a self-hosted OpenAI-compatible endpoint,
configured with `LLM_BASE_URL`. When that server is off, asleep, or off the
network, the failure arrives as an openai-SDK `APIConnectionError` /
`APITimeoutError`, an `httpx.ConnectError`, a bare `ConnectionRefusedError`, or
a DNS `socket.gaierror` on whatever host name `LLM_BASE_URL` points at — none
of which mean anything to someone watching an upload fail.

Everything that talks to the local endpoint funnels those into
`LocalLLMUnreachableError` so the API layer can answer 503 with a sentence that
says what happened and what to do about it.

There is deliberately no automatic fallback here: Gemini and OpenAI engage only
when their key is non-empty. An unreachable local endpoint is reported as an
outage — the honest answer is "nothing was extracted", not a silent switch to
another provider.
"""

from __future__ import annotations

import socket

LOCAL_LLM_UNREACHABLE_MESSAGE = (
    "The local extraction model is not reachable. Nothing was extracted; "
    "try again once it is back."
)

LOCAL_LLM_UNREACHABLE_CODE = "LOCAL_MODEL_UNREACHABLE"

# Substrings that only ever show up when a socket could not be opened. Needed
# because some layers re-raise a connection failure as a plain RuntimeError
# carrying only the text (the sync ladder, thread hand-offs, and any call site
# that raises its own RuntimeError instead of propagating the original).
_CONNECTION_PHRASES = (
    "connection error",
    "connection refused",
    "connection aborted",
    "connection reset",
    "failed to establish a new connection",
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "no route to host",
    "network is unreachable",
    "actively refused",
)


class LocalLLMUnreachableError(RuntimeError):
    """The local LLM endpoint could not be reached at all.

    Distinct from "the model answered badly" and from "the model was slow":
    nothing was sent, nothing was extracted, and retrying once the server is back
    is the entire remedy.
    """

    def __init__(self, message: str = LOCAL_LLM_UNREACHABLE_MESSAGE) -> None:
        super().__init__(message)


def _is_connection_type(exc: BaseException) -> bool:
    """True when this one exception object is a transport-level failure."""
    if isinstance(exc, LocalLLMUnreachableError):
        return True
    # ConnectionRefusedError/ResetError/AbortedError are all ConnectionError;
    # gaierror is the `LLM_BASE_URL` host name failing to resolve.
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return True

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        pass
    else:
        # ConnectError = refused/unroutable, ConnectTimeout = never answered the
        # handshake. A ReadTimeout is deliberately NOT here: the endpoint did
        # answer, it was just slow, which is the separate 504 TIMEOUT case.
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True

    try:
        from openai import APIConnectionError
    except ImportError:  # pragma: no cover - openai is a hard dependency
        pass
    else:
        # APITimeoutError subclasses APIConnectionError in the openai SDK, so
        # this covers both of the cases a dead endpoint produces.
        if isinstance(exc, APIConnectionError):
            return True

    return False


def is_local_endpoint_down(exc: BaseException | None) -> bool:
    """True when `exc` (or anything it wraps) is the local endpoint not answering.

    Walks `__cause__`/`__context__` because the openai client wraps the
    underlying httpx error, and because several call sites re-raise with
    `from exc`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _is_connection_type(current):
            return True
        text = str(current).lower()
        if any(phrase in text for phrase in _CONNECTION_PHRASES):
            return True
        current = current.__cause__ or current.__context__
    return False


def as_local_unreachable(exc: BaseException) -> LocalLLMUnreachableError:
    """Build the user-facing error for `exc`, keeping the original as the cause."""
    err = LocalLLMUnreachableError()
    err.__cause__ = exc
    return err

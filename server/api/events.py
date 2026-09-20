"""Server-Sent Events (SSE) for real-time updates.

SSE rather than WebSocket: it is simpler, survives an intermediary that only
understands HTTP, and server->client push is all this application needs. Each
authenticated user gets their own event queue.
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from server import tickets
from server.auth import AuthUser, _verify_token, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

# Per-user event queues. Key is user_id, value is set of asyncio.Queue.
# A user can have multiple browser tabs, each with its own queue.
_user_queues: dict[str, set[asyncio.Queue]] = {}

# How long the stream may sit silent before it emits a keepalive. Short enough
# to stay under the idle timer of an intermediary between the browser and this
# process, which would otherwise reset a quiet connection.
_HEARTBEAT_SECONDS = 20.0


def send_event(user_id: str, event_type: str, data: dict | None = None) -> None:
    """Send an SSE event to all connected clients for a user.

    Call this from anywhere in the backend to push updates:
        send_event(user.id, "receipts_updated", {"count": 5})
        send_event(user.id, "reconciliation_complete", {"matched": 42})
        send_event(user.id, "transactions_updated")
    """
    queues = _user_queues.get(user_id, set())
    if not queues:
        return

    event = {
        "type": event_type,
        "data": data or {},
        "timestamp": time.time(),
    }

    dead_queues = set()
    for queue in queues:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            dead_queues.add(queue)

    # Clean up dead queues
    for q in dead_queues:
        queues.discard(q)


async def _event_generator(
    user_id: str, queue: asyncio.Queue, request: Request
) -> AsyncGenerator[str, None]:
    """Generate SSE events for a single client connection."""
    try:
        # Send initial connection event
        yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                event_type = event.get("type", "message")
                event_data = json.dumps(event.get("data", {}))
                yield f"event: {event_type}\ndata: {event_data}\n\n"
            except asyncio.TimeoutError:
                # Keepalive. A comment line plus the ping event: the comment
                # is the cheapest thing that puts bytes on the wire for any
                # proxy counting idle time, and the event is what the browser
                # client treats as proof the connection is still healthy.
                yield ": keepalive\n\n"
                yield f"event: ping\ndata: {json.dumps({'ts': time.time()})}\n\n"
    finally:
        # Clean up queue on disconnect
        queues = _user_queues.get(user_id, set())
        queues.discard(queue)
        if not queues and user_id in _user_queues:
            del _user_queues[user_id]
        logger.info("SSE client disconnected for user %s", user_id[:8])


async def _get_user_from_header_ticket_or_token(
    request: Request,
    ticket: str | None = Query(None, alias="ticket"),
    token: str | None = Query(None, alias="token"),
) -> AuthUser:
    """Extract user from (in order): Authorization header, single-use ticket,
    or legacy ?token= query param.

    The ticket path is preferred for EventSource because a ticket grants no
    privilege beyond the single SSE connection. The ?token= path is deprecated
    and kept only so a tab opened before the ticket existed does not break.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    # 1. Authorization header (preferred when available)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=auth_header[7:]
        )
        return await get_current_user(creds)

    # 2. Single-use ticket (preferred for EventSource / downloads)
    if ticket:
        user_id = tickets.consume(ticket, "sse")
        if user_id:
            return AuthUser(id=user_id, email=None, role="authenticated")
        raise HTTPException(status_code=401, detail="Invalid or expired ticket")

    # 3. Deprecated ?token= path
    if token:
        logger.warning("SSE connect used legacy ?token= query param; migrate to /api/events/ticket")
        try:
            payload = _verify_token(token)
            user_id = payload.get("sub")
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid token")
            return AuthUser(
                id=user_id,
                email=payload.get("email"),
                role=payload.get("role", "authenticated"),
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    raise HTTPException(status_code=401, detail="Missing authorization")


@router.post("/ticket")
async def mint_sse_ticket(user: AuthUser = Depends(get_current_user)):
    """Mint a 60-second single-use ticket for connecting to /api/events/stream.

    Client flow:
        POST /api/events/ticket  (Authorization: Bearer <JWT>)
        → { "ticket": "abc…", "ttl_seconds": 60 }
        new EventSource(`/api/events/stream?ticket=abc…`)
    """
    token = tickets.issue(user.id, "sse", ttl_seconds=60)
    return {"ticket": token, "ttl_seconds": 60}


@router.get("/stream")
async def event_stream(
    request: Request,
    user: AuthUser = Depends(_get_user_from_header_ticket_or_token),
):
    """SSE endpoint -- connect to receive real-time updates.

    Events:
    - connected: Initial connection confirmation
    - ping: Keepalive (see ``_HEARTBEAT_SECONDS``)
    - receipts_updated: New receipt uploaded or processed
    - transactions_updated: New bank transactions imported or status changed
    - reconciliation_complete: Reconciliation run finished
    - export_ready: PDF/ZIP export is ready for download
    - sync_progress: Progress update during bank/email sync
    - proofs_updated: Proof added/removed/approved/rejected for a transaction
    - statement_parse_started: LLM statement parsing has begun
      payload: {source_file: str}
    - statement_parse_complete: LLM statement parsing finished successfully
      payload: {source_file, imported, duplicates, batch_confidence,
                balance_matches, flagged_row_count, parse_method, statement_id}
    - statement_parse_error: LLM statement parsing failed unrecoverably
      payload: {source_file: str, error: str, retry_allowed: bool}
    - job_updated: one document-queue job changed state. Fires
      on queued → running → done/failed, on every retry and every
      waiting_for_model park, and on page progress inside a long document.
      payload: the whole job —
        {job_id, id, kind: "receipt"|"statement", filename, status, position,
         ahead, attempts, max_attempts, pages_total, pages_done, error_code,
         error_message, result, next_attempt_at, created_at, started_at,
         finished_at, updated_at}
      `status` is one of queued | running | waiting_for_model | done | failed |
      cancelled. `position` is the submission sequence for this user and never
      changes; `ahead` is queue depth — how many of this user's uploads are
      still queued or waiting in front of this one (0 = next, null = no longer
      waiting) — and it is the number the UI shows. `result` carries
      document_id / statement_id and a `summary` block once the job finishes.
    - jobs_summary: that user's queue counts plus the worker block, sent
      alongside every job_updated so a header does not need to re-read the list
      and so the ETA and the "model unreachable" banner change on the event
      rather than on the next poll.
      payload: {queued, running, waiting_for_model, done, failed, cancelled,
                worker: {concurrency, model_reachable, avg_seconds_receipt,
                         avg_seconds_statement, eta_seconds, pending}}

    A note on the older statement events: they are still emitted, by the same
    code, for the same reasons. `job_updated` is the general shape (it covers
    receipts too); `statement_parsed` / `statement_failed` /
    `statement_parse_started` remain the statement-specific ones the
    transactions page listens for.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    # Register queue for this user
    if user.id not in _user_queues:
        _user_queues[user.id] = set()
    _user_queues[user.id].add(queue)

    logger.info("SSE client connected for user %s", user.id[:8])

    return StreamingResponse(
        _event_generator(user.id, queue, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )

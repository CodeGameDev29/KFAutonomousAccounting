"""FastAPI application server.

REST API + SSE + local JWT auth + local PostgreSQL, all in one process.

Usage:
    uvicorn server.app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before anything reads os.environ. override=True so that an
# explicitly empty KEY= in .env switches a provider off even when the shell
# that launched the server exports one; see config/settings.py.
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

# There is no error-tracking integration: the server's log file is the record
# of what went wrong.

# Force UTF-8 on the log streams before anything is configured to write to
# them. When stdout/stderr are redirected into files, a Windows Python defaults
# those streams to the ANSI codepage with errors="backslashreplace", so
# non-ASCII characters land mangled in a file whose every other line is UTF-8.
# PYTHONUTF8=1 in the launcher scripts covers the launcher path; this covers
# every other way the app gets started.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("aa-server")


# ── Scrub JWTs from access logs (CWE-532) ──────────────────────────────
# SSE and file-download endpoints accept ``?token=<JWT>`` as a fallback for
# clients that can't set Authorization headers. Without this filter the raw
# JWT ends up in uvicorn.access logs (and anything shipping them to log
# aggregators), giving an attacker with log access a usable credential.
import re as _re

_TOKEN_QS_RE = _re.compile(r"([?&]token=)[^&\s\"']+", _re.IGNORECASE)
_BEARER_RE = _re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", _re.IGNORECASE)


def _redact_str(s: str) -> str:
    s = _TOKEN_QS_RE.sub(r"\1REDACTED", s)
    s = _BEARER_RE.sub(r"\1REDACTED", s)
    return s


class _RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                record.args = tuple(
                    _redact_str(a) if isinstance(a, str) else a
                    for a in record.args
                )
            if isinstance(record.msg, str) and ("token=" in record.msg or "Bearer " in record.msg):
                record.msg = _redact_str(record.msg)
        except Exception:
            pass
        return True


_redact_filter = _RedactTokenFilter()
# Attach to every uvicorn logger (access, error, base) plus the application's
# own logger so JWTs cannot leak through error tracebacks or child loggers.
for _name in ("uvicorn.access", "uvicorn.error", "uvicorn", "aa-server"):
    logging.getLogger(_name).addFilter(_redact_filter)


DEPLOY_MODE = "cloud"


# ── Lifespan: startup / graceful shutdown ──────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("Server starting (mode=%s)", DEPLOY_MODE)

    # Storage is a directory on the server's own disk — refuse to start if it
    # is not writable, rather than failing on the first receipt upload.
    if DEPLOY_MODE == "cloud":
        try:
            from core.file_storage import validate_storage_connection
            validate_storage_connection()
        except Exception as e:
            logger.critical("Local storage validation failed: %s", e)
            raise RuntimeError(f"Cannot start server without writable storage: {e}")

    # Auth is local and symmetric — there is no JWKS document to pre-fetch any
    # more. Verify the signing secret is present now so a misconfiguration
    # surfaces at boot rather than as a 503 on the first sign-in.
    if not os.environ.get("AUTH_JWT_SECRET"):
        raise RuntimeError(
            "AUTH_JWT_SECRET is not set — no session could be issued or verified. "
            "See .env.example."
        )

    # Rewrite any single-bucket 'wise_api' source_file into the per-month form
    # the rest of the code expects. Idempotent, and non-fatal if it fails.
    try:
        from db.connection import get_pool
        pool = get_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE transactions
                    SET source_file = 'wise_' || currency || '_'
                        || SUBSTRING(date_posted FROM 1 FOR 4)
                        || SUBSTRING(date_posted FROM 6 FOR 2)
                    WHERE source_file = 'wise_api' AND account = 'Wise'
                """)
                if cur.rowcount > 0:
                    logger.info(
                        "Migrated %d wise_api transactions to per-month source_files",
                        cur.rowcount,
                    )
            conn.commit()
        finally:
            pool.putconn(conn)
    except Exception as e:
        logger.warning("Wise migration check failed (non-fatal): %s", e)

    # ── The document queue ───────────────────────────────────────────────
    # Every uploaded receipt and statement is processed by the worker below, not
    # by the request that carried the file. A restart kills the asyncio tasks but
    # not the rows, so recovery comes first: anything left 'running' goes back to
    # 'queued' with its position intact, and only then is a statement still
    # sitting in 'processing' with no live job declared dead. Order matters —
    # a queued statement is waiting its turn, not stalled.
    queue_worker = None
    try:
        from server.jobs.document_queue import (
            fail_orphaned_processing_statements,
            requeue_running_jobs,
            start_worker,
        )
        requeue_running_jobs()
        fail_orphaned_processing_statements()
        queue_worker = await start_worker()
    except Exception as e:
        # Uploads would still be accepted and queued, but nothing would drain
        # them — that is worth an ERROR rather than a shrug.
        logger.error("Document queue worker failed to start: %s", e, exc_info=True)

    # A transaction's status is derived from the rows that prove it. Anything
    # that disagrees — MATCHED with no match row (and so no receipt, while
    # still counting as reconciled and being invisible to the matcher), or a
    # receipt released while it still proves something — is corrected and
    # logged here, once per boot.
    try:
        from server.jobs.status_repair import repair_status_drift
        repair_status_drift()
    except Exception as e:
        logger.warning("Status drift repair failed (non-fatal): %s", e)

    yield
    logger.info("Server shutting down — closing connections")
    if queue_worker is not None:
        # Hands anything in flight back to the queue as 'queued', so the restart
        # this shutdown is half of picks it straight back up.
        try:
            from server.jobs.document_queue import stop_worker
            await stop_worker()
        except Exception:
            logger.warning("Document queue worker shutdown failed", exc_info=True)
    try:
        from db.connection import close_pool
        close_pool()
    except Exception as e:
        logger.warning("Pool cleanup error: %s", e)


app = FastAPI(
    title="Autonomous Accounting API",
    version="0.1.0",
    description="Self-hosted bookkeeping for Canadian small businesses",
    lifespan=lifespan,
    docs_url="/docs" if DEPLOY_MODE != "cloud" else None,
    redoc_url="/redoc" if DEPLOY_MODE != "cloud" else None,
    openapi_url="/openapi.json" if DEPLOY_MODE != "cloud" else None,
)

# ── Rate limiting ──────────────────────────────────────────────────────
# The limiter itself lives in ``server/rate_limit.py`` and knows nothing about
# this module. Route modules import ``rate_limit`` from there, so a router can be
# imported before the app without dragging the app in mid-definition — which
# would leave the limit silently off, or the router with no routes at all.
from server.rate_limit import RateLimitExceeded, limiter  # noqa: E402

if limiter is not None:
    from fastapi.responses import JSONResponse as _JSONResponse

    app.state.limiter = limiter

    async def _rate_limit_exceeded_handler(request: Request, exc: Exception):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Rate limit exceeded: %s %s from %s",
            request.method,
            request.url.path,
            client_ip,
        )
        return _JSONResponse(
            status_code=429,
            content={"detail": "Too many requests — slow down"},
        )

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
elif DEPLOY_MODE == "cloud":
    raise RuntimeError("slowapi is required in cloud mode — install it: pip install slowapi")

# CORS — permissive in dev, restrict in production
_localhost_origins = "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://localhost:8080"
_env_origins = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [
    o for o in f"{_env_origins},{_localhost_origins}".split(",") if o
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


# ── Global exception handler — prevent stack trace leaks (CWE-209) ─────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Security headers middleware ─────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if DEPLOY_MODE == "cloud":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'sha256-1KZB4MJXKxVVIX+ulI4wQIRAeeFN5VEXvYi0L4ngRU4=' https://accounts.google.com; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data: blob: https://*.googleusercontent.com; "
            "connect-src 'self' https://accounts.google.com; "
            "frame-src 'self' blob:; "
            "frame-ancestors 'none'"
        )
    return response


# Cached reachability of the local extraction endpoint. /health is polled by the
# launcher scripts, by any reverse proxy in front of the app and by whatever
# monitoring is pointed at it, and a 2s probe on every call would add 2s to each
# one whenever the endpoint is down — which is exactly when health gets polled
# most. Cached 60s, keyed on the URL so an .env change during a restart cannot
# serve a stale verdict for the wrong host.
_LOCAL_LLM_PROBE_TTL_S = 60.0
_local_llm_probe: dict = {"at": 0.0, "url": None, "reachable": False}


async def _local_llm_reachable() -> bool:
    """True when the local OpenAI-compatible endpoint answers GET /models."""
    import time

    from config.settings import local_llm_config

    if not local_llm_config.enabled:
        return False

    url = local_llm_config.base_url.rstrip("/") + "/models"
    now = time.monotonic()
    if (
        _local_llm_probe["url"] == url
        and now - _local_llm_probe["at"] < _LOCAL_LLM_PROBE_TTL_S
    ):
        return bool(_local_llm_probe["reachable"])

    reachable = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        # Any answer at all means something is serving; a 4xx would still mean
        # the process is alive, which is what this flag is for.
        reachable = resp.status_code < 500
    except Exception:
        reachable = False

    _local_llm_probe.update({"at": now, "url": url, "reachable": reachable})
    return reachable


@app.get("/health")
async def health():
    result = {"status": "ok"}

    # ── The numbers a client has to agree with the server about ──────────
    # Read from the live settings on every call and built BEFORE the probes
    # below, which swallow their exceptions: a degraded instance — no database,
    # no extraction endpoint — still has to report the thresholds and limits it
    # will enforce, because the UI mirrors them (it shows the bulk-approve bar,
    # it rejects an over-sized file before uploading it). Hard-coding any of
    # these in the bundle is how the client and the server drift apart.
    from config.settings import MAX_UPLOAD_MB, reconciliation_config

    result["reconciliation"] = {
        "auto_approve_threshold": float(reconciliation_config.auto_approve_threshold),
        "review_threshold": float(reconciliation_config.review_threshold),
        "bulk_approve_min_confidence": float(
            reconciliation_config.bulk_approve_min_confidence
        ),
        # The fallback date window the matcher uses for an account that has not
        # set its own max_date_variance_days.
        "default_date_window_days": int(reconciliation_config.date_window_days),
    }
    result["limits"] = {"max_upload_mb": int(MAX_UPLOAD_MB)}

    if DEPLOY_MODE == "cloud":
        try:
            from db.connection import get_pool
            pool = get_pool()
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.commit()
            finally:
                pool.putconn(conn)
        except Exception:
            result["status"] = "degraded"
        # Check local storage is present and writable
        try:
            from core.file_storage import validate_storage_connection
            validate_storage_connection()
            result["storage"] = "ok"
        except Exception as e:
            result["storage"] = f"error: {str(e)[:50]}"
            result["status"] = "degraded"
        # Which extraction model this server is actually configured to use.
        # Neither value is a secret, and without them a primary-provider
        # outage is indistinguishable from a silent fallback: uploads keep
        # succeeding on the secondary provider at lower accuracy, with
        # nothing in the response or the DB to say so.
        try:
            from config.settings import (
                extraction_provider_ladder,
                gemini_config,
                local_llm_config,
                openai_config,
            )
            provider_ladder = extraction_provider_ladder()
            result["extraction"] = {
                # Which provider an upload would actually reach first. None
                # means nothing is configured and uploads fail extraction
                # outright rather than degrading quietly.
                "primary": provider_ladder[0] if provider_ladder else None,
                "primary_model": (
                    local_llm_config.model if provider_ladder and provider_ladder[0] == "local"
                    else (gemini_config.model if gemini_config.enabled else openai_config.model)
                ),
                "local": {
                    "enabled": local_llm_config.enabled,
                    "base_url": local_llm_config.base_url,
                    "model": local_llm_config.model,
                    "reachable": await _local_llm_reachable(),
                },
                "gemini_key_present": bool(gemini_config.api_key),
                "openai_key_present": bool(openai_config.api_key),
                # Kept for backwards compatibility with clients reading the
                # older shape: primary_key_present is the Gemini key, ladder the
                # Gemini model chain, fallback the OpenAI model.
                "primary_key_present": bool(gemini_config.api_key),
                "ladder": provider_ladder,
                "gemini_ladder": gemini_config.model_candidates,
                "fallback": openai_config.model,
            }
        except Exception:
            pass
        # Whether this server will accept a new account, so the sign-up page
        # can read the answer instead of carrying a build-time constant.
        # Derived from AUTH_ALLOW_SIGNUP, the same value the endpoint itself
        # enforces.
        try:
            from server.api.auth_validation import signup_enabled
            result["signup_enabled"] = signup_enabled()
        except Exception:
            pass
    return result


# ── Web mode: REST API + SSE ────────────────────────────────────────

if DEPLOY_MODE == "cloud":
    logger.info("Starting in WEB mode (REST + SSE + local JWT auth, self-hosted)")

    # Core REST API (all authenticated endpoints)
    from server.api import api_router
    app.include_router(api_router)

    # Local identity service — sign-in, refresh, logout, confirm.
    # Without it nobody can obtain a session, so an import failure here is fatal
    # rather than a warning like the optional routers below.
    from server.local_auth import router as local_auth_router
    app.include_router(local_auth_router)

    # Sign in with Google, talking to Google directly. An account created that
    # way may have no password at all, so for that account this is not a
    # convenience — it is its only door.
    from server.google_auth import router as google_auth_router
    app.include_router(google_auth_router)

    # Public email validation endpoint (no auth, rate-limited)
    try:
        from server.api.auth_validation import router as auth_validation_router
        app.include_router(auth_validation_router)
    except ImportError:
        logger.warning("Auth validation endpoint module not available")

    # SSE events endpoint
    try:
        from server.api.events import router as events_router
        app.include_router(events_router)
    except ImportError:
        logger.warning("SSE events module not available")

    # File upload/download (local disk, HMAC-signed URLs)
    from server.api.files import router as files_router
    app.include_router(files_router)

    # Shared reports (public, no auth)
    try:
        from server.api.shared import router as shared_router
        app.include_router(shared_router)
    except ImportError:
        logger.warning("Shared reports module not available")

    # Serve React static build in production
    STATIC_DIR = PROJECT_ROOT / "web" / "dist"
    if STATIC_DIR.is_dir():
        _STATIC_ROOT = STATIC_DIR.resolve()
        # Serve static files
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

        # SPA fallback: serve index.html for all non-API routes
        from fastapi import HTTPException
        from fastapi.responses import FileResponse

        _INDEX_HTML = (STATIC_DIR / "index.html").resolve()
        # The entry HTML must ALWAYS be revalidated. Without this, browsers apply
        # heuristic caching (no cache-control plus a last-modified) and keep
        # serving a stale index.html that still references the previous hashed JS
        # bundle after a rebuild. "no-cache" (revalidate, allow 304) keeps the
        # entry fresh while the content-hashed /assets/* stay long-cached, which
        # is safe because their hash changes whenever their content does.
        _HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

        @app.get("/{path:path}")
        async def spa_fallback(path: str):
            # Don't intercept API routes
            if path.startswith("api/") or path == "health":
                raise HTTPException(status_code=404)
            # Resolve and confine to STATIC_DIR — reject any .. traversal
            try:
                candidate = (STATIC_DIR / path).resolve()
                if (
                    candidate.is_file()
                    and candidate.is_relative_to(_STATIC_ROOT)
                    and candidate != _INDEX_HTML
                ):
                    return FileResponse(str(candidate))
            except (OSError, ValueError):
                pass
            # Otherwise serve index.html (SPA routing) — never heuristically cached.
            return FileResponse(str(_INDEX_HTML), headers=_HTML_NO_CACHE)

if __name__ == "__main__":
    from config.settings import SERVER_PORT
    port = int(os.environ.get("PORT", str(SERVER_PORT)))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

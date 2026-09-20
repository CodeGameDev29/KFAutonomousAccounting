"""REST API client for the E2E suites.

Handles authentication, request/response, and provides typed helpers for every
API domain. Auth is the app's own local identity service on the same origin
(`/api/auth/*`) — there is no external provider.

Registration is closed by default (`AUTH_ALLOW_SIGNUP=0`), so the suites sign in
to an existing throwaway account instead of creating users:

    AA_E2E_BASE_URL   where the app is (default http://localhost:8080)
    AA_E2E_EMAIL      the throwaway test account
    AA_E2E_PASSWORD   its password

The account must hold synthetic data only: the suites upload, mutate and
**delete everything** in it. A second account is created only when the server
itself reports that registration is open; see `signup_open()`.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# <repo>/.claude/skills/e2e-test/lib/api_client.py -> the repo root is parents[4].
# Off by one here and the `.env` fallback below silently never finds a file.
PROJECT_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_BASE_URL = "http://localhost:8080"

# Second accounts (signup and isolation checks) live under a reserved
# documentation domain, so no address a test invents can belong to anybody.
THROWAWAY_EMAIL_DOMAIN = "example.com"


def env_value(key: str, default: str | None = None) -> str | None:
    """Read a setting from the process env, falling back to the project `.env`."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    found = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return found or default
    except Exception:
        pass
    return default


def default_base_url() -> str:
    """`AA_E2E_BASE_URL`, or the port the README's quickstart starts the app on."""
    return (env_value("AA_E2E_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


# ── Timeouts, sized for a local model rather than guessed ─────────────────
# Ordinary reads land in under a second. Receipt extraction on a local vision
# model takes tens of seconds per file, and a reconciliation run's LLM pass
# takes minutes per batch — so nothing here holds one request open for a whole
# run; the long operations are started and then polled.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=120.0)
UPLOAD_TIMEOUT = 300.0
# Ceiling for a whole reconciliation run, polled. Override with
# AA_E2E_RECONCILE_TIMEOUT (seconds) when the LLM endpoint is slower than this
# allows; the runner sets the same default and the two must agree.
RECONCILE_TIMEOUT = float(env_value("AA_E2E_RECONCILE_TIMEOUT") or 5400)
RECONCILE_POLL_SECONDS = 5.0
# A `/start` that answers `already_running` belongs to somebody else's run — a
# browser journey or another agent may be driving the same account. Wait for
# that run and try again, this many times, inside the same deadline.
RECONCILE_START_ATTEMPTS = 30

# The access token lives AUTH_ACCESS_TOKEN_TTL_SECONDS (one hour by default)
# and a reconciliation poll loop can outlive it, so the client refreshes at 45
# minutes rather than waiting to be told.
TOKEN_MAX_AGE_SECONDS = float(env_value("AA_E2E_TOKEN_MAX_AGE") or 45 * 60)

# Ceiling for one queued document job (a receipt's extraction), polled. The
# queue is FIFO across the whole install, so the wait is for the line, not the
# file.
JOB_TIMEOUT = float(env_value("AA_E2E_JOB_TIMEOUT") or 900)
JOB_POLL_SECONDS = 3.0
JOB_TERMINAL_STATUSES = ("done", "failed", "cancelled")


def test_account() -> tuple[str | None, str | None]:
    """The credentials every suite signs in with: `AA_E2E_EMAIL` / `AA_E2E_PASSWORD`.

    There is no default for either. A missing value is a precondition failure,
    not something to work around — the caller stops and says so rather than
    running against nothing.
    """
    return env_value("AA_E2E_EMAIL"), env_value("AA_E2E_PASSWORD")


def throwaway_credentials(purpose: str) -> tuple[str, str]:
    """A never-used address and a random password for a second account.

    The password is generated per call and satisfies the server's strength
    rules (upper, lower, digit, special, 8+); nothing about it is fixed.
    """
    email = f"e2e-{purpose}-{int(time.time())}-{secrets.token_hex(3)}@{THROWAWAY_EMAIL_DOMAIN}"
    return email, f"Aa1!{secrets.token_urlsafe(18)}"


def signup_open(base_url: str | None = None) -> bool:
    """Whether this install will create a new account right now.

    `GET /health` reports `signup_enabled` — the same value
    `POST /api/auth/signup` enforces — so this costs nothing and creates
    nothing. Any transport error, or an older server without the key, means
    "no": signup being shut is the normal state of an install and must never
    fail a run on its own.
    """
    url = (base_url or default_base_url()).rstrip("/")
    try:
        resp = httpx.get(f"{url}/health", timeout=15.0)
        return bool(resp.json().get("signup_enabled"))
    except Exception:
        return False


class APIClient:
    """Authenticated REST client for the web app."""

    def __init__(self, base_url: str | None = None, timeout: Any = None):
        if base_url is None:
            base_url = default_base_url()
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: str | None = None
        self.email: str | None = None
        # When the access token this client holds was minted, and how many times
        # it has been rotated. Both are read by `_request`, which keeps the
        # session alive underneath every call in the suites.
        self._token_minted_at: float | None = None
        self.refresh_count = 0
        self._client = httpx.Client(timeout=timeout or DEFAULT_TIMEOUT)

    def close(self):
        self._client.close()

    # ── Auth ──────────────────────────────────────────────────────────

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @property
    def auth_headers(self) -> dict[str, str]:
        """Authorization only — a multipart body sets its own Content-Type."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @staticmethod
    def _error_of(resp: httpx.Response) -> str:
        """Pull the message out of an error body.

        The local identity service answers with `{"error": "..."}`; FastAPI's own
        validation/dependency failures answer with `{"detail": ...}`.
        """
        try:
            data = resp.json()
        except Exception:
            return resp.text or f"HTTP {resp.status_code}"
        if isinstance(data, dict):
            return str(data.get("error") or data.get("detail") or data)
        return str(data)

    def _apply_session(self, session: dict, email: str) -> None:
        """Adopt a session object returned by /api/auth/{signup,token,refresh}."""
        self.token = session.get("access_token")
        self.refresh_token = session.get("refresh_token")
        self.user_id = (session.get("user") or {}).get("id")
        self.email = email
        self._token_minted_at = time.time() if self.token else None

    def signup(self, email: str, password: str) -> dict:
        """Create a new user via the app's own local auth service.

        Call this only after `signup_open()` returns True: registration is
        closed by default and the endpoint answers 403.

        `POST /api/auth/signup` returns 200 `{success, confirmed, email_sent,
        message, session?}`. With AUTH_AUTOCONFIRM=1 (the default) `session` is
        present and the account is usable immediately — no confirmation step,
        no inbox to wait on.
        """
        resp = self._client.post(
            f"{self.base_url}/api/auth/signup",
            json={"email": email, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code == 403:
            return {"error": "Registration is closed (AUTH_ALLOW_SIGNUP=0)", "signup_closed": True}
        if resp.status_code == 429:
            return {"error": "Signup is rate-limited (HTTP 429); retry later", "rate_limited": True}
        if resp.status_code >= 400:
            return {"error": self._error_of(resp)}

        data = resp.json()
        session = data.get("session")
        if session:
            self._apply_session(session, email)
            return data

        # A 200 with no session means one of two things and the server
        # deliberately will not say which: the address is already registered
        # (the response is shaped identically to a fresh signup so it cannot be
        # used to enumerate accounts), or AUTH_AUTOCONFIRM is off and the
        # account is waiting on a confirmation link. Logging in settles it.
        session = self.login(email, password)
        if "error" in session:
            return {"error": f"Signup returned no session and login failed: {session['error']}"}
        return {**data, "confirmed": True, "session": session}

    def login(self, email: str, password: str) -> dict:
        """Sign in via `POST /api/auth/token`. Returns the session, or {"error": ...}.

        The local service answers a bad password, an unknown address and a
        disabled account identically with 400 `{"error": "Invalid login
        credentials."}` — do not assert on which of the three it was.
        """
        resp = self._client.post(
            f"{self.base_url}/api/auth/token",
            json={"email": email, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return {"error": self._error_of(resp)}
        data = resp.json()
        self._apply_session(data, email)
        return data

    def refresh_session(self) -> dict:
        """Rotate the refresh token for a fresh access token."""
        if not self.refresh_token:
            return {"error": "No refresh token held"}
        resp = self._client.post(
            f"{self.base_url}/api/auth/refresh",
            json={"refresh_token": self.refresh_token},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return {"error": self._error_of(resp)}
        data = resp.json()
        self._apply_session(data, self.email or "")
        return data

    def logout(self):
        """Revoke this user's refresh tokens server-side, then drop local state.

        The server call is best-effort: local state is cleared either way so a
        caller can always fall back to an unauthenticated client.
        """
        if self.token:
            try:
                self._client.post(
                    f"{self.base_url}/api/auth/logout",
                    headers=self.headers,
                    json={"refresh_token": self.refresh_token or ""},
                    timeout=15.0,
                )
            except Exception:
                logger.debug("Server-side logout failed; clearing local session anyway")
        self.token = None
        self.refresh_token = None
        self.user_id = None
        self.email = None
        self._token_minted_at = None

    # ── Generic HTTP, with the session kept alive underneath ──────────

    def _token_is_stale(self) -> bool:
        """True once the held access token is older than TOKEN_MAX_AGE_SECONDS."""
        if not self.token or self._token_minted_at is None:
            return False
        return (time.time() - self._token_minted_at) > TOKEN_MAX_AGE_SECONDS

    def _try_refresh(self, why: str) -> bool:
        """Rotate for a fresh access token. False when there is nothing to rotate.

        Never raises: a client with no refresh token (an unauthenticated probe, a
        deliberately forged token) has to keep the answer the server gave it, or
        the auth tests would be re-authenticated out of their own subject.
        """
        if not self.refresh_token:
            return False
        result = self.refresh_session()
        if "error" in result:
            logger.warning("Token refresh (%s) failed: %s", why, result["error"])
            return False
        self.refresh_count += 1
        logger.info("Access token refreshed (%s) — refresh #%d", why, self.refresh_count)
        return True

    def _request(self, method: str, path: str, *, auth_only: bool = False,
                 **kwargs) -> httpx.Response:
        """Every API call in the suites goes through here.

        Two things happen that no individual test should have to think about:

        * **Proactive refresh.** The access token lives one hour and a
          reconciliation poll loop can run longer than that on a large account;
          without this the run loses its verdicts to `401 Invalid or expired
          token` an hour in. Older than 45 minutes and the token is rotated
          before the request leaves.
        * **Retry once on 401.** Anything the proactive window misses (a clock
          jump, a token minted by a previous client) is refreshed on the spot and
          the request is replayed. Safe on POST/PATCH: a 401 comes from the auth
          dependency, so the handler never ran.

        A client with no session at all is left alone — `_try_refresh` returns
        False and the 401 stands, which is what the auth and IDOR tests assert on.
        """
        url = f"{self.base_url}{path}"
        if self._token_is_stale():
            self._try_refresh(f"proactive, token older than {TOKEN_MAX_AGE_SECONDS / 60:.0f} min")

        headers = self.auth_headers if auth_only else self.headers
        resp = self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code != 401 or not self.token:
            return resp
        if not self._try_refresh(f"401 on {method} {path}"):
            return resp
        headers = self.auth_headers if auth_only else self.headers  # now carries the new token
        return self._client.request(method, url, headers=headers, **kwargs)

    def get(self, path: str, params: dict | None = None, **kwargs) -> httpx.Response:
        return self._request("GET", path, params=params, **kwargs)

    def post(self, path: str, json_data: Any = None, **kwargs) -> httpx.Response:
        return self._request("POST", path, json=json_data, **kwargs)

    def patch(self, path: str, json_data: Any = None, **kwargs) -> httpx.Response:
        return self._request("PATCH", path, json=json_data, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self._request("DELETE", path, **kwargs)

    def upload(self, path: str, file_path: Path, field: str = "file") -> httpx.Response:
        """Upload a file via multipart form.

        The bytes are read up front rather than streamed from an open handle:
        `_request` may replay the call after a token refresh, and a consumed file
        object replays as an empty upload.
        """
        payload = Path(file_path).read_bytes()
        return self._request(
            "POST", path, auth_only=True,
            files={field: (Path(file_path).name, payload)},
            timeout=UPLOAD_TIMEOUT,
        )

    @staticmethod
    def _json_ok(resp: httpx.Response, what: str) -> Any:
        """The JSON body of a call that had to have worked, or an AssertionError.

        A list helper that hands back `resp.json()` whatever the status was turns
        a dead session (`{"detail": "Invalid or expired token"}`) into *no
        matches, no documents, nothing to approve* — a page of verdicts that each
        claim the data was simply empty. "The call failed" and "there is nothing
        there" are different findings.
        """
        if not 200 <= resp.status_code < 300:
            raise AssertionError(f"{what}: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except Exception as exc:
            raise AssertionError(
                f"{what}: HTTP {resp.status_code} with a body that is not JSON ({exc}): "
                f"{resp.text[:200]}"
            ) from None

    # ── Domain helpers ────────────────────────────────────────────────

    def health(self) -> dict:
        return self._json_ok(self.get("/health"), "GET /health")

    def onboarding_status(self) -> dict:
        return self._json_ok(self.get("/api/onboarding/status"), "GET /api/onboarding/status")

    def save_profile(self, company_name: str, fiscal_year: int = 2026,
                     currency: str = "CAD") -> dict:
        resp = self.post("/api/onboarding/profile", {
            "company_name": company_name,
            "fiscal_year": fiscal_year,
            "base_currency": currency,
            "tax_jurisdiction": "CA",
        })
        return self._json_ok(resp, "POST /api/onboarding/profile")

    def complete_onboarding(self) -> dict:
        return self._json_ok(self.post("/api/onboarding/complete"),
                             "POST /api/onboarding/complete")

    def dashboard_summary(self) -> dict:
        return self._json_ok(self.get("/api/dashboard/summary"), "GET /api/dashboard/summary")

    def list_transactions(self, page: int = 1, per_page: int = 50) -> dict:
        resp = self.get("/api/transactions", {"page": page, "per_page": per_page})
        return self._json_ok(resp, "GET /api/transactions")

    def list_documents(self, page: int = 1, per_page: int = 50) -> dict:
        resp = self.get("/api/receipts", {"page": page, "per_page": per_page})
        return self._json_ok(resp, "GET /api/receipts")

    def get_document(self, doc_id: int | str) -> dict:
        return self._json_ok(self.get(f"/api/receipts/{doc_id}"), f"GET /api/receipts/{doc_id}")

    def get_job(self, job_id: str) -> dict:
        """One row of the document queue (`GET /api/jobs/{id}`)."""
        return self._json_ok(self.get(f"/api/jobs/{job_id}", timeout=30.0),
                             f"GET /api/jobs/{job_id}")

    def wait_for_job(self, job_id: str, deadline_seconds: float | None = None) -> dict:
        """Poll a queued document job to a terminal state and return the row.

        `POST /api/receipts/upload` answers 202 the moment the file is stored:
        extraction happens later, in `server/jobs/document_queue.py`, behind
        every other upload on the machine. Asserting on that 202 proves the file
        was accepted and nothing about whether a document came out of it, which
        is exactly the gap a run wants closed before it reconciles.

        Returns the job row (`status` one of done/failed/cancelled, with
        `result`). Raises on a deadline so a stuck queue is visible as itself.
        """
        limit = deadline_seconds or JOB_TIMEOUT
        t0 = time.time()
        deadline = t0 + limit
        row: dict = {}
        polls = 0
        while time.time() < deadline:
            row = self.get_job(job_id)
            status = row.get("status")
            if status in JOB_TERMINAL_STATUSES:
                logger.info("  job %s finished as %s in %.0fs", job_id[:8], status, time.time() - t0)
                return row
            polls += 1
            if polls % 10 == 1:
                logger.info("  waiting on job %s (%.0fs): %s, %s ahead, page %s/%s",
                            job_id[:8], time.time() - t0, status, row.get("ahead"),
                            row.get("pages_done"), row.get("pages_total"))
            time.sleep(JOB_POLL_SECONDS)
        raise AssertionError(
            f"document job {job_id} was still {row.get('status')!r} after {limit:.0f}s "
            f"({row.get('ahead')} job(s) ahead of it). If the status is 'waiting_for_model', the "
            "LLM endpoint is unreachable (see GET /health). Raise AA_E2E_JOB_TIMEOUT if the queue "
            "is genuinely this deep; the extraction itself continues server-side."
        )

    def wait_for_statement(self, statement_id: str, deadline_seconds: float = 900) -> dict:
        """Poll `GET /api/statements/{id}` until the parse leaves `processing`.

        A statement PDF answers 202 and parses in a background job, so asserting
        on the 202 alone proves nothing about the parse. Returns the statement
        row; raises on a deadline so a stuck parse is visible.
        """
        deadline = time.time() + deadline_seconds
        row: dict = {}
        while time.time() < deadline:
            resp = self.get(f"/api/statements/{statement_id}", timeout=30.0)
            if resp.status_code != 200:
                raise AssertionError(
                    f"GET /api/statements/{statement_id}: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            row = resp.json()
            if row.get("status") != "processing":
                return row
            time.sleep(5.0)
        raise AssertionError(
            f"statement {statement_id} was still 'processing' after {deadline_seconds:.0f}s "
            f"(last row: {str(row)[:200]})"
        )

    def reconciliation_progress(self) -> dict:
        """Current server-side reconciliation state for this user.

        `{status: idle|running|complete|error, phase, percent, matches_so_far}`,
        plus `result` with the full stats once status is `complete` — where
        `matched` is the number of matches the run STORED, and a pair already on
        file that was only re-scored appears as `refreshed`.
        """
        return self._json_ok(self.get("/api/reconciliation/progress", timeout=30.0),
                             "GET /api/reconciliation/progress")

    def _wait_for_foreign_run(self, deadline: float) -> dict:
        """Block until this account has no reconciliation in flight.

        `/start` answering `already_running` means somebody else's run holds the
        slot — its result is not this test's to read. Waiting is the honest move;
        failing on it would report a scheduling collision as a product defect.
        """
        last: dict = {}
        polls = 0
        while time.time() < deadline:
            last = self.reconciliation_progress()
            if last.get("status") != "running":
                return last
            if polls % 12 == 0:
                logger.info("  another reconciliation holds this account: %s %s%% — "
                            "%s matches so far; waiting",
                            last.get("phase"), last.get("percent"), last.get("matches_so_far"))
            polls += 1
            time.sleep(RECONCILE_POLL_SECONDS)
        return last

    def run_reconciliation(self, deadline_seconds: float | None = None) -> dict:
        """Reconcile to completion and return the engine's result dict.

        Starts the run in the background (`POST /api/reconciliation/start`) and
        polls `GET /api/reconciliation/progress` until a terminal state. The LLM
        pass takes minutes, and holding one blocking request open for it means
        the client gives up on work the server goes on to finish — which then
        reads as dozens of product failures.

        A `/start` that answers `already_running` is another run on this
        account, not ours: it is waited out and the start retried, because
        reading that run's numbers as this test's would be a fabricated verdict.

        Raises AssertionError on a server-side failure or on exceeding the
        deadline, both carrying the last phase the server reported.
        """
        limit = deadline_seconds or RECONCILE_TIMEOUT
        t0 = time.time()
        deadline = t0 + limit

        last: dict = {}
        for attempt in range(1, RECONCILE_START_ATTEMPTS + 1):
            started = self.post("/api/reconciliation/start", timeout=60.0)
            if started.status_code != 200:
                raise AssertionError(
                    f"POST /api/reconciliation/start: HTTP {started.status_code}: {started.text[:300]}"
                )

            body = started.json() if started.content else {}
            if not isinstance(body, dict) or body.get("status") != "already_running":
                last = (body.get("progress") if isinstance(body, dict) else None) or {}
                break

            logger.info("  /api/reconciliation/start: already_running (attempt %d/%d) — "
                        "waiting for the run in flight before starting ours",
                        attempt, RECONCILE_START_ATTEMPTS)
            last = self._wait_for_foreign_run(deadline)
            if time.time() >= deadline:
                raise AssertionError(
                    f"another reconciliation held this account for the whole {limit:.0f}s window "
                    f"(last phase: {last.get('phase')!r} {last.get('percent')}%), so this run "
                    "never started. Re-run when the account is free."
                )
        else:
            raise AssertionError(
                f"POST /api/reconciliation/start answered already_running "
                f"{RECONCILE_START_ATTEMPTS} times in {time.time() - t0:.0f}s — something else is "
                "reconciling this account back-to-back and this run never got the slot."
            )
        polls = 0
        while time.time() < deadline:
            progress = self.reconciliation_progress()
            status = progress.get("status")
            polls += 1
            if polls % 6 == 1:
                # A multi-minute wait has to say what it is waiting on, or the
                # only visible thing is a stalled log.
                logger.info("  reconciling (%.0fs): %s %s%% — %s matches so far",
                            time.time() - t0, progress.get("phase"),
                            progress.get("percent"), progress.get("matches_so_far"))
            if status == "complete":
                result = progress.get("result")
                if not isinstance(result, dict):
                    raise AssertionError(
                        "reconciliation progress reported complete with no result block: "
                        f"{str(progress)[:300]}"
                    )
                logger.info(
                    "  reconciliation finished in %.0fs: matched=%s (new) refreshed=%s "
                    "proposed=%s rate=%s",
                    time.time() - t0, result.get("matched"), result.get("refreshed"),
                    result.get("proposed"), result.get("rate"),
                )
                return result
            if status == "error":
                tail = "; ".join(str(line) for line in (progress.get("logs") or [])[-4:])
                raise AssertionError(
                    f"reconciliation failed server-side after {time.time() - t0:.0f}s: "
                    f"{progress.get('detail')} | {tail}"
                )
            if status == "idle":
                raise AssertionError(
                    "reconciliation progress went to 'idle' after a start was accepted — the "
                    "run thread died or the server restarted mid-run"
                )
            last = progress
            time.sleep(RECONCILE_POLL_SECONDS)

        raise AssertionError(
            f"reconciliation did not reach a terminal state within {limit:.0f}s "
            f"(last phase: {last.get('phase')!r} {last.get('percent')}%, "
            f"matches so far: {last.get('matches_so_far')}). Raise AA_E2E_RECONCILE_TIMEOUT if the "
            "LLM endpoint is genuinely this slow; the run itself continues server-side."
        )

    def reconciliation_summary(self) -> dict:
        return self._json_ok(self.get("/api/reconciliation/summary"),
                             "GET /api/reconciliation/summary")

    def list_matches(self, status: str | None = None, page: int = 1) -> dict:
        """Matches, optionally filtered by status. Raises unless the call worked.

        A caller reads `matches` to decide whether there is anything to approve,
        reject or unmatch — so a failed request answering with an empty list is
        indistinguishable from an empty review queue, and the whole review
        surface reports "nothing to act on" while actually being untested.
        """
        params = {"page": page, "per_page": 50}
        if status:
            params["status"] = status
        resp = self.get("/api/reconciliation/matches", params)
        return self._json_ok(
            resp, f"GET /api/reconciliation/matches (status={status or 'any'})")

    def approve_match(self, match_id: int) -> dict:
        resp = self.patch(f"/api/reconciliation/matches/{match_id}/approve")
        return self._json_ok(resp, f"PATCH /api/reconciliation/matches/{match_id}/approve")

    def reject_match(self, match_id: int) -> dict:
        resp = self.patch(f"/api/reconciliation/matches/{match_id}/reject")
        return self._json_ok(resp, f"PATCH /api/reconciliation/matches/{match_id}/reject")

    def get_report(self, year: int, month: int) -> dict:
        return self._json_ok(self.get(f"/api/reports/{year}/{month}"),
                             f"GET /api/reports/{year}/{month}")

    def system_status(self) -> dict:
        return self._json_ok(self.get("/api/actions/status"), "GET /api/actions/status")

    def reset_data(self) -> dict:
        return self._json_ok(self.post("/api/actions/reset", {"confirm": True}),
                             "POST /api/actions/reset")

    def missing_receipts(self, year: int | None = None, month: int | None = None) -> dict:
        params = {}
        if year:
            params["year"] = year
        if month:
            params["month"] = month
        return self._json_ok(self.get("/api/actions/missing-receipts", params),
                             "GET /api/actions/missing-receipts")

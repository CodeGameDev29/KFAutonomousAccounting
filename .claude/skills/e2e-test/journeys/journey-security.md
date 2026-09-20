# Journey: Cross-Cutting Security & Isolation

**Purpose**: the controls that span every persona — auth enforcement, per-user data filtering,
what error pages reveal, injection handling, session persistence.
**Runs after**: at least one persona journey, so the account holds data.
**Account**: `$AA_E2E_EMAIL` / `$AA_E2E_PASSWORD` against `$AA_E2E_BASE_URL`. A throwaway account
with synthetic data only — this journey writes an XSS payload into its profile.

**Verdicts**: every step ends PASS, FAIL or NOT-EXERCISED with a written reason. Ambiguous = FAIL.

**Browser only**: all checks go through `browser_*` against the URL a user would use, because the
headers and the Content-Security-Policy that matter are the ones a browser actually receives.
`browser_evaluate` calling `fetch` from the page's own origin is the browser. What cannot be
observed from inside a browser session (a foreign-origin preflight) is NOT-EXERCISED with a pointer
to the `runner.py` case that covers it.

---

## Phase 1: Authentication enforcement

### 1.1 Protected routes redirect when signed out
Clear storage. Open `/dashboard`, then `/transactions`, `/review`, `/reports`, `/analytics`,
`/activity`, `/settings`, `/security`.
- **PASS if**: each lands on `/login` and renders none of the protected page.
- **FAIL if**: any protected content renders, even for a moment in a screenshot.

### 1.2 A forged session is not trusted
`() => localStorage.setItem('aa.session', JSON.stringify({access_token: 'x.y.z', refresh_token:
'nope'}))`, then open `/dashboard`.
- **PASS if**: the app ends on `/login` and no API call made with that token returned data.
- **FAIL if**: the dashboard renders data.
Clear storage afterwards.

### 1.3 Wrong password
On `/login`, submit the test email with a password you just made up.
- **PASS if**: "Invalid login credentials." is shown, the URL stays `/login`, and the wording is the
  same as for an address that does not exist (try one at a reserved domain).
- **FAIL if**: the two cases are distinguishable.

---

## Phase 2: Per-user data filtering

Registration is closed by default, so the suite is normally given one account. Isolation is then
checked from inside it: nothing that does not belong to it is reachable by id or by crafted URL.
The two-account variant (2.4) exists only when the operator opened registration for the run.

### 2.1 Inventory
Sign in; on `/transactions` open one matched row and record a transaction id and a document id
from the network requests as `owned_ids`.
- **FAIL if**: the account is empty — the persona journey did not run, which is an execution
  failure, not an environment limit.

### 2.2 Foreign transaction ids
From the page origin, with the session's bearer token (see `journey-bookkeeper.md` Step 4.1 for how
the page holds it): `GET /api/transactions/<id>` for three ids far outside `owned_ids`.
- **PASS if**: every answer is 404 or 403, with no transaction fields in the body.

### 2.3 Foreign document ids
Same against `/api/receipts/<id>`, `/api/receipts/<id>/download` and `/api/receipts/<id>/preview`.
- **PASS if**: 404/403 every time and no signed file URL is issued.

### 2.4 Two accounts — only when registration is open
If `/health` reports `signup_enabled: true`, sign up a second throwaway account at a reserved
documentation domain, onboard it, open `/transactions`, `/reports` and `/activity`.
- **PASS if**: it sees zero rows of the first account, anywhere.
- **NOT-EXERCISED if**: registration is closed. Name `AUTH_ALLOW_SIGNUP`. Never borrow somebody's
  real account to be "the other user".

### 2.5 A share token grants that report and nothing else
With a share link from an earlier journey, signed out, from the shared page's origin try
`GET /api/transactions` and `GET /api/shared/<token>/receipt/999999999`.
- **PASS if**: 401/403 and 404 respectively.
- **NOT-EXERCISED if**: no share link exists yet.

---

## Phase 3: Headers and documentation exposure

### 3.1 Security headers on the document response
Open `/login`; read the top-level document's response headers from `browser_network_requests`.
- **PASS if**: `x-content-type-options: nosniff`, `x-frame-options: DENY`, a `referrer-policy`, and
  a `content-security-policy` whose `font-src` and `script-src` name no third-party host.
- **NOT-EXERCISED if**: the browser tooling cannot return response headers — `runner.py`'s
  `security_headers_present` and `referrer_policy_header` assert the first three.

### 3.2 No API documentation is served
Open `/docs`, `/redoc` and `/openapi.json`.
- **PASS if**: each shows the app's own "Page not found" (or a 404) — never Swagger UI, ReDoc, or a
  JSON schema listing `/api/*` routes. The API and the SPA share one origin, so anything FastAPI
  serves is reachable by whoever can reach the app.

---

## Phase 4: CORS

### 4.1 Foreign-origin preflight
- **NOT-EXERCISED**, normally: a browser will not let page script set `Origin`, so the server never
  sees the forged value. `runner.py`'s `cors_not_wildcard` sends the real preflight. If your
  tooling can send it, PASS requires `access-control-allow-origin` to be neither `*` nor the
  foreign origin.

---

## Phase 5: What errors reveal

### 5.1 Unknown page
Signed in, open `/this-page-does-not-exist`.
- **PASS if**: "Page not found"; no `Traceback`, no `File "`, no filesystem path, no SQL, no
  `psycopg`.

### 5.2 Path traversal
Open `/../../../etc/passwd`, then `/api/nonexistent/../../etc/passwd`, then
`/api/files/download?key=../../.env` from the page origin.
- **PASS if**: no file content in any answer; the API answers with a JSON error; nothing names a
  server path.
- **FAIL if**: any byte of a file outside the storage root comes back.

### 5.3 Rejected upload
Upload `$FIXTURES/e2e_journey_not_a_pdf.pdf` as a receipt and read the error.
- **PASS if**: a plain refusal; no traceback, no path.

### 5.4 Console
- **PASS if**: `browser_console_messages` at error level is empty apart from `ResizeObserver loop`.
  "Some warnings are fine" is not a verdict.

---

## Phase 6: Session persistence

### 6.1 Reload
Sign in, reach `/dashboard`, reload.
- **PASS if**: still `/dashboard`, still rendered.

### 6.2 Navigation
"Transactions" → "Dashboard" via the sidebar.
- **PASS if**: no bounce to `/login`.

### 6.3 Sign-out revokes, it does not merely forget
Record the refresh token from `aa.session` (in memory only — never write it to the evidence), click
"Sign out", then from the page origin `POST /api/auth/refresh` with that token.
- **PASS if**: the refresh is refused.
- **FAIL if**: a signed-out refresh token still mints a session.

---

## Phase 7: Injection

### 7.1 Script in the company name
"Settings" → "Profile": record the current "Company Name", set it to
`<script>alert('xss')</script>`, "Save Profile". Visit `/dashboard`, a report month, and generate
and open a share link for that month (the company name is printed on all three).
- **PASS if**: the payload renders as literal text everywhere, and no dialog opened
  (`browser_handle_dialog` never fired).
- **FAIL if**: anything executes.

### 7.2 Restore
Set "Company Name" back to the recorded value and save.
- **FAIL if**: the save fails and the account is left holding the payload. Revoke the share link.

### 7.3 Search field
On `/transactions`, search for `'; DROP TABLE transactions; --` and then for an unbalanced regex
such as `([`.
- **PASS if**: the page keeps working — no rows, or rows, but no error page, and the table returns
  when the search is cleared. The search box accepts a regex, so a malformed one must be handled as
  text or refused politely.
- **FAIL if**: a 500, a database error, or a table that does not come back.

### 7.4 Formula injection in exports
Add a note starting with `=HYPERLINK(` to a January transaction, export "QuickBooks CSV".
- **PASS if**: the cell is neutralised (prefixed or quoted) in the CSV.
- **FAIL if**: it is written as a live formula. Remove the note afterwards.

---

## Phase 8: Rate limiting

### 8.1 Sign-in is limited
`POST /api/auth/token` is limited to 20 per minute per address. From the page origin, send 25
sign-in attempts for an address nobody owns at a reserved domain, with a made-up password.
- **PASS if**: at least one 429, and the app still works a minute later.
- **FAIL if**: all 25 are 400 — no limit applied on an auth route.
- **Cost**: the limiter keys on the client address, so the *real* test account is also locked out of
  signing in for up to a minute. Run this phase last, and never aim it at the sign-up route — that
  budget (per hour) is what the two-account check needs.

---

## Phase 9: Health

### 9.1 Health is public and says only what is safe
Signed out, open `/health`.
- **PASS if**: JSON with `"status": "ok"`, storage and extraction blocks, `signup_enabled`, and no
  secret: no key value, no `DATABASE_URL`, no token. `gemini_key_present` / `openai_key_present` are
  booleans by design.
- **FAIL if**: it needs auth, or prints a credential.

---

## Phase 10: Cleanup
Clear storage; `browser_close`.

## Scoring
Every step ends PASS, FAIL, or NOT-EXERCISED with a written reason — never SKIP, never silence.
2.4 and 4.1 are the two steps that are usually NOT-EXERCISED; anything else reported that way
needs a reason a stranger would accept.

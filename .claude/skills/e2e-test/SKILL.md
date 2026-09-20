---
name: e2e-test
description: >-
  Prove the web app works for a real user by driving every user-reachable surface through a real
  browser — four persona journeys, a functionality-completeness sweep and a security journey, each
  step judged independently — plus the API suites that prove the contract underneath. Reach for this
  before a release or after a change with wide blast radius. It needs a running app and a throwaway
  account; for engine logic alone, the project's own `pytest` suite is the faster proof.
---

# E2E test

## Goal

Every surface a user can reach in the running web app has been driven by a real browser and judged
by someone who did not run the test, the API contract under it exercised directly, every gap named.

## Preconditions

- **The app is up**: `curl $AA_E2E_BASE_URL/health` (default `http://localhost:8080`) answers
  `"status":"ok"`. Start it as the README's quickstart says; this skill never starts it for you.
- **A throwaway account**: `AA_E2E_EMAIL` and `AA_E2E_PASSWORD` in the environment (or `.env`).
  There is no default and `runner.py` exits 2 without them. **The account must hold synthetic data
  only — the suite uploads, mutates and deletes everything in it** (`POST /api/actions/reset`).
  Never an account with real books. To make one: `AUTH_ALLOW_SIGNUP=1`, restart, sign up, set it
  back to `0`.
- **An LLM endpoint** reachable at `LLM_BASE_URL` (`/health` → `extraction.local.reachable`).
  Without it uploads park in `waiting_for_model` and every extraction and reconciliation verdict is
  NOT-EXERCISED with that cause — say so up front rather than discovering it sixty times.
- **A browser-automation MCP** (Playwright: `browser_navigate` callable) for the journeys. Without
  one only the API suites run, and they prove the contract and nothing about the experience.
- **Deps**: the project venv — `python -c "import httpx, fitz"`.

## Done means

- A report under `reports/` giving every step PASS, FAIL or NOT-EXERCISED, the reason for each
  NOT-EXERCISED, and a screenshot plus raw tool output for every step, passes included — the judge
  reads those artifacts, not a summary of them.
- Per-suite API results alongside the browser verdicts:
  `python .claude/skills/e2e-test/runner.py --no-cleanup` (venv active; otherwise
  `.venv/bin/python` or `.venv\Scripts\python.exe`).
- Every route in `web/src/App.tsx` either exercised or listed as a gap. A route nobody touched and
  nobody mentioned is the failure this section exists to prevent.

## Judgment calls you own

- **You execute; you do not judge.** Each verdict comes from a separate agent with zero context from
  you, working from the step's own criteria, the raw output and the screenshot. Default FAIL; any
  timeout, 500 or exception is a FAIL unless the step expects it (`templates/judge-prompt.md`).
- **Three verdicts, no SKIP.** NOT-EXERCISED is for what this install genuinely cannot offer —
  registration closed, an integration with no credential, no mail transport, no model. If the
  prerequisite was yours to arrange, it is a FAIL and an execution failure.
- **Registration being closed is the product's state, not a blocker.** Never fail a run over it. A
  step that truly needs a second account is NOT-EXERCISED unless the operator opened registration
  for the run; the suites read that from `/health` (`signup_enabled`) and never probe by signing up.
- **One account, so order and resets are load-bearing.** All four personas drive the same account:
  reset between them or persona 2 inherits persona 1's ledger and every count drifts — but read
  `--no-reset` below before wiping data somebody is using. Generate a fresh fixture set per journey
  (`lib/fixtures.py --out`): uploads are content-hash deduped, so identical bytes are swallowed and
  the run reports success against the last run's data. Clear `localStorage` between personas. And
  don't restart the server mid-run: check `/health` before concluding it died.
- **Synthetic data only, in both directions.** Nothing real goes in, and nothing from a run gets
  committed: `reports/`, `screenshots/` and `progress.json` are gitignored here for that reason.
- **Backend access is a narrow exception**: the pre-run reset and fixture generation. In-page
  `browser_evaluate` calling `fetch` from the app's own origin is still the browser; everything else
  goes through the UI ([`VERIFICATION.md`](../../project/VERIFICATION.md)).
- **A failing step does not abort the journey** — later steps often reveal the root cause.

## Failure modes

- A journey passing against data left by a previous run (reset skipped, or a fixture hash-collided),
  or a step passed by the agent that clicked because "the screenshot looks right".
- Wrong upload control: **Upload Statements** sends files (PDFs included) to statement parsing,
  **Upload Receipts & Invoices** to extraction; the wrong one accepts plausibly and yields nothing.
- Stale element refs after a Radix dialog, select, sheet or tooltip mounts into a portal —
  re-snapshot after anything opens and after any navigation. Same for timing: extraction is a queued
  job, and moving on before it reaches a terminal state passes a failed upload.
- `window.open` surfaces (the receipt viewer) need `browser_tabs` to find the tab; the `noopener`
  invariant is only proven by `window.opener === null` plus an empty `document.referrer` there.
- **A test that passes on a 4xx**: an accepted status (or a substring checked over the first 100
  characters of a body) must mean *the feature worked*; a test whose precondition failed is
  NOT-EXERCISED with that cause, never FAIL — hence `depends_on` in `SUITE_REGISTRY`.
- **A skip standing in for a verdict**: `SkipTest` records FAIL because a silent skip reads as
  green. Absent things raise `NotExercised` with the reason; a prerequisite that was ours still fails.
- **"The call failed" reported as "there is nothing there"**: list helpers raise on a non-2xx.

## Where things live

- **Journeys**: `journeys/` — four personas, `journey-functionality-completeness.md`,
  `journey-security.md` (run it last: its rate-limit phase locks sign-in for a minute). Mapping:
  `config/profiles.json`; persona behaviour flags: `profiles/__init__.py` → `ALL_PROFILES`.
- **API suites**: `runner.py` → `suites/` (auth, onboarding, data ingestion, reconciliation,
  reports, security); writes `reports/e2e-api-<ts>.md` and `progress.json`. Flags: `--profile`,
  `--url`, `--no-cleanup`, `--no-reset`, `--api-only` (no-op). Accounts and HTTP in
  `lib/api_client.py`, verdicts and `depends_on` gating in `lib/test_framework.py`; order inside a
  suite is load-bearing. It is not part of the project's `pytest` (`testpaths = tests`) — keep it so.
- **`--no-reset`**: suppresses every `POST /api/actions/reset` — pre-run, between personas, cleanup.
  The default reset deletes every transaction, document, match and ledger entry the account owns.
- **Fixtures**: none are shipped. `lib/fixtures.py` names the repository's synthetic files under
  `tests/fixtures/` and generates the per-run ones — a receipt PDF with a unique order number and a
  bank CSV carrying a row with that receipt's vendor, date and amount, so a correct matcher has one
  honest pair to find. `python .claude/skills/e2e-test/lib/fixtures.py --out <dir outside the repo>`.
- **Timing**: reconciliation is started then polled (`AA_E2E_RECONCILE_TIMEOUT`, default 5400s);
  uploads answer 202 and are followed with `wait_for_job()` (`AA_E2E_JOB_TIMEOUT`, default 900s); the
  client refreshes its token at 45 minutes and on any 401 (`AA_E2E_TOKEN_MAX_AGE`). A `/start`
  answering `already_running` is someone else's run: the client waits rather than read its numbers.
- **URLs**: one process serves the API and the built SPA on `:8080`; the Vite dev server on `:5174`
  proxies `/api`. Test the one the change touched.

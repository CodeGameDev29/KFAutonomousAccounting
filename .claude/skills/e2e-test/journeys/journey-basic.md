# Journey: Basic Small Business Owner

**Persona**: Example Consulting Inc. — wants zero complexity and a five-minute monthly workflow
**Pain points**: confused by integration jargon; wants upload → match → download
**Account**: `$AA_E2E_EMAIL` / `$AA_E2E_PASSWORD` — the throwaway test account every persona shares.
Its data is reset before this journey starts. It holds synthetic data only.
**Base URL**: `$AA_E2E_BASE_URL` (default `http://localhost:8080`; `http://localhost:5174` also works
when the Vite dev server is what you are testing).
**Fixtures**: `$FIXTURES` is the directory printed by
`python .claude/skills/e2e-test/lib/fixtures.py --out <dir>` — run it once per journey so the
receipt's content hash is new. The CSV it writes has 6 rows dated 2026-01; its `EXAMPLE RETAILER`
row (2026-01-18, 40.32) is the pair for the receipt PDF.

Every step ends PASS, FAIL or NOT-EXERCISED (with the reason). Take a screenshot and keep the raw
tool output for every step, passes included; a separate judge reads those
(`templates/judge-prompt.md`). A failing step does not end the journey.

---

## Phase 1: Front door and sign-in

### Step 1.1: Open the landing page
**Action**: `browser_evaluate` → `() => { localStorage.clear(); sessionStorage.clear(); }`.
`browser_navigate` → `$AA_E2E_BASE_URL/`. Snapshot.
- **PASS if**: the heading "Autonomous Accounting", a "Sign in" control and the "Where the data
  lives" section are all present, with no signed-in UI (no sidebar).
- **FAIL if**: the page is blank, errors, or any of the three is missing. The front door of a
  self-hosted install carries no price, no trial and no subscribe button; finding one is the bug.

### Step 1.2: The primary control leads to the auth surface
**Action**: click "Sign in".
- **PASS if**: the URL becomes `/login` and an email and a password field render.
- **FAIL if**: nothing happens, an error appears, or the target renders "Page not found".

### Step 1.3: Closed registration is stated, not a dead end
**Action**: `browser_navigate` → `/signup`, snapshot; then `/login?mode=signup`, snapshot.
From the page's own origin: `() => fetch('/health').then(r => r.json()).then(j => j.signup_enabled)`.
- **PASS if** (`signup_enabled` false): both URLs land on a usable sign-in page that says "This
  instance is not accepting new accounts." and offers no sign-up form.
- **PASS if** (`signup_enabled` true): a sign-up form renders. Do not submit it here.
- **FAIL if**: either URL renders "Page not found", an empty page, or a form the server would refuse
  (a sign-up form while `signup_enabled` is false).

### Step 1.4: Sign in with the test account
**Action**: on `/login`, fill the email and password fields, click "Sign in". Wait up to 15s.
- **PASS if**: the URL becomes `/onboarding` or `/dashboard` and that page renders.
- **FAIL if**: an error appears or the page stays on `/login`. Hard FAIL — a wrong or missing
  credential is a setup problem to fix, not a blocked run.

---

## Phase 2: Onboarding

If the account is already onboarded the app goes straight to `/dashboard`; navigate to
`/onboarding` to drive the wizard anyway. If it redirects away, record 2.1–2.3 NOT-EXERCISED
("account already onboarded; the API suite's onboarding cases cover the endpoints").

### Step 2.1: Business profile
**Action**: under "What's your business called?", fill "Company Name" with
`Example Consulting Inc.`, choose a province, set "Year to track" to 2026 and "Main currency" to
CAD. Click "Next".
- **PASS if**: the wizard advances to "Upload Your First Documents".
- **FAIL if**: "Next" stays disabled with valid input, a validation error appears, or nothing moves.

### Step 2.2: Skip the first upload
**Action**: click "Skip for now".
- **PASS if**: a completion screen reading "You're all set, Example Consulting Inc.!" appears, or
  the URL becomes `/dashboard`.
- **FAIL if**: the wizard does not advance or errors.

### Step 2.3: Arrive at the dashboard
**Action**: click "Go to Dashboard" if the completion screen is showing.
- **PASS if**: the URL is `/dashboard` and the page renders inside the sidebar layout.
- **FAIL if**: anything else.

---

## Phase 3: Upload a bank statement (CSV)

### Step 3.1: Open Transactions and record the baseline
**Action**: sidebar → "Transactions". Snapshot. Record `baseline_tx_count` from the pagination text
("Showing …") or 0 for the empty state.
- **PASS if**: the URL is `/transactions`, the "Transactions" heading renders and a count is readable.
- **FAIL if**: the page does not load.

### Step 3.2: Upload the CSV through the statements control
**Action**: click "Upload Statements" and hand `$FIXTURES/e2e_journey_bank_cad.csv` to
`browser_file_upload`. Wait up to 30s, re-snapshotting.
- **PASS if**: the "Uploaded Files" panel lists the file under "Statements" and the transaction
  count becomes `baseline_tx_count + 6`.
- **FAIL if**: an error is shown, the count does not change, or fewer than 6 rows arrive — a success
  message with no new rows is a phantom success.
- **Note**: "Upload Statements" and "Upload Receipts & Invoices" are different pipelines. The wrong
  one accepts the file plausibly and yields nothing.

### Step 3.3: The rows are the rows that were uploaded
**Action**: snapshot the table.
- **PASS if**: at least three of `EXAMPLE RETAILER`, `ANYTOWN COFFEE HOUSE`, `PINEGROVE MOBILE`,
  `BLUEPEAK SOFTWARE`, `HARBOURLINE LOGISTICS`, `MERIDIAN FUEL` appear, with the amounts 40.32 and
  9,600.00 among them.
- **FAIL if**: the descriptions or amounts are not the CSV's.

---

## Phase 4: Upload a receipt

### Step 4.1: Upload the generated receipt
**Action**: click "Upload Receipts & Invoices", upload `$FIXTURES/e2e_journey_receipt_<tag>.pdf`.
Extraction is queued: watch the processing queue until the file reaches a terminal state (allow
several minutes on a local model; re-snapshot rather than sleeping blind).
- **PASS if**: the receipt appears under "Receipts & Invoices" with a vendor reading Example
  Retailer, a total of 40.32 and the date 2026-01-18.
- **FAIL if**: extraction fails, or the row carries no vendor or no total.
- **NOT-EXERCISED if**: the queue reports it is waiting for the extraction model and `/health`
  shows the LLM endpoint unreachable. Name `LLM_BASE_URL`; every later reconciliation step then
  inherits this cause.

### Step 4.2: The same bytes twice are one document
**Action**: record the receipt count, upload the same PDF again, wait for the queue to settle.
- **PASS if**: the UI marks the second upload as a duplicate and the receipt count is unchanged.
- **FAIL if**: the count increases, or the second upload vanishes with no feedback at all.

### Step 4.3: An image receipt is accepted
**Action**: upload `tests/fixtures/test_receipt_retailer.png` through the receipts control.
- **PASS if**: the file is accepted and queued (no "Unsupported file type" message).
- **FAIL if**: a PNG is refused.

---

## Phase 5: Match and review

### Step 5.1: Run matching
**Action**: on `/transactions`, click "Match Receipts". Follow the progress panel to completion
(minutes, not seconds, when the LLM pass runs).
- **PASS if**: a "Reconciliation complete:" result appears reporting at least 1 match.
- **FAIL if**: the run errors, never finishes, or reports zero matches — the fixtures contain one
  exact vendor + date + amount pair, so zero is an engine miss.

### Step 5.2: The match is persisted, not just announced
**Action**: set the status filter to "Matched". Snapshot. Click the `EXAMPLE RETAILER` row.
- **PASS if**: at least one row is listed and the "Transaction Details" panel shows a "Matched
  Receipt" section naming the vendor and total.
- **FAIL if**: no row is Matched, or the panel reads "No receipt matched to this transaction."

### Step 5.3: Review page
**Action**: press Escape to close the panel; sidebar → "Review".
- **PASS if**: the URL is `/review` and the three tabs "Auto-matched", "Needs Review" and "No Match"
  render with counts, at least one of them non-zero.
- **FAIL if**: the page fails to load or every tab is empty after a run that reported a match.

### Step 5.4: Inspect one match
**Action**: click the first row in whichever tab holds the `EXAMPLE RETAILER` pair.
- **PASS if**: the detail panel shows "Bank Transaction", "Matched Receipt" and "Match Analysis",
  with a vendor, a date and a total.
- **FAIL if**: the panel is empty or has only one side of the pair.

### Step 5.5: Bulk approve (this persona approves everything)
**Action**: if "Approve All High-Confidence" is enabled, click it and confirm the dialog.
- **PASS if**: the pending count drops and no error toast appears.
- **NOT-EXERCISED if**: the button is absent or disabled because nothing is pending — every match
  scored above the auto-approve threshold. That is a state of the data; say so.

### Step 5.6: A second run stores nothing new
**Action**: back on `/transactions`, click "Match Receipts" again and wait for the result.
- **PASS if**: the result reports no new matches (pairs already on file are reported as such).
- **FAIL if**: the second run stores roughly as many matches as the first — duplicate matches.

---

## Phase 6: Dashboard, activity and report

### Step 6.1: Dashboard reflects the run
**Action**: sidebar → "Dashboard". Step the period selector ("Previous month") back to January 2026
if it is not already there.
- **PASS if**: the "Transactions Resolved" card shows at least 1 resolved, and the status banner
  either lists what needs attention or reads "Your books are up to date".
- **FAIL if**: the card still shows 0 resolved, or no status banner renders.

### Step 6.2: The audit trail recorded it
**Action**: sidebar → "Activity"; choose "All time".
- **PASS if**: "Activity Log" lists at least one entry (a match run, an upload or an approval)
  with a timestamp.
- **FAIL if**: "No activity in this date range" after everything above.

### Step 6.3: The monthly report carries the data
**Action**: sidebar → "Reports" → year 2026 → January.
- **PASS if**: the month view lists the uploaded rows, including 40.32 and 9,600.00, and the
  company name `Example Consulting Inc.` appears on the report.
- **FAIL if**: the year or month is missing, or the table is empty.

---

## Phase 7: Sign out

### Step 7.1: Sign out and prove it
**Action**: sidebar → "Sign out". Then `browser_navigate` → `/dashboard`.
- **PASS if**: signing out lands on `/` or `/login`, and the direct visit to `/dashboard` redirects
  to `/login`.
- **FAIL if**: the dashboard renders without signing in again.

### Step 7.2: Clear browser state
**Action**: `browser_evaluate` → `() => { localStorage.clear(); sessionStorage.clear(); }`.
Leaked dismissal flags make the next persona's first-run experience untestable.

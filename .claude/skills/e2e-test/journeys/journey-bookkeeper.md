# Journey: Bookkeeper

**Persona**: Example Bookkeeping Professional Corp — record-keeping for the CRA, every export
format, a shared month, an audit trail someone else could follow
**Account / base URL / fixtures**: as in `journey-basic.md` — `$AA_E2E_EMAIL`, `$AA_E2E_PASSWORD`,
`$AA_E2E_BASE_URL`, and a fresh `$FIXTURES` from `lib/fixtures.py --out <dir>`. The account's data
is reset before this journey.

---

## Phase 1: Sign in and name the business

### Step 1.1: Sign in
**Action**: clear storage, open `$AA_E2E_BASE_URL/login`, sign in.
- **PASS if**: the URL becomes `/dashboard` or `/onboarding`.
- **FAIL if**: it stays on `/login`.

### Step 1.2: Profile
**Action**: "Settings" → "Profile" → set "Company Name" to `Example Bookkeeping Professional Corp`,
"Fiscal Year" 2026, "Base Currency" CAD, a province → "Save Profile".
- **PASS if**: "Saved" appears and the values survive a reload.

---

## Phase 2: Client data in, matched

### Step 2.1: Statement — the count is the assertion
**Action**: `/transactions` → "Upload Statements" → `$FIXTURES/e2e_journey_bank_cad.csv`.
- **PASS if**: exactly **6** transactions arrive. Count them; "it said imported" is not evidence.
- **FAIL if**: any other number.

### Step 2.2: Receipt — every field, or it is a FAIL
**Action**: "Upload Receipts & Invoices" → `$FIXTURES/e2e_journey_receipt_<tag>.pdf`; follow the
processing queue to a terminal state.
- **PASS if**: vendor is Example Retailer (not blank, not "Unknown"), total is 40.32, GST 1.80 is
  captured as tax, and the date is 2026-01-18.
- **FAIL if**: any field is blank or wrong. The 7% provincial line (RST 2.52) must not be reported as
  GST/HST — an input tax credit is only ever claimed on evidence of GST or HST.

### Step 2.3: Match and approve
**Action**: "Match Receipts"; then on `/review` approve each correct pending match with "Approve",
reading "Match Analysis" first.
- **PASS if**: at least 1 match exists, and each one inspected carries an explanation that cites
  specifics (amount, date distance, vendor) rather than a generic sentence.
- **FAIL if**: zero matches, or explanations are blank or generic.
- **NOT-EXERCISED** (the approve action only): nothing was pending because every match was
  auto-approved.

---

## Phase 3: Exports

All of Phase 3 runs on "Reports" → 2026 → January. For each export, read the response with
`browser_network_requests`: status, `content-type` and size are the evidence — "a download started"
is not.

### Step 3.1: PDF report
**Action**: "Export" → "PDF Report".
- **PASS if**: `GET /api/reports/2026/1/pdf` → 200, `application/pdf`, non-empty.

### Step 3.2: Audit binder
**Action**: "Export" → "Audit Binder (ZIP)".
- **PASS if**: `GET /api/reports/2026/1/binder` → 200, a zip content type, non-empty. The binder is
  unencrypted by design (it is what gets handed to an auditor): no password prompt, ever.
- **FAIL if**: a 500, an empty body, or an encrypted archive.

### Step 3.3: Accounting-package CSVs
**Action**: "Export" → "QuickBooks CSV", then "Export" → "Xero CSV".
- **PASS if**: `…/export-qbo` and `…/export-xero` both answer 200 with a CSV body containing the
  month's rows.
- **FAIL if**: either errors, or a CSV sums CAD and USD rows into one total.

### Step 3.4: Sales tax summary
**Action**: on the 2026 year view, find "Sales Tax Summary"; click its "CSV" control.
- **PASS if**: the card shows a GST amount for January that includes the receipt's 1.80, and the
  CSV downloads.
- **NOT-EXERCISED if**: Step 2.2 did not produce a document.

### Step 3.5: A failed export says so
**Action**: open `/reports/2019/1` (a month with no data) and try "Export" if it is offered.
- **PASS if**: an empty state such as "No transactions for" that month renders, and any export
  attempt fails with a visible message rather than a blank file.
- **FAIL if**: the page crashes or shows another month's data.

---

## Phase 4: Closed months

The month-close endpoints exist (`POST /api/reports/{year}/{month}/close` and `/reopen`) and the
month grid shows a "Closed" marker, but this build has no close or reopen *button*. Drive it from
the page's own origin — still the browser, still the user's session:

### Step 4.1: Close January
**Action**: the app keeps its session in `localStorage` under `aa.session`
(`web/src/lib/auth-client.ts`):
```javascript
() => { const s = JSON.parse(localStorage.getItem('aa.session') || '{}');
        return fetch('/api/reports/2026/1/close', { method: 'POST',
          headers: { Authorization: 'Bearer ' + s.access_token } }).then(r => r.status); }
```
Never print the token itself into the evidence. If no token is readable from page context, record
4.1–4.3 NOT-EXERCISED and note that `runner.py`'s `close_month` and `reopen_month` cases cover the
endpoints.
- **PASS if**: 200 (or 409 if already closed), and the 2026 month grid then shows "Closed" on
  January after a reload.
- **FAIL if**: the endpoint succeeds but the grid never shows it.

### Step 4.2: Closed survives navigation
**Action**: go to `/dashboard` and back to the 2026 grid.
- **PASS if**: January still shows "Closed".

### Step 4.3: Reopen
**Action**: same call against `/reopen`.
- **PASS if**: 200 and the "Closed" marker is gone after a reload.

---

## Phase 5: Share with the client

### Step 5.1: Generate
**Action**: January → "Share" → "Generate Link". Record `SHARE_URL`.
- **PASS if**: a `/shared/` URL and "This link expires on" render.

### Step 5.2: Anonymous read is accurate
**Action**: clear storage, open `SHARE_URL`.
- **PASS if**: no login is demanded; the company name is **exactly**
  `Example Bookkeeping Professional Corp`; at least two of 40.32, 12.50, 89.99, 300.00, 9,600.00 and
  67.30 are visible.
- **FAIL if**: the name is truncated or different, or fewer than two amounts are recognisable.

---

## Phase 6: Audit trail

### Step 6.1: Every action left a trace
**Action**: sign in → "Activity" → "All time".
- **PASS if**: "Activity Log" holds timestamped entries for the statement upload, the receipt
  upload, the match run, and each approval made in Step 2.3.
- **FAIL if**: any action taken in this journey has no entry. List what was found and what was not.

### Step 6.2: Export it
**Action**: click "Download CSV".
- **PASS if**: `GET /api/reconciliation/audit-log/export` → 200 with a CSV body.
- **FAIL if**: "CSV export failed — please try again." or a 500.

---

## Phase 7: Account recovery

### Step 7.1: Forgot password is enumeration-safe
**Action**: sign out → `/login` → "Forgot password?" → submit `$AA_E2E_EMAIL` with "Send Reset
Link"; go "Back to sign in" and repeat with an address that does not exist at a reserved domain
(`nobody` at `example.invalid`).
- **PASS if**: both get the same "Check your inbox" confirmation.
- **FAIL if**: the two answers differ in any visible way.
- **NOT-EXERCISED**: following the emailed link through `/auth/reset-confirm`. Without `SMTP_HOST`
  configured no mail is sent. Do not change the test account's password to work around it.
- **If 429**: NOT-EXERCISED, "reset is rate-limited (5/hour); retry later".

---

## Phase 8: Cleanup
Clear `localStorage` and `sessionStorage`.

## Reporting
One row per step: step, what was done, PASS / FAIL / NOT-EXERCISED, and the evidence (status code,
content type, count, or the exact text seen). There is no SKIP.

# Journey: Functionality Completeness — the surfaces the personas leave behind

**Purpose**: close the coverage gaps of the four persona journeys — every public route, the auth
deep dive, every filter, the transaction slide-over's tools, Analytics, Activity, every Settings
tab, the dashboard widgets, error and empty states, a phone-sized viewport, keyboard access, and the
live event stream.

**Runs after**: at least the `basic` persona journey, so the account holds matched transactions and
receipts. Signs in as `$AA_E2E_EMAIL` / `$AA_E2E_PASSWORD` against `$AA_E2E_BASE_URL` throughout.

**Verdicts**: default FAIL; anything ambiguous is FAIL; no SKIP. What a test install legitimately
lacks — credentials for Gmail / Wise / PayPal, a mail transport, open registration — is
NOT-EXERCISED with the reason. "I couldn't find the button" is not a reason: snapshot again.

**Browser only**: everything goes through the `browser_*` tools. `browser_evaluate` calling `fetch`
from the page's own origin is still the browser.

**Per step**: run the action, take a screenshot, keep the raw tool output, hand all three to a
separate judge (`templates/judge-prompt.md`), and continue even on FAIL.

**Route coverage is the exit condition.** `web/src/App.tsx` is the list. When a route exists there
and no step in any journey touched it, name it as a gap in the report.

---

## Phase A: Public routes

### A.1 Landing, signed out
Clear storage, open `/`.
- **PASS if**: "Autonomous Accounting", "Self-hosted bookkeeping", a "Sign in" control and "Where the
  data lives" (Hardware, Certification, Encryption at rest, Backups) render, with no sidebar.
- **FAIL if**: the custody section is missing or softened into marketing ("enterprise-grade",
  "encrypted cloud storage"), or any price, trial or subscribe language appears. The project takes
  no payment and hosts nothing.

### A.2 Footer and the other public pages
Click "Privacy" then "Terms" in the footer; then open `/sample-report` directly.
- **PASS if**: all three render real content; the sample report is labelled "SAMPLE" and shows
  `Example Corp Inc.` data only.
- **FAIL if**: any renders "Page not found" or blank.

### A.3 Landing, signed in
Sign in, open `/`.
- **PASS if**: "Go to Dashboard" is offered.

### A.4 Unknown route
Open `/this-route-does-not-exist-12345`.
- **PASS if**: "Page not found" with "Go back" and "Return home". No stack trace.

---

## Phase B: Auth deep dive

### B.1 Forgot password
Sign out, `/login` → "Forgot password?".
- **PASS if**: the heading becomes "Reset password", one email field, a "Send Reset Link" button.

### B.2 Inline validation
Type `notanemail`, click "Send Reset Link".
- **PASS if**: "Please enter a valid email address." appears and nothing is submitted.

### B.3 Enumeration safety
Submit the test account's address, then an address nobody owns at a reserved domain.
- **PASS if**: identical "Check your inbox" confirmations.
- **NOT-EXERCISED**: the emailed link (no mail transport unless `SMTP_HOST` is set); a 429.

### B.4 Registration state
Read `signup_enabled` from `/health` (page origin), then open `/login?mode=signup`.
- **PASS if**: closed → "This instance is not accepting new accounts." and no sign-up form; open →
  a sign-up form. The page and the server must agree.
- **NOT-EXERCISED**: the password-strength rules, while registration is closed.

### B.5 Google button
- **PASS if**: "Sign in with Google" is present exactly when `/api/auth/google/status` says enabled.
  Never drive the consent screen.

---

## Phase C: Transactions — filter, search, sort, page

Sign in, open `/transactions`.

| Step | Action | PASS if |
|---|---|---|
| C.1 | status filter → "Matched" | only Matched rows; the count is lower than "All Status" |
| C.2 | status filter → "Unmatched", then "Pending Review" | each list contains only that status |
| C.3 | month filter → January 2026 (default "All Months") | every row is dated 2026-01 |
| C.4 | account filter → first option after "All Accounts" | every row shows that account |
| C.5 | search `EXAMPLE RETAILER` | only rows containing it; then search `40.32` finds the same row |
| C.6 | "Min" / "Max" amount bounds | rows outside the bounds disappear |
| C.7 | click the date column header twice | the first row's date changes between the clicks |
| C.8 | pagination, if more than one page | "Showing" range advances; otherwise note one page only |
| C.9 | "Clear" | every filter resets and the unfiltered count returns |
| C.10 | press `j`, `k`, `Enter`, `Esc` with the table focused | row focus moves, the panel opens, the panel closes |

**FAIL** for any row of the table: the control is missing, does nothing, or leaks rows that do not
match.

---

## Phase D: The transaction panel's tools

Open a Matched row ("Transaction Details").

### D.1 Category change
Change the row's category through its category control.
- **PASS if**: the new category shows on the row and survives a reload.
- **FAIL if**: "Couldn't update category — change reverted." or the value reverts silently.

### D.2 Note and a supplementary document
In "Notes & Supplementary Docs", type a note, save, attach
`tests/fixtures/test_receipt_retailer.png`.
- **PASS if**: both persist after closing and reopening the panel; the attachment opens.

### D.3 Link two transactions
"Link another" (or "Link Transaction") → pick a second transaction → confirm.
- **PASS if**: both rows show a link indicator and "Cross-Statement Linkage" lists the other. Then
  remove the link and confirm it is gone.

### D.4 Receipt viewer opens safely
Open the matched receipt in a new tab ("Open in new tab"); find the tab with `browser_tabs`.
- **PASS if**: the document renders, and in that tab `window.opener === null` and
  `document.referrer === ''`.
- **FAIL if**: the opened tab can reach back into the app window.

### D.5 Delete a receipt
In "Uploaded Files" → "Receipts & Invoices", delete one matched receipt ("Delete receipt?").
- **PASS if**: "Receipt deleted." and its transaction is listed as Unmatched again — not deleted
  with it.
- **FAIL if**: the transaction vanished (cascade) or still claims a match.

### D.6 Delete a statement
Delete one statement ("Delete statement?").
- **PASS if**: exactly that statement's transactions go, and the toast names how many.

---

## Phase E: Review extras

### E.1 Empty states tell the user what to do
Open each of the three tabs on `/review`.
- **PASS if**: an empty tab explains itself and points at "Match Receipts" rather than rendering a
  blank list.

### E.2 Reject and reassign
On a matched row, "Reject & Reassign Receipt" → pick another receipt.
- **PASS if**: the transaction ends up linked to the chosen receipt and the old receipt is free.
- **NOT-EXERCISED if**: only one receipt exists.

### E.3 Undo
Approve a pending match, then use the undo offered in the toast.
- **PASS if**: "Undone — the match is back in the queue." and it is.
- **NOT-EXERCISED if**: nothing is pending.

### E.4 Confidence explanation
Open "How confidence works".
- **PASS if**: HIGH / MEDIUM / LOW are explained in words, and the numbers agree with
  `reconciliation.auto_approve_threshold` and `review_threshold` from `/health`.

---

## Phase F: Analytics

### F.1 Page
Sidebar → "Analytics".
- **PASS if**: "Where your money went" with summary tiles, a month-by-month trend and a category
  table. Totals in different currencies are shown separately, never summed.

### F.2 Range
Change the range to January 2026.
- **PASS if**: tiles and table re-render for that month; an empty range reads "No transactions in
  this range".

### F.3 Drill-down
Click a category row.
- **PASS if**: `/transactions` opens "Filtered by category:" that category, with "Clear filter".

### F.4 Peer comparison
- **PASS if**: with no benchmark table built (`config/ised_benchmarks.json` absent) the
  reasonableness card is absent or says why; it never invents a comparison.
- **NOT-EXERCISED**: the verdicts themselves, unless the operator built the table.

---

## Phase G: Activity

### G.1 Ranges
"This month", "Year to date", "All time".
- **PASS if**: the list changes accordingly; an empty range reads "No activity in this date range".

### G.2 Entries are specific
- **PASS if**: entries carry an action label (Approved, Rejected, Unmatched, Match Run, Statement
  Uploaded, Receipt Uploaded, …), a timestamp and what they acted on.

---

## Phase H: Settings and Security

### H.1 Profile validation
"Settings" → "Profile": clear "Company Name", click "Save Profile".
- **PASS if**: the save is refused or disabled. Record which.
- **FAIL if**: a blank name is saved. Restore the name afterwards either way.

### H.2 Connections
- **PASS if**: Gmail, Wise, PayPal and Amazon cards render with a status; PayPal and Amazon explain
  the CSV-import route and link to "Upload Statements".

### H.3 Security tab and page
"Security" tab, then open `/security`.
- **PASS if**: "What protects your data" renders and distinguishes what the software does from what
  depends on how the instance is hosted. It claims no certification.

---

## Phase I: Dashboard widgets

| Step | Action | PASS if |
|---|---|---|
| I.1 | "Previous month" / "Next month" | the summary cards refetch for that month |
| I.2 | status banner action | it navigates to `/review` or a filtered `/transactions` |
| I.3 | "Monthly Status" year arrows | the grid changes year; months show Resolved / Unresolved |
| I.4 | "Quick Export" | offers PDF Report, ZIP Audit Binder, Full Year ZIP; a failure says "Export failed" |
| I.5 | "Recent Transactions" | lists the latest rows with Matched / Unmatched badges |
| I.6 | "What does this measure?" | explains the documented share and links the CRA guidance |

---

## Phase J: Errors, empty states, limits

### J.1 Empty month
Open `/reports/2019/1`.
- **PASS if**: an empty state; no crash; no other month's data.

### J.2 Oversized upload
Read `limits.max_upload_mb` from `/health`. Build a file just over it in the page
(`new File([new Uint8Array(n)], 'big.pdf')`) and hand it to the receipts input via a `DataTransfer`.
- **PASS if**: "This file is too large. This server accepts up to … MB." before any upload starts,
  or the server's 413 is surfaced.
- **FAIL if**: the UI hangs or the file is accepted.

### J.3 Offline
Patch `window.fetch` to reject, trigger a refetch, snapshot, then restore it.
- **PASS if**: widgets show "Couldn't load" with "Retry" (or an error toast); no white screen.

### J.4 Console
`browser_console_messages` at error level at the end of the phase.
- **PASS if**: none, other than the benign `ResizeObserver loop` message and the errors J.3 caused
  on purpose.

---

## Phase K: Small screens and keyboard

### K.1 375 × 812
`browser_resize`, then `/dashboard`, `/transactions`, `/login`.
- **PASS if**: no horizontal page scroll; the sidebar is collapsed behind its trigger; the table
  scrolls inside its container; the login form is fully usable.
Restore 1280 × 800 afterwards.

### K.2 Tab order
On `/dashboard` press Tab ten times, reading `document.activeElement` each time.
- **PASS if**: focus moves through visible controls in order and is always visible.

### K.3 Dialog focus
Open "Share" on a report month; press Tab ten times; press Escape.
- **PASS if**: focus never leaves the dialog, and Escape closes it and returns focus to "Share".

---

## Phase L: Live updates

### L.1 The event stream drives the UI
On `/transactions`, upload a fresh receipt and watch `browser_network_requests`.
- **PASS if**: a request to `/api/events/stream` (after `POST /api/events/ticket`) is open, and the
  queue and the receipt list update without a reload.
- **FAIL if**: the list only changes after a manual refresh.

---

## Phase M: Cleanup
"Sign out", clear storage, close extra tabs.

## Legitimate NOT-EXERCISED reasons
- A credential or transport the install does not have (Gmail, Wise, PayPal, SMTP).
- A second account is needed and registration is closed.
- The LLM endpoint is unreachable — name `LLM_BASE_URL` and what `/health` reported.
- State consumed by an earlier step (nothing pending to approve).

Not legitimate: a control that was hard to find, a step that was slow, a failure that also happened
last time.

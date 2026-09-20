# Journey: Power User

**Persona**: Example Digital Services Inc. — reviews every match individually, corrects extraction,
shares the month with an accountant
**Account / base URL / fixtures**: as in `journey-basic.md` — `$AA_E2E_EMAIL`, `$AA_E2E_PASSWORD`,
`$AA_E2E_BASE_URL`, and a fresh `$FIXTURES` from `lib/fixtures.py --out <dir>`. The account's data
is reset before this journey.

**Optional integrations**: Gmail, Wise, PayPal and Amazon are optional and a test install has no
credentials for them. An integration step passes on the app *handling that honestly* — a card that
reads "Not connected", a refusal in words a person can act on — and is NOT-EXERCISED for the leg
that needs a live credential. A crash, a silent no-op, or a card claiming "Connected" with nothing
behind it is a FAIL.

---

## Phase 1: Sign in and set the profile

### Step 1.1: Sign in
**Action**: clear storage, `browser_navigate` → `$AA_E2E_BASE_URL/login`, fill the credentials, click
"Sign in".
- **PASS if**: the heading "Welcome back" rendered before sign-in and the URL becomes `/dashboard`
  or `/onboarding` within 15s.
- **FAIL if**: the credentials are rejected or nothing happens. Hard FAIL; fix the credential.

### Step 1.2: Set the company name in Settings
**Action**: sidebar → "Settings" → "Profile" tab. In "Business Profile", set "Company Name" to
`Example Digital Services Inc.` and click "Save Profile".
- **PASS if**: "Saved" appears; after `browser_navigate` → `/settings` again the field still holds
  the new name.
- **FAIL if**: "Failed to save. Please try again." appears or the value reverts after reload.

---

## Phase 2: Integrations say what they are

### Step 2.1: Connections inventory
**Action**: "Connections" tab. Snapshot.
- **PASS if**: cards for Gmail, Wise, PayPal and Amazon render and each unconfigured one reads "Not
  connected".
- **FAIL if**: a card is missing, or one reads "Connected" on an account that never connected it.

### Step 2.2: Wise refuses a token that is not one
**Action**: on the Wise card, enter `not-a-real-token` in the access-token field and submit.
Wait up to 15s.
- **PASS if**: the card shows a readable refusal and stays "Not connected".
- **FAIL if**: the card flips to "Connected", hangs, or shows a raw error body.
- **NOT-EXERCISED if**: the machine has no route to the internet — the endpoint then answers that
  Wise is unavailable, which is also honest; record which message appeared. The successful
  connection is always NOT-EXERCISED here: no token exists on a test install.

### Step 2.3: Google sign-in offer matches the server
**Action**: from the page's own origin:
`() => fetch('/api/auth/google/status').then(r => r.json())`. Then sign out, open `/login`, snapshot.
- **PASS if**: the "Sign in with Google" button is present exactly when the endpoint reports it
  enabled. Never click it: it needs a real consent screen and proves nothing the password route
  does not.
- **FAIL if**: the button and the endpoint disagree, or the endpoint errors.
Sign back in before continuing.

---

## Phase 3: Data in

### Step 3.1: Statement
**Action**: `/transactions` → "Upload Statements" → `$FIXTURES/e2e_journey_bank_cad.csv`.
- **PASS if**: 6 new rows appear and the file is listed under "Statements".
- **FAIL if**: fewer rows, an error, or no listing.

### Step 3.2: Three documents, all of which must extract
**Action**: through "Upload Receipts & Invoices", upload in turn
`$FIXTURES/e2e_journey_receipt_<tag>.pdf`, `tests/fixtures/test_invoice_contractor.pdf` and
`tests/fixtures/test_receipt_retailer.pdf`. Follow each through the processing queue.
- **PASS if**: every one of the three ends as a document with a vendor and a total. Record a table:
  file, vendor, total, final state.
- **FAIL if**: any one fails or lands without vendor or total. "At least one worked" is a FAIL.
- **Note**: the two repository fixtures print "This document is a synthetic test fixture" on their
  face. If the extractor answers that one of them is not a financial document, record that verdict
  with the reason it gave — it is a finding about the extractor's handling of the disclaimer, and
  the generated receipt (which carries none) is the control.

---

## Phase 4: Individual review

### Step 4.1: Match
**Action**: "Match Receipts"; follow the progress panel to the result.
- **PASS if**: "Reconciliation complete:" with at least 1 match.
- **FAIL if**: error, no result, or zero matches.

### Step 4.2: Record the tab counts
**Action**: sidebar → "Review". Record the counts on "Auto-matched", "Needs Review", "No Match".
- **PASS if**: all three tabs render and at least one count is non-zero.

### Step 4.3: Approve one match by hand
**Precondition**: "Needs Review" is non-empty. If it is empty, 4.3–4.5 are NOT-EXERCISED with the
reason "every match scored above the auto-approve threshold" — do not invent a pending match.
**Action**: open "Needs Review", click the first row, record its description, click "Approve".
- **PASS if**: "Match approved." appears, the "Needs Review" count drops by exactly 1 and that
  description is now under "Auto-matched".
- **FAIL if**: the count is unchanged or the match is in neither tab.

### Step 4.4: Reject one
**Action**: next row in "Needs Review": record it, click "Reject".
- **PASS if**: "Match rejected." appears, the count drops by 1, and on `/transactions` with the
  status filter on "Unmatched" that transaction is listed again.
- **FAIL if**: the transaction stays matched, or disappears from both lists.

### Step 4.5: Keyboard review
**Action**: back on "Needs Review" with a row selected, press `?` and snapshot the "Keyboard
Shortcuts" overlay, press Escape, then press `ArrowDown` and `a`.
- **PASS if**: the overlay lists the shortcuts, `ArrowDown` moves the selection, and `a` approves
  the *selected* match (verify by description, not by "a count changed").
- **FAIL if**: a key does nothing while matches are pending.

### Step 4.6: Correct an extracted field, and it persists
**Action**: select any match, click "Edit" on the receipt side, change "Vendor" to
`Example Vendor Correction`, save. Navigate to `/dashboard` and back to `/review`, reselect it.
- **PASS if**: "Receipt details saved." appeared and the corrected vendor is still shown after the
  round trip.
- **FAIL if**: the value reverts.

### Step 4.7: Unmatch an approved match
**Action**: "Auto-matched" → select a row → "Unmatch" → in "Unmatch this transaction?" click
"Confirm Unmatch".
- **PASS if**: the match leaves the tab and the transaction is listed under "Unmatched" on
  `/transactions`.
- **FAIL if**: the dialog never opens, or the transaction stays matched.

---

## Phase 5: Report and share

### Step 5.1: Open January 2026
**Action**: sidebar → "Reports" → 2026 → January.
- **PASS if**: the month view renders the uploaded rows with Income, Expenses and Net totals.

### Step 5.2: Create a share link
**Action**: click "Share" → in "Share Report" click "Generate Link". Read the URL from the dialog
(`() => [...document.querySelectorAll('input')].map(i => i.value).find(v => v.includes('/shared/'))`).
- **PASS if**: a URL containing `/shared/` is shown with "This link expires on" and a date. Record
  it as `SHARE_URL`.
- **FAIL if**: "Failed to generate share link. Please try again." or no URL.

### Step 5.3: Read it with no session
**Action**: clear storage, `browser_navigate` → `SHARE_URL`. Snapshot.
- **PASS if**: "Shared Report" renders without a redirect to `/login`, shows
  `Example Digital Services Inc.` exactly, shows at least one transaction row, and offers no edit,
  approve or delete control.
- **FAIL if**: a login is demanded, the company name is wrong or missing, or any mutating control is
  exposed to an anonymous visitor.

### Step 5.4: Revoke it
**Action**: sign in again → "Reports". Under "Shared Report Links" click "Revoke" and confirm
"Revoke shared link?". Clear storage and open `SHARE_URL` again.
- **PASS if**: the page now reads "Report unavailable" / "This share link may have expired or is
  invalid." and shows no report content.
- **FAIL if**: the report still renders after revocation.

---

## Phase 6: Cleanup
Sign in if needed, restore nothing (the next reset clears the account), sign out, and clear
`localStorage` and `sessionStorage`.

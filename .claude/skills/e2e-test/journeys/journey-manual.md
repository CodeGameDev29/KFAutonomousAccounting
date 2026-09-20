# Journey: Manual-Only Privacy Skeptic

**Persona**: Example Privacy Consulting — connects nothing, uploads everything by hand, and pokes
at every error path
**Account / base URL / fixtures**: as in `journey-basic.md` — `$AA_E2E_EMAIL`, `$AA_E2E_PASSWORD`,
`$AA_E2E_BASE_URL`, and a fresh `$FIXTURES` from `lib/fixtures.py --out <dir>`. The account's data
is reset before this journey.

---

## Phase 1: Sign in

### Step 1.1: Sign in
**Action**: clear storage, open `$AA_E2E_BASE_URL/login`, fill the credentials, click "Sign in".
- **PASS if**: the URL becomes `/dashboard` or `/onboarding` with no error shown.
- **FAIL if**: it stays on `/login`. There is no sign-up fallback while registration is closed; a
  rejected credential is a setup problem to fix.

### Step 1.2: The login page makes the privacy claims the app can back
**Action**: before signing in (or after signing out), snapshot `/login`.
- **PASS if**: it states that data is stored on the machine the instance runs on and that there is
  no analytics or telemetry in the application.
- **FAIL if**: it claims a cloud provider, a certification or encryption the install does not have.

---

## Phase 2: Files that must be refused

### Step 2.1: Unsupported extension
**Action**: `/transactions` → "Upload Receipts & Invoices". Remove the input's `accept` attribute so
the browser lets the file through:
`() => document.querySelectorAll('input[type="file"]').forEach(i => i.removeAttribute('accept'))`.
Upload `tests/fixtures/test_unsupported.txt`.
- **PASS if**: a message containing "Unsupported file type" and the list after "Supported:" appears,
  and no document is created.
- **FAIL if**: the message is generic ("Upload failed"), or the file is accepted.

### Step 2.2: A text file wearing a `.pdf` name
**Action**: upload `$FIXTURES/e2e_journey_not_a_pdf.pdf` through the receipts control.
- **PASS if**: it is refused (the server checks magic bytes) with a message a person can act on,
  and the receipt count is unchanged.
- **FAIL if**: it is queued for extraction, or the error shows a stack trace or a file path.

### Step 2.3: A receipt handed to the statement pipeline
**Action**: "Upload Statements" → `tests/fixtures/test_receipt_retailer.pdf`. This goes through the
statement detector and may take a minute.
- **PASS if**: the app says the file "does not appear to be a bank statement" and imports no
  transactions.
- **FAIL if**: transactions are invented from a retail receipt, or the failure is silent.
- **NOT-EXERCISED if**: the LLM endpoint is unreachable and the upload is parked or refused for that
  reason — record the message.

---

## Phase 3: Statements that must import

### Step 3.1: CSV
**Action**: "Upload Statements" → `$FIXTURES/e2e_journey_bank_cad.csv`. Record the transaction count
afterwards as `COUNT_AFTER_CSV`.
- **PASS if**: exactly 6 rows arrive.

### Step 3.2: The same CSV again
**Action**: upload the identical file a second time.
- **PASS if**: the transaction count is still `COUNT_AFTER_CSV` and the UI says the rows were
  already on file (a same-name statement replaces its own rows; identical rows are duplicates).
- **FAIL if**: the count doubles.

### Step 3.3: PDF statement
**Action**: "Upload Statements" → `tests/fixtures/test_bank_statement.pdf`. The PDF path is parsed by
the LLM in a background job; follow the processing queue to a terminal state.
- **PASS if**: the statement finishes as imported, the transaction count rises above
  `COUNT_AFTER_CSV`, and the new rows include `HARBOURLINE LOGISTICS`. The opening-balance and
  closing-total lines of the statement must **not** appear as transactions.
- **FAIL if**: the parse fails, or the balance rows are imported as transactions, or the `AT1.3500`
  FX token is read as a second amount.
- **NOT-EXERCISED if**: the LLM endpoint is unreachable (the job waits for the model). Say so.

---

## Phase 4: Receipts by hand

### Step 4.1: Upload and read back
**Action**: "Upload Receipts & Invoices" → `$FIXTURES/e2e_journey_receipt_<tag>.pdf`; follow the
queue.
- **PASS if**: the document lands with vendor Example Retailer, total 40.32, date 2026-01-18.
- **FAIL if**: any of the three is missing or wrong — a wrong number is worse than a blank.

### Step 4.2: Duplicate
**Action**: upload the same PDF again.
- **PASS if**: it is marked duplicate and the receipt count does not change.

---

## Phase 5: Match, then look at what did *not* match

### Step 5.1: Match
**Action**: "Match Receipts"; wait for the result.
- **PASS if**: at least 1 match is reported.

### Step 5.2: Unmatched rows are visibly unmatched
**Action**: status filter → "Unmatched". Click one row.
- **PASS if**: the "Transaction Details" panel reads "No receipt matched to this transaction." and
  offers "Match Manually".
- **FAIL if**: an unmatched row shows a receipt, or matched and unmatched rows are indistinguishable
  in the table.

### Step 5.3: Manual match and undo
**Action**: click "Match Manually", pick the generated receipt in the dialog if it is still free
(otherwise any unlinked receipt), confirm. Then open the same row and click "Unmatch", confirm.
- **PASS if**: the row becomes Matched with that receipt, then returns to Unmatched.
- **NOT-EXERCISED if**: no unlinked receipt exists to pick — say so rather than forcing one.

### Step 5.4: Internal lines are excluded, not force-matched
**Action**: clear the filter and find the statement's interest or fee line (from Step 3.3) if the
PDF imported.
- **PASS if**: its status reads "Excluded" or "No receipt needed" rather than an unmatched expense
  demanding a receipt.
- **FAIL if**: a bank fee or interest line was matched to a receipt, or counts as missing one.
- **NOT-EXERCISED if**: Step 3.3 was not exercised.

---

## Phase 6: Nothing is connected

### Step 6.1: Connections stay off
**Action**: sidebar → "Settings" → "Connections".
- **PASS if**: Gmail, Wise, PayPal and Amazon all read "Not connected".
- **FAIL if**: any card reads "Connected" for this account.

### Step 6.2: The page talked to nobody else
**Action**: `browser_network_requests` for the whole session.
- **PASS if**: every request went to the app's own origin. The app ships no webfonts and no
  analytics; the Content-Security-Policy allows no third-party origin except Google's sign-in
  endpoints, which this journey never touches.
- **FAIL if**: any request left for a font host, a CDN, an analytics or an error-tracking service.

---

## Phase 7: Cleanup
Sidebar → "Sign out" (PASS if the URL becomes `/` or `/login`), then clear `localStorage` and
`sessionStorage`.

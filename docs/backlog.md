# Backlog

Open engineering items, stated plainly so nobody has to rediscover them; each names the
file to start from. The *Security hardening* section lists what a pre-release review found
and did not fix. None of it is reachable on the install this project is built for — one
owner, loopback only, registration closed — and each entry says what changes that.

## Engine correctness

- **Wire-fee tolerance applies to every same-currency pair.** `core/amount_match.py`
  (`WIRE_FEE_TOLERANCE`) raises the amount score of any pair within the tolerance, not
  only wires. It can inflate amount evidence for small same-currency pairs that the
  confidence floor in `core/reconciliation.py` exists to catch. Scope it to descriptions
  the wire keywords match.
- **Dismissed reasonableness flags for vehicle and meals.** The flag keys written on
  dismissal and the keys checked when building the comparison rows differ for these two
  lines in `core/reasonableness.py`, so a dismissed flag does not suppress its own row.
- **Statement year inference uses the wall clock.** `core/pdf_statement_parser.py`
  (`_parse_date`, `_parse_bmo_business_pdf`) falls back to the current year when a
  statement's header year cannot be read, so the parsed year depends on the upload date.
  Prefer failing the parse, or the year of the statement period when one is present.
- **Industry inference from data.** `core/reasonableness.py` (`_infer_naics_from_data`)
  infers a NAICS code from three hand-written spending signatures (restaurant,
  construction, software/IT). Each is one threshold pair with no test of its own; an
  explicit industry on the profile always wins.
- **QuickBooks and Xero CSVs ignore the stored category.** `server/api/reports.py` (the
  `qbo` and `xero` export routes) rebuilds categories with `_auto_categorize_transactions`
  — the regex pre-pass only — instead of reading what the categorization agent stored, so
  a row the agent categorized exports as `Uncategorized` with a blank GIFI code, and Xero
  flags it for review with no tax code even when a GST document is linked. The XLSX
  binder and the PDF report use the stored category. Found by running
  `scripts/gen_demo_data.py`'s month through the UI.
- **The extracted payment date never reaches the matcher.** `core/reconciliation.py`
  (`_extract_payment_date`, `_extract_alt_amount`) parses `extraction_raw_json` as JSON,
  then as quote-swapped repr; the stored value is a Python repr containing
  `ExtractionAttempt(...)` objects, which neither reads, so both helpers return nothing
  and matching falls back to the invoice date. Store real JSON at write time.
- **A three-letter token can score as a vendor match.** `compute_vendor_score` accepts any
  word of three letters or more, so `inc` from "Example Corp Inc." matches inside
  "INCOMING WIRE …" and scores 0.8. Exclude corporate suffixes and require a word
  boundary.
- **Pending-review pairs are reported as matched.** The post-run summary counts
  `PENDING_REVIEW` pairs under "matched" and reports zero pending; the XLSX shows the same
  rows as "Matched" with proof "Missing" until they are approved.
- **The tax summary adds tax collected to tax paid.** `GET /api/reports/{year}/tax-summary`
  sums GST over every linked document, including GST charged on the company's own
  outgoing invoices, into one figure. Split by document direction.
- **Own invoices need `OWN_COMPANY_PATTERNS`.** A document is treated as income only when
  its vendor contains one of those patterns; with the placeholder value every document is
  an expense and an invoice you issued can never match the wire that paid it. The server
  logs a warning, the UI says nothing.
- **Fallback keyword lists.** The date-gap and amount-discrepancy fallbacks in
  `core/match_validator.py` and the relevance scorer in `core/gather/doc_scorer.py` use
  generic keyword lists that have no dedicated tests.

## Security hardening

Read this before exposing an instance beyond loopback or opening registration to people
you do not know.

- **Upload bodies are parsed before authentication.** FastAPI reads a multipart body
  before it resolves `get_current_user`, and `MAX_UPLOAD_MB` is checked only after the
  file is in memory (`server/api/files.py`, `receipts.py`, `transactions.py`). On an
  instance published through a tunnel or proxy, an anonymous client can make the server
  spool arbitrarily large bodies. Put a request-body limit on the reverse proxy; the
  in-app fix is an ASGI middleware that rejects an over-limit `Content-Length` (sized for
  multi-file uploads) before the body is read.
- **Google sign-in joins an existing account by e-mail alone.** `server/google_auth.py`
  looks the account up by the verified Google address. With `AUTH_ALLOW_SIGNUP=1` and
  `AUTH_AUTOCONFIRM=1`, someone can register another person's address with a password of
  their own; if that person later signs in with Google they land in the account the first
  party still holds a password to. Not reachable while registration is closed. Decide
  between refusing Google sign-in for an account that has a password it did not prove, or
  clearing the password and revoking sessions on first Google sign-in.
- **OAuth `state` is bound to a user, not to a browser.** Gmail connect
  (`server/api/onboarding.py`) and Google sign-in store a single-use state but set no
  cookie tying the callback to the browser that started the flow. On a multi-user
  instance a consent link can be sent to another user, whose Gmail grant is then stored
  under the sender's account. Set an HttpOnly nonce cookie at start and require it on the
  callback.
- **The default rate limit is declared but not enforced.** `server/rate_limit.py` sets a
  default, but `server/app.py` never adds `SlowAPIMiddleware`, so only the explicitly
  decorated routes are limited; `/api/auth/refresh`, `/confirm` and `/api/files/blob` are
  not. Behind a proxy, run uvicorn with `--proxy-headers --forwarded-allow-ips 127.0.0.1`
  (as `scripts/local/start-aa.ps1` does) or every client shares one bucket.
- **Session-grade tokens are stored unhashed.** `auth.refresh_tokens.token`,
  `recovery_token` and `confirmation_token` (`db/local_auth_schema.sql`) are plaintext, so
  a database dump — including the one `scripts/local/backup-aa.ps1` writes — is a set of
  live credentials. Confirmation tokens also never expire (`server/local_auth.py`,
  `confirm`). Store SHA-256 digests and check `confirmation_sent_at`.
- **One secret, several jobs, no minimum strength.** `AUTH_JWT_SECRET` signs sessions and,
  unless the dedicated variables are set, also keys signed URLs, OAuth state and
  credential encryption; a short value is accepted at boot. Generate it as the README
  says, set `STORAGE_URL_SECRET` and `CREDENTIAL_ENCRYPTION_SECRET` separately, and
  consider refusing anything under 32 characters at startup.
- **The documented DSN is the `postgres` superuser.** Row-level security is enforced per
  transaction with `SET LOCAL ROLE authenticated`, which a SQL injection could `RESET`.
  None is known, but a dedicated `NOSUPERUSER` owner role would bound the damage.
- **Smaller items.** `get_current_user` does not re-check `is_active`, so a deactivated
  account keeps access until its access token expires (an hour);
  `/api/auth/update-password` does not ask for the current password; `/health` is
  unauthenticated and names the LLM base URL and which provider keys are set;
  `_sanitize_filename` (`core/file_storage.py`) does not strip `:` `<` `>` `|` `?` `*`,
  which on Windows can address an alternate data stream; a signed download URL minted for
  a share link stays valid for its TTL after the link is revoked; uploaded XLSX files are
  parsed by openpyxl without `defusedxml`.

## Dead or inert code

- **`core/categorization.py` has no callers.** `config/vendor_rules.yaml`'s `category`
  and `priority` fields are therefore inert; only `vendor_pattern` is read (by
  `core/reconciliation.py`). Either wire rule-based categorization into the
  categorization agent's pre-pass or delete the module and the two fields.
- **Gather validators have no callers.** `core/gather/rules_validator.py`
  (`validate_gather_file`, `validate_rename_file`), `core/gather/orchestrator.py` and
  `core/gather/DOCUMENT_RULES.md` are referenced by nothing. If the gather pipeline is
  meant to call them, note that the personal-vendor rule discards a document outright
  where a review flag would be safer; otherwise delete them.
- **Excluded tests.** `pytest.ini` excludes eleven test files and thirteen classes or
  tests whose contract no longer matches the code (see the file's header). Each should be
  rewritten against the current interfaces or deleted; `tests/test_ledger.py` and
  `tests/test_html_binder.py` would close most of the remaining coverage gap.

## Application

- **No API documentation surface.** `server/app.py` fixes `DEPLOY_MODE` to `"cloud"`,
  which disables `/docs`, `/redoc` and `/openapi.json`. Add a setting that enables them
  for local development.
- **Onboarding message when the model is unreachable.** Covered for the queued path;
  confirm every extraction error code maps to a specific message in
  `web/src/pages/Onboarding.tsx` (`getExtractionDiagnostic`).
- **Transactions page empty-state.** The page can show "No statements uploaded" while the
  statements panel lists an imported statement; the two read different queries.
- **Bundle size.** The production build emits one ~1.6 MB JavaScript chunk; split by
  route.

## Verification gaps

- A later pass did reach a vision model (a local OpenAI-compatible endpoint) and drove the
  synthetic demo month from `scripts/gen_demo_data.py` through the UI and the API: all
  eight documents extracted with the right vendor, amount, date and currency; four pairs
  auto-approved, two sent to review (a six-day date gap, a USD invoice paid in CAD), fee,
  interest and transfer rows excluded; binder, PDF and CSV exports downloaded. What that
  run found wrong is listed under *Engine correctness* above. One model, one clean
  dataset: it is a smoke test, not an accuracy claim.

- The release was verified without a reachable LLM endpoint: upload, queueing, outage
  handling, deterministic parsing, reconciliation scoring, row-level security and every
  documented command were exercised; LLM extraction, the LLM statement parser, the
  categorization agent and the exported binder/report artifacts were covered by the unit
  suite only.

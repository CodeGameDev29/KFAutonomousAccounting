# Backlog

Open engineering items, stated plainly so nobody has to rediscover them. None is a
security hole; each names the file to start from.

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
- **Fallback keyword lists.** The date-gap and amount-discrepancy fallbacks in
  `core/match_validator.py` and the relevance scorer in `core/gather/doc_scorer.py` use
  generic keyword lists that have no dedicated tests.

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

- The release was verified without a reachable LLM endpoint: upload, queueing, outage
  handling, deterministic parsing, reconciliation scoring, row-level security and every
  documented command were exercised; LLM extraction, the LLM statement parser, the
  categorization agent and the exported binder/report artifacts were covered by the unit
  suite only.

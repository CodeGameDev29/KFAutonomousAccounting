# Testing

```bash
pytest                                        # from the repo root
ruff check .
cd web && npm run typecheck && npm run build
```

`scripts/gates.sh` runs all four in one go. There is no CI; these are the checks.

Dependency advisories are a fifth check worth running before a release, and neither tool
is pinned here: `pip-audit -r requirements.txt --no-deps` and, in `web/`, `npm audit`.
Both were clean when this tree was published.

## What `pytest` does about the database

It needs PostgreSQL and a disposable database, and it arranges both itself. It takes
`DATABASE_URL` from your shell, or — if the shell does not have one — parses it out of
`.env`, and then runs against a `_test` sibling of that database: `autonomous_accounting`
becomes `autonomous_accounting_test`. It creates that database if it does not exist, using
the same host, port and credentials, and lays down `local_auth_schema.sql`,
`schema_pg.sql`, any migrations and `local_grants.sql` in the Quickstart's order. It
never connects to the database named in `DATABASE_URL` itself, so it cannot touch your
books. Set `TEST_DATABASE_URL` to point somewhere else entirely; either way the
resolved name must end in `_test` or the suite refuses to start. If PostgreSQL is not
running at all, the tests that need it skip themselves and the rest still run.

`.env` is read for that one value and otherwise **not** applied to the test process:
the suite sets its own environment, so a run does not depend on how your install
happens to be configured.

Every fixture the suite reads is synthetic and generated — see `tests/fixtures/README.md`
and `scripts/gen_fixtures.py`. No real financial document or real book belongs in this
repository, in a test or anywhere else.

## What the synthetic fixtures do and do not exercise

The fixtures under `tests/fixtures/` are generated, deterministic and small. They exist so
the suite runs end to end — parse, match, categorise, export — with no network, no LLM
endpoint and nobody's real books. They do not prove the engine against reality. They do
not cover:

- **Scale.** Hundreds of rows, not the tens of thousands a few years of books produce.
  Matcher cost is superlinear in the candidate set.
- **Real bank-descriptor variety.** Every institution mangles merchant names
  differently — truncation, acquirer city tags, reference numbers, different casing per
  channel. The fixtures use a handful of clean shapes.
- **Real document layouts.** Generated PDFs are clean and typeset. Phone photos, faint
  thermal receipts, multi-page invoices with continuation pages and scanned statements
  are where extraction actually gets hard.
- **Non-Canadian tax.** GST/HST/PST and the ISED/GIFI taxonomy are wired in throughout.
  VAT, sales tax and any other regime are not modelled.

Expect to tune the rule files under `config/` and the reconciliation thresholds in `.env`
against your own statements before the output is trustworthy.

## Excluded tests

`pytest.ini` excludes eleven test files and thirteen more classes or tests. They assert a
contract the code does not have — imports that are gone, renamed database methods, a
different column order — and are excluded from collection rather than deleted. The header
of `pytest.ini` says so; nobody should add new tests to those files. What that costs in
coverage is small but real: `pytest --cov=core.ledger --cov=core.html_binder` reports 85%
for `core/ledger.py` and 91% for `core/html_binder.py`, and the uncovered lines in
`core/ledger.py` are mostly its accounting-package exports.

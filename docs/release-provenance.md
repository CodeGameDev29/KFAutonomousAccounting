# Release provenance

How this repository was produced.

This repository is a fresh tree with no history, built by an export pipeline that
scanned the source tree line by line, judged every hit, had the judgements reviewed by
the copyright holder, applied them mechanically, and then verified the result. What it
contains is the bookkeeping engine and the self-hosted application around it.

Nothing below names a value that was taken out. The point of this document is that you
can see the shape and the volume of the work without any of it being restated here.

## How it was produced

1. **Scan.** Every tracked file, plus its history, was searched for credentials,
   personal identifiers, private infrastructure, organisation references, real financial
   records, build artefacts, leaked context in comments, licence risks and places where
   the code was hard-wired to one private dataset.
2. **Judge.** Each hit became a finding with a category, a proposed verdict
   (`DROP` / `REDACT` / `REFACTOR` / `SHARE`) and a mechanical action
   (`drop_file` / `drop_line` / `replace` / `synthesize` / none).
3. **Review.** The copyright holder settled every finding that the rubric could not, and
   set the scope rules for the release. **Zero findings were left undecided** when the
   export ran.
4. **Export.** A copy of the tree was made with no `.git` directory, and the ledger was
   applied to it one finding at a time, each edit logged against its finding id.
5. **Verify.** The export was swept again for every value the ledger replaced and every
   path it dropped, installed into a clean virtualenv, bootstrapped against a throwaway
   PostgreSQL cluster, started, and exercised over HTTP.
6. **Commit.** One initial commit. There is no earlier history to dig through, because
   there is none here to dig.

## What the scan found, by category

1,713 findings across 461 distinct source paths. Counts only — no finding is described by
its content.

| Category | Findings | Verdicts |
|---|---:|---|
| `SECRET` — credentials, keys, tokens | 29 | 23 dropped, 6 redacted |
| `PII_AUTHOR` — identifiers of the author | 112 | 67 dropped, 45 redacted |
| `PII_THIRD_PARTY` — identifiers of other people | 59 | 29 dropped, 29 redacted, 1 kept |
| `PERSONAL_CONTEXT` — private circumstances, internal narrative | 218 | 156 dropped, 61 redacted, 1 kept |
| `INTERNAL_INFRA` — private hosts, paths, machine names | 278 | 184 dropped, 84 redacted, 10 kept |
| `ORG_REFERENCE` — the operating company and its vendors | 237 | 194 dropped, 35 redacted, 8 kept |
| `REAL_DATA` — real financial records and figures | 301 | 213 dropped, 82 redacted, 6 kept |
| `ARTIFACT` — build output, caches, generated files | 23 | 23 dropped |
| `COMMENT_LEAK` — private context inside code comments | 154 | 76 rewritten generically, 75 dropped, 3 kept |
| `LICENSE_RISK` — dependencies and derived code with obligations | 15 | 9 kept and attributed, 5 dropped, 1 redacted |
| `DATA_COUPLING` — code wired to one private dataset | 287 | 162 refactored behind a config seam, 119 dropped, 4 redacted, 2 kept |
| **Total** | **1,713** | 0 undecided |

By action: 978 findings resolved by dropping the file outright, 419 by an in-place value
substitution, 162 by refactoring a coupling point behind a config seam, 74 by dropping
individual lines, 38 by regenerating the file synthetically, 2 by a change with no single
line to point at, and 40 by review deciding the hit was safe to keep.

Those are the counts from the first pass. The export was then read back several more
times by a reviewer working only from the exported files, and each round resolved more.
Those rounds are not counted here, because a count of them, broken down, would start to
describe what was found.

This release is the bookkeeping engine and the self-hosted application around it:
tooling and documents outside that scope are not part of the tree.

No credential of any kind ships with this code: no key, token, password, connection string,
or credential file, and no git history that could contain one. Every secret the running
application needs is generated locally into a git-ignored `.env` — see `.env.example`.

## What was replaced with synthetic data

Where a test or a default needed data that happened to be real, the data was regenerated
rather than trimmed. The code paths are unchanged; only the values are.

| Kind | What it is now |
|---|---|
| Bank-statement fixtures (CSV and PDF) | Generated statements in the layouts the built-in parsers read, carrying the headers, descriptor shapes and edge cases those parsers key on — both currencies, and the quirks the code special-cases |
| Receipt and invoice fixtures (PDF) | Generated documents with invented vendors, line items, tax lines and totals; none corresponds to a real purchase |
| Categorisation rules (`config/vendor_rules.yaml`) | Rewritten with the same schema and obviously-generic example entries |
| Ledger and profit-and-loss figures inside tests | Invented amounts, chosen to exercise the assertion they sit under |
| Peer-benchmark sample | A small generated benchmark table, so the reasonableness engine and its tests run without the public dataset download |

Fixtures are deterministic (fixed seed) and regenerable with `scripts/gen_fixtures.py`.
See `tests/fixtures/README.md`.

The real peer-benchmark table is not a fixture and is not committed: it is public
government data, and `scripts/ingest_ised_benchmarks.py` downloads and rebuilds it. See
the README and `NOTICE`.

## What the application is

Every signed-in account has full access to every feature. The application is the
engine — extraction, reconciliation, categorization, ledger and reports — plus the
FastAPI server, the React app and the local identity service that make it usable on one
machine. `requirements.txt` and `web/package.json` list what it runs on;
`server/api/__init__.py` aggregates the feature routers and `server/app.py` mounts those
alongside the auth, files, events and share routers, so those two files are the whole
route table a running server serves.

## The schema ships as a single baseline

`db/schema_pg.sql` is the entire schema — every table, column, index, constraint,
trigger and row-level-security policy the application uses — written out once, in
dependency order, with a comment on each table saying what it is for. `db/migrations/`
ships empty apart from a README, and a change made after this release is a new file
there numbered from `001`.

Every table in the schema is reachable from code in this repository, and the schema
defines no view at all, because a view over these tables runs with its owner's
privileges unless it is created `WITH (security_invoker = true)`. `authenticated` is the
only role `db/local_grants.sql` grants on, and the application connects as the cluster
owner and switches down to it for every request.

Every table carries row-level security with one policy, `USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid())` — both halves, so a signed-in session can neither
read nor write another account's row. The two identity tables, `auth.users` and
`auth.refresh_tokens`, are covered the same way and the `authenticated` role holds no
privilege on them at all: the code that reads them connects as the database owner.
`tests/test_rls_enforcement.py` enumerates both schemas from the catalogue and fails if
any table lacks a policy, if a write-capable policy lacks `WITH CHECK`, or if a session
whose `auth.uid()` owns nothing can see a row or insert one under another id. The one
table without row-level security is the migration ledger, which holds no user data and
which `authenticated` cannot read.

The schema seeds no application data. A fresh install starts with zero rows in every
table except the single baseline marker in the migration ledger; the
category taxonomy is written per account, from `config/ised_categories.py`, the first
time an account needs it.

## Not a security claim

This document describes an export process, not a guarantee. It is an honest account of
what was done, offered so you can judge it — not a warranty that nothing slipped through.
If you find something in this repository that should not be here, please open an issue
saying only where it is, not what it says.

# Autonomous Accounting

Self-hosted bookkeeping for a small Canadian corporation. You give it receipts,
invoices and bank statements; an LLM reads them, a multi-pass matcher reconciles the
documents against the bank lines, a categorization agent assigns CRA/GIFI categories,
and the result is a ledger you can export as an XLSX workbook, a PDF report, or an
unencrypted audit binder with every source document attached.

Everything runs on one machine you own: PostgreSQL, the file store, the identity
service and the web server. There is no account to create with anyone, no vendor to
pay, and no copy of your books anywhere but your own disk.

## What it is

- **Extraction** — receipts, invoices and PDF bank statements are rasterised and read by
  a vision LLM into structured JSON (vendor, date, amount, tax, currency), totals
  cross-checked against line items. Uploads are queued and answered `202`; a background
  worker drains the queue, so dropping half a year of documents at once cannot time out
  and a restart re-queues whatever was mid-flight.
- **Reconciliation** — four passes over the unmatched set: strict same-currency, an
  FX-relaxed pass inside a configurable band, an aggressive pass for splits and
  installments, and an LLM pass that links across accounts. Bank fees, interest and
  internal transfers are excluded rather than force-matched; anything uncertain is
  flagged for review instead of guessed.
- **Categorization** — a deterministic regex pre-pass (`config/category_rules.yaml`)
  locks what it recognises, vendor clustering normalises descriptors, and only what is
  left reaches the model, under a per-run call budget.
- **Ledger and reports** — monthly and annual ledgers, a GST/HST/PST tax summary, a PDF
  report, a QuickBooks-style and a Xero-style CSV, a shareable read-only link, and a ZIP
  audit binder (XLSX workbook + `proof_of_transaction/` documents + a self-contained
  HTML report). Amounts in different currencies are never summed into one total.
- **Peer benchmarks** — an optional "reasonableness" pass compares your expense ratios
  against published Canadian industry data and explains what stands out.

## What it is not

- **Not a hosted service.** Nothing to sign up for. You run it or it does not run.
- **Not something you buy.** The software takes no payment. Every signed-in account has
  full access to every feature.
- **Not supported.** No warranty, no SLA, no security response commitment, no promise
  that an upgrade will not want a manual migration. See the licence.
- **Not a filing service.** It produces a ledger and reports for a Canadian small
  corporation. It files nothing with the CRA and it is not accounting advice. Check the
  numbers before you use them.

## Architecture

```
upload / gather ─► extract (LLM) ─► reconcile ─► categorize ─► ledger + reports
                        │               │            │              │
                   queued jobs      4 passes    rules → LLM     XLSX / PDF /
                   (SSE progress)                               CSV / ZIP binder
```

| Layer | What runs |
|---|---|
| API + web app | FastAPI on uvicorn, port `8080` by default; serves `/api/*` and the built SPA from one origin |
| Database | PostgreSQL with row-level security keyed on the signed-in user |
| File storage | A directory on local disk, served only through short-lived HMAC-signed URLs |
| Frontend | React 19 + Vite + TypeScript, Tailwind, TanStack Query, Zustand |
| Extraction | Any **OpenAI-compatible, vision-capable** endpoint you point `LLM_BASE_URL` at — vLLM, llama.cpp, Ollama's `/v1`, LM Studio. Gemini and OpenAI are optional fallbacks that stay inert while their keys are empty |
| Auth | Local email + password (`server/local_auth.py`): HS256 access tokens, rotating refresh tokens, PBKDF2-HMAC-SHA256 hashes. Sign in with Google is optional |

This project hosts no model of its own. If the endpoint is unreachable, extraction jobs
park in `waiting_for_model` and resume by themselves; a direct upload returns HTTP 503
and says so. Nothing silently falls back to a paid provider.

## Quickstart

Prerequisites: **Python 3.11+**, **Node 20+**, **PostgreSQL 15+** reachable on loopback,
and an OpenAI-compatible vision LLM endpoint. (Nothing in `db/` needs a server feature
newer than PostgreSQL 13; 15 is just the oldest release still maintained upstream.)

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # Windows: copy .env.example .env
```

Fill in three values in `.env`. Generate each secret exactly as `.env.example` says:

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"   # AUTH_JWT_SECRET
python -c "import secrets;print(secrets.token_urlsafe(48))"   # STORAGE_URL_SECRET
```

and set `DATABASE_URL` to the database you are about to create.

Create the database and lay down the schema **in this order** — it is not
interchangeable:

```bash
createdb autonomous_accounting
psql -d autonomous_accounting -f db/local_auth_schema.sql
psql -d autonomous_accounting -f db/schema_pg.sql
python -m db.migrate
psql -d autonomous_accounting -f db/local_grants.sql
```

`local_auth_schema.sql` must run first: it creates the `auth` schema, `auth.uid()` and
the `authenticated` role, all of which `schema_pg.sql` needs. `schema_pg.sql` is the
whole schema — every table, index, trigger and row-level-security policy — as a single
baseline; `db/migrations/` ships empty and `python -m db.migrate` therefore has nothing
to do on a fresh install, but run it anyway so anything added after the baseline is
picked up. `local_grants.sql` must run last: it grants on the tables the steps above
created, and without it every query fails with *permission denied*.

Each step is idempotent, so re-running the sequence on an existing database changes
nothing.

Run the backend from the repo root:

```bash
python -m uvicorn server.app:app --port 8080
```

Then build the frontend — FastAPI serves the bundle from the same origin — or start the
Vite dev server, which proxies `/api` to the backend:

```bash
cd web
npm ci
npm run build          # production bundle into web/dist
npm run dev            # …or the dev server, on http://localhost:5174
```

`GET /health` on the backend reports database, storage and LLM reachability.

**Creating the first account.** Registration is closed by default
(`AUTH_ALLOW_SIGNUP=0`) so that an install on your own machine is not an open invitation
to fill your disk. Set `AUTH_ALLOW_SIGNUP=1`, restart, sign up in the browser, then set
it back to `0`. `AUTH_AUTOCONFIRM=1` (the default) means the account works immediately
without any mail transport.

### Peer-benchmark data (optional, one command)

The reasonableness engine needs a benchmark table, which is **not** committed:

```bash
python scripts/ingest_ised_benchmarks.py --year 2024 --out config/ised_benchmarks.json
```

That downloads the Financial Performance Data CSVs published by Innovation, Science and
Economic Development Canada on the Government of Canada Open Government Portal,
normalises them into per-industry cohort cells, validates them fail-loud, and writes a
large JSON file that `.gitignore` excludes. The data is licensed under the
**Open Government Licence – Canada**; the generated file and every report built from it
carry the attribution the licence requires. No Open Government Licence data file is
distributed with this repository — you build the table yourself. See `NOTICE`.

Every run validates structure and plausibility. `--anchor anchor.json` adds an optional
check that one named cell still carries the figures you expect, so a refresh that quietly
changes what a cell means aborts instead of overwriting your table; with no anchor
supplied, no cell-specific figures are demanded.

`--seed-only` writes a small table of invented example figures instead, with no download
— enough for the engine and its tests to run, and labelled in its own `meta` as not being
industry data. Skip the step entirely and the feature stays off; nothing else is affected.

## Bring your own data

Nothing in this repository contains anyone's real books. The schema you need in order to
point it at yours is written down in **[`docs/data-schema.md`](docs/data-schema.md)**:
every editable data file, the transaction/document/match/ledger record shapes and their
status vocabularies, the fixed category taxonomy, and the minimum CSV the bank parser
accepts.

**Config files you are expected to edit** — six YAML files, all shipped with generic
example defaults. Five degrade to an empty table with a warning rather than failing to
start; `aa_category_gifi_map.yaml` is opened directly, so it must be present or
`GIFI_MAPPING_PATH` must point somewhere that is:

| File | What it holds | Loaded by |
|---|---|---|
| `config/accounts.yaml` | account keys → display names, and the order sections appear in reports | `config/data_files.py` |
| `config/category_rules.yaml` | the deterministic regex pre-pass: pattern → category, plus non-reconcilable flags | `config/data_files.py` |
| `config/vendor_aliases.yaml` | descriptor regex → stable vendor key, so one merchant is one vendor | `config/data_files.py` |
| `config/transfer_keywords.yaml` | descriptor regex → transfer type, side and valid account types, for cross-statement linking | `core/transaction_linker.py` |
| `config/vendor_rules.yaml` | your own clients and vendors: vendor pattern → category, priority, USD-billing hints | `core/reconciliation.py` (`_known_vendor_patterns`, reads the file directly), **not** through `data_files.py` |
| `config/aa_category_gifi_map.yaml` | category → GIFI/industry-benchmark bucket, for the reasonableness pass | path from `GIFI_MAPPING_PATH` |

**Ingest routes.** Statements arrive through `POST /api/transactions/upload-statement`
(CSV/XLSX parse deterministically; PDF goes through the institution-agnostic LLM parser).
Receipts arrive through `POST /api/receipts/upload`. Gmail, PayPal and Wise are optional
gather integrations that stay hidden until you configure credentials. The CSV signatures
the parser recognises, and the five columns a hand-made CSV needs, are in
`docs/data-schema.md`.

**Built-in statement parsers:** BMO chequing and credit-card CSV/PDF exports, Wise,
PayPal, Amazon; every other institution goes through the generic LLM statement parser,
which reads whatever the statement prints. The built-in parsers key on the header row of
a CSV and on the descriptor grammar a format uses, so they need no configuration; the
LLM path needs no format knowledge at all.

**What the synthetic fixtures do and do not exercise.** The fixtures under
`tests/fixtures/` are generated, deterministic and small. They exist so the suite runs
end to end — parse, match, categorise, export — with no network, no LLM endpoint and
nobody's real books. They do not prove the engine against reality. They do not cover:

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

Expect to tune the rule files above and the reconciliation thresholds in `.env` against
your own statements before the output is trustworthy.

## Honesty about data

- **Everything sits on the machine you run it on** — database, uploaded documents,
  exports, logs. No replica, no point-in-time recovery, no managed anything.
- **No encryption at rest beyond what your OS gives you.** Stored integration
  credentials are encrypted with a key derived from your own secrets; the database files
  and the uploaded documents are not. Whole-disk encryption is your job.
- **Back it up yourself.** This is the only copy of your books; losing the disk loses the
  data. `scripts/local/backup-aa.ps1` is a `pg_dump`-plus-storage wrapper, and it writes
  to the same machine unless you point it elsewhere. It also copies `.env` into the
  backup as `env.txt`, in clear text, because a database restored without
  `AUTH_JWT_SECRET`, `STORAGE_URL_SECRET` and `CREDENTIAL_ENCRYPTION_SECRET` is not a
  restore. That makes the backup folder as sensitive as `.env` itself — restrict it,
  and do not sync it anywhere you would not put `.env`.
- **Your documents reach a model.** Prompts and page images go wherever `LLM_BASE_URL`
  points. Point it at a server on your own machine or LAN and nothing leaves; point it at
  a remote provider, or fill in `GEMINI_API_KEY` / `OPENAI_API_KEY`, and your financial
  documents are sent to that provider under their terms.
  An empty value in `.env` means **off**, even when the shell that starts the server
  exports that key: `.env` is loaded with `override=True` precisely so a stray
  `OPENAI_API_KEY` in your environment cannot quietly send your documents to a paid
  provider. `GET /health` shows which provider an upload would actually reach.
- **No outbound request from the browser.** The app ships no webfonts and renders in the
  system font stack, so loading a page fetches nothing from anyone else, and the
  Content-Security-Policy allows no external origin except Google's: its OAuth endpoint,
  reached only if you sign in with Google, and `*.googleusercontent.com` images, fetched
  only to show the profile picture of an account created that way. There is no analytics, error-tracking or
  telemetry SDK anywhere in this project — the log files on your disk are the only record
  of what happened.

## Known issues

Open engineering items that are not visible to a user are tracked in
[`docs/backlog.md`](docs/backlog.md).

Things a stranger will meet, written down rather than quietly left for them to find.
None of them loses data; all of them are cosmetic unless the entry says otherwise.

- **`pytest.ini` excludes eleven test files and thirteen more classes or tests.** They
  assert a contract the code does not have — imports that are gone, renamed database
  methods, a different column order — and are excluded from collection rather than
  deleted. The header of `pytest.ini` says so; nobody should add new tests to those
  files. What that costs in coverage is small but real:
  `pytest --cov=core.ledger --cov=core.html_binder` reports 85% for `core/ledger.py` and
  91% for `core/html_binder.py`, and the uncovered lines in `core/ledger.py` are mostly
  its accounting-package exports.
- **Gmail's OAuth redirect has to match what you registered.** The redirect the code
  builds is `<BASE_URL>/api/onboarding/gmail/callback`, and `BASE_URL` defaults to
  `http://localhost:8080`. If you serve the app anywhere else, set `BASE_URL` (or
  `GMAIL_REDIRECT_URI`) before connecting Gmail, and register the same value with
  Google.

## Testing

```bash
pytest                                        # from the repo root
ruff check .
cd web && npm run typecheck && npm run build
```

`scripts/gates.sh` runs all four in one go. There is no CI; these are the checks.

**What `pytest` does about the database.** It needs PostgreSQL and a disposable
database, and it arranges both itself. It takes `DATABASE_URL` from your shell, or —
if the shell does not have one — parses it out of `.env`, and then runs against a
`_test` sibling of that database: `autonomous_accounting` becomes
`autonomous_accounting_test`. It creates that database if it does not exist, using the
same host, port and credentials, and lays down `local_auth_schema.sql`,
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): synthetic fixtures only, `ruff` and `tsc` clean,
one topic per pull request, sign off your commits (`git commit -s`).

## Licence

Autonomous Accounting — self-hosted bookkeeping with LLM document extraction and bank
reconciliation. Copyright (C) 2026 Autonomous Accounting contributors

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU Affero General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along with this
program. If not, see <https://www.gnu.org/licenses/>.

Full text in [LICENSE](LICENSE). Because this is the AGPL, running a modified version as
a network service obliges you to offer its source to its users. Third-party attributions
— including PyMuPDF, itself AGPL — are in [NOTICE](NOTICE).

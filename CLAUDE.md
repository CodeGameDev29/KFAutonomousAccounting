# Notes for AI-assisted development

Context for a coding agent. Humans: read `README.md` and `CONTRIBUTING.md`.

**Stack.** FastAPI + PostgreSQL (row-level security keyed on the signed-in user) +
React 19 / Vite / TypeScript. `core/` is the engine (extraction, reconciliation,
categorization, ledger, reports), `server/api/` the HTTP surface, `db/` the schema and
data access, `web/src/` the SPA. Extraction talks to any
OpenAI-compatible, vision-capable endpoint via `LLM_BASE_URL`; this project hosts no
model of its own.

## Commands

```bash
pytest                                     # from the repo root
ruff check .                               # the only Python linter of record
cd web && npm run typecheck && npm run build   # build does NOT typecheck — run both
./scripts/gates.sh                         # all four of the above, with a summary
python -m uvicorn server.app:app --port 8080
```

## The database

`db/schema_pg.sql` is the whole schema as one baseline; `db/migrations/` starts empty
and new migrations are numbered from `001`. A schema change goes in **both** — a
migration for existing databases and `schema_pg.sql` for fresh ones — and an applied
migration is never edited. Every table has row-level security with a policy carrying
both `USING` and `WITH CHECK` on `user_id = auth.uid()`; `tests/test_rls_enforcement.py`
enforces that from the catalogue.

## Two invariants

1. **Engine correctness is never traded for speed or cost.** Flag an uncertain match for
   review rather than guess it; a wrong number in a tax filing is worse than a blank.
   Never sum amounts across currencies. When a test catches a miss, fix the engine, not
   the test — and never weaken a test to make a gate green.
2. **Never commit real financial data.** No real receipt, statement, account number,
   merchant or dollar figure from anyone's books, in a fixture or anywhere else.
   Fixtures are synthetic and deterministic: generate with `scripts/gen_fixtures.py`,
   and read `tests/fixtures/README.md` first.

## Where things live

Editable data tables — behaviour belongs in code, tables belong here — are the six YAML
files under `config/`: `accounts.yaml`, `category_rules.yaml` and `vendor_aliases.yaml`
(loaded by `config/data_files.py`), `transfer_keywords.yaml` (loaded by
`core/transaction_linker.py`), `vendor_rules.yaml` (read directly by
`core/reconciliation.py:_known_vendor_patterns`, not through `data_files.py`) and
`aa_category_gifi_map.yaml` (path from `GIFI_MAPPING_PATH`; unlike the others it is
opened without a guard, so it must be present). Record shapes, status
vocabularies, taxonomy and the accepted CSV format: `docs/data-schema.md`. Every
environment variable with a default: `.env.example`. Licence AGPL-3.0-only (`LICENSE`),
attributions (`NOTICE`), export provenance (`docs/release-provenance.md`).

## The agent harness

`.claude/` is a shared, tracked harness; `.claude/README.md` says what each part does and
how to switch it off. In short: three fail-soft hooks (a session-start orientation, a
reminder that fires the relevant rule when a prompt heads toward it, and a post-edit
guardrail for secrets, telemetry, schema and test discipline), the `/gates` and `/status`
commands, an `executor` agent, skills under `.claude/skills/` (catalog in `SKILLS.md`,
authoring contract in `AUTHORING.md`), and three project notes worth reading before
non-trivial work: `.claude/project/VERIFICATION.md` (what counts as proof),
`DOMAIN.md` (the accounting facts the engine must get right) and `LESSONS.md` (checks
to run before claiming done). `settings.json` denies agent reads of `.env`, `data/` and
`logs/` on purpose: on a real install those hold secrets and someone's books.

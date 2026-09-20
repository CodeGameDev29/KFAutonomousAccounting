# Contributing

Thanks for looking. This is a small, self-hosted project; patches are welcome and there
is no process beyond what is written here.

## Set up

Follow the Quickstart in [README.md](README.md): virtualenv, `pip install -r
requirements.txt`, `cp .env.example .env`, the database bootstrap in the order given,
then `npm ci` in `web/`.

You only need PostgreSQL and an LLM endpoint to run the app. Linting and the frontend
gates need neither.

## Gates

There is no CI. These four checks are the gate, and all four must be green:

```bash
pytest
ruff check .
cd web && npm run typecheck
cd web && npm run build
```

`./scripts/gates.sh` runs all four and prints a summary (`--quick` runs `ruff` alone for
the inner loop).

## Code style

- **Python** — `ruff` is the only linter and formatter of record; its configuration is in
  `ruff.toml`. `ruff check .` must be clean. Type hints where they help, `from __future__
  import annotations` at the top of new modules.
- **TypeScript** — `tsc -p web/tsconfig.check.json` (`npm run typecheck`) must be clean.
  The project is `strict`, with `noUnusedLocals` and `noUnusedParameters` on. Note that
  `npm run build` does **not** typecheck — Vite strips types without checking them — so
  run the typecheck separately.
- Match the surrounding code rather than reformatting a file you are editing.

## Changing the schema

The schema is one baseline, `db/schema_pg.sql`, and `db/migrations/` starts empty. A
change needs **both**: a new `db/migrations/001_<what>.sql` (numbered from `001`, with a
`.rollback.sql` beside it when the change can be undone) so existing databases get it,
and the same change written into `db/schema_pg.sql` so a fresh install has it. A
migration that has been applied is never edited or renamed — write another one.

A new table needs `ENABLE ROW LEVEL SECURITY` and a policy with **both** `USING` and
`WITH CHECK` on `user_id = auth.uid()`. `USING` alone decides what is visible and lets a
signed-in session write a row owned by somebody else; `tests/test_rls_enforcement.py`
enumerates the catalogue and will fail. Table privileges are handled for you —
`db/local_grants.sql` grants on every table in `public`, including ones a later
migration adds.

## No real financial data — ever

**Synthetic fixtures only.** No real receipt, invoice, bank statement, account number,
merchant relationship or dollar figure from anyone's actual books goes into this
repository, in a test fixture or anywhere else — not even "just the amounts", not even
redacted.

Generate what a test needs with `scripts/gen_fixtures.py` and read
`tests/fixtures/README.md` before adding a file there. Fixtures are deterministic
(fixed seed) so tests do not flake, and they keep the shape and the quirks of real data
without any of its values. If a fixture is missing a quirk your fix depends on, extend
the generator rather than pasting in a real document.

A pull request containing real financial data will be rejected on that basis alone.

## Engine correctness comes first

Extraction, reconciliation, categorization and the ledger are the reason this project
exists. Correctness there is never traded for speed or cost:

- Flag an uncertain match for review rather than guess it. A wrong number in a tax filing
  is worse than a blank.
- Never sum amounts across currencies.
- If a test catches a miss, fix the engine — not the test.

## Pull requests

- One topic per pull request. Say what changed, why, and which gates you ran.
- Include a test for a behaviour change, and say in the description if you could not.
- Do not delete or weaken a test to make a gate pass. If a test is genuinely obsolete,
  say so explicitly in the pull request.
- Documentation changes are welcome on their own.

## Secrets

Secrets live in your local `.env`, which `.gitignore` excludes; `.env.example` documents
every variable the code reads with a safe default or an empty value. If you commit one by
accident, rotate it at its source first, then remove it from the tree — a rotated secret
in git history is an annoyance, an un-rotated one is an incident.

## Licence and sign-off

This project is licensed under the **GNU Affero General Public License v3.0 only**
(`LICENSE`). By submitting a contribution you agree it is licensed under the same terms,
and you certify the [Developer Certificate of Origin](https://developercertificate.org/):
that you wrote the patch, or have the right to submit it under the AGPL.

Sign off your commits with `git commit -s`, which appends

```
Signed-off-by: Your Name <your@email.example>
```

Third-party code brought into the tree must be licence-compatible with AGPL-3.0 and
recorded in `NOTICE`.

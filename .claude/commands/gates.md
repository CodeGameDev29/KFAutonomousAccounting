---
description: Run every local pre-merge check (ruff, pytest, web typecheck, web build) and report the verdict
allowed-tools: Bash, Read
---

Run `./scripts/gates.sh $ARGUMENTS` with a generous timeout (the full run compiles the frontend —
allow 600000ms). Pass `--quick` through for the fast inner loop (ruff only). The web gates need
`npm ci` to have been run in `web/` once; pytest needs PostgreSQL reachable, and creates its own
`_test` database (README.md, *Testing*).

Report the `GATES SUMMARY` table verbatim.

If anything is RED, that is a stop: do not merge. Open the named log, find the first real failure,
and fix it. The one tolerable red is a failure that already exists on `main` — confirm it by
checking out `main` and re-running before you claim it, and say which test it is when you report.

These gates are the only CI there is; nothing runs remotely. Never weaken a check, skip a test or
reach for `--no-verify` — there is no second line of defence behind it. When a test catches a
miss, fix the engine, not the test.

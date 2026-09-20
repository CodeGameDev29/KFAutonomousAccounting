# Skill catalog

Four skills, two commands. Invoke as `/<name>`. A skill states the goal, its preconditions, what
done means, and the judgment calls; see [`AUTHORING.md`](AUTHORING.md). The orchestrating session
reads a skill to brief the work; agents (for instance [`executor`](../agents/executor.md)) carry it
out.

A skill directory holds its definition (`SKILL.md`, runner scripts, `templates/`, `prompts/`,
`config/`). Anything a run produces is gitignored.

## Product verification

| Skill | Reach for it when |
|---|---|
| `e2e-test` | Before a release or after a wide-blast-radius change. Persona journeys, a functionality-completeness sweep and a security journey through a real browser, judged independently, plus API suites. Needs a running app, a browser-automation MCP server, and a **throwaway account** passed in `AA_E2E_EMAIL` / `AA_E2E_PASSWORD` — it uploads, mutates and deletes. |
| `address-manual-test-bug-report` | A human tester filed a batch of unrelated bugs and every one needs closing with a traceable research → plan → fix → report chain. |

## Design

| Skill | Reach for it when |
|---|---|
| `frontend-design` | Any web UI is being created or restyled. Inside this app that means extending the existing design system — system font stack, no third-party fetches. Apache-2.0, see its `LICENSE.txt`. |

## Harness and codebase health

| Skill | Reach for it when |
|---|---|
| `postmortem` | Work was called done and a person had to correct it, or feedback arrived that should change how sessions work. Produces a ranked action list and an applied change to [`LESSONS.md`](../project/LESSONS.md). |

## Commands

- `/gates`: every local pre-merge check (ruff, pytest, web typecheck, web build). This is the only
  CI there is.
- `/status`: local stack health, which model an upload would reach, what landed recently.

## What is deliberately not here

Dependency auditing needs no skill: `pip-audit -r requirements.txt` and `npm audit` in `web/` are
the whole procedure. Engine-accuracy harnesses that score the matcher against a hand-reconciled
ledger need a private corpus of real books, which by this repository's first rule cannot ship; the
comparison tooling they are built on is in `scripts/qa/`, and the header of
`scripts/qa/manifest.py` says how to point it at your own data (`AA_QA_DATA_DIR`, default
`data/books/`, gitignored).

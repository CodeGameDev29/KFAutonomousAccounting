---
name: address-manual-test-bug-report
description: >-
  Take a human tester's free-text bug report — several unrelated bugs in one file, often with
  screenshots — and close every bug in it with a traceable research → plan → fix → report chain per
  bug, parallelised where the bugs do not touch the same files. Reach for this when the input is a
  batch of independent bugs from someone who was clicking through the app. For a single bug you
  already understand, just fix it; for finding bugs rather than fixing them, use `e2e-test`.
---

# Address manual test bug report

## Goal

Every bug the tester listed is either fixed in the app or explicitly reported as not-fixed with a
reason — with a research report, a plan and a fix report on disk for each, so a later session can
reconstruct why the code looks the way it does. `pipeline.py` holds the mechanics; agents reason.

## Preconditions

- A tester's report file exists on disk, plus any screenshots that came with it. `pipeline.py setup`
  takes the path; nothing else in the pipeline reads the original. Keep the report **outside the
  repository** if it quotes real books — run output is gitignored, the report itself is not.
- The app is running and reachable (`curl http://localhost:8080/health`), because every bug closes
  in the browser. Sign in with a throwaway account holding synthetic data only
  ([`VERIFICATION.md`](../../project/VERIFICATION.md)).
- `pipeline.py setup` produces a run dir and a `bugs.json` whose titles match the report. A split
  that looks wrong invalidates the entire run, so this check is the precondition for delegating.

## Done means

- A run directory under `runs/<timestamp>/` containing `bugs.json`, `knowledge_base.md`, and one
  file per bug in `research/`, `plans/`, and `fixes/`, plus `final_report.md`.
- Every bug carries a status a person can act on: FIXED, PARTIAL, FAILED — never a missing report
  quietly rolled into a success rate.
- The symptom the tester described was reproduced and re-checked *in the browser* after the fix.
  A passing pytest is corroboration, not the proof.
- `./scripts/gates.sh` is green once all fixes are in.

## Judgment calls you own

**Trust the bug split, but check it.** The parser splits on numbered lists, bullets and headers,
then falls back to paragraphs, so a prose report can collapse into one giant "bug" or shatter into
thirty fragments. The whole run is shaped by that split and re-running setup is cheap.

**Overlap analysis decides parallelism, and it is the one genuinely load-bearing ordering.**
`analyze-overlaps` reads the *plans*, extracts file paths and chains bugs sharing a file into one
sequential group; two fixing agents editing one file concurrently silently lose each other's edits.
The extraction only sees paths in a `Files Modified` section or on `**File**:` lines, so a plan that
names its files in prose is misgrouped as independent — treat a suspiciously overlap-free result as
a reason to read the plans, not as good news.

**The knowledge base exists to stop contradictory designs.** Compiled from all the research before
any planning starts, cross-referencing which bugs touch the same files: plan bug 3 without it and
the design undoes bug 1's fix.

**A tester's framing is a symptom, not a diagnosis.** "Login sometimes sends me to onboarding" is
where to start looking, not what to fix. Research establishes the root cause from the code path —
component through API handler to query — and the fix goes where the cause is. A bug that turns out
to be already fixed gets reported as such, never as a manufactured change.

**When the plan is wrong, adapt and say so.** A plan built on research that missed something is not
a reason to ship a wrong fix. Deviations get documented in the fix report.

**Never edit the test to match the code, never stub the dependency.** The bug is in the app. And a
tester's attached statement or receipt never becomes a fixture: reproduce its shape with
`scripts/gen_fixtures.py` (invariant 2 in [`CLAUDE.md`](../../../CLAUDE.md)).

## Failure modes

- **`all_complete: true` read as "the bugs are fixed."** `validate` checks only that each expected
  file exists and is over 100 bytes — a FAILED fix report passes it. Read the statuses.
- **Work fanned out off the bug list instead of the overlap groups.** Fast, green, and quietly
  lossy — the second agent's write clobbers the first's.
- **Fix reports with an unparseable status line.** `compile-report` matches a handful of
  `## Status` / `**Status**:` forms; anything else is labelled UNKNOWN and counted as failed (NO
  REPORT when the file is missing), whatever the agent actually did. The templates in `prompts/` produce a matching shape — keep
  it when you adapt them.
- **Closing on tests alone.** These bugs were found by a human clicking; a stale dev-server proxy or
  a wrong post-login redirect is only visible in a browser.
- **Screenshots left unread.** Reports arrive with image attachments alongside the text; the
  screenshot often contains the error message the prose omitted.

## Where things live

All subcommands run from the repo root with the venv's interpreter (`python` once the venv is
activated; otherwise `.venv/bin/python` or `.venv\Scripts\python.exe`):
`python .claude/skills/address-manual-test-bug-report/pipeline.py <subcommand>`

| Subcommand | Arguments | Produces |
|---|---|---|
| `setup` | `<bug_report_file>` | run dir, `bugs.json`, `progress.json`; JSON on stdout |
| `generate-prompt` | `<research\|planning\|fixing> <bug_id> <run_dir>` | that phase's brief on **stdout**, for you to adapt into the agent's brief |
| `validate` | `<run_dir> <research\|planning\|fixing>` | per-bug present/missing JSON |
| `compile-kb` | `<run_dir>` | `knowledge_base.md` with shared-file cross-references |
| `analyze-overlaps` | `<run_dir>` | `execution_order` groups, each `"mode": "parallel"` or `"sequential"` |
| `compile-report` | `<run_dir>` | `final_report.md` + summary JSON |

`update-progress <run_dir> <bug_id> <phase> <status>` exists for manual debugging only.

- `prompts/research_agent.md`, `planning_agent.md`, `fixing_agent.md` — what `generate-prompt`
  fills. They are inputs you adapt, not scripts: edit these, never the emitted text, when what an
  agent must establish changes. The emitted brief still has to carry whatever this run needs that
  the template could not know. Delegated work goes to the
  [`executor`](../../agents/executor.md) agent.
- `runs/<timestamp>/` — created on first `setup` and gitignored (`.gitignore` beside this file):
  run output quotes the tester's report and absolute local paths, so it never gets committed.

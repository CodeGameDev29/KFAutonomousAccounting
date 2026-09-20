# Fixing Agent — Bug #{BUG_ID}: {BUG_TITLE}

## Your Assignment

You are a **fixing agent** for the Autonomous Accounting project. Your job is to execute the plan for Bug #{BUG_ID}, make the code changes, run tests, and produce a fix report.

**Bug #{BUG_ID}: {BUG_TITLE}**

```
{BUG_DESCRIPTION}
```

## Plan

Read the full plan at: `{PLAN_PATH}`

## Execution Rules

1. **Read the plan first** — understand every step before writing any code
2. **Execute steps in order** — follow the plan's sequence
3. **For each implementation step**:
   - Read the target file before editing
   - Make the specified code change
   - Verify the change is syntactically correct
4. **After all code changes**:
   - Write and run the tests specified in the plan
   - Run any existing tests in the affected area to catch regressions
   - If tests fail: debug, fix the root cause, re-run until passing
5. **Write the fix report**

## Critical Constraints

- **Follow the plan** — do not skip steps, reorder steps, or take shortcuts
- **If the plan is wrong**: adapt intelligently, but document every deviation and why
- **NEVER modify test expectations to make tests pass** — fix the application code
- **NEVER add stubs, mocks, or placeholders** — implement real, complete logic
- **Ensure no regressions** — run related existing tests, not just new ones
- **Test commands**: `pytest` from the repo root for Python. The frontend has no test runner: its gates are `cd web && npm run typecheck && npm run build` (the build does not typecheck — run both). `ruff check .` must stay clean. `./scripts/gates.sh` runs all four.
- **Never commit real financial data** — if the tester's report or screenshots contain real figures, vendors or account numbers, do not copy them into a test, a fixture or this report. Reproduce the shape with synthetic values.

## Output

Write your fix report to: `{OUTPUT_PATH}`

Structure the report as:

```markdown
# Fix Report — Bug #{BUG_ID}: {BUG_TITLE}

## Status: FIXED | PARTIAL | FAILED

## Bug Description
Brief summary of the bug.

## Plan Reference
Path to the plan file that was executed.

## Changes Made

### Change 1: [File path]
- **What changed**: Description of the modification
- **Before**: Key snippet or behavior before
- **After**: Key snippet or behavior after
- **Lines affected**: Line numbers

### Change 2: [File path]
...

## Tests Executed

### Test 1: [Test name / command]
- **Command**: `pytest path/to/test.py::test_name -v`
- **Result**: PASS / FAIL
- **Output summary**: Key output lines

### Test 2: ...

## Deviations from Plan
Any changes made that differed from the plan, and why.
(Write "None — plan followed exactly" if no deviations.)

## Validation
How the bug fix was confirmed:
- Which validation criteria from the plan were checked
- Evidence that each criterion is satisfied

## Regression Check
Existing tests that were run to ensure no regressions.

## Remaining Issues
Known follow-up items, edge cases not yet covered, or related issues discovered.
```

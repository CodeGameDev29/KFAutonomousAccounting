#!/usr/bin/env python3
"""UserPromptSubmit hook - fire the relevant standing rule at the moment it applies.

The rules in CLAUDE.md are read once at session start and then compete with everything else in
context. This injects the one that matters exactly when a prompt heads toward it: silent when
nothing matches, and at most two printed, so there is no per-turn noise. Always exits 0.
"""
import json
import sys

RULES = [
    # (trigger keywords, reminder)
    (("fixture", "test data", "sample data", "real receipt", "real statement", "my statement",
      "my receipts", "my books", "example data", "seed data", "demo data"),
     "[focus] Never commit real financial data: no real receipt, statement, account number, "
     "merchant or dollar figure. Fixtures are synthetic and deterministic - generate them with "
     "scripts/gen_fixtures.py and read tests/fixtures/README.md first."),

    (("schema", "migration", "new table", "add a column", "add column", "alter table", "rls",
      "row-level", "row level", "policy"),
     "[focus] A schema change goes in BOTH a new numbered file in db/migrations/ and "
     "db/schema_pg.sql; an applied migration is never edited; every table needs RLS with USING "
     "and WITH CHECK on user_id = auth.uid() (tests/test_rls_enforcement.py)."),

    (("reconcil", "matcher", "match ", "categoriz", "extraction", "ledger", "currency", "fx ",
      "exchange rate", "total", "tax summary", "gst", "hst"),
     "[focus] Engine correctness is never traded for speed or cost: flag an uncertain match for "
     "review rather than guess it, never sum amounts across currencies, and when a test catches "
     "a miss fix the engine, not the test (.claude/project/VERIFICATION.md)."),

    (("failing test", "test fails", "tests fail", "red gate", "make it pass", "make the test",
      "skip the test", "xfail", "flaky", "pytest.ini"),
     "[focus] Never weaken, skip or exclude a test to make a gate green. A failing test means "
     "the code is wrong until proven otherwise; pytest.ini's exclusion list only shrinks."),

    (("verify", "works in the ui", "in the browser", "e2e", "playwright", "end to end",
      "end-to-end", "sign in", "sign-in", "user can"),
     "[focus] 'A user can do X' is proven by a browser doing X against the running app, not by "
     "curl, an import or a mock (.claude/project/VERIFICATION.md). Use a throwaway account that "
     "holds only synthetic data."),

    (("analytics", "telemetry", "error tracking", "sentry", "tracking", "cdn", "webfont",
      "google font", "hosted service", "saas"),
     "[focus] README.md promises no telemetry, no outbound request from the browser and no "
     "hosted dependency; the CSP enforces it. Adding one is a product decision, not a "
     "convenience."),

    (("skill", "harness", "skill.md", "hook"),
     "[focus] Skills state the goal, the definition of done and the judgment calls - not the "
     "steps. Read .claude/skills/AUTHORING.md before writing or editing one, and after editing "
     "a hook pipe a sample payload through it (.claude/project/LESSONS.md)."),
]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = (data.get("prompt") or "").lower()
    if not prompt:
        return
    out = [msg for triggers, msg in RULES if any(t in prompt for t in triggers)]
    if out:
        print("\n".join(out[:2]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

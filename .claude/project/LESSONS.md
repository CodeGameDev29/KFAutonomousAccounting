# Lessons — checks to run before claiming done

Each entry is a **check**, not a story: something a session can actually run before saying a piece
of work is finished. Entries are added by [`postmortem`](../skills/postmortem/SKILL.md) when a
real gap is found, and removed when they stop being true.

The bar for adding one: it has to name a failure that actually happened in this codebase, and it
has to be checkable. "Be careful with edge cases" is not a lesson.

---

### A green test proves nothing about a case the fixture can't express

**Happened:** bugs shipped past green unit and smoke suites because both were checking a contract
shape the authors had invented — the real third-party payload was shaped differently, and no
fixture contained the input that broke it.

**Check:** before calling a fix verified, name the specific input that would break it and confirm
the fixture actually contains that input. If it doesn't, the run is unverified — go through the
real UI against the real dependency instead. The synthetic fixtures are small and clean by design
(README.md, *What the synthetic fixtures do and do not exercise*).

---

### The launch command in a doc is not a launch command until it has run

**Happened:** an interpreter path that only exists on one operating system (`.venv/bin/python` vs
`.venv\Scripts\python.exe`) survived across skills and docs long after the checkout it was written
on changed platform. Every affected entry point was broken and nothing surfaced it, because the
commands were being read rather than run.

**Check:** if you write or edit a command in a doc, execute it once. If a skill you touched has an
entry point, run it far enough to see it start. Write commands so they work on both platforms, or
name both forms.

---

### Verified through the API is not verified

**Happened:** repeatedly. Backend-level checks pass while the user-facing path is broken by a
serialization difference, an auth boundary, or a frontend contract.

**Check:** the claim "a user can do X" is only supported by a browser doing X. See
[`VERIFICATION.md`](VERIFICATION.md).

---

### A hook that has never printed anything is a hook that has never run

**Happened:** every hook in `.claude/settings.json` invoked `python3`, which on a stock Windows
machine resolves to the Microsoft Store stub and prints *"Python was not found"*. All of them were
dead for months and nobody noticed, because a hook that produces no output looks exactly like a
hook that had nothing to say. (The hook commands now try `python` and fall back to `python3`.)

**Check:** after editing a hook or `settings.json`, pipe a sample payload through the exact command
string from `settings.json` and see output, for example:

```bash
echo '{"prompt":"add a migration"}' | python .claude/hooks/focus_reminder.py
```

Reading the command is not running it.

---

### A caught exception can hide a feature that never ran

**Happened:** `core/date_validator.py`'s prompt carried a literal
`{"after" if txn_after else "before"}` field inside a plain (non-f) string, so `.format()` raised
`KeyError` before any model was reached. The caller caught `Exception`, logged *"Date validation
failed… using heuristic"*, and returned the heuristic fallback — so every date-gap verdict the
engine produced came from the heuristic's flat windows while the code, the docstrings and the docs
all read as if a model decided. Nothing failed, no test went red, and the logs looked like a
degraded provider.

**Check:** `grep -rn "except Exception" core` and, for every broad catch that wraps a `.format()`
or a prompt/template use, confirm the happy path actually executes — not just that the fallback
works. Run `python -m pytest tests/test_prompt_templates.py -q`: it formats every `.format()`-ed
template under `core/` and checks each placeholder against the keywords the call site passes. Then
grep the last real run's log for the fallback's own warning line — a fallback that fires on
*every* call is a dead feature, not a degraded one.

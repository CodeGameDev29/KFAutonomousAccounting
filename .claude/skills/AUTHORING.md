# Skill authoring contract

The standard every `SKILL.md` in this repo is held to. Read it before writing or editing one.

## Who reads a skill

Two readers:

- **The orchestrating session.** It reads the skill to decide whether the task is worth doing,
  what done looks like, and how to brief the work.
- **Agents carrying out a brief** (the [`executor`](../agents/executor.md) agent, or any other).
  They receive the brief plus this skill and choose their own steps.

So a skill is written for a strong, autonomous reader. It does not teach the reader how to work. It
records what the reader could not know from the repo alone: what done means here, which judgment
calls have bitten before, and where the non-obvious things live.

## Why a skill exists

A skill exists when a task has a **non-obvious definition of done** or a **non-obvious success
bar**, something a capable agent would otherwise get subtly, expensively wrong.

If a competent agent would produce the right result from the plain request alone, the skill is
noise. Delete it. Reorganising a folder, restarting the server, fixing a bug you already
understand: those are plain requests, not skills.

## What a SKILL.md declares

Six things. Nothing else is load-bearing.

**1. Goal.** The outcome as a change in the world, not an activity.

**2. Preconditions.** What has to exist on this machine for the skill to run at all: a running
stack, a throwaway account, a credential, `node_modules`, a prior run's output. State each one and
how to check it in one command. A skill whose preconditions are not met stops and says so; it
does not fake a run. This section exists because a harness can carry skills whose runners drive a
UI that no longer exists, and nobody notices for months.

**3. Done means.** The observable evidence that the goal is met, checkable by someone who was not
in the room.

**4. Judgment calls you own.** The decisions that are genuinely hard, each with the principle for
resolving it. This is the heart of the document. Write the *why* so the reader can extend it to
the case you did not foresee.

**5. Failure modes.** What a plausible-looking-but-wrong run looks like: the specific ways *this*
task has been faked or fumbled before.

**6. Where things live.** Entry points, artifacts, fixtures, output paths. Facts the reader cannot
derive by looking. State them once, precisely, with commands that have actually been run — and
that work on both Windows (`.venv\Scripts\python.exe`) and POSIX (`.venv/bin/python`), or that
name both.

## What must not be in a SKILL.md

- **Numbered procedures the reader could derive.** If an ordering is load-bearing, say why.
- **Verbatim subagent prompts.** State what each agent must independently establish; the
  orchestrator writes the brief.
- **Model names or agent types.** A skill that says "spawn model X" is wrong the day the roster
  changes.
- **Exact output templates in prose.** If a shape matters because a script parses it, put the
  file in `templates/` and reference it; otherwise say what the output must let a reader decide.
- **Restatements of general competence** or of `CLAUDE.md` / `.claude/project/`. Link instead.
- **Anything real.** No real account, e-mail address, hostname, credential, document or figure —
  the repository's *never commit real financial data* rule covers the harness too. Skills take
  URLs and test accounts from environment variables.

## Scripts vs. reasoning

Deterministic, high-volume, or token-expensive work belongs in a script: scanning, diffing,
scoring against fixed criteria, driving a browser through a known flow. Agents reason about what
the output *means*. Scripts write to files; agents read results, not raw input. Long runs emit
progress somewhere pollable inside one blocking foreground command, never a chain of
sleep-and-check calls.

A script that is in the repo is a script that runs. If it targets a UI, endpoint, or path that no
longer exists, delete it rather than leave a `TODO` banner on it. Python under `.claude/` is linted
by the same `ruff check .` gate as everything else.

Anything a run produces (`runs/`, `reports/`, `screenshots/`, progress files, logs) is gitignored:
a skill directory holds the definition, never the output of a run — run output is where real data
leaks from.

## Size

Aim under 100 lines. Length is a symptom: durable facts belong in `references/`, shapes in
`templates/`, computation in `scripts/`.

## Frontmatter

```yaml
---
name: kebab-case-matching-the-directory
description: >-
  What outcome this produces and when to reach for it, written so an agent choosing between two
  similar skills picks correctly.
---
```

## Editing an existing skill

Skills encode expensive lessons. When you shorten one, the test is: *does every deleted line
correspond to a judgment the reader can now make on its own?* If a line records something the
project learned the hard way, it survives, reworded as judgment. Everything else goes.

# The agent harness

This directory configures [Claude Code](https://claude.com/claude-code) for this repository. It is
optional: nothing in the application reads it, and deleting it changes nothing about how the app
builds, runs or tests. It is here because the project is developed with coding agents, and the
rules that keep an agent from doing damage in a bookkeeping codebase — never commit real data,
never weaken a test, never trade correctness for a green gate — work better enforced than
remembered.

Everything in it is plain text you can read in a few minutes. Read it before you trust it; that
is the right posture toward any repository that ships agent configuration.

## What is here

| Path | What it does |
|---|---|
| `settings.json` | Registers the three hooks, pre-approves a short list of read-only and gate commands (`pytest`, `ruff check`, `npm run typecheck/build`, `git status/diff/log`), and **denies** agent reads of `.env`, `data/` and `logs/`. |
| `hooks/session_compass.py` | Session start: prints git state, whether the local server and Vite are listening, and a one-line summary of the backend's own `/health`. Loopback only. |
| `hooks/focus_reminder.py` | On each prompt: prints the one standing rule the prompt is heading toward (fixtures, schema changes, engine changes, failing tests, UI verification, telemetry, skills). Silent otherwise. |
| `hooks/guardrails.py` | After each file edit: warns about credential-shaped strings, real-looking e-mail addresses in data files, hosted telemetry in shipping code, schema changes missing their migration or RLS, hand-placed fixtures, and test skips. Warns only; never blocks. |
| `commands/gates.md` | `/gates` — run `scripts/gates.sh` and report the verdict. |
| `commands/status.md` | `/status` — local stack health, which model an upload would reach, recent work. |
| `agents/executor.md` | A subagent that carries out a written brief and reports faithfully. |
| `project/` | `VERIFICATION.md`, `DOMAIN.md`, `LESSONS.md` — the judgment an agent cannot derive from the code. |
| `skills/` | Reusable workflows. Catalog: `skills/SKILLS.md`. Contract: `skills/AUTHORING.md`. |

The hooks are small Python scripts using only the standard library. They make no network request
except `session_compass.py`'s `GET http://127.0.0.1:<PORT>/health`, read nothing from `.env`
except the `PORT=` line, write nothing, and always exit 0.

## Why `.env`, `data/` and `logs/` are denied

On a working install `.env` holds the JWT and storage secrets and possibly hosted-provider API
keys; `data/` holds uploaded statements and receipts; `logs/` holds per-run reconciliation audits
that list every transaction. An agent session is sent to a model provider. The deny rules keep
someone's books and secrets out of that traffic by default. If you need an agent to help edit
`.env`, do that step yourself, or relax the rule locally in `.claude/settings.local.json`
(gitignored) — not in the shared file.

## Browser verification

`project/VERIFICATION.md` asks for UI claims to be proven in a real browser. The harness does not
register a browser-automation MCP server for you — a repository that silently adds one is a
repository you should distrust. To add Playwright yourself:

```bash
claude mcp add playwright -- npx @playwright/mcp@latest
```

Its snapshots land in `.playwright-mcp/`, which is gitignored: they are screenshots of whatever
books the app was showing.

## Switching it off

Delete or rename `.claude/settings.json` to disable the hooks and permission rules; the commands,
agent and skills are inert until invoked. To disable one hook, remove its block from
`settings.json`. Personal overrides belong in `.claude/settings.local.json`.

## Changing it

After editing a hook, pipe a sample payload through it and look at the output — a hook that fails
silently looks exactly like a hook with nothing to say (`project/LESSONS.md`):

```bash
echo '{"prompt":"add a migration"}' | python .claude/hooks/focus_reminder.py
echo '{}' | python .claude/hooks/session_compass.py
```

Python under `.claude/` is linted by the same `ruff check .` gate as the rest of the repository.
The harness follows the repository's first rule like everything else: no real account, address,
hostname, credential, document or figure, anywhere in it.

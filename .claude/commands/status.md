---
description: Where this checkout is right now — local stack health, which model an upload would reach, recent work
allowed-tools: Bash, Read, Glob, Grep
---

Local stack (everything is on loopback; change the port if `.env` sets a different `PORT`):
!`curl -s -m 5 -o /dev/null -w 'local health %{http_code} in %{time_total}s\n' http://127.0.0.1:8080/health || echo 'local health check failed - is uvicorn running?'`
!`curl -s -m 5 http://127.0.0.1:8080/health || echo 'no health body'`

Recent work:
!`git log --oneline -15`

Working tree:
!`git status --short`

Report three things, in this order, and nothing else.

**1. Is the stack healthy.** From the health body: overall `status` (`ok`, or `degraded` when
uvicorn is up but PostgreSQL or storage is not), then the `extraction` block — which provider an
upload would actually reach first, the provider order, and whether the local endpoint answered its
last probe. A local primary that is unreachable is the interesting failure: the engine is pointed
at a model that is not serving, so uploads park in `waiting_for_model` and direct uploads return
503 until it is back. Say plainly if a hosted key (`GEMINI_API_KEY` / `OPENAI_API_KEY`) is present,
because that means documents can leave the machine. If nothing answers, the stack is simply not
running: `python -m uvicorn server.app:app --port 8080` from the repo root (README.md,
*Quickstart*).

**2. What landed recently.** Group the commits into themes — a capability reachable in the UI,
versus a fix, versus docs and tooling. A running server keeps serving the code it started with, so
say so if commits are newer than the process, and note that `web/dist` needs `npm run build` when
frontend files changed.

**3. What is next.** The shortest honest list, ordered: a red gate, a dirty tree, a broken
surface, whatever was last asked for. Don't pad it.

Keep it scannable: a few bullets, then a short ordered list.

---
name: postmortem
description: >-
  Turn feedback, or the trail of corrections a person had to make on top of work an agent called
  done, into two things: a ranked action list, and an applied edit to the living docs so a future
  session inherits the lesson without anyone remembering to pass it on. Reach for this after a
  change lands and gets corrected, when asked "what did I have to clean up after you", or when
  feedback carries a signal about how sessions are being spent rather than just a bug.
---

# Postmortem

## Goal

The gap between "the agent called it done" and "the user could actually use it" gets named,
root-caused, and converted into a check a future session runs *before* claiming done. The report is
a byproduct; the applied change is the deliverable.

## Preconditions

Either a feedback artifact (a message, a review thread, an issue) or a baseline — the commit the
corrected work landed at, the user's if they name one: `git rev-parse --verify <baseline>`. This
skill only reads git — never commit, push, revert or rebase from here.

## Done means

1. Direct **and implied** pain both extracted, each tied to the words or commit that produced it,
   with the correction boundary confirmed rather than assumed.
2. A root cause about the agent's process, not about the code.
3. A ranked action list, ordered by how much each item improves the engine's correctness or the
   time it takes someone to close a month of their books.
4. At least one reusable, checkable lesson **applied** to a living doc — usually
   [`LESSONS.md`](../../project/LESSONS.md) — not proposed.
5. A dated report under `reports/postmortems/`, and a plain-language reply: what you heard, what
   changes in the product, what changes in how you work.

## Judgment calls you own

**Feedback is two signals wearing one coat.** The surface signal is about the artifact — broken,
missing, ugly, confusing. The buried signal is about *what sessions were spent on*, and it is
usually the more expensive of the two. A run that produces only the bug list has failed: that list
would have surfaced anyway. So ask of each remark what it presupposes was already true, what the
subtext is (frustration, distrust of progress), whether it is a symptom of a systemic gap, and
whether it is secretly about communication rather than the product. "I couldn't tell what changed"
is not a UI note — it is evidence that sessions are not shipping, or not reporting, a visible delta.

**Find the correction boundary from the shape of the history, not from authorship.** When a person
and an agent commit under the same identity, and most commits carry a co-author trailer, authorship
tells you nothing. What works: the merge or squash commit of a pull request is where the agent's
work landed, and later commits touching the same files are the correction pass. Good, not certain —
interleaved unrelated work, a human commit inside the pull request, and an agent hot-fix after merge
all break it. Propose the boundary with evidence and confirm it before analysing; never silently
guess. Read the correction commits' own messages before the diff — they say what the person thought
was wrong, where the diff only says what changed — and keep only changes that undo, repair or
complete what was claimed done. Building the next feature on top of the work is a continuation, not
a correction.

**The root cause is about the agent.** "Categorization didn't handle refunds" is a bug description.
"The agent verified against fixtures containing no refund and treated a green run as proof" is a
root cause, and only the second generates a check. Test the recurring shapes explicitly: a green
test against a fixture that cannot express the failing case (the synthetic set is small and clean by
design — see `tests/fixtures/README.md` for what it does not exercise); a rule that holds on the
fixtures but not on real statements; an accuracy shortcut in the engine; a fix made in the test
instead of `core/`; something verified through the API that a user would have hit differently in
the UI.

**Be honest at your own expense, then try to refute yourself.** Critique recent sessions as an
outside reviewer with no stake would: effort spent on things nobody can see, a session that shipped
a plan instead of a capability, a gap a person had to catch. Then have a separate agent re-read the
raw input from scratch and attack that reading — sandbagged critique, missed point, cosmetic
changes, a wrong ranking. Record what it changed; a pass that changed nothing was not adversarial.

**Rank against the two invariants in [`CLAUDE.md`](../../../CLAUDE.md).** A wrong number in a ledger
outranks everything. After that, the question for each item is whether it shortens the path from a
pile of documents to books someone can trust, and what it costs. Correct-but-invisible ranks below
visible-and-approximately-right only when it touches no figure a report prints.

**A change only counts if a future session is guaranteed to read it.** Name the living doc it lands
in and the failure it prevents; if you can't, it is a feel-good edit. Cross-cutting pre-done checks
go to `LESSONS.md`; a check specific to one workflow becomes a failure mode in that skill's
`SKILL.md`; how to prove a change works goes to
[`VERIFICATION.md`](../../project/VERIFICATION.md). Edit living docs only: dated reports are an
audit trail, so new insight becomes a new dated report, never an edit to an old one.

## Failure modes

- Shipping the bug list and skipping the process critique. This is the default failure.
- Proposing changes instead of applying them. Nobody should have to land your conclusions for you.
- Guessing the boundary and postmortem-ing someone's unrelated work.
- A narrative instead of a check, or a lesson so general it cannot fail ("mind the edge cases").
- Blaming the codebase. The question is always what the agent could have verified and didn't.
- Ranking by what is easiest to build next, or an adversarial pass that agrees with everything.
- Quoting real figures, vendors or account numbers from someone's books in a report or a lesson.
  Describe the shape of the data; never paste it (invariant 2 in `CLAUDE.md`).

## Where things live

- Reports: `reports/postmortems/<YYYY-MM-DD>-<slug>.md`. `/reports/` is in `.gitignore`, so a report
  is a local audit trail; the durable, shared part is the edit to
  [`LESSONS.md`](../../project/LESSONS.md).
- History: `git log --oneline`, `git log --merges --oneline` for landing points, `git show`,
  `git diff <baseline>..HEAD -- <paths>`.
- For anything the database can answer — document counts, extraction health, reconciliation
  outcomes — ask the running app rather than estimating from commit activity: `GET /health`, and
  signed in, `GET /api/actions/status` and `GET /api/reconciliation/summary`.

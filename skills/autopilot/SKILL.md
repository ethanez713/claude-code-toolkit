---
name: autopilot
description: Run every piece of work that does not need the user through to completion in one go — make the clear decisions yourself and report them, hold only the workstreams that are genuinely blocked on the user, and stop only when no unblocked work is left. Use when told "proceed with the plan", "keep working until you need me", "get as far as you can without my input", "do everything that doesn't require my attention", "only stop when my decisions are required", or /autopilot. A bare "continue" is not a trigger.
---

# Autopilot

The user comes back once: to finished work and a short numbered list of what only they can
settle. Every extra "should I continue?" is a failure of this skill.

## The loop

1. **Map the work.** Establish state (worklog, plan index, tree), then sort every remaining
   step into *ready* or *held on <what>*. With no plan, the task's own parts are the steps.
2. **Run every ready step**: do it, verify it, commit it, record it in the worklog. Then the
   next one.
3. **A blocker holds one step and whatever `needs:` it — never the run.** Write the hold in
   the worklog and take the next ready step, in another phase or workstream if necessary.
4. **Before stopping, attack each hold.** Try to clear it yourself: research it, read the
   code or logs, measure it, run the test, drive the device. Only what survives is the user's.
5. **Stop only when every remaining step is held on the user.** Not at a phase boundary, a
   milestone, a passing build, a question, or a usage-limit nudge.

**Ask before starting, once, in one batch**, whatever needs the user's sign-off and is
already visible when the work is mapped: high-impact decisions, guardrail items, a flaw in
the plan. When in doubt whether something needs sign-off, ask it here. With nothing to sign
off, start without asking. Questions that surface after work has begun never end the turn
while ready work remains; they queue for the final report. If an open question would
invalidate a ready step, that step is held on it too.

## Deciding without the user

| The decision | Do |
|---|---|
| One option is clearly favoured by the plan, the codebase, or the user's stated preferences, and it is cheap to reverse | Take it. Log it. |
| High impact — architecture, data model, project scope, anything later work builds on or that is costly to undo | Hold it and present options with pros, cons and impact. Take it yourself only when the context lays the trade-off out so plainly that no other option makes sense; then it leads the final report as a critical decision. |
| A guardrail in the global `CLAUDE.md` — destructive or history-altering operations, private-data egress, live operations on real accounts, security / permission / hook / settings changes | Always the user's. Hold. |

Within the first row, these are explicitly allowed: choosing between implementation
approaches, fixing an adjacent bug or adding a small piece the plan missed (its own commit),
adding a vetted, pinned, hash-locked dependency, and reordering — starting a later
independent phase or rewriting a step reality has overtaken.

Log each decision in the worklog as it is made: what was chosen, the alternative, and the
commit that reverts it. The bar is that the user rarely has to undo one; when a decision is
not clearly in the first row, it is in the second.

## Grok

An autopilot run is long and unattended, so Claude's quota is what ends it early. Every step
whose design is settled and whose check is a command goes to Grok per the `grok-handoff`
skill. Call `quota` when mapping the work and mark each
step's executor in the plan or worklog. Steps that stay in the session: diagnosis, anything
checked on a device, and whatever `grok-handoff` §2 disqualifies. While a Grok job runs, take
a ready step that touches different files; `grok-handoff` §4 sets how many jobs may run at
once.

## Gates

- **Mechanical gate** — the pass criterion is something you can observe objectively (state
  survived a restart, a log line appeared, a value matches): run it, record the evidence,
  mark it `DONE <sha> (self-passed)` in the plan index, and let its dependents proceed. A
  failure is a bug: fix it and rerun.
- **Human gate** — needs the user's taste, their normal use over time, a soak period, or a
  physical action: hold it. Finish everything around it so the user's part is the minimum —
  build installed, exact steps, what a pass looks like.

## Devices

Autopilot is standing permission to drive any device the local adb connection helper reaches — install,
navigate, read logs, and screenshot when logs cannot answer the question. Text may be typed
into a field but never sent, submitted or purchased. A device the user called busy or
off-limits is not touched. Undo any state a test wrote.

## Final report

In this order, each item one or two lines:

1. **Critical decisions taken** — the trade-off, the alternative, how to revert. Omit if none.
2. **Needs you** — numbered and answerable in chat: decisions (options, recommendation,
   impact) and actions (what to do, on which device, what a pass looks like), each with the
   steps it unblocks.
3. **Done** — step and sha.
4. **Decisions made** — "chose X over Y", with the revert sha.
5. **Resume** — whether to clear context. Leave the worklog and plan index so that a fresh
   session needs only the answers to 2 and "continue the plan".

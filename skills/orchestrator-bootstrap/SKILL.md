---
name: orchestrator-bootstrap
description: Resume or author a phased project plan that any agent can pick up cold. Use when told "continue this project", "where are we", "what's next", or when asked to write, update, or restructure a multi-phase roadmap, implementation plan, or punchlist that other agents or a later session will execute. Covers determining current state in two commands, per-step pass criteria that cannot be argued with, and human-test gates that must not be crossed.
---

# Phased plans any agent can pick up cold

## Resuming — do this before opening any phase file

1. `git log --oneline -8` and `git status --short`.
2. Read `docs/plans/README.md`. One line per phase: status and the sha that proves it.
3. Open **only** the phase file the index calls current.

**Trust the tree over the index.** A phase claiming done without a sha, or with a sha not
in the log, is not done. A dirty tree is work in flight — never `checkout --`/`stash` to
tidy it. Reconcile the index, state in one line what is done / in flight / blocked, continue.

If the current phase is awaiting a **GATE**, stop and report. Start another phase only if
the index already lists it as independent of that gate.

## Layout

```
docs/plans/README.md        index: phases, order, status, sha. The only file read on resume.
docs/plans/NN-<slug>.md     one phase. Loaded only when that phase is current.
```

Splitting by phase is what keeps resumption cheap — the index is short, and ten phases of
detail never enter context at once.

The index carries, per phase: number, name, one-line outcome, `needs:` (phase numbers),
status, sha. Nothing else — everything else lives in the phase file.

Statuses: `TODO` · `IN PROGRESS` · `GATE <id>` (awaiting the human) · `DONE <sha>` · `BLOCKED <why>`.

## Writing a phase file

Open with: the outcome in one sentence, what it depends on, what to read before starting,
and what it must not touch.

Then numbered steps. Each step names the work, the files it is expected to touch, and
exactly one pass criterion:

- `check:` — a command an agent runs, plus what its output must show. Deterministic and
  runnable with no human present.
- `GATE <id>:` — a human must do it. Name what they do, what passing looks like, and what
  to report back.

A step whose pass criterion is "it works" or "looks right" is not written yet. Rewrite it
until failure is unambiguous.

**Plans are ephemeral, so specify freely** — file names, message shapes, the exact command.
Under-specifying only moves the cost onto whoever executes it. (Persistent docs are the
opposite; see the doc-lifetime rule in the global `CLAUDE.md`.)

Size a phase so it ends in a commit. If it cannot, it is two phases.

## Briefing an implementer

A phase file is read by an agent that inherits nothing from the conversation that wrote it.
Carry these, because their absence is what makes a delegated task fail:

- **Baseline.** HEAD sha, tree state, and the current test count. "304 tests, green at HEAD"
  turns a later red run into a fact rather than an argument.
- **A reading budget.** Name the files to read, and for a large one name the *functions* and
  say not to read it end to end. Running out of context before writing a line of code is a
  real failure mode, not a hypothetical.
- **Facts as given.** Anything already established on device or by reading gets stated with
  "do not re-investigate". Re-derivation is the second-biggest waste after re-reading.
- **Decisions as settled.** Prefix a design already chosen with "implement this, do not
  redesign". Where a constraint looks like a mistake, say why under a heading that tells the
  reader not to improve it.
- **Ownership.** The files this phase may change, and the ones it may not — *even to fix
  something that looks broken*. Report those instead of touching them.
- **Prohibitions.** Whatever this project forbids: pushing, editing the primer, driving a
  device, writing outside the repo.

## Gates are hard stops

A gate exists because the next phase would build on top of the thing being tested, and a
bug caught after that becomes load-bearing.

- Never begin a phase that lists an unpassed gate in `needs:`.
- Never mark a gate passed on the agent's own judgment — only on the human's report.
- Under the `autopilot` skill, its gate and reordering rules replace the rule above and
  the independence rule below; every other rule here still holds.
- On reaching one: set the index status to `GATE <id>`, commit the work so far, and tell
  the user what to test, on which device, and what a pass looks like.
- Work with no dependency on the gate may continue **only** if the index already declares
  it independent. When in doubt, stop.

## Keeping it honest

- A phase is `DONE` only when its checks passed **and** it is committed, with the sha in
  the index. The checkbox is a claim; the sha is the proof.
- Update the index in the same commit as the work it describes.
- Ordering goes in the `needs:` field, never in prose. A resumed session should read
  ordering as fact, not re-derive it.
- When a phase ships, its file is archived or deleted. Rewrite a plan that reality has
  overtaken rather than annotating around it.

## The project primer

The project `CLAUDE.md` (or README) is how an agent gets oriented before it reads anything
else. It should be short and stable: what the project is, where the plan index lives, the
resume procedure above in three lines, and how to build and test.

It must **not** restate the plan or describe module behaviour — that is the persistent-doc
half of the lifetime rule, and a primer that drifts is worse than none.

## Relationship to the worklog

`~/.claude/worklog/<project>.md` tracks steps **inside the current phase** during a session
— cheap to write, outside the repo, survives a clean tree. The plan index tracks **phases**
and is the durable record. Split by lifetime, so neither restates the other.

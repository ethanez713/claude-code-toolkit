---
name: grok-handoff
description: Default route for implementation once its design is settled, under any Claude model — hand the step to Grok through the grok MCP server instead of writing it here, and use Grok as a cheap second reviewer of risky diffs. Grok's quota is separate from Claude's, so Claude pays only for a short brief and the review. Research, planning, architecture and diagnosis never go to Grok. Use without being asked; and on resume when the worklog names grok jobs.
---

# Grok handoff

Claude researches, designs, diagnoses and reviews; Grok implements. Grok gets none of the
research, planning or architecture: it is an implementer and a cheap second reviewer.
Claude's cost for a task
is mostly turns × context size, so every implementation turn moved to Grok is Claude quota
saved. A handoff costs about six turns: brief, result, two or three review reads, finish.
Treat Grok like a capable engineer: trustworthy, and it may make mistakes. Give it the goal
and the settled decisions, and catch its mistakes in review.

## 1. Route

Call `quota` once per session and before a batch of handoffs (`run` re-checks by itself).

| route | meaning |
|---|---|
| normal | apply §2 |
| conserve | Grok ≥ 85% used with > 36 h to reset: hand off only the largest eligible steps (≈ 20+ tool calls) |
| burn | Grok credit resets within 24 h with ≥ 20% unused: hand off anything §2 doesn't disqualify, reviews included |
| claude-only | Grok is out or its quota is unknown, or Claude's own quota is about to expire unused: do everything here |

When Grok's weekly credit hits 100% (a reading, or a credit error from a job), the server records
the reset time and every `run` fails until then, so don't attempt one: `quota` prints `route
claude-only (… out until reset <date>)`, and the lockout lifts itself at that time. A lockout
whose reset is unknown lifts at the first fresh read that shows credit.

Private data never goes to Grok: financial records, account data, PII, secrets. That work
stays here, or goes to the local Ollama model per `CLAUDE.md`. The server refuses a cwd
under a private path and the sandbox hides those paths, but this rule is broader than that
list.

## 2. Decide

Implementation goes to Grok by default, whichever Claude model is running; the user should
never have to ask. Once the design
is settled (what to build and the load-bearing decisions are known), hand the step off
(`mode: edit`) unless one of these holds:
- The bug isn't diagnosed yet. Diagnosis stays in the session; hand off the fix once you know it.
- The architecture is unsettled, or the work needs back-and-forth with the user. Settle it
  here first, then hand off the build.
- You'd finish it here in about 4 tool calls or fewer (a few-line change, a one-file tweak).
  The handoff's ≈ 6 turns cost more than that.
- Success can't be checked by a command Grok can run (tests, build, lint), and you can't
  name one in the brief.
- Verifying it needs gitignored files the worktree won't have (`.env`,
  `local.properties`, data fixtures).

A multi-step plan hands off step by step: each settled step is its own job (§4 pipelines
them), while the planning and the review between steps stay here.

Kinds handed off: `implement`, `fix`, `refactor`, `tests`, `build`, `other`.
Kinds kept here: none.
A Measurement verdict moves a kind between these two lines.

Use `mode: consult`, kind `review`, for an independent review of a risky diff before
committing. It's a different model family with different blind spots, and it can run while
you run the tests. A consult runs in `cwd` as it is, uncommitted changes included, and can't
modify files. Research, investigation and "where is X handled" questions are not consults:
they stay here.

State the choice and the reason in one line, then proceed.

## 3. Brief

Grok works out the implementation itself. The brief carries what it can't get from the code
(the architecture, the decisions and their reasons, the traps) and where to start reading.

```
Goal: <the outcome, 1–2 sentences>
Design: <the settled architecture and decisions, with the why where it isn't obvious>
Start at: <repo-relative files and symbols you already read that the change touches or must imitate>
Gotchas: <non-obvious constraints, traps, things that look wrong but are deliberate, what must not change>
Done when: <command and what it must show>
```

Aim for 300–1,500 chars. Keep implementation guidance high-level (which layer owns the
change, which pattern to follow); don't list steps and don't paste code. `Start at` is the
exception to brevity: Grok's cost is its model calls, and without it more than half of a
job's calls go to finding files you read while designing. List them; don't describe them. Every line should be something a capable engineer reading the repo cold
would get wrong or have to ask about. No private data. `kind` is the measurement key, so
choose it honestly.
- Write repo-relative paths. An absolute path into your checkout points Grok at your live
  tree instead of its worktree.
- `cwd` is where Grok starts, and project rules load from the repo root down to it: point it
  at the part of the repo the task is about. Grok writes wherever else the task needs.
- Settle any new dependency in Decisions, pinned. Grok's `ask` rules don't stop a headless job
  from installing packages, so check manifests and lockfiles in review for ones you didn't
  name.
- Defaults: effort `high`, model `grok-4.7`. Use `grok-4.7-build-fast` for purely mechanical
  changes.

## 4. Run in parallel

`run` blocks; Claude Code moves it to the background after 2 min, and its result arrives as
a notification. Use the wait:
- **Pipeline:** hand off step N, then design step N+1 or review N−1.
- **Fan-out:** concurrent jobs split by file ownership, each in its own worktree, up to the
  cap `quota` prints (`max N concurrent`); `run` refuses a job past it.
  Shared-boundary files (a common interface, a registry, a build file) stay with you, edited
  before or after the fan-out. If the files don't partition cleanly, go sequential.
- **Cross-check:** a consult review of your own risky diff while the tests run.

When `run` backgrounds, call `result()` to read the new job's id, and put it on the step's
worklog line (`grok g1a2b3c`).

Once backgrounded, the task's status line in Claude Code's task list shows live grok activity
(`grok g1a2b3c 06:12 · 64 tools · edit Streak.kt`), read from grok's own transcript at no token cost.

## 5. Review

The report is a claim, not evidence.
1. Read STATUS, then ASSUMPTIONS and OPEN. Assumptions are where an underdetermined brief
   turned into code.
2. Run the project's check yourself in the worktree (`<repo>/.grok-jobs/<id>`), with its
   output trimmed.
3. Read the diff selectively with `git diff <base>..grok/<id> -- <file>` for the files that
   carry logic; read the whole patch only when it's under ≈ 150 lines. Walk each of these
   as a targeted read:
   - state that is set but never cleared (selection, loading flags, cache keys)
   - encoding at a boundary (URL/query escaping, JSON/HTML, paths with spaces)
   - timestamps re-stamped on read, or the wrong date field in user-facing text
   - fields nulled on an error path the caller still needs
   - locale and format assumptions (decimal separators, date order)
   - UI added but unreachable: trace it from the entry point. On phone projects the
     smoketest settles this.

## 6. Escalate by failure class

- **Thoroughness** (missed edge case, forgotten file, shallow tests): `run(job=<id>,
  task=<the specific defects>)`, which resumes the same worktree and Grok session.
- **Reasoning** (wrong approach, misread architecture, ambiguous brief): `finish discard`,
  then do it here.
- **Anchored** on a wrong approach: discard, then `run` fresh with a corrected brief.
- Cap: 3 Grok rounds per feature, then finish it here. A new, distinct request resets the
  count.

## 7. Finish

`finish(job, "merge", message)` squash-merges onto the current branch as one commit.
- Write the message as for your own commits, attribution lines included; the server adds
  `Grok-Job:`.
- It refuses a tree with uncommitted tracked changes, so commit your own work first and
  never stash.
- On a conflict it leaves your branch untouched and keeps the Grok branch.
- After merging, push and (on phone projects, when the change needs the device) run
  `android-smoketest`, exactly as for your
  own commits.

`finish(job, "discard")` drops work you won't take. Consult jobs finish themselves.

## 8. Resume

Jobs outlive the session. `result()` lists unfinished jobs and `result(job)` re-attaches.
Where the worklog and the job disagree, trust the job.

## Measurement

Every round logs a `job_end` event (tokens, Grok % before and after, Claude % at round end,
diff size, status), and `finish` logs the outcome. `scripts/report.py` joins these with the
transcripts and with an inline baseline built from Claude's own commits.

The rule is pre-registered. Never change it because of the verdicts it produces; propose a
change to the user instead.
- **Question:** for each `kind`, does a handoff save Claude quota, and what does a point of it
  cost in Grok credit? The exchange ratio is R = Grok % spent ÷ Claude % saved.
- **Minimum n:** 5 finished edit jobs of that kind.
- **Delegate by default:** ≥ 70% merged and the median job saves Claude quota. **Keep inline:**
  < 50% merged, or the median job saves nothing. Otherwise the kind is unchanged.
- **R ranks kinds; it never switches one off.** Grok credit unspent at the weekly reset is
  worth nothing, so a poor ratio is a reason to spend the credit on a better kind first, not
  to leave it unspent. Under `conserve`, hand off the lowest-R kind first.
- A job's saving is the median cost of Claude's own commits within 2× of its diff size, less
  the handoff's overhead. Never a per-line rate: cost per line falls about 35× between a
  10-line commit and a 1,000-line one.
- The tool-call floor in §2 moves to the measured break-even once there are ≥ 20 baseline rows
  and ≥ 10 jobs.

self-improve applies these verdicts. Don't hand-tune §2 from anecdotes.

## Troubleshooting

- Grok refuses to start, or reports an auth error: ask the user to run `! python3
  ~/.claude/skills/grok-handoff/server/doctor.py`.
- A call ended after 30 min with no result: the job kept running, so use `result(job)`. If
  it recurs, ask the user to set `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` in the settings env.
- `grok_out`: the route stays claude-only until the recorded reset time, or, when none was
  recorded, until a fresh read shows credit. Clear a wrong lockout by deleting
  `~/.local/state/grok-mcp/grok_out`.

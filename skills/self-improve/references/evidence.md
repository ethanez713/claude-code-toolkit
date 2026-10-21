# Evidence base

**last_verified: 2026-07-25** · re-verify when older than **90 days**, or before any run
that would warn about context-file size.

Every consolidation warning and routing preference below must cite a line from this file.
No warning without evidence. If a claim here is stale and unconfirmed, the rule it backs
is **dormant**, not "applied anyway".

## Context-file size does NOT measurably hurt adherence

McMillan 2026, *Instruction Adherence in Coding Agent Configuration Files: A Factorial
Study of Four File-Structure Variables* (arXiv:2605.10039) — 1,650 Claude Code CLI
sessions, 16,050 function-level observations, 2 TypeScript codebases, Sonnet 4.6 primary
with Opus 4.6 cross-model check, mixed-effects models + Bayesian companion:

- Manipulated **file size 25–500 lines**, instruction position, file architecture (split
  across files), and presence of a contradicting instruction.
- **None** of the four variables, nor three two-way interactions, produced a detectable
  contrast after multiple-testing correction.
- Size and conflict nulls carry **affirmative-null Bayes factors (BF₁₀ 0.05–0.10)** —
  positive evidence *for* no effect, not merely failure to detect. Position and
  architecture are failures to reject, without Bayes support.

**Therefore:** do not warn about global or project `CLAUDE.md` length while it sits inside
25–500 lines. Popular "past 200 lines Claude ignores your rules" advice is not supported by
the only controlled experiment on the question; treat it as folklore. A size warning is
justified only when a file exceeds **500 lines** (outside the tested range, so genuinely
unknown), and it must say so in those terms.

## What does degrade compliance: within-session depth

Same study, largest measured effect:

- Each additional function the agent generates within a session carries **~5.6% lower odds
  of compliance** (OR = 0.944), non-monotonic rather than a constant per-step decay.
- A substantial share of the attenuation lands in the **first three to four generated
  functions**; median first omission at generation position **4**.
- Reproduced in direction on a second codebase and on Opus 4.6.

**Do NOT compound this into a session-level claim.** The effect is explicitly non-monotonic
and front-loaded, so `0.944^N` is not a valid estimate of compliance at step N, and sessions
of 50+ edits are outside the tested range. `edit_steps` (every Edit/Write call) is also not
the paper's unit — it counts re-reads and config tweaks, not generated functions. The
compliance proxy was one trivial annotation, not a body of user rules.

**Tested locally in run-000 and not reproduced:** violations per 100 edit steps by session
quintile were 8.1 / 11.0 / 4.7 / 1.2 / 6.8 across 38 sessions — no late-session trend. See
`state/BACKLOG.md` § Rejected after measurement.

**Therefore:** `edit_steps` is reported as context only, not as a friction finding. Treat any
published effect size as a hypothesis to test against this box's data before acting on it.

## Task identity beats file structure

Same study: a **26.2 percentage-point** compliance gap between two specific tasks (refactor
of an existing layout component vs. greenfield analytics-dashboard build) — a stronger
predictor than any structural variable. Reported as a single-contrast observation between
two tasks, not a modify-vs-write generalisation.

**Therefore:** prefer rules attached to a task type (a skill that loads for that work) over
another line of global prose.

## Context files pay for themselves on cost

Lulla et al. 2026, *On the Impact of AGENTS.md Files on the Efficiency of AI Coding Agents*
(arXiv:2601.20404, JAWs @ ICSE): mean output tokens **5,744.81 → 4,591.46 (−20.08%)** with
an AGENTS.md present. The mean falls more than the median, so the saving comes from
suppressing a few very high-cost runs rather than uniformly.

**Therefore:** context files are cost-positive. Do not trim for trimming's sake — trim only
what is dead, duplicated, or superseded by mechanical enforcement.

## Prefer mechanical enforcement over prose

McMillan 2026 §1.1 notes config files carry "no schema and no built-in confirmation that
[their] rules have been followed (without supporting tooling — agent hooks, guardrails,
linters, CI checks) a missed instruction may go unnoticed."

**Therefore:** route a fix to a hook, a permission allowlist, or a wrapper script when one
can express it. Justification is enforceability and detectability, **not** file size.

## Detection quality: hybrid, not regex alone

`netresearch/coach` collected friction via always-on hooks and regex over corrections, exit
codes, and tone, into SQLite. It was **retired** in favour of `retro-skill`, whose stated
reason is that "pre-recorded heuristic proxies" produced worse signal than one LLM pass over
the actual session. `retro-skill` also caps output (≤10 findings per medium session) and
rejects 1000-candidate generation.

**Therefore:** regex indexes and ranks; the LLM adjudicates the shortlist. Never ship a
finding on a regex hit alone — every detector here has known false-positive modes (a
legitimate `adb pair` after `source env.sh`, a screenshot the user explicitly asked for).

## Delta updates, not rewrites

Zhang et al., *Agentic Context Engineering* (arXiv:2510.04618, ICLR 2026): contexts as
evolving playbooks via Generator / Reflector / Curator, where the Curator emits compact
delta entries **merged by deterministic non-LLM logic**. Motivated by two named failure
modes — **brevity bias** (summarising away domain detail) and **context collapse**
(iterative rewriting eroding detail). +10.6% on agent benchmarks, +8.6% on finance.

**Therefore:** edit by targeted insertion/removal. Never regenerate a whole CLAUDE.md or
SKILL.md from a summary.

## Self-evolving agents misevolve

*Your Agent May Misevolve* (arXiv:2509.26354) and the constitutional-enforcement literature:
documented failure modes are proxy-metric reward hacking, silent policy drift, and memory
poisoning. Recurring mitigations: immutable audit log, versioning with rollback to
last-good, and safety constraints extracted into a deterministic machine-checkable path
rather than left to model judgement.

**Therefore:** git-backed rollback, append-only logs, declarative (never shell) metrics, a
propose-only constitution, and per-run blast-radius caps.

## How to refresh this file

1. Search for newer controlled studies on agent instruction adherence and context-file
   structure. Prefer measured experiments with sample sizes over practitioner blog claims;
   the two disagree sharply on size.
2. Update the numbers, the citation, and `last_verified`.
3. If a threshold moves, log it as a change with its own metric — the thresholds are
   themselves hypotheses under test.

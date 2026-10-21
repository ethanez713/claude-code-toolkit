---
name: review
description: Strict principal-engineer code review followed by a mandatory Implement/Optional/Dismiss triage, tuned for small local personal projects (no scale/availability concerns; simple code wins). Use when asked to review code, a diff, a file, or a whole repo.
---

# Strict review + triage

Two phases, always in this order. **Phase 2 is the point — never stop after phase 1.**
If the project has a `CLAUDE.md` with its own review instructions, follow those too; they
usually encode this same flow plus project specifics. Review **locally** — read the code
with your own tools; never paste the code/diff into an external service or download remote
tooling to do the review.

## Phase 1 — Review (exhaustive, nitpicky)

Take the stance of an experienced principal engineer and surface **every real issue you
can find** — including ones you are unsure about or think are low-severity. Your job in
this phase is **coverage, not filtering**: phase 2 is the filter, and it can only rank
findings that reached it. Surfacing something that later gets dismissed is the cheap
outcome; silently dropping a real bug because it looked minor is the expensive one. Do not
pre-filter for brevity or politeness. Cover:

- **Correctness** — bugs, edge cases, and **silent failure modes** (swallowed exceptions,
  wrong defaults, unhandled None/empty/NaN, off-by-one, mis-ordered logic).
- **Intent** — does the change actually do what it claims / what I asked? If the intent is
  unclear, ask before reviewing rather than guessing.
- **Readability** — naming, structure, dead code, duplication. If a section is hard for
  *you* to follow, future readers won't manage either: say so and propose clearer
  naming/structure instead of silently reverse-engineering it. On comments, apply the
  global `CLAUDE.md` policy: a comment that restates the code (or a banner, or a docstring
  echoing the function name) is a finding, and the fix is usually a better name rather than
  a better comment. A comment that has already drifted from the code it describes is a
  correctness finding, not a nit. Docs in the diff get the stateless-spec rule from that
  same file: a changelog, a date, a "previously/no longer", or narration of the edit itself
  in a `CLAUDE.md`, README, skill or spec is a finding, and the fix is to state the current
  rule and let git hold the history.
- **Consistency** — does it match the patterns and style already in the codebase? Diverging
  from an established pattern (even if independently valid) is a finding.
- **State / IO** — file/IO handling and error paths.
- **Security** — hardcoded secrets/credentials; secrets written to logs or committed files;
  injection (shell, SQL, CSV/formula); unsafe deserialization (`pickle`, `yaml.load`,
  `eval`); overbroad file permissions or API/OAuth scopes; unexpected network egress;
  untrusted input trusted blindly. Defer to the security baseline in the global `CLAUDE.md`.
- **Speculative generality (YAGNI)** — code solving problems it doesn't have yet: unused
  parameters, premature abstraction, config for a single caller. It's the bloat that makes
  future fixes harder.
- **Test coverage of the change** — does changed/added logic have a test that would catch
  its regression? Are existing tests still meaningful, or now asserting the wrong thing?

Be concrete: cite `file:line`, state the actual failure or risk — not vague unease. Tag
each finding with a **confidence** (how sure you are it's real) and a **severity**, so
phase 2 has something to rank on. Prefix purely cosmetic/preference comments with
**"Nit:"** so they're distinguishable from substantive findings.

## Phase 2 — Triage (mandatory)

Do an audit pass over your **own** findings and classify each one. The lens (default unless
the project's `CLAUDE.md` says otherwise): **this is a small, local, single-user,
non-productized project.** Scale, availability, concurrency, multi-user, and
theoretical/hypothetical concerns **do not apply**. Simple code and fast runs win; added
abstraction, defensive layers, feature flags, and compat shims are themselves a cost
because they make future fixes harder. (If a project is genuinely productionized or shared,
raise the bar accordingly.)

**Security is the exception to this lens:** a real secret exposure, data leak, or injection
risk is always **Implement** — never Dismissed as "theoretical," even for a personal project.

- **Implement** — genuine bug or meaningful maintainability risk. Worth doing now.
- **Optional** — real but low-priority for a personal script.
- **Dismiss** — scale/availability/theoretical/over-engineering.

Bias toward **fewer, higher-confidence fixes** over comprehensive coverage. This bias
belongs to phase 2 only — do not let it reach back and thin out phase 1.

## Then act

1. **Implement** the Implement tier.
2. Show the **Optional** list and ask before doing any of them.
3. List the **Dismissed** items briefly for visibility; do not act on them.

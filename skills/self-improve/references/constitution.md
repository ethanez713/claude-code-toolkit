# Constitution

Regions an unattended run may **never** write. It may — and should — *propose* changes here.

## Protected regions

In `~/.claude/CLAUDE.md`:

- **`## Security baseline for local / small / personal projects`** and every subsection
  (secret & credential management, network & data egress, supply-chain hardening, untrusted
  input, general hygiene).
- **`## Version control`** — the commit/push-to-main permission and the destructive-op
  guardrails.

Also protected, everywhere:

- This file, and `references/evidence.md`'s cited numbers (refresh via the documented
  procedure, not by ad-hoc edit).
- **The phase 7 fetch rules** in this skill's own `SKILL.md` — read-only, no third-party
  execution, one-way traffic, primary-source authority — and the matching hard rule. That
  phase reaches the network and then edits this machine's configuration; relaxing its own
  limits is not a thing an unattended run gets to do to itself.
- `state/external-truth.json`: entries may be added freely and `last_verified` stamped
  **only after an audit actually happened** (`state/drift.jsonl` is the trail). Deleting an
  entry, or stretching its cadence so a check stops coming due, is propose-only — that is
  the cheapest available way to fake this phase, so it is closed off explicitly.
- Anything under `~/.secrets/` or any project's `.secrets/` — never read, never write.
- Project source code. self-improve edits configuration and skills, not applications.

## Propose-only protocol

Silence is a failure mode here. If a protected rule is *itself* the friction source, that is
a finding worth surfacing, not one to drop.

1. Write the proposal to `state/BACKLOG.md` under `## Needs approval (constitution)`, with
   the evidence: signal id, hit count, sessions affected, and the concrete diff.
2. Register it with `impact.py add --constitution` so it is tracked like any other change,
   with status `proposed` rather than `probation`.
3. **Surface it in the run summary**, at the top, above ordinary auto-applied changes. Never
   bury a constitution proposal in a backlog the user has to go looking for.
4. Apply only after explicit in-session approval. Approval covers that one diff — it does
   not carry to the next run.

Precedent: the user asked for exactly this in prompt #701 — *"my instructions and skills are
too restrictive in what Claude can download/send over network... loosen the restrictions
appropriately, I want to be sensitive to security concerns, but our current stance is too
restrictive."* Over-restriction is real friction. Surface it; let the user decide.

## Direction rules

- Outside the constitution: may add, tighten, loosen, or delete freely, within the per-run
  cap and with probation tracking.
- Inside the constitution: **loosening is always propose-only.** Tightening is too — an
  unattended run must not widen *or* narrow the security posture unreviewed.

## Blast radius per unattended run

- ≤ **3** applied changes.
- Writes confined to: `~/.claude/CLAUDE.md` (non-protected regions), `~/.claude/settings.json`,
  `~/.claude/skills/**`, `~/.claude/projects/<project>/memory/**`, project `CLAUDE.md`
  files, and this skill's own `scripts/` + `state/`.
- Every applied change is committed to the `~/.claude` git repo tagged with its run id, so
  `git revert` is always available.
- Metrics stay declarative. An unattended run must never execute a self-authored shell
  command as part of measurement.
- Transcript content never leaves the machine. No cloud model, no web request carrying
  session text — this is the user's own egress rule applied to the skill itself.

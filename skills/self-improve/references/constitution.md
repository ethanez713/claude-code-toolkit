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
- **The fetch rules** in this skill's own `SKILL.md` (step 6) — read-only, no third-party
  execution, one-way traffic, primary-source authority — and the matching hard rule. That
  step reaches the network and then edits this machine's configuration; relaxing its own
  limits is not a thing an unattended run gets to do to itself.
- `state/external-truth.json`: entries may be added freely and `last_verified` stamped
  **only after an audit actually happened** (`state/drift.jsonl` is the trail). Deleting an
  entry, or stretching its cadence so a check stops coming due, is propose-only — that is
  the cheapest available way to fake that step, so it is closed off explicitly.
- Anything under `~/.secrets/` or any project's `.secrets/` — never read, never write.
- Project source code. self-improve edits configuration and skills, not applications.

## Propose-only protocol

Silence is a failure mode here. If a protected rule is *itself* the friction source, that is
a finding worth surfacing, not one to drop.

1. Write the proposal to `state/BACKLOG.md` under `## Needs approval (constitution)`, with
   the evidence: error signature, sessions affected, and the concrete diff.
2. **Surface it in the run summary**, at the top, above ordinary auto-applied changes. Never
   bury a constitution proposal in a backlog the user has to go looking for.
3. Apply only after explicit in-session approval. Register it with `audit.py fixes add` like any other fix. Approval covers that one diff — it does
   not carry to the next run.

Over-restriction is real friction too: the user has asked for over-tight network rules to be
loosened. Surface it; let the user decide.

## Direction rules

- Outside the constitution: may add, tighten, loosen, or delete freely, within the per-run
  cap, each fix registered in `state/fixes.json`.
- Inside the constitution: **loosening is always propose-only.** Tightening is too — an
  unattended run must not widen *or* narrow the security posture unreviewed.

## Blast radius per unattended run

- ≤ **3** applied changes.
- Writes confined to: `~/.claude/settings.json`, `~/.claude/hooks/**`, `~/.claude/skills/**`,
  `~/claude-code-toolkit/**`, wrappers in `~/.local/bin/`, this skill's own `state/`, and
  non-protected regions of `~/.claude/CLAUDE.md`, memory and project `CLAUDE.md` files for
  correcting a false statement only.
- Every applied change is committed, tagged with its run id, to the git repo that holds the
  edited file once links are resolved (`git -C "$(dirname "$(realpath <file>)")"`): `~/.claude`,
  or the public toolkit a skill links into. So `git revert` is always available.
- Metrics stay declarative: a fix's signature and opportunity are regexes, never shell
  commands an unattended run would execute.
- Transcript content never leaves the machine. No cloud model, no web request carrying
  session text — this is the user's own egress rule applied to the skill itself.

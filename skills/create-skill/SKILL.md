---
name: create-skill
description: Build or refine a reusable skill by mining the user's own history for their idiosyncratic preferences, combining them with researched best practices, letting the user pick what goes in, and writing a streamlined SKILL.md. Use when asked to create, generate, write, or refine a skill.
---

# Creating a skill

Goal: capture how **this user** wants a recurring task done — their idiosyncrasies plus
proven best practice — as a portable, streamlined `SKILL.md`. Work the steps in order.

## 1. Mine the user's history first

Before researching anything, extract the user's **own** stated preferences on the topic —
the specifics that wouldn't come by default. Search past transcripts in
`~/.claude/projects/*/*.jsonl` (each line is a JSON event; keep `type=="user"` +
`message.role=="user"` text, drop tool results, `<...>` injects, and `Caveat:`/continuation
lines). Grep topic keywords across **all** projects, dedupe, read the real prompts, and
quote the recurring patterns and hard rules.

## 2. Then research best practice

Do deep web research: canonical guides, conventions, real examples from others. Cite
sources. Keep only what's genuinely additive; discard advice that doesn't fit the user's
context (e.g. team/process overhead for a solo personal project).

## 3. Separate quirks from defaults, then combine

Capture the user's idiosyncrasies (that's the point) and the high-value external practices.
**Drop anything Claude already does by default** — generic advice is noise that bloats the
skill.

## 4. Surface design decisions for the user to sign off

The user would rather decide up-front than discover a weird encoded rule later — they likely
won't re-read the finished skill, so a silently-encoded inference they didn't want is worse
than an extra question. **Lean toward asking.** Present as explicit choices (the question UI
with concrete previews when comparing helps): researched additions, genuine judgment calls,
**and any preference you are *inferring* rather than quoting back to them**. Bake in without
asking only what the user has explicitly and unambiguously stated. When in doubt, ask —
re-asking is cheap; silently encoding something unwanted is not.

## 5. Keep it portable

Write for **all** the user's projects, not the one in front of you. Strip project-specific
paths, flags, and names — those belong in that project's `CLAUDE.md`. State assumptions as
defaults a project `CLAUDE.md` can override.

## 6. Streamline (mandatory)

After combining quirks + research, audit for bloat: no concept stated twice, collapse
overlaps, single-screen and scannable. State **what to do, not why** — once loaded, every
line is a recurring token cost. Prefer one generalizable principle to a list of instances;
if you are writing a third example of the same idea, write the rule behind them instead.

A `SKILL.md` is a spec, so it is stateless: it describes the current practice with no
changelog, no dates, no "this used to say", no note of which pass added what. Refining a
skill means rewriting the rule, never appending an amendment under it. See the
stateless-spec rule in the global `CLAUDE.md`.

Then tell the user what you deliberately left out and why.

## 7. Authoring mechanics

- Path: `~/.claude/skills/<name>/SKILL.md` (global, cross-project) or
  `<project>/.claude/skills/<name>/SKILL.md` (project-local). **The directory name becomes
  the `/command`.**
- Frontmatter `name` + `description`. Write the **description as a trigger**: "<what it
  does>. Use when <intents / phrases>." — so Claude auto-invokes it by intent, not only by
  slash command. Put the key use case first (the listing truncates ~1,536 chars).
- Body: concise standing instructions, ideally < ~500 lines. Move large reference material
  or examples to supporting files in the skill dir and point to them, so they load on demand.
- Add `disable-model-invocation: true` only for side-effecting actions the user must trigger
  deliberately (deploy, commit, send). Most skills stay default (both invocable).
- Avoid command-name collisions with built-in/bundled skills (e.g. `/code-review`).

## 8. Make it discoverable beyond the slash command

Add a one-line behavioral pointer in the global `CLAUDE.md` ("when asked to X, follow the
`<name>` skill") so it's honored by intent even when not typed.

## 9. Verify

Read the assembled `SKILL.md` back; make sure sections don't contradict (scope exceptions
explicitly). If you just built it, offer to dogfood it on a real task for immediate feedback.

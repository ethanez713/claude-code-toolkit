---
name: refine-spec
description: Turn a rough idea into a finalized spec, plan, or design doc through a question-and-answer loop — draft, surface the load-bearing open decisions in chat with enough context to answer them, fold the answers in, repeat until nothing is open. Use whenever planning, writing or revising a spec/plan/roadmap/design doc/RFC, when a long rough prompt describes something to build, or when asked what's still undecided, what needs review, or to finalize a doc.
---

# Refining a spec by loop

A rough idea becomes a finished spec by draft-and-interrogate rounds, not by one long
generation. Run the loop by default whenever the output is a doc that something later gets
built from — no need to be asked for it.

## The loop

**1. Draft.** Write the doc, taking the shape from the target: an implementation plan follows
the `orchestrator-bootstrap` skill, an ephemeral plan specifies freely, a persistent doc stays
short. Every doc obeys the stateless-spec rule in the global `CLAUDE.md` — including between
rounds, where the temptation is worst.

Where the answer is genuinely unambiguous, **decide it and write it in**, then list it in the
round as decided rather than asking. A question with an obvious answer costs a round trip and
buys nothing.

**2. Surface the open decisions in chat.** Not as a `## Open questions` section in the doc, not
as `TBD` markers, not as a file to go read — in the conversation, numbered, answerable in one
reply. Order them by blast radius: the ones later choices hang off go first.

**3. Fold the answers in.** Edit the doc so it reads as if the answer was always the design.
Do not record that a question was asked, who answered, or what was rejected. Then re-open the
consequences: an answer usually invalidates something downstream or creates a new fork, and
that is the next round's material.

**4. Repeat until a round has nothing load-bearing left**, then say so plainly and stop. Don't
manufacture a final round of cosmetic questions to look thorough.

## What earns a question

Ask when **different answers produce materially different work** and no default is clearly
right. In practice that is:

- A fork where both branches are defensible and the choice constrains later steps.
- Scope: what is in this build versus a later one.
- A threshold, default, or policy value with no principled derivation.
- A tradeoff only the user can price — their time, their risk, their taste, their money.
- Anything touching the constitution: live data, real accounts, secrets, destructive ops.
- **A preference you are inferring rather than quoting.** A silently-encoded guess the user
  didn't want is worse than an extra question, because they won't re-read the doc to catch it.

Decide it yourself when there is a clear optimum, a strong convention, an existing pattern in
the codebase, or the global/project `CLAUDE.md` already answers it. **Say which ones you
decided and why, in one line each** — that is where a wrong assumption gets caught cheaply,
and it is not the same thing as asking.

Don't ask what you can find out: read the code, run the command, check the file first.

## Shape of a round

Numbered list in chat, so several can be answered in one freeform reply — that is how the
answers actually arrive, often with unprompted extra direction attached. Reserve
`AskUserQuestion` for a genuinely closed set of options worth comparing side by side.

Each item carries, tightly:

- **What is being decided**, in a sentence, in the user's terms rather than the code's.
- **What it affects** — the sections or steps that change with the answer, and anything
  already written that assumes one branch.
- **The real options** with the tradeoff that separates them. Not a survey; two or three.
- **Your recommendation and why.** Always give one. A question with no default makes the
  user do the reasoning twice.
- **What you'll do absent an answer**, so silence is a valid reply.

Carry enough context that the item stands alone. The user is answering from the chat, not
from a re-read of the doc, and may be answering hours later on another device.

Keep the round to what fits in one reply — roughly 3–7 items. Beyond that, split by dependency
and send the blocking ones first; the rest are usually resolved by the answers anyway.

## Between rounds, and at the end

The doc is always in a state where a cold reader would take it as final. That means no
question markers, no alternatives left in as commentary, no "pending user input", and no note
of which round produced what. Open questions live in the chat; the doc holds only resolutions.

If a decision's rationale would stop a later agent from "fixing" it, keep that as one
present-tense clause on the rule. Everything else about how the doc was reached is provenance.

## The cold review

When the doc is done, offer a **fresh session** to review it — a reviewer with none of this
context, so it cannot rubber-stamp a claim it helped invent or inherit an error that has been
repeated until it reads as settled. The cost of the loop is that late rounds trust early ones.

Hand it over as: read this doc at this path, plus the commands to verify its factual claims
against the repo, plus the question — is this internally consistent, does it match what the
code actually does, and is any step unbuildable as written? Do not summarize the design or the
reasoning behind it; a summary re-imports exactly the context the fresh session exists to
exclude.

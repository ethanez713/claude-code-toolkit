---
name: orchestrator
description: Show what is waiting on the user across every project and session, closed ones included, log their answers, and offer to restart the work. Use when asked "what's waiting on me", "what needs my attention", "what should I look at", "what's blocked on me", "where do things stand across projects", when the user answers an item from the board, or /orchestrator. For resuming one project's own plan, use `orchestrator-bootstrap`.
---

# Orchestrator

`orch` (`~/claude-code-toolkit/orchestrator`) derives the board from the projects. Change what it says through its
commands or the projects themselves, never by hand-keeping a list. Contract: `docs/design.md` there.
The user is often on their phone over Remote Control: one screen, no preamble.

## Show

1. Run `orch`. If it lists session endings to review, run `orch sweep`, read each ending, and either
   `orch add` an item for a real ask or `orch sweep --ack <session>`. Rerun `orch`.
2. Show the board with its numbers. Lead with Right now, then Decide, since those clear fastest. Say
   when an item looks stale because git or the plan contradicts it, and `orch done` it once confirmed.
3. Read a project's plan or worklog only to answer a question about an item.

## Log answers

Answers arrive freeform and numbered, often several at once ("1 drop it, 3 passed"). For each:

- `orch answer <ref> "<the user's words>"`. Numbers change on rescan, so use ids after one.
- "Later" or a date: `orch snooze`. "No longer applies": `orch done`.
- Record a gate as passed only if the user said so.

Logging never starts anything.

## Offer the kick-off

After logging, run `orch route <project> --json` for each project with answers and make one offer, one
line per project, naming what would happen:

```
notes-app: live and idle at 62%, worklog current → /clear, then continue
camera: no live session → new window
planner: busy → deliver when it goes idle
```

Nothing starts without a yes. The user may take a subset or change a reset.

Follow the route's recommendation, with one override: when a live session's current work is unrelated
to the answered items, offer `clear` if its worklog is current and its tree clean, else `compact`.

## Deliver

On a yes: `orch deliver <project> "<text>" --reset <none|clear|compact>`, with this text unless the user
said otherwise:

> Answers to waiting items are logged as `[a]` lines in your worklog. Apply them, then proceed with the plan.

If the user says a device is available, add that the session may drive the gates it can.

| Route | Do |
|---|---|
| `wait` | `SendMessage` to that session with `notify_when_idle: true` and no message; on the notice, re-route and deliver |
| `attend` | Tell the user that session has a dialog open |
| `unreachable` | Tell the user it isn't in tmux; offer `new` only if they close it first |
| refused | Report why. Never clear the input box: it may hold the user's typing |

Report what was sent where. The work happens in the project's session, not here.

## Drive an item yourself

Only when the user asks this session to clear items itself. Prefix every `aconnect` call with
`ACONNECT_PROJECT=orchestrator`, because a phone reserved for named projects admits this session only
under that name. Filter a phone browser's DevTools `/json` listing to the app's own origin before printing it,
since the unfiltered list exposes every tab the user has open. Leave a gate as `[?]` with your evidence;
it passes only on the user's word.

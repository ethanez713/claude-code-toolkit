# Orchestrator design

One place that says what is waiting on the user, across every project and session, including
sessions that have closed. The user visits this directory or talks to its Claude session over
Remote Control.

## Principles

- **Derive, don't duplicate.** Waiting state lives in the projects (worklog lines, plan index
  rows). The board is generated from them and holds no second copy, so editing a project is the
  only way to change what the board says. The only stored state here is what no project can own:
  snooze dates, first-seen dates, reviewed session endings.
- **No LLM in the scanner.** It is deterministic and stdlib-only, cheap enough to run from a tmux
  status line. Judgement lives in the `orchestrator` skill.
- **Nothing leaves the machine.** Generated output and stored state are gitignored because item
  text can quote private project data.
- **Answering and acting are separate.** Answering only writes to the project's worklog. Starting
  an agent is a distinct step the user approves.

## Worklog markers

Worklogs are `~/.claude/worklog/<project-dir-name>.md`. Two line states carry the contract:

```
- [?] <kind> | <where> | <text> | unblocks: <x> | gate: <id> | since: YYYY-MM-DD
- [a] <kind> | <where> | <text> | answer: <text> | answered: YYYY-MM-DD
```

Only `kind`, `where` and `text` are required; fields after them are `name: value` and any order.
Text containing ` | ` is not supported: the separator is reserved. A malformed `[?]` line still
appears on the board, with kind `?`, so a mistake is visible and never silently dropped.

| Kind | Meaning | Reminder that fits |
|---|---|---|
| `decide` | A choice answerable in chat | Any time, any device |
| `check` | A short test the user performs now | When the named device is at hand |
| `event` | Can only happen during real use (next shop, next cook) | When that use happens |
| `soak` | Runs in the background over days, then gets judged | A start, then a follow-up |
| `desk` | Admin at the computer (push, revoke, restart) | At the computer |

`where` is `anywhere`, `phone`, `desk`, or a device name.

An item's id is `<project>#<4 hex of sha1(text)>`, stable when the line turns from `[?]` to `[a]`.
An agent that picks up an `[a]` line applies the answer, then deletes the line.

## Sources the board reads

1. **Worklog markers**, per above.
2. **Plan index gates.** `docs/plans/README.md` table rows whose last cell (status) names an open
   gate: `GATE <id>` or `GATE-<id>`, unless the word after the id is `passed` or `accepted` or the
   cell starts with `DONE` or `CLOSED`, since a finished row may name a later gate. A bare `GATE:`
   is an open gate with id `row-<#>`. Kind
   `check`, where `phone`, text from the row's phase and outcome cells, id `<project>#<gate id>`.
   A worklog line with `gate: <id>` replaces the row, which is how a gate gets a richer kind or
   text. Answering a gate row appends an `[a]` line carrying `gate: <id>`, and the row stays on the
   board under Answered until the plan index changes. Marking a gate passed is the picking-up
   agent's job, on the user's report.
3. **Git state** of each project: uncommitted files or unpushed commits become derived `desk` items
   (`<project>#git-dirty`, `<project>#git-ahead`), unless a session in that project is busy.
4. **Live sessions**: `claude agents --json` is liveness, joined by pid to
   `~/.claude/sessions/<pid>.json` for pane and `waitingFor`. Those files alone are not liveness:
   they outlive their process. Pane refs resolve to a tmux socket through
   `~/.claude/autoresume/panes/`, whose files are `socket<TAB>session<TAB>pane<TAB>cwd`.
5. **Runnable work**: plan index rows with status `TODO` or `IN PROGRESS` whose `needs` cell names
   no unfinished row and no open gate; worklog `[ ]` and `[>]` lines whose `needs:` steps are all
   `[x]`, in worklogs changed within `runnable_days`, and not under a heading containing `findings`
   or `reported, not fixed` (those count as backlog). Lines mentioning `GATE` or `held` are not
   runnable. A project with a busy session lists no runnable work, and the session shows under
   Running.

Worklog stems map to projects by: strip the home directory's encoded prefix (`-home-<user>-`); try the stem as a directory under
the root; try it with `-` and `_` swapped; then the `aliases` table in `orch.toml`. A stem that
resolves to nothing is listed in the board footer as an orphan and its markers still show.

## The board

`orch` prints the board and writes `STATUS.md` and `state/board.json`. Sections, skipping empty ones:
Right now (a live session with `status: waiting`) · Decide · Check · Next real use · Over days · Desk
· Answered, awaiting pick-up · Runnable · Running · Session endings to review. Footer: snoozed count,
backlog count, orphan worklogs.

Waiting and answered items carry a number, oldest first within a section. Numbers are valid until
the next scan and every command accepts a number or an id. Age is from `since:`, else the first scan
that saw the id.

## Commands

| Command | Effect |
|---|---|
| `orch` / `orch scan` | Board, as above |
| `orch count` | `⚑ N` of waiting items, empty when none; rescans only if the last scan is over `count_ttl_seconds` old; never fails loudly, it runs in the tmux status line |
| `orch add <project> <kind> <where> "<text>" [--unblocks X] [--gate G]` | Append a `[?]` line with today as `since:`, creating the worklog if absent |
| `orch answer <ref> "<text>"` | Turn the line into `[a]`, or append one for a gate row; a derived git item is refused |
| `orch snooze <ref> <YYYY-MM-DD or Nd>` | Hide the item until that date |
| `orch done <ref>` | Delete a `[?]` or `[a]` line that no longer applies; refused for gates and git items |
| `orch open <ref or project>` | Focus the project's live tmux window, else open a new one continuing its latest session |
| `orch route <ref or project> [--json]` | The facts and recommended action below |
| `orch deliver <project> "<text>" [--reset none\|clear\|compact] [--dry-run]` | Type the text into the live session, or start a new one |
| `orch sweep [--json]` / `orch sweep --ack <session prefix>` | List or dismiss unreviewed session endings |

## Sweep

Asks that reach only the chat are caught after the fact. A project's latest top-level transcript is a
candidate when its last assistant text, in the final 600 characters, has a `?` or a phrase asking for
the user (`needs you`, `your call`, `your check`, `want me to`, `let me know`), and no user message
follows it. It is dropped when acknowledged, when older than `sweep_days`, when it belongs to this
project, or when the project's worklog has a `[?]` line and changed after the session ended. A regex
both over- and under-matches, so a candidate is only a prompt for the skill to read the ending and
either `orch add` an item or `orch sweep --ack` it.

## Routing an answer back

Route prefers a live session, because it may hold context a fresh one would rebuild. Among several,
idle beats busy, then most recently updated. The recommendation:

| Situation | Action |
|---|---|
| Live, idle, context under `reset_context_pct` | `send` |
| Live, idle, at or over it, worklog has no `[>]` line and tree is clean | `clear`, then send: nothing is lost that isn't on disk |
| Live, idle, at or over it, otherwise | `compact`, then send |
| Live, busy | `wait` |
| Live, `status: waiting` | `attend`: a dialog in that session needs the user first |
| Live, no tmux pane | `unreachable`: it can't be typed into, and a second session risks competing edits |
| None live | `new`: a window running the autoresume wrapper with the text as its prompt |

Delivery types into the pane with tmux, as `claude-autoresume` does. Cross-session messages are not
usable: receivers treat them as information rather than instructions and may hold them for approval.

Delivery must never destroy the user's typing. Immediately before each step it re-reads the session's
status and the pane's input box, and refuses unless the status is idle and the box is empty. Text
drawn dim (SGR 2 in `capture-pane -e`) is a placeholder or suggestion and counts as empty. An unreadable box is refused too, which also covers open
dialogs. The box is the text between the last two full-width rule lines, less the `❯` prompt. Context
comes from the same capture, `Ctx N%` in the status line; unknown context is treated as under the
threshold.

The send is: type literally, wait 0.5 s, Enter, wait 0.8 s; if the text is still in the box, Enter once
more, since the command popup can eat the first. Text is collapsed to one line and leading slashes are
stripped, so a prompt can never run as a command. After `/clear` or `/compact` it polls for idle before
sending, by pid because `/clear` changes the session id: up to 30 s for clear, 600 s for compact.
Success is the session reaching busy within 15 s; otherwise it reports failure and sends nothing more.

## Configuration

`orch.toml`, gitignored, else the committed `orch.example.toml`: `root` (project parent), `exclude` (directories that are not projects), `aliases`
(worklog stem to directory), `no_git_check` (projects whose git state is noise), `runnable_days`,
`sweep_days`, `count_ttl_seconds`, `reset_context_pct`, `wrapper` (autoresume launcher),
`tmux_session`, `default_socket`.

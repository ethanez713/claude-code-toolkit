---
name: test-drive
description: Use an app the way its real user would, journal every friction point, fix the clear ones, and hold the judgement calls for the user. Use when asked to test drive, UX test, do a UX pass or a user-journey / user-experience test, "navigate around as a user", "behave like a user and take notes", critique a UI, or find what is annoying to use. Arguments - `notes-only` (journal, no fixes), a screen or journey to focus on, a device (`phone`, `emulator`, `desktop`).
---

# Test drive

Invoking this skill is the request to launch the app, screenshot it and drive its UI.

## 1. Isolate before touching anything

- **Real data is never modified.** Run against a copy in a throwaway location. Where the app
  cannot be pointed at a copy, back up every persisted file it can write (inside and outside
  the repo), restore them after the run, and confirm the restore by checksum.
- Pulling data from a device or account into the copy is fine; pushing anything back is not.
- Add simulated data to the copy wherever real data leaves a path unreachable.
- Serve on a free port. Leave every other server and process on the machine alone, and stop
  only what this run started.
- No way to actually drive the UI: stop and say so. A code read is not a test drive.

## 2. Pick the surface

- **Web:** a phone viewport and a desktop viewport, both.
- **Android:** the emulator, set up per `android-smoketest`. The real phone when the user
  names it or the feature needs hardware the emulator lacks (camera, overlays, the keyboard,
  sensors, a second device).
- Every surface: portrait and landscape, light and dark theme.

## 3. The user is context-free

Whoever drives the app knows only what a new user would. If this session has read the app's
code or plans, spawn a subagent as the user and give it the app's purpose, the journeys, the
launch details and the journal format — nothing about the implementation. A session that has
read none of it drives the app itself.

## 4. Drive it

- Write the journeys first: what this user comes to the app to get done, across a realistic
  stretch of use (a normal month, a whole trip, one weekly loop). Run each end to end as one
  pass, then sweep whatever features and paths the journeys did not reach — every one.
- Name the question the home screen has to answer for this user, and check that it does.
- On each screen, sweep the states: empty, loading, error, long content, bad input, offline.
- Try every one-tap irreversible action. Log each that lacks a confirmation or an undo, and
  each destructive control styled as the primary one.

Judge against:

| Dimension | Asks |
|---|---|
| Correctness | Is what the screen shows true, and did the action do what it said? |
| Usefulness | Does this help with the journey, and what is missing? |
| Discoverability | Could the user find it unprompted? How many interactions to reach it? Frequent actions sit up front; rare ones may sit a few taps deep. |
| Density | As much as fits while text stays readable and targets stay tappable (48dp / 44px minimum). Sparse screens and tiny targets are both findings. |
| Aesthetics | Does it look deliberate and feel comfortable to use for a long session? |

Be as harsh as is fair. Nothing is fixed, including code already written: a finding that
needs a redesign or a re-architecture gets logged with the same weight as a typo.

## 5. Journal

One file per round, in the project's docs or plans directory (default
`docs/ux/journal-<date>-<n>.md`); a new round starts a new file. Log while driving, not from
memory afterwards. Record observations only — diagnosis belongs to whoever fixes it.

Each finding:

```
### <n>. <one-line problem> — S<1-4> — <bug | friction | missing | visual>
Where: <screen / journey step>
Steps: <exact taps or inputs>
Expected / actual: <…>
Shot: <path>
```

Severity: **S1** blocks a journey or shows wrong data · **S2** hit on most uses with a costly
workaround · **S3** occasional or cheap to work around · **S4** cosmetic.

## 6. Fix, re-drive, hold

Skip this section under `notes-only`.

1. Fix every finding whose right answer is clear, highest severity first, starting with
   whatever would be called out at first glance. Visual fixes follow the project's design
   system (for example `~/design_system`).
2. Drive the changed screens again and critique them as harshly as the first round. Repeat
   while each cycle is an improvement; stop when the result is ready for the user's eye.
3. Delete each journal entry as its fix is committed. Move the rest into the project's plan.
4. Finish with a numbered list of what was held and why: anything with more than one
   defensible answer, a feature the user may not want, a redesign, a change to real data.
   Give each a recommendation.

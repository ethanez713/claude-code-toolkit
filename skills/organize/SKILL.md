---
name: organize
description: Keep a project's file structure clean and maintainable — clean root, feature isolation, config separated from logic, self-documenting files, and a safe way to restructure. Use when asked to organize, clean up, restructure, or audit a project's layout, or when a session has left a mess of files.
---

# Project organization

Keep the tree clean enough that the project explains itself at a glance. Apply these as
standing preferences; on judgment calls, prefer the simplest layout that stays tidy.

## Layout

- **Clean root.** The root holds only the entry point and the standard top-level
  descriptors/config — nothing else. File all other code, data, and runtime files into
  subdirectories.
- **Once a project outgrows a single file**, put importable code in **`src/`** behind a
  **thin root entry-point shim** so it still runs from the project root. A one-file script
  can stay flat — don't impose the structure prematurely.
- **Test data and samples live in their own dirs**, separate from source. Slow/integration
  tests go in their own dir too, so they aren't run on every change.
- **Quarantine secrets and runtime files** in dedicated dirs kept close to the code: a
  gitignored `.secrets/` for credentials/tokens, and a `data/` dir for generated/runtime
  data (logs, caches, learned state). Never commit secrets.
- **Sensitive/personal data lives OUTSIDE the repo entirely** when the code repo may ever
  be shared: a private external data root (its own dir or git repo) mirroring the project
  layout, resolved by every component the same way — env var → a root config file → a
  default path. Keep a defensive `data/` gitignore rule in the code repo anyway. Secrets
  stay machine-local in `.secrets/` — they move with neither the code nor the data.

## Code shape

- **Feature isolation + one-call wiring.** Each feature lives in its own module and wires
  into the main flow with a single import + method call. Keep separable components loosely
  coupled — no tight coupling between things that can run or change independently.
- **Config separated from logic.** All editable policy/knobs live in a dedicated config
  module, distinct from execution code. Secrets come from env or a gitignored file, never
  hardcoded.
- **Files document themselves.** Whatever documentation a file genuinely needs lives *in*
  that file, never duplicated into a separate doc that drifts out of sync. Most files need
  none: a clear name and clear contents are the documentation (see the comment policy in
  the global `CLAUDE.md`). Reach for a header comment only when the file's *purpose* isn't
  evident from its name and contents — and then keep it to the why, not an inventory of
  what's inside, which is the part that goes stale first.

## Hygiene

- **gitignore secrets the moment they appear**, and rename any file whose name itself leaks
  something sensitive.
- **Move, never delete, files you didn't create** when tidying. Organizing is local-only —
  no network calls, downloads, or uploads.

## Restructuring safely

When moving things around:

1. **Commit first** — a clean checkpoint to revert to.
2. Use **`git mv`** so history follows the file.
3. **Update every import, path, and reference** to the moved files.
4. **Confirm tests still pass** afterward.
5. **Re-check `.gitignore` still covers any secret/generated files at their new paths** — a
   move can silently un-ignore a secret and stage it for commit.

## Auditing an existing project

List what's misplaced — root clutter, mixed concerns, docs drifting from code, secrets at
risk — and propose concrete moves/renames toward the clean target layout, **even if that
replaces the existing scheme**; don't preserve a mess for consistency's sake. Then apply via
the restructuring steps above.

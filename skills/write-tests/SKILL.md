---
name: write-tests
description: Write intentional regression tests for critical paths and edge cases (not coverage padding), separate fast unit from slow integration tests, keep test config in parity with real runs, and avoid overfitting. Use when asked to add, improve, or critically review tests.
---

# Intentional test writing

## Core principles

- **Intentional, not voluminous.** Test the critical code paths and the edge cases that
  would actually break them. Do **not** write tests for the sake of more test code or to
  chase a coverage number — that's noise that slows future changes.
- **Test behavior, not implementation.** Assert on observable outputs via the public
  surface, not internal structure. A correctness-preserving refactor should not break a
  test — if it does, the test was coupled to the wrong thing.
- **Cover the failure paths**, not just the happy path — error cases, bad input, empty/None.
- **Every bug fix gets a regression test** that fails before the fix and passes after —
  proof the bug existed and is gone.
- **A parser or detector gets a fixture from outside the default format.** The bugs that
  survive longest are unstated input assumptions — a hardcoded `.` decimal separator, a
  keyword that a different layout doesn't use, a URL shape one browser doesn't produce. A
  suite built only from convenient inputs confirms the assumption instead of testing it, so
  include at least one non-US-locale / non-default-shape case per format the code claims to
  read.
- **Run them and report.** After writing, run the tests and report pass/fail, runtime, and
  an honest read of what they cover *and what they don't*. Tests you didn't run aren't done.

## Structure (FIRST + Given-When-Then)

- Hold each test to **FIRST**: **F**ast, **I**ndependent (no shared mutable state or
  ordering dependence), **R**epeatable (same result on any machine/order/run),
  **S**elf-validating (clear pass/fail), **T**imely (written with the code).
- Shape each test **Given-When-Then**: preconditions (Given), the one action under test
  (When), the expected result (Then) — one logical behavior per test. Name it so it reads
  as that sentence — `given_<precondition>_when_<action>_then_<result>` — so a failing name
  alone explains what broke.
- **No logic in tests** (no `if`/`for`/`try` in the test body) — branching hides untested
  paths; split into separate tests instead.

## Tests over real local data (UI / integration)

When synthetic fixtures can't represent the real thing, tests MAY read real local data
**read-only** — but only behind two guards, both mandatory:

- **Redirect every write path** the exercised code can reach (logs, queues, state files)
  into a per-test tmp dir via autouse fixtures, before the app/code under test boots.
- **Digest-guard the live files**: hash them before and after every test and fail loudly
  on any change — belt and braces for the redirection.

Missing real data ⇒ **skip, never error** (a fresh clone has none). The hard line stays:
a test never fetches/pushes across a trust boundary (network APIs, cloud sync) and never
appends to live stores.

## Keep tests honest (parity, determinism, no overfitting)

- **Parity with real runs.** Exercise the code the same way production does; reuse the same
  config/constants. Don't introduce test-only parameter values (temperature, seed,
  threshold, model) that make the test behave differently than actual usage — that overfits
  and gives false confidence.
- **Determinism** (for ordinary tests): fixed inputs, seed randomness, freeze "now"; no
  hardcoded sleeps (wait on a condition); isolate/stub external deps so a test fails only
  when the code is actually wrong. This is what makes FIRST's *Repeatable* hold.
- After a behavioral/integration test, ask: *does passing this give real confidence it
  works in production, or just that it passes?* If the latter, fix the test — don't loosen
  the threshold to make a weak test go green.
- **Separate fast from slow.** Fast deterministic tests run every time; slow/expensive ones
  (LLM, network, external services) go in their **own directory** so they aren't run on
  every change.

## Non-deterministic / "good enough" components (e.g. LLMs)

The explicit exception to determinism above. Here:

- The bar is **usefully good, not perfect** — assert against **thresholds/ranges**, not
  exact output.
- Use **enough cases that run-to-run variance is small** — a handful is too noisy.
- **Construct realistic hard cases**, don't only replay captured data. Reason about what
  inputs the code path actually emits (e.g. the marginal rows a deterministic step would
  produce) and build those.
- **Tier cases by difficulty** (easy / medium / hard): easy cases guard against **false
  positives** (don't flag what's fine); hard/ambiguous cases test the real capability.
- **Don't over-tune.** Never hardcode giant example lists into prompts or tests to force a
  pass — prefer high-level principles. Over-flagging correct cases is a failure, not a win.
- **Batch where possible** (one query covering many cases) to keep runs fast; compare
  before/after runtimes when you optimize.

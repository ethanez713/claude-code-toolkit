# Sample plan

| # | Phase | Outcome | needs | independent of | Status | note |
|---|---|---|---|---|---|---|
| 01 | Paint | Pick a colour | — | — | GATE r6 | ok |
| 02 | Shop | Buy the brush | 01 | — | GATE G2 (built 4bd25c2) | ok |
| 03 | Welcome | Schema flag | 01 | — | Steps 1–2 DONE fe98b2b, 0463f50; step 3 /welcome held (needs a schema flag); GATE G4 | ok |
| 04 | Later | Step three | 01 | — | Steps 1–2 DONE 9bfbbf7; step 3 at G6; GATE G6 | ok |
| 05 | Lens | Wide lens | 01 | every gate | GATE-L0 — 12.1–12.4 landed (the S20U ...) | ok |
| 06 | [PassedA](06-passed.md) | Done work | — | — | DONE 99a14c3 (GATE-A passed: pts 1 and 3) | ok |
| 07 | Early | Default landed | — | GATE-Y | TODO (1080p30 default landed early: 22d86ee; GATE-D accepted) | GATE-Z |
| 08 | Siren | Loud | 06 | — | DONE c101a58 (GATE-F accepted; the siren is confirmed loud) | ok |
| 09 | Sec | Hardening | — | — | DONE 1eb7e6b (GATE-SEC passed: see its row) | ok |
| 10 | Next | After early | 07 | — | TODO | ok |
| 11 | Moving | Waiting on open | 05, L0 | — | IN PROGRESS | ok |
| 12 | Blocked | Needs unfinished | 10 | — | TODO | ok |
| 13 | AfterLens | Wait | L0 | — | TODO | ok |
| 14 | AfterPassed | Go | 06 | — | TODO | ok |
| 16 | Doing | Work now | 06 | — | IN PROGRESS — notes | ok |

| # | Name | Outcome | needs | status |
|---|---|---|---|---|
| R4 | Prep | Ready | — | DONE |
| R6 | Ask | Colour | R4, GATE r4 | GATE r6 |
| R7 | Quiet | Nothing queued | R4 | TODO |

| # | Phase | Status |
|---|---|---|
| 99 | Ghost | GATE ghost |

| Gate | After | What the user checks |
|---|---|---|
| NOT-A-ROW | later | ignored |

```
| # | Phase | Outcome | needs | Status |
|---|---|---|---|---|
| F1 | Fenced | Nope | — | GATE fenced |
```
